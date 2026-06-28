"""
交易统计：持仓管理、交易记录、盈亏计算。
数据持久化使用 SQLite（通过 Database 模块）。
"""

import logging
import time
from typing import Any, Dict, List, Optional

from .core_types import Position, TradeRecord
from .database import Database

logger = logging.getLogger("btc_live")


class TradingStats:
    """交易统计与持仓管理"""

    def __init__(self, db: Optional[Database] = None, mode: str = "live"):
        """
        Args:
            db: 数据库实例。如果不提供，使用默认路径创建。
            mode: 'live' 或 'simulation'
        """
        self._db = db or Database()
        self._mode = mode
        self.position: Optional[Position] = None
        self.trades: List[TradeRecord] = []
        self.markets_seen: int = 0
        self.current_market_slug: str = ""
        self.position_closed_this_market: bool = False
        self.entry_blocked: bool = False  # 当前市场禁止再次入场
        self._load()

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = value
        self._load()

    def _load(self):
        """从数据库加载历史交易记录"""
        try:
            rows = self._db.get_trades(mode=self._mode, limit=0)
            self.trades = [
                TradeRecord(
                    market_slug=r["market_slug"],
                    token_name=r["token_name"],
                    entry_price=r["entry_price"],
                    exit_price=r["exit_price"],
                    contracts=r["contracts"],
                    pnl=r["pnl"],
                    won=bool(r["won"]),
                    timestamp=r["timestamp"],
                    max_drawdown_abs=r.get("max_drawdown_abs", 0.0),
                    max_drawdown_pct=r.get("max_drawdown_pct", 0.0),
                )
                for r in reversed(rows)  # 按时间升序
            ]
            self.markets_seen = self._db.get_markets_seen_count(self._mode)
        except Exception as e:
            logger.warning(f"加载交易记录失败: {e}")

    def summary_dict(self) -> Dict[str, Any]:
        """生成统计摘要（用于仪表盘和模拟摘要文件）"""
        tc = len(self.trades)
        wins = sum(1 for t in self.trades if t.won)
        losses = tc - wins
        total = sum(t.pnl for t in self.trades)
        pnls = [t.pnl for t in self.trades]
        wr = (wins / tc * 100.0) if tc else 0.0
        return {
            "total_pnl_usd": round(total, 6),
            "trade_count": tc,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round(wr, 4),
            "avg_trade_pnl_usd": round(total / tc, 6) if tc else 0.0,
            "best_trade_pnl_usd": round(max(pnls), 6) if pnls else None,
            "worst_trade_pnl_usd": round(min(pnls), 6) if pnls else None,
            "last_close_unix": max((t.timestamp for t in self.trades), default=None),
        }

    def new_market(self, slug: str):
        """切换到新市场，重置持仓状态"""
        if slug != self.current_market_slug:
            self.current_market_slug = slug
            self.markets_seen += 1
            self.position = None
            self.position_closed_this_market = False
            self.entry_blocked = False  # 新市场重置入场封锁

    def can_enter(self) -> bool:
        """是否可以执行入场（无持仓、本市场未平仓、未被封锁）"""
        return (
            self.position is None
            and not self.position_closed_this_market
            and not self.entry_blocked
        )

    def block_entry(self, reason: str = ""):
        """封锁当前市场的再次入场尝试"""
        self.entry_blocked = True
        if reason:
            logger.warning(f"入场已封锁: {reason}")

    def record_entry(
        self,
        token_name: str,
        token_id: str,
        opposite_token_id: str,
        price: float,
        contracts: int,
        market_slug: str,
        order_id: str = "",
    ):
        """记录入场持仓"""
        self.position = Position(
            token_name=token_name,
            token_id=token_id,
            opposite_token_id=opposite_token_id,
            entry_price=price,
            contracts=contracts,
            entry_time=time.time(),
            market_slug=market_slug,
            order_id=order_id,
            min_price_seen=price,  # 入场时初始化最低价
        )

    def record_hedge(self, contracts: int, price: float):
        """记录对冲订单"""
        if self.position:
            self.position.hedged = True
            self.position.hedge_contracts = contracts
            self.position.hedge_price = price

    def update_drawdown(self, current_price: float):
        """更新入场以来的最低价格（用于回撤计算）"""
        if self.position and current_price > 0:
            if current_price < self.position.min_price_seen:
                self.position.min_price_seen = current_price

    def close_position(self, final_price: float) -> Optional[TradeRecord]:
        """
        平仓并生成交易记录。

        Args:
            final_price: 最终结算价格

        Returns:
            TradeRecord，如果无持仓则返回 None
        """
        if not self.position:
            return None

        won = final_price >= 0.70  # 赢的阈值
        entry_cost = self.position.contracts * self.position.entry_price

        if won:
            pnl = self.position.contracts - entry_cost
        else:
            pnl = -entry_cost

        # 计算从入场的最大回撤
        dd_abs = max(0, self.position.entry_price - self.position.min_price_seen)
        dd_pct = (
            (dd_abs / self.position.entry_price * 100)
            if self.position.entry_price > 0
            else 0
        )

        record = TradeRecord(
            market_slug=self.position.market_slug,
            token_name=self.position.token_name,
            entry_price=self.position.entry_price,
            exit_price=final_price,
            contracts=self.position.contracts,
            pnl=pnl,
            won=won,
            timestamp=time.time(),
            max_drawdown_abs=dd_abs,
            max_drawdown_pct=dd_pct,
        )

        # 写入数据库
        pos = self.position  # 先保存引用再清空
        try:
            self._db.insert_trade({
                "market_slug": record.market_slug,
                "token_name": record.token_name,
                "entry_price": record.entry_price,
                "exit_price": record.exit_price,
                "contracts": record.contracts,
                "pnl": record.pnl,
                "won": record.won,
                "timestamp": record.timestamp,
                "entry_time": pos.entry_time,
                "exit_time": time.time(),
                "order_id": getattr(pos, "order_id", ""),
                "hedge_order_id": getattr(pos, "hedge_order_id", ""),
                "max_drawdown_abs": record.max_drawdown_abs,
                "max_drawdown_pct": record.max_drawdown_pct,
                "hedged": pos.hedged,
                "mode": self._mode,
            })
        except Exception as e:
            logger.error(f"保存交易记录到数据库失败: {e}")

        self.trades.append(record)
        self.position = None
        self.position_closed_this_market = True
        return record

    @property
    def total_pnl(self) -> float:
        """累计盈亏"""
        return sum(t.pnl for t in self.trades)

    @property
    def win_count(self) -> int:
        """胜场数"""
        return sum(1 for t in self.trades if t.won)

    @property
    def trade_count(self) -> int:
        """总交易数"""
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        """胜率百分比"""
        if not self.trades:
            return 0.0
        return (self.win_count / self.trade_count) * 100
