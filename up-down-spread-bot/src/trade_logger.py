"""
交易记录器——记录所有买入/卖出操作的详细信息
"""
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict

# 设置交易记录器
# 确定日志路径（相对于项目根目录）
project_root = Path(__file__).parent.parent
log_dir = project_root / "logs"
log_dir.mkdir(exist_ok=True)

trades_logger = logging.getLogger('trades')
trades_logger.setLevel(logging.INFO)
trades_handler = logging.FileHandler(log_dir / 'trades.log')
trades_handler.setFormatter(logging.Formatter(
    '%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
trades_logger.addHandler(trades_handler)

def log_buy_attempt(market_slug: str, side: str, contracts: float, price: float, attempt: int, max_attempts: int):
    """记录买入订单尝试"""
    trades_logger.info(
        f"BUY_ATTEMPT | Market: {market_slug} | Side: {side} | "
        f"Contracts: {contracts:.2f} | Price: ${price:.4f} | "
        f"Expected USD: ${contracts * price:.2f} | Attempt: {attempt}/{max_attempts}"
    )

def log_buy_result(market_slug: str, side: str, 
                   requested_contracts: float, filled_contracts: float,
                   requested_usd: float, filled_usd: float,
                   success: bool, error: Optional[str] = None,
                   fak_attempts: int = 1, elapsed_ms: int = 0):
    """记录买入订单结果"""
    fill_pct = (filled_contracts / requested_contracts * 100) if requested_contracts > 0 else 0
    
    if success:
        trades_logger.info(
            f"BUY_SUCCESS | Market: {market_slug} | Side: {side} | "
            f"Requested: {requested_contracts:.2f} contracts (${requested_usd:.2f}) | "
            f"Filled: {filled_contracts:.2f} contracts (${filled_usd:.2f}) | "
            f"Fill: {fill_pct:.1f}% | FAK Attempts: {fak_attempts} | "
            f"Time: {elapsed_ms}ms"
        )
    else:
        trades_logger.error(
            f"BUY_FAILED | Market: {market_slug} | Side: {side} | "
            f"Requested: {requested_contracts:.2f} contracts (${requested_usd:.2f}) | "
            f"Filled: {filled_contracts:.2f} contracts (${filled_usd:.2f}) | "
            f"Fill: {fill_pct:.1f}% | Error: {error} | FAK Attempts: {fak_attempts}"
        )

def log_sell_attempt(market_slug: str, side: str, contracts: float, price: float, attempt: int, max_attempts: int):
    """记录卖出订单尝试"""
    trades_logger.info(
        f"SELL_ATTEMPT | Market: {market_slug} | Side: {side} | "
        f"Contracts: {contracts:.2f} | Price: ${price:.4f} | "
        f"Expected USD: ${contracts * price:.2f} | Attempt: {attempt}/{max_attempts}"
    )

def log_sell_result(market_slug: str, side: str,
                    requested_contracts: float, sold_contracts: float,
                    requested_usd: float, received_usd: float,
                    success: bool, error: Optional[str] = None,
                    fak_attempts: int = 1, elapsed_ms: int = 0):
    """记录卖出订单结果"""
    fill_pct = (sold_contracts / requested_contracts * 100) if requested_contracts > 0 else 0
    
    if success:
        trades_logger.info(
            f"SELL_SUCCESS | Market: {market_slug} | Side: {side} | "
            f"Requested: {requested_contracts:.2f} contracts (expected ${requested_usd:.2f}) | "
            f"Sold: {sold_contracts:.2f} contracts (${received_usd:.2f}) | "
            f"Fill: {fill_pct:.1f}% | FAK Attempts: {fak_attempts} | "
            f"Time: {elapsed_ms}ms"
        )
    else:
        trades_logger.error(
            f"SELL_FAILED | Market: {market_slug} | Side: {side} | "
            f"Requested: {requested_contracts:.2f} contracts | "
            f"Sold: {sold_contracts:.2f} contracts | "
            f"Fill: {fill_pct:.1f}% | Error: {error} | FAK Attempts: {fak_attempts}"
        )

def log_position_summary(market_slug: str, position: Dict):
    """交易后记录仓位摘要"""
    up_shares = position.get('UP', {}).get('total_shares', 0)
    down_shares = position.get('DOWN', {}).get('total_shares', 0)
    up_invested = position.get('UP', {}).get('total_invested', 0)
    down_invested = position.get('DOWN', {}).get('total_invested', 0)
    total_invested = up_invested + down_invested
    
    trades_logger.info(
        f"POSITION | Market: {market_slug} | "
        f"UP: {up_shares:.2f} shares (${up_invested:.2f}) | "
        f"DOWN: {down_shares:.2f} shares (${down_invested:.2f}) | "
        f"Total: ${total_invested:.2f}"
    )

def log_exit_trigger(market_slug: str, exit_reason: str, coin: str = None, 
                     trigger_price: float = None, threshold_price: float = None,
                     unrealized_pnl: float = None, threshold_pnl: float = None,
                     time_remaining: int = None):
    """
    🔥 新增：记录退出触发器（止损、翻转止损、紧急退出）
    适用于所有 4 个币种（BTC、ETH、SOL、XRP）
    适用于两种卖出类型（止损 + 翻转止损）
    
    参数：
        market_slug: 市场标识符
        exit_reason: 'stop_loss'、'flip_stop'、'emergency_exit'
        coin: 币种名称（btc、eth、sol、xrp）
        trigger_price: 触发退出的当前价格
        threshold_price: 阈值价格（用于翻转止损）
        unrealized_pnl: 当前未实现盈亏（用于止损）
        threshold_pnl: 盈亏阈值（用于止损）
        time_remaining: 距离市场结束的秒数
    """
    msg_parts = [f"EXIT_TRIGGER | Market: {market_slug}"]
    
    if coin:
        msg_parts.append(f"Coin: {coin.upper()}")
    
    msg_parts.append(f"Reason: {exit_reason.upper()}")
    
    if exit_reason == 'stop_loss':
        if unrealized_pnl is not None:
            msg_parts.append(f"PnL: ${unrealized_pnl:.2f}")
        if threshold_pnl is not None:
            msg_parts.append(f"Threshold: ${threshold_pnl:.2f}")
    
    elif exit_reason == 'flip_stop':
        if trigger_price is not None:
            msg_parts.append(f"Price: ${trigger_price:.2f}")
        if threshold_price is not None:
            msg_parts.append(f"Flip-Stop: ${threshold_price:.2f}")
    
    elif exit_reason == 'emergency_exit':
        if time_remaining is not None:
            msg_parts.append(f"Time Remaining: {time_remaining}s")
    
    trades_logger.warning(" | ".join(msg_parts))

def log_market_closing_blocked(market_slug: str, blocked_at: str):
    """
    🔥 新增：记录竞态条件保护——阻止买入订单
    适用于所有 4 个币种（BTC、ETH、SOL、XRP）
    
    参数：
        market_slug: 市场标识符
        blocked_at: 阻止发生的位置（例如 'BUY_ORDER_INIT'、'BUY_ORDER_FAK_ATTEMPT_1'）
    """
    trades_logger.warning(
        f"RACE_CONDITION_BLOCK | Market: {market_slug} | "
        f"Blocked at: {blocked_at} | "
        f"Reason: Market closing, preventing new buy orders"
    )
