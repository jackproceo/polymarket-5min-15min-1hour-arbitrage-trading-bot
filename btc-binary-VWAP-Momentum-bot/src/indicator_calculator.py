"""
指标计算器：VWAP、偏差、动量、Z-Score 等统计计算方法。
所有方法均为静态方法，无状态。
"""

import statistics
import time
from collections import deque
from typing import List, Optional

from .core_types import Trade


class IndicatorCalculator:
    """无状态指标计算工具类"""

    @staticmethod
    def get_trades_in_window(trades: deque, window_seconds: float) -> List[Trade]:
        """获取指定时间窗口内的交易"""
        now = time.time()
        cutoff = now - window_seconds
        return [t for t in trades if t.timestamp >= cutoff]

    @staticmethod
    def calc_vwap(trades: List[Trade]) -> float:
        """计算成交量加权平均价格（VWAP）"""
        if not trades:
            return 0.0
        total_value = sum(t.price * t.size for t in trades)
        total_volume = sum(t.size for t in trades)
        return total_value / total_volume if total_volume > 0 else 0.0

    @staticmethod
    def calc_deviation(current_price: float, vwap: float) -> float:
        """计算当前价格相对于 VWAP 的偏差百分比"""
        if vwap == 0:
            return 0.0
        return ((current_price - vwap) / vwap) * 100

    @staticmethod
    def calc_momentum(
        trades: deque,
        current_price: float,
        window: float = 120,
        avg_band: float = 1.5,
    ) -> Optional[float]:
        """
        价格动量：当前价格相对于约 window 秒前的平均价格的变化率。

        取 [now-window-avg_band, now-window+avg_band]（3 秒区间）内的所有交易，
        计算算术平均价格，返回从该平均价格到当前价格的百分比变化。

        如果在区间内找不到交易（历史数据不足），则返回 None。
        """
        now = time.time()
        band_start = now - window - avg_band
        band_end = now - window + avg_band

        band_prices = [t.price for t in trades if band_start <= t.timestamp <= band_end]

        if not band_prices:
            return None

        avg_price_ago = sum(band_prices) / len(band_prices)
        if avg_price_ago == 0:
            return None

        return ((current_price - avg_price_ago) / avg_price_ago) * 100

    @staticmethod
    def calc_zscore(trades: deque, current_price: float, window: float = 5) -> float:
        """计算 Z-Score：当前价格偏离近期均值的标准差数"""
        now = time.time()
        recent = [t for t in trades if t.timestamp >= now - window]
        if len(recent) < 2:
            return 0.0
        prices = [t.price for t in recent]
        mean_price = statistics.mean(prices)
        std_price = statistics.stdev(prices) if len(prices) > 1 else 0.001
        return (current_price - mean_price) / std_price if std_price > 0 else 0.0
