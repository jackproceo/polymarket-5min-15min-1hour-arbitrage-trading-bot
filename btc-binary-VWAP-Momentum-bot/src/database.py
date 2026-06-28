"""
SQLite 数据库模块：
- 交易记录表（trades）
- 资金账户表（account）
- 资金快照表（account_snapshots）

数据库文件保存在 data/ 文件夹。
支持实盘和模拟两种模式，通过 mode 字段区分。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("btc_live.database")

# 默认数据库路径
DEFAULT_DB_PATH = "data/trading.db"

# ── SQL 建表语句 ─────────────────────────────────────────────────────────
CREATE_TRADES_TABLE = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    market_slug     TEXT    NOT NULL,
    token_name      TEXT    NOT NULL,
    entry_price     REAL    NOT NULL,
    exit_price      REAL    NOT NULL,
    contracts       INTEGER NOT NULL,
    pnl             REAL    NOT NULL,
    won             INTEGER NOT NULL DEFAULT 0,
    timestamp       REAL    NOT NULL,
    entry_time      REAL    DEFAULT 0.0,
    exit_time       REAL    DEFAULT 0.0,
    order_id        TEXT    DEFAULT '',
    hedge_order_id  TEXT    DEFAULT '',
    max_drawdown_abs REAL   DEFAULT 0.0,
    max_drawdown_pct REAL   DEFAULT 0.0,
    hedged          INTEGER NOT NULL DEFAULT 0,
    mode            TEXT    NOT NULL DEFAULT 'live',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_ACCOUNT_TABLE = """
