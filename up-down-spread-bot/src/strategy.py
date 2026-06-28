"""
Meridian——后期窗口入场策略（Late Entry V3 / late_v3）。
基于时间的仓位规模；支持 5 分钟和 15 分钟 Polymarket 窗口（参见 data_sources.polymarket.market_interval_sec）。
"""
import time
from typing import Optional, Dict


class LateEntryStrategy:
    """后期窗口入场：在窗口的最后几分钟交易优势方。"""
    
    def __init__(self, config: Dict):
        # 从配置读取所有参数（无硬编码值！）
        strategy_cfg = config.get('strategy', {})
        pm = config.get("data_sources", {}).get("polymarket", {})
        self.market_interval_sec = int(pm.get("market_interval_sec", 900))
        if self.market_interval_sec <= 0:
            self.market_interval_sec = 900
        
        # 默认入场窗口：15m 约最后 4 分钟，5m 约最后 2 分钟（可在配置中覆盖）
        default_entry = 240 if self.market_interval_sec >= 900 else min(120, self.market_interval_sec - 10)
        raw_ew = int(strategy_cfg.get("entry_window_sec", default_entry))
        # 如果在 5m 市场上配置仍使用 15m 风格的值（如 240），则回退到默认值
        if self.market_interval_sec < 900 and raw_ew > self.market_interval_sec * 0.5:
            raw_ew = default_entry
        self.entry_window = min(raw_ew, max(10, self.market_interval_sec - 5))
        self.entry_freq = strategy_cfg.get('entry_frequency_sec', 7)
        self.min_confidence = strategy_cfg.get('min_confidence', 0.30)
        self.max_spread = strategy_cfg.get('max_spread', 1.05)
        self.price_max = strategy_cfg.get('price_max', 0.93)
        
        # 仓位规模（合约数）——从配置读取，基于时间！
        sizing_cfg = strategy_cfg.get('sizing', {})
        self.size_above_180 = sizing_cfg.get('above_180_sec', 8)
        self.size_above_120 = sizing_cfg.get('above_120_sec', 10)
        self.size_below_120 = sizing_cfg.get('below_120_sec', 12)
        # 为较短窗口（如 5m → 60s/40s）缩放 180s/120s 阈值
        scale = self.market_interval_sec / 900.0
        self.sizing_t1 = max(15, int(180 * scale))
        self.sizing_t2 = max(10, int(120 * scale))
        
        # 每市场最大投资额
        self.max_investment = strategy_cfg.get('max_investment_per_market', 300)
        
        # 翻转止损价格（价格反转保护）
        exit_cfg = config.get('exit', {})
        flip_cfg = exit_cfg.get('flip_stop', {})
        self.flip_stop_price = flip_cfg.get('price_threshold', 0.48)
        
        # 跟踪每市场的最后入场
        self.last_entry = {}
        self.last_favorite = {}
    
    def should_enter(self, state: Dict, position: Optional[Dict] = None) -> Optional[Dict]:
        """
        检查是否应该入场（Late Entry V3 逻辑）
        
        参数：
            state: 市场状态，包含以下键：
                - market_slug: str
                - seconds_till_end: int
                - up_ask: float
                - down_ask: float
            position: 可选的持仓统计
        
        返回：
            信号字典或 None
        """
        market = state['market_slug']
        time_left = state['seconds_till_end']
        up_ask = state['up_ask']
        down_ask = state['down_ask']
        
        # 时间：仅在配置的后期窗口内
        if time_left > self.entry_window or time_left <= 0:
            return None
        
        # 频率
        now = time.time()
        if market in self.last_entry and now - self.last_entry[market] < self.entry_freq:
            return None
        
        # 价差
        spread = up_ask + down_ask
        if spread > self.max_spread or spread <= 0:
            return None
        
        # 置信度
        confidence = abs(up_ask - down_ask)
        if confidence < self.min_confidence:
            return None
        
        # 优势方
        favorite = 'UP' if up_ask > down_ask else 'DOWN'
        fav_price = up_ask if favorite == 'UP' else down_ask
        
        # 最高价格限制
        if fav_price > self.price_max:
            return None
        
        # 投资限制
        if position:
            total_cost = position.get('total_cost', 0)
            if total_cost >= self.max_investment:
                return None
        
        # 风险检查——止损已移除，仅通过 main.py 进行翻转止损
        # 翻转止损逻辑在 main.py 中（检查：our_price <= strategy.flip_stop_price）
        
        # 入场（仓位规模阈值随市场长度缩放：15m → 180/120s，5m → 60/40s）
        size = (
            self.size_above_180
            if time_left > self.sizing_t1
            else (self.size_above_120 if time_left > self.sizing_t2 else self.size_below_120)
        )
        
        self.last_entry[market] = now
        self.last_favorite[market] = favorite
        
        return {
            'favored': {
                'side': favorite,
                'price': fav_price,
                'contracts': size,
            },
            'hedge': {
                'side': 'DOWN' if favorite == 'UP' else 'UP',
                'price': down_ask if favorite == 'UP' else up_ask,
                'contracts': 0,
            },
            'confidence': confidence,
            'is_recovery': False,
            'entry_reason': f'late_entry_{time_left}s',
            'winner_ratio': 0.0
        }
    
    def get_stats(self) -> Dict:
        """获取策略统计信息（用于仪表板兼容）"""
        return {
            'generated': 0,
            'skipped': 0,
            'total': 0,
            'skip_breakdown': {},
            'gen_pct': 0,
            'skip_pct': 0,
            'wr_recoveries': 0
        }
    
    def reset_market(self, market_slug: str):
        """重置市场的跟踪信息"""
        if market_slug in self.last_entry:
            del self.last_entry[market_slug]
        if market_slug in self.last_favorite:
            del self.last_favorite[market_slug]
