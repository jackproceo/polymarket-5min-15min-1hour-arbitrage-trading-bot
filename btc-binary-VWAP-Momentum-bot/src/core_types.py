"""
核心数据类型：交易、代币、市场状态、持仓、交易记录。
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Trade:
    """单笔交易记录"""
    timestamp: float
    price: float
    size: float
    side: str


@dataclass
class TokenData:
    """单个代币（Up 或 Down）的数据"""
    token_id: str
    name: str

    best_bid: float = 0.0
    best_bid_size: float = 0.0
    best_ask: float = 0.0
    best_ask_size: float = 0.0

    trades: deque = field(default_factory=lambda: deque(maxlen=5000))

    last_price: float = 0.0
    last_trade_time: float = 0.0

    trade_count: int = 0
    volume_total: float = 0.0
    volume_buy: float = 0.0
    volume_sell: float = 0.0

    def reset(self):
        """重置所有数据（新市场开始时调用）"""
        self.best_bid = 0.0
        self.best_bid_size = 0.0
        self.best_ask = 0.0
        self.best_ask_size = 0.0
        self.trades.clear()
        self.last_price = 0.0
        self.last_trade_time = 0.0
        self.trade_count = 0
        self.volume_total = 0.0
        self.volume_buy = 0.0
        self.volume_sell = 0.0


@dataclass
class MarketState:
    """当前市场状态"""
    market_id: str = ""
    condition_id: str = ""
    slug: str = ""
    end_time: float = 0.0

    up_token: Optional[TokenData] = None
    down_token: Optional[TokenData] = None

    connected: bool = False
    last_update: float = 0.0

    # Chainlink BTC/USD 价格追踪
    btc_anchor_price: float = 0.0    # 市场开始时的锚定价格
    btc_current_price: float = 0.0   # 最新 Chainlink 价格
    btc_last_update: float = 0.0     # 最后一次价格更新时间戳
    btc_connected: bool = False      # RTDS 连接状态


@dataclass
class Position:
    """当前持仓"""
    token_name: str
    token_id: str
    opposite_token_id: str
    entry_price: float
    contracts: int
    entry_time: float
    market_slug: str
    order_id: str = ""         # Polymarket 入场订单号
    hedge_order_id: str = ""   # 对冲订单号
    hedged: bool = False
    hedge_contracts: int = 0
    hedge_price: float = 0.0
    min_price_seen: float = 0.0  # 入场后的最低价（用于回撤追踪）


@dataclass
class TradeRecord:
    """已完成交易记录"""
    market_slug: str
    token_name: str
    entry_price: float
    exit_price: float
    contracts: int
    pnl: float
    won: bool
    timestamp: float
    max_drawdown_abs: float = 0.0   # 从入场的最大绝对价格跌幅
    max_drawdown_pct: float = 0.0   # 从入场的最大百分比回撤