CREATE TABLE IF NOT EXISTS account (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    initial_capital  REAL    NOT NULL DEFAULT 1000.0,
    current_capital  REAL    NOT NULL DEFAULT 1000.0,
    realized_pnl     REAL    NOT NULL DEFAULT 0.0,
    mode             TEXT    NOT NULL DEFAULT 'live',
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

CREATE_SNAPSHOTS_TABLE = """
CREATE TABLE IF NOT EXISTS account_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    capital          REAL    NOT NULL,
    realized_pnl     REAL    NOT NULL,
    trade_count      INTEGER NOT NULL DEFAULT 0,
    win_count        INTEGER NOT NULL DEFAULT 0,
    loss_count       INTEGER NOT NULL DEFAULT 0,
    win_rate_pct     REAL    DEFAULT 0.0,
    mode             TEXT    NOT NULL DEFAULT 'live',
    timestamp        REAL    NOT NULL,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# ── 索引 ──────────────────────────────────────────────────────────────────
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode);",
    "CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_slug);",
    "CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id);",
    "CREATE INDEX IF NOT EXISTS idx_account_mode ON account(mode);",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_mode ON account_snapshots(mode);",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON account_snapshots(timestamp);",
]

# ── 迁移 SQL（为新版本数据库添加缺失字段）─────────────────────────────────
MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN entry_time REAL DEFAULT 0.0;",
    "ALTER TABLE trades ADD COLUMN exit_time REAL DEFAULT 0.0;",
    "ALTER TABLE trades ADD COLUMN order_id TEXT DEFAULT '';",
    "ALTER TABLE trades ADD COLUMN hedge_order_id TEXT DEFAULT '';",
]


def _iso_now() -> str:
    """返回 ISO 8601 UTC 时间字符串"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    """
    SQLite 数据库管理器。

    用法:
        db = Database("data/trading.db")
        db.initialize()

        # 交易记录
        db.insert_trade(...)
        trades = db.get_trades(mode="simulation")

        # 账户
        db.init_account(initial_capital=1000, mode="simulation")
        db.update_account(realized_pnl=25.5, mode="simulation")
        account = db.get_account(mode="simulation")

        # 快照
        db.save_snapshot(mode="simulation", ...)
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self._db_path = Path(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ── 生命周期 ────────────────────────────────────────────────────────

    def initialize(self) -> None:
        """创建数据库文件、表和索引，执行迁移"""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.execute(CREATE_TRADES_TABLE)
            conn.execute(CREATE_ACCOUNT_TABLE)
            conn.execute(CREATE_SNAPSHOTS_TABLE)
            # 执行迁移（ALTER TABLE ADD COLUMN 如果已存在会报错，忽略即可）
            for mig_sql in MIGRATIONS:
                try:
                    conn.execute(mig_sql)
                except sqlite3.OperationalError:
                    pass  # 字段已存在，跳过
            conn.commit()

        # 索引在事务外创建
        with self._get_conn() as conn:
            for idx_sql in INDEXES:
                try:
                    conn.execute(idx_sql)
                except sqlite3.OperationalError:
                    pass
            conn.commit()

        logger.info(f"数据库已初始化: {self._db_path}")

    def close(self) -> None:
        """关闭数据库连接"""
        with self._lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    @contextmanager
    def _get_conn(self):
        """获取线程安全的数据库连接"""
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(
                    str(self._db_path),
                    check_same_thread=False,
                )
                self._conn.row_factory = sqlite3.Row
        try:
            yield self._conn
        except Exception:
            self._conn.rollback()
            raise

    # ── 交易记录 CRUD ───────────────────────────────────────────────────

    def insert_trade(self, record: Dict[str, Any]) -> int:
        """
        插入一条已完成的交易记录。

        Args:
            record: 包含以下字段的字典:
                market_slug, token_name, entry_price, exit_price,
                contracts, pnl, won, timestamp,
                entry_time, exit_time, order_id, hedge_order_id,
                max_drawdown_abs, max_drawdown_pct, hedged, mode

        Returns:
            新插入行的 id
        """
        sql = """
        INSERT INTO trades (
            market_slug, token_name, entry_price, exit_price,
            contracts, pnl, won, timestamp,
            entry_time, exit_time, order_id, hedge_order_id,
            max_drawdown_abs, max_drawdown_pct, hedged, mode, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            record["market_slug"],
            record["token_name"],
            record["entry_price"],
            record["exit_price"],
            record["contracts"],
            record["pnl"],
            1 if record.get("won") else 0,
            record.get("timestamp", time.time()),
            record.get("entry_time", 0.0),
            record.get("exit_time", 0.0),
            record.get("order_id", ""),
            record.get("hedge_order_id", ""),
            record.get("max_drawdown_abs", 0.0),
            record.get("max_drawdown_pct", 0.0),
            1 if record.get("hedged") else 0,
            record.get("mode", "live"),
            _iso_now(),
        )
        with self._get_conn() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.lastrowid

    def get_trades(
        self,
        mode: Optional[str] = None,
        limit: int = 0,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        查询交易记录。

        Args:
            mode: 过滤模式（'live' / 'simulation'），None 表示全部
            limit: 限制返回行数，0 表示不限制
            offset: 偏移量

        Returns:
            交易记录列表（每行为 dict）
        """
        sql = "SELECT * FROM trades"
        params: list = []
        if mode:
            sql += " WHERE mode = ?"
            params.append(mode)
        sql += " ORDER BY id DESC"
        if limit > 0:
            sql += f" LIMIT {int(limit)}"
            if offset > 0:
                sql += f" OFFSET {int(offset)}"

        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def get_trade_count(self, mode: Optional[str] = None) -> int:
        """返回交易总数"""
        sql = "SELECT COUNT(*) as cnt FROM trades"
        params: list = []
        if mode:
            sql += " WHERE mode = ?"
            params.append(mode)
        with self._get_conn() as conn:
            row = conn.execute(sql, params).fetchone()
            return row["cnt"] if row else 0

    def get_trade_summary(self, mode: Optional[str] = None) -> Dict[str, Any]:
        """
        返回交易汇总统计。

        Returns:
            {
                trade_count, wins, losses, win_rate_pct,
                total_pnl_usd, avg_trade_pnl_usd,
                best_trade_pnl_usd, worst_trade_pnl_usd,
                last_close_unix
            }
        """
        where = "WHERE mode = ?" if mode else ""
        params = [mode] if mode else []

        sql = f"""
        SELECT
            COUNT(*) as trade_count,
            SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN won = 0 THEN 1 ELSE 0 END) as losses,
            SUM(pnl) as total_pnl,
            ROUND(AVG(pnl), 6) as avg_pnl,
            MAX(pnl) as best_pnl,
            MIN(pnl) as worst_pnl,
            MAX(timestamp) as last_close
        FROM trades
        {where}
        """
        with self._get_conn() as conn:
            row = conn.execute(sql, params).fetchone()
            if not row or row["trade_count"] == 0:
                return {
                    "trade_count": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate_pct": 0.0,
                    "total_pnl_usd": 0.0,
                    "avg_trade_pnl_usd": 0.0,
                    "best_trade_pnl_usd": None,
                    "worst_trade_pnl_usd": None,
                    "last_close_unix": None,
                }
            tc = row["trade_count"]
            wr = round((row["wins"] / tc * 100.0), 4) if tc else 0.0
            return {
                "trade_count": tc,
                "wins": row["wins"],
                "losses": row["losses"],
                "win_rate_pct": wr,
                "total_pnl_usd": round(row["total_pnl"], 6),
                "avg_trade_pnl_usd": round(row["avg_pnl"], 6),
                "best_trade_pnl_usd": round(row["best_pnl"], 6) if row["best_pnl"] is not None else None,
                "worst_trade_pnl_usd": round(row["worst_pnl"], 6) if row["worst_pnl"] is not None else None,
                "last_close_unix": row["last_close"],
            }

    def get_markets_seen_count(self, mode: Optional[str] = None) -> int:
        """返回见过的市场数量（去重 slug）"""
        where = "WHERE mode = ?" if mode else ""
        params = [mode] if mode else []
        sql = f"SELECT COUNT(DISTINCT market_slug) as cnt FROM trades {where}"
        with self._get_conn() as conn:
            row = conn.execute(sql, params).fetchone()
            return row["cnt"] if row else 0

    # ── 资金账户 CRUD ───────────────────────────────────────────────────

    def init_account(
        self,
        initial_capital: float = 1000.0,
        mode: str = "live",
    ) -> None:
        """
        初始化账户（如果该模式已存在则跳过）。

        Args:
            initial_capital: 初始资金（美元）
            mode: 'live' 或 'simulation'
        """
        with self._get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM account WHERE mode = ?", (mode,)
            ).fetchone()
            if existing:
                return  # 账户已存在，不覆盖

            conn.execute(
                """INSERT INTO account (initial_capital, current_capital, realized_pnl, mode, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (initial_capital, initial_capital, 0.0, mode, _iso_now()),
            )
            conn.commit()
            logger.info(
                f"账户已初始化: mode={mode}, capital=${initial_capital:.2f}"
            )

    def get_account(self, mode: str = "live") -> Optional[Dict[str, Any]]:
        """查询账户信息"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM account WHERE mode = ?", (mode,)
            ).fetchone()
            return dict(row) if row else None

    def update_account(self, realized_pnl: float, mode: str = "live") -> None:
        """
        更新账户的已实现盈亏（同时更新当前资金）。

        current_capital = initial_capital + realized_pnl
        """
        account = self.get_account(mode)
        if not account:
            logger.warning(f"账户不存在: mode={mode}，请先调用 init_account()")
            return

        new_capital = account["initial_capital"] + realized_pnl
        with self._get_conn() as conn:
            conn.execute(
                """UPDATE account
                   SET realized_pnl = ?, current_capital = ?, updated_at = ?
                   WHERE mode = ?""",
                (realized_pnl, new_capital, _iso_now(), mode),
            )
            conn.commit()

    def reset_account(self, initial_capital: float = 1000.0, mode: str = "live") -> None:
        """重置指定模式的账户资金"""
        with self._get_conn() as conn:
            conn.execute(
                "DELETE FROM account WHERE mode = ?", (mode,)
            )
            conn.commit()
        self.init_account(initial_capital, mode)

    # ── 资金快照 ────────────────────────────────────────────────────────

    def save_snapshot(
        self,
        capital: float,
        realized_pnl: float,
        trade_count: int,
        win_count: int,
        loss_count: int,
        win_rate_pct: float,
        mode: str = "live",
    ) -> int:
        """保存一条资金快照记录"""
        sql = """
        INSERT INTO account_snapshots (
            capital, realized_pnl, trade_count, win_count, loss_count,
            win_rate_pct, mode, timestamp, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            capital, realized_pnl, trade_count, win_count, loss_count,
            win_rate_pct, mode, time.time(), _iso_now(),
        )
        with self._get_conn() as conn:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.lastrowid

    def get_snapshots(
        self,
        mode: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """查询资金快照记录"""
        sql = "SELECT * FROM account_snapshots"
        params: list = []
        if mode:
            sql += " WHERE mode = ?"
            params.append(mode)
        sql += " ORDER BY id ASC"
        if limit > 0:
            sql += f" LIMIT {int(limit)}"

        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    # ── 工具方法 ────────────────────────────────────────────────────────

    def export_trades_csv(self, filepath: str, mode: Optional[str] = None) -> None:
        """导出交易记录为 CSV 文件（便于 Excel 分析）"""
        import csv

        trades = self.get_trades(mode=mode, limit=0)
        if not trades:
            logger.warning("没有交易记录可导出")
            return

        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=trades[0].keys())
            writer.writeheader()
            writer.writerows(trades)
        logger.info(f"已导出 {len(trades)} 条记录到 {filepath}")

    def get_all_trades_as_dicts(self, mode: Optional[str] = None) -> List[Dict[str, Any]]:
        """返回所有交易记录（字典列表），用于兼容旧版摘要输出"""
        trades = self.get_trades(mode=mode, limit=0)
        return [
            {
                "market_slug": t["market_slug"],
                "token_name": t["token_name"],
                "entry_price": t["entry_price"],
                "exit_price": t["exit_price"],
                "contracts": t["contracts"],
                "pnl": t["pnl"],
                "won": bool(t["won"]),
                "timestamp": t["timestamp"],
                "max_drawdown_abs": t["max_drawdown_abs"],
                "max_drawdown_pct": t["max_drawdown_pct"],
            }
            for t in trades
        ]
