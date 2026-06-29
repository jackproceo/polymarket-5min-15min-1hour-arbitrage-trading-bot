"""
余额管理 - 钱包余额查询、POL 价格、活跃持仓查询。
"""
import time
import requests
import threading
from typing import Optional

from utils.logging_setup import get_logger

log = get_logger("balance")


def get_pol_price_usd() -> float:
    """通过 CoinGecko API 获取 POL 价格，失败返回 0.45。"""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        params = {'ids': 'polygon-ecosystem-token', 'vs_currencies': 'usd'}
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code == 200:
            price = resp.json().get('polygon-ecosystem-token', {}).get('usd')
            if price:
                log.info("POL price: $%.4f", price)
                return float(price)
        log.warning("POL price API failed, using fallback $0.45")
        return 0.45
    except Exception as e:
        log.error("POL price error: %s, fallback $0.45", e)
        return 0.45


def get_active_positions(wallet_address: str) -> Optional[list]:
    """通过 Polymarket Data API 获取活跃持仓。"""
    if not wallet_address:
        log.warning("No wallet address")
        return None
    try:
        url = "https://data-api.polymarket.com/positions"
        params = {
            'user': wallet_address,
            'sizeThreshold': 0.1,
            'limit': 50,
            'sortBy': 'CURRENT',
            'sortDirection': 'DESC'
        }
        log.info("Fetching positions for %s...", wallet_address[:6])
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            positions = resp.json()
            log.info("Got %d positions", len(positions))
            return positions
        log.warning("Positions API HTTP %d", resp.status_code)
        return None
    except Exception as e:
        log.error("Positions API error: %s", e)
        return None


def check_balance_async(
    order_executor,
    safety_guard,
    wallet_balance_ref: list,
    lock: threading.Lock,
):
    """异步查询钱包 USDC 余额并更新共享引用。wallet_balance_ref 是一个 [float] 列表用于间接引用。"""
    try:
        if not safety_guard.dry_run:
            new_balance = order_executor.get_wallet_usdc_balance()
            if new_balance and new_balance > 0:
                with lock:
                    old = wallet_balance_ref[0]
                    wallet_balance_ref[0] = new_balance
                    change = new_balance - old
                    log.info("Balance updated: $%.2f ($%+.2f)", new_balance, change)
    except Exception as e:
        log.error("Balance check failed: %s", e)
