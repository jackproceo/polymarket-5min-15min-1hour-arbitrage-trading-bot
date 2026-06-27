#!/usr/bin/env python3
"""
Polymarket BTC 5分钟/15分钟 涨跌自动交易 - 全局状态模块
运行时数据、仪表盘状态、Flask应用示例。
"""
import threading
from datetime import datetime, timezone

from flask import Flask

from config import STATIC_DIR, _btc_market_minutes, SIMULATION_MODE

# 全局价格快照
price_data = {
    "btc": None,           # Chainlink BTC（交易参考价）
    "binance": None,       # Binance BTC（辅助参考）
    "ptb": None,           # 基准价（Price to Beat）
    "up_price": None,      # UP代币中间价
    "down_price": None,    # DOWN代币中间价
    "up_bid": None,
    "up_ask": None,
    "down_bid": None,
    "down_ask": None,
    "btc_update_ts": 0.0,
    "up_update_ts": 0.0,
    "down_update_ts": 0.0,
    "last_update": None,
}

dashboard_lock = threading.Lock()
dashboard_cond = threading.Condition(dashboard_lock)
dashboard_version = 0
dashboard_state = {
    "updated_at": None,
    "market": {},
    "wallet_balance": None,
    "prices": {},
    "position": {},
    "pending_order": {},
    "last_order": {},
    "trade_history": [],
    "wallet_positions": [],
    "wallet_history": [],
    "live_trades": [],
    "live_positions_count": 0,
    "live_realized_pnl": 0.0,
    "live_unrealized_pnl": 0.0,
    "live_total_pnl": 0.0,
    "auto_redeem": {},
    "activity": [],
    "btc_market_minutes": _btc_market_minutes,
    "cumulative_realized_pnl": 0.0,
    "simulation_mode": SIMULATION_MODE,
}

app = Flask(__name__, static_folder=STATIC_DIR)


def _dashboard_set(**kwargs):
    """安全更新仪表盘状态并通知SSE客户端。"""
    global dashboard_version
    with dashboard_cond:
        for k, v in kwargs.items():
            dashboard_state[k] = v
        dashboard_state["updated_at"] = datetime.now().isoformat()
        dashboard_version += 1
        dashboard_cond.notify_all()
