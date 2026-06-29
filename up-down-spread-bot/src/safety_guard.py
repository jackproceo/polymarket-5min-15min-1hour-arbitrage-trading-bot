"""
安全卫士——真实资金交易的保护层
"""
import time
import json
from pathlib import Path
from typing import Dict, Tuple

from utils.logging_setup import get_logger
log = get_logger("safety")


class SafetyGuard:
    """防止意外真实资金交易"""
    
    def __init__(self, config: Dict):
        self.config = config
        
        # 从配置读取（无回退值——必须明确指定！）
        safety_config = config.get("safety")
        if not safety_config:
            raise ValueError("❌ CRITICAL: 'safety' section missing in config.json!")
        
        # 检查必需参数
        if "dry_run" not in safety_config:
            raise ValueError("❌ CRITICAL: 'dry_run' not set in config.json!")
        if "max_order_size_usd" not in safety_config:
            raise ValueError("❌ CRITICAL: 'max_order_size_usd' not set in config.json!")
        if "max_total_investment" not in safety_config:
            raise ValueError("❌ CRITICAL: 'max_total_investment' not set in config.json!")
        
        self.dry_run = safety_config["dry_run"]
        self.max_order_size_usd = safety_config["max_order_size_usd"]
        self.max_orders_per_minute = safety_config.get("max_orders_per_minute", 100)  # 可接受的回退值
        self.max_total_investment = safety_config["max_total_investment"]
        
        # 跟踪记录
        self.orders_history = []
        self.invested_per_market = {}  # {market_slug: invested_usd} - 按市场！
        self.emergency_stop = False
        
        # 日志
        self.safety_log = Path("logs/safety.log")
        self.safety_log.parent.mkdir(exist_ok=True)
        
        self._log_init()
    
    def _log_init(self):
        """记录初始化信息"""
        mode = "🟢 DRY_RUN (SAFE)" if self.dry_run else "🔴 LIVE TRADING (REAL MONEY)"
        msg = f"\n{'='*80}\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] SafetyGuard Initialized\n"
        msg += f"Mode: {mode}\n"
        msg += f"Max order size: ${self.max_order_size_usd}\n"
        msg += f"Max orders/min: {self.max_orders_per_minute}\n"
        msg += f"Max total investment: ${self.max_total_investment}\n"
        msg += f"{'='*80}\n"
        
        with open(self.safety_log, 'a', encoding='utf-8') as f:
            f.write(msg)
        
        log.info(msg)
    
    def check_order_allowed(self, side: str, contracts: int, price: float, 
                           market_slug: str) -> Tuple[bool, str]:
        """
        检查订单是否允许
        
        返回：
            (allowed: bool, reason: str)
        """
        # 紧急停止
        if self.emergency_stop:
            return False, "EMERGENCY_STOP_ACTIVE"
        
        # 模拟模式——阻止所有真实订单
        if self.dry_run:
            return False, "DRY_RUN_MODE"
        
        # 订单大小
        order_size_usd = contracts * price
        if order_size_usd > self.max_order_size_usd:
            return False, f"ORDER_TOO_LARGE (${order_size_usd:.2f} > ${self.max_order_size_usd})"
        
        # 速率限制
        recent_orders = [o for o in self.orders_history 
                        if time.time() - o['timestamp'] < 60]
        if len(recent_orders) >= self.max_orders_per_minute:
            return False, f"RATE_LIMIT ({len(recent_orders)}/{self.max_orders_per_minute} per min)"
        
        # 该市场的总投资额（市场切换时重置！）
        current_market_invested = self.invested_per_market.get(market_slug, 0.0)
        
        if current_market_invested + order_size_usd > self.max_total_investment:
            return False, f"INVESTMENT_LIMIT for {market_slug} (${current_market_invested:.2f} + ${order_size_usd:.2f} > ${self.max_total_investment})"
        
        return True, "OK"
    
    def record_order(self, side: str, contracts: float, price: float, 
                    market_slug: str, order_id: str = None):
        """记录已执行的订单"""
        order_size_usd = contracts * price
        
        order = {
            'timestamp': time.time(),
            'market_slug': market_slug,
            'side': side,
            'contracts': contracts,
            'price': price,
            'size_usd': order_size_usd,
            'order_id': order_id,
            'dry_run': self.dry_run
        }
        
        self.orders_history.append(order)
        
        # 累计到该市场（不全局累加！）
        if market_slug not in self.invested_per_market:
            self.invested_per_market[market_slug] = 0.0
        
        self.invested_per_market[market_slug] += order_size_usd
        
        # 写入日志
        with open(self.safety_log, 'a', encoding='utf-8') as f:
            f.write(json.dumps(order) + '\n')
    
    def reset_market(self, market_slug: str):
        """
        重置已关闭市场的投资跟踪
        
        在赎回或市场关闭后调用。
        这样可以在新市场交易而不受之前市场的限制！
        """
        if market_slug in self.invested_per_market:
            invested_amount = self.invested_per_market[market_slug]
            del self.invested_per_market[market_slug]
            log.info(f"[SAFETY] ♻️ Investment tracking reset for {market_slug} (was ${invested_amount:.2f})")
            
            # 写入日志
            with open(self.safety_log, 'a', encoding='utf-8') as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] RESET_MARKET: {market_slug} (${invested_amount:.2f})\n")
    
    def get_market_investment(self, market_slug: str) -> float:
        """获取当前市场投资额"""
        return self.invested_per_market.get(market_slug, 0.0)
    
    def get_total_investment_all_markets(self) -> float:
        """获取所有活跃市场的总投资额（仅供参考）"""
        return sum(self.invested_per_market.values())
    
    def activate_emergency_stop(self, reason: str):
        """激活紧急停止"""
        self.emergency_stop = True
        msg = f"\n🚨 EMERGENCY STOP ACTIVATED: {reason}\n"
        log.info(msg)
        
        with open(self.safety_log, 'a', encoding='utf-8') as f:
            f.write(msg)
