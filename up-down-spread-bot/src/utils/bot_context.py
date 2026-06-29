"""
BotContext - 交易机器人的共享状态容器。

消除 main.py 中的闭包变量捕获，用显式依赖注入替代全局可变状态。
"""
import threading
import time
from typing import Any, Dict, List, Optional

from utils.config import Config
from utils.logging_setup import get_logger

log = get_logger("context")


class BotContext:
    """交易机器人所有共享状态的显式容器。"""

    def __init__(self, config: Config):
        self.config = config

        # 实例引用（启动时注入）
        self.order_executor = None
        self.multi_trader = None
        self.data_feed = None
        self.notifier = None
        self.dashboard = None
        self.keyboard_listener = None
        self.redeem_collector = None
        self.strategies: Dict[str, Any] = {}
        self.strategy_names: List[str] = []

        # 线程池
        self.sys2_executor = None
        self.redeem_executor = None

        # ── 运行状态 ──
        self.stop_flag = False
        self.session_start_time = time.time()
        self.wallet_balance = 0.0

        # 跟踪每个币种跳过的市场
        self.markets_skipped: Dict[str, int] = {coin: 0 for coin in config.coins}

        # 每市场统计
        self.total_completed_markets = 0
        self.last_chart_at = 0

        # 市场起始价格跟踪 {coin: {market_slug: price_or_status}}
        self.market_start_prices: Dict[str, Dict] = {coin: {} for coin in config.coins}

        # 待处理市场
        self.pending_markets: Dict[str, Dict] = {coin: {} for coin in config.coins}

        # 是否观察到市场切换
        self.witnessed_market_switch: Dict[str, bool] = {coin: False for coin in config.coins}

        # 线程锁
        self.market_lock = threading.Lock()
        self.wallet_balance_ref: List[float] = [0.0]  # 间接引用用于跨线程更新

        # 余额检查去重
        self._balance_checked_markets: set = set()

    # ── 余额管理 ──

    @property
    def wallet_balance(self) -> float:
        return self._wallet_balance

    @wallet_balance.setter
    def wallet_balance(self, value: float):
        self._wallet_balance = value
        self.wallet_balance_ref[0] = value

    # ── 快捷方法 ──

    @property
    def coins(self) -> list:
        return self.config.coins

    @property
    def strategy_bases(self) -> list:
        return self.config.strategy_bases

    def strategy_name_for(self, base: str, coin: str) -> str:
        return f"{base}_{coin}"
