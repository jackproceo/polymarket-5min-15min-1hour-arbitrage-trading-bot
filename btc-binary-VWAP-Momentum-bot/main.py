#!/usr/bin/env python3
"""
BTC 5/15-min 实时交易机器人

模块结构:
  src/core_types.py          - 核心数据类型
  src/indicator_calculator.py - 指标计算（VWAP、偏差、动量、Z-Score）
  src/win_rate_table.py       - CSV 胜率表
  src/trading_stats.py        - 交易统计与持仓管理
  src/market_ws_client.py     - Polymarket 行情 WebSocket
  src/chainlink_ws_client.py  - Chainlink BTC/USD 价格流
  src/dashboard.py            - Rich 终端仪表盘 + Web 快照
  src/live_bot.py             - 实时交易机器人主逻辑

用法:
    python main.py
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path

from rich.console import Console

# ── 日志初始化（必须在所有模块导入之前） ───────────────────────────────────
Path("logs").mkdir(exist_ok=True)

# 主日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler('logs/bot.log')],
)

# 详细订单日志
order_logger = logging.getLogger("btc_live.orders")
order_handler = logging.FileHandler('logs/orders.log')
order_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
))
order_logger.addHandler(order_handler)
order_logger.setLevel(logging.DEBUG)

# 详细对冲日志
hedge_logger = logging.getLogger("btc_live.hedges")
hedge_handler = logging.FileHandler('logs/hedges.log')
hedge_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
))
hedge_logger.addHandler(hedge_handler)
hedge_logger.setLevel(logging.DEBUG)

# 信号日志
signal_logger = logging.getLogger("btc_live.signals")
signal_handler = logging.FileHandler('logs/signals.log')
signal_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
))
signal_logger.addHandler(signal_handler)
signal_logger.setLevel(logging.DEBUG)

# Rich 控制台
console = Console()

# ── 项目导入 ──────────────────────────────────────────────────────────────
from src.live_bot import LiveTradingBot


async def main():
    """启动交易机器人"""
    bot = LiveTradingBot(console)

    loop = asyncio.get_event_loop()

    def shutdown():
        """信号处理器：优雅停止"""
        bot.running = False

    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, shutdown)

    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]已中断。[/yellow]")
