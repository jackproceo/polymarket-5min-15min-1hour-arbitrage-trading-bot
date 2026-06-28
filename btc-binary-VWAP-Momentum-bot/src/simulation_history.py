#!/usr/bin/env python3
"""
模拟交易历史记录器：
将 OPEN/CLOSE 事件写入 SQLite 数据库（通过 Database 模块），
并可选导出 CSV/JSONL/JSON 摘要文件。

数据持久化由 trading_stats（SQLite）自动处理；
本模块负责日志记录和定期摘要输出。
"""

from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .database import Database

logger = logging.getLogger("btc_live.simulation_history")

CSV_COLUMNS = [
    "event",
    "time_utc",
    "unix_ts",
    "market_slug",
    "side",
    "contracts",
    "entry_price",
    "exit_price",
    "entry_cost_usd",
    "trade_pnl_usd",
    "cumulative_pnl_usd",
    "won",
    "trade_number",
    "total_closed_trades",
    "win_rate_pct",
    "max_dd_abs",
    "max_dd_pct",
    "hedged",
]


def _iso(ts: Optional[float] = None) -> str:
    t = ts if ts is not None else time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SimulationHistoryLogger:
    """
    记录模拟交易的 OPEN 和 CLOSE 事件到数据库，
    并可选导出 CSV / JSONL / summary JSON 文件。
    """

    def __init__(
        self,
        db: Database,
        mode: str = "simulation",
        csv_path: str = "",
        jsonl_path: str = "",
        summary_path: str = "",
    ):
        """
        Args:
            db: 数据库实例
            mode: 'live' 或 'simulation'
            csv_path: CSV 导出路径，空字符串则不导出
            jsonl_path: JSONL 导出路径，空字符串则不导出
            summary_path: 摘要 JSON 路径，空字符串则不导出
        """
        self._db = db
        self._mode = mode

        cp = (csv_path or "").strip()
        self.csv_path = Path(cp) if cp else None
        jp = (jsonl_path or "").strip()
        self.jsonl_path = Path(jp) if jp else None
        sp = (summary_path or "").strip()
        self.summary_path = Path(sp) if sp else None

        self._csv_header_written = bool(
            self.csv_path
            and self.csv_path.exists()
            and self.csv_path.stat().st_size > 0
        )

    # ── CSV / JSONL 辅助 ────────────────────────────────────────────────

    def _append_csv_row(self, row: Dict[str, Any]) -> None:
        if not self.csv_path:
            return
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_header = not self._csv_header_written
        with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if write_header:
                w.writeheader()
                self._csv_header_written = True
            w.writerow({k: row.get(k, "") for k in CSV_COLUMNS})

    def _append_jsonl(self, obj: Dict[str, Any]) -> None:
        if not self.jsonl_path:
            return
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")

    # ── 事件记录 ────────────────────────────────────────────────────────

    def log_open(
        self,
        *,
        market_slug: str,
        token_name: str,
        contracts: int,
        avg_price: float,
        total_cost: float,
        cumulative_realized_pnl: float,
        hedged: bool,
        trade_number: int,
    ) -> None:
        """记录入场事件。trade_number = 已平仓交易数 + 1"""
        ts = time.time()
        row = {
            "event": "OPEN",
            "time_utc": _iso(ts),
            "unix_ts": f"{ts:.3f}",
            "market_slug": market_slug,
            "side": token_name,
            "contracts": contracts,
            "entry_price": f"{avg_price:.6f}",
            "exit_price": "",
            "entry_cost_usd": f"{total_cost:.4f}",
            "trade_pnl_usd": "",
            "cumulative_pnl_usd": f"{cumulative_realized_pnl:.4f}",
            "won": "",
            "trade_number": trade_number,
            "total_closed_trades": "",
            "win_rate_pct": "",
            "max_dd_abs": "",
            "max_dd_pct": "",
            "hedged": hedged,
        }
        self._append_csv_row(row)
        self._append_jsonl({
            "type": "open",
            "time_utc": row["time_utc"],
            "unix_ts": ts,
            "market_slug": market_slug,
            "side": token_name,
            "contracts": contracts,
            "avg_price": avg_price,
            "entry_cost_usd": total_cost,
            "cumulative_realized_pnl_usd": cumulative_realized_pnl,
            "hedged": hedged,
            "trade_number": trade_number,
        })
        logger.info(
            f"[SIM] OPEN {token_name} x{contracts} @ {avg_price:.4f} "
            f"cost=${total_cost:.2f} | 已实现盈亏 ${cumulative_realized_pnl:+.4f}"
        )

    def log_close(
        self,
        record: Any,  # TradeRecord-like
        *,
        cumulative_pnl: float,
        total_closed: int,
        win_rate_pct: float,
        hedged: bool,
    ) -> None:
        """记录平仓事件"""
        ts = getattr(record, "timestamp", None) or time.time()
        row = {
            "event": "CLOSE",
            "time_utc": _iso(ts),
            "unix_ts": f"{ts:.3f}",
            "market_slug": record.market_slug,
            "side": record.token_name,
            "contracts": record.contracts,
            "entry_price": f"{record.entry_price:.6f}",
            "exit_price": f"{record.exit_price:.6f}",
            "entry_cost_usd": f"{record.contracts * record.entry_price:.4f}",
            "trade_pnl_usd": f"{record.pnl:+.4f}",
            "cumulative_pnl_usd": f"{cumulative_pnl:+.4f}",
            "won": record.won,
            "trade_number": total_closed,
            "total_closed_trades": total_closed,
            "win_rate_pct": f"{win_rate_pct:.2f}",
            "max_dd_abs": f"{record.max_drawdown_abs:.6f}",
            "max_dd_pct": f"{record.max_drawdown_pct:.2f}",
            "hedged": hedged,
        }
        self._append_csv_row(row)
        self._append_jsonl({
            "type": "close",
            "time_utc": row["time_utc"],
            "unix_ts": ts,
            "market_slug": record.market_slug,
            "side": record.token_name,
            "contracts": record.contracts,
            "entry_price": record.entry_price,
            "exit_price": record.exit_price,
            "trade_pnl_usd": record.pnl,
            "cumulative_pnl_usd": cumulative_pnl,
            "won": record.won,
            "trade_number": total_closed,
            "total_closed_trades": total_closed,
            "win_rate_pct": win_rate_pct,
            "max_drawdown_abs": record.max_drawdown_abs,
            "max_drawdown_pct": record.max_drawdown_pct,
            "hedged": hedged,
        })
        logger.info(
            f"[SIM] CLOSE #{total_closed} {record.token_name} "
            f"盈亏 ${record.pnl:+.4f} | "
            f"累计 ${cumulative_pnl:+.4f} | "
            f"胜率 {win_rate_pct:.1f}% ({total_closed} 笔)"
        )

    def write_summary(
        self,
        trades_as_dicts: List[Dict[str, Any]],
        summary: Dict[str, Any],
    ) -> None:
        """
        写入完整摘要快照（从数据库导出，用于快速分析）。

        同时将资金快照写入数据库的 account_snapshots 表。
        """
        # 写入数据库快照
        try:
            account = self._db.get_account(self._mode)
            capital = account["current_capital"] if account else 0.0
            self._db.save_snapshot(
                capital=capital,
                realized_pnl=summary.get("total_pnl_usd", 0.0),
                trade_count=summary.get("trade_count", 0),
                win_count=summary.get("wins", 0),
                loss_count=summary.get("losses", 0),
                win_rate_pct=summary.get("win_rate_pct", 0.0),
                mode=self._mode,
            )
        except Exception as e:
            logger.warning(f"保存资金快照失败: {e}")

        # 写入 JSON 摘要文件
        if not self.summary_path:
            return
        self.summary_path.parent.mkdir(parents=True, exist_ok=True)
        out = {
            "updated_at_utc": _iso(),
            **summary,
            "trades": trades_as_dicts,
        }
        with open(self.summary_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
