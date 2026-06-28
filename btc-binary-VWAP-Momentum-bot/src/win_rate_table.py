"""
胜率表：从 CSV 文件加载历史胜率数据，按价格区间和时间分箱查询。
"""

import csv
import logging
from typing import Optional

logger = logging.getLogger("btc_live")


class WinRateTable:
    """从 CSV 加载的胜率查询表"""

    def __init__(self, csv_path: str):
        self.data = {}
        self.price_ranges = []
        self._load(csv_path)

    def _load(self, csv_path: str):
        """从 CSV 文件加载胜率数据"""
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                next(reader)  # 跳过表头
                for row in reader:
                    if not row or not row[0]:
                        continue
                    price_range = row[0]
                    self.price_ranges.append(price_range)
                    self.data[price_range] = {}
                    for i, val in enumerate(row[1:], start=0):
                        if val:
                            try:
                                self.data[price_range][i] = float(val)
                            except ValueError:
                                pass
        except Exception as e:
            logger.warning(f"无法加载 win_rate.csv: {e}")

    def get_winrate(
        self, price: float, minute: int, interval_minutes: int = 15
    ) -> Optional[float]:
        """
        查询指定价格和时间分箱的胜率。

        Args:
            price: 代币价格
            minute: 当前分钟（从 0 开始的时间分箱）
            interval_minutes: 市场周期分钟数（5 或 15）

        Returns:
            胜率百分比，或 None 如果未找到
        """
        price_range = None
        for pr in self.price_ranges:
            try:
                low, high = pr.split('-')
                if float(low) <= price <= float(high):
                    price_range = pr
                    break
            except Exception:
                continue
        if not price_range and price > 0.99 and self.price_ranges:
            price_range = self.price_ranges[-1]
        if not price_range:
            return None
        cap = max(0, interval_minutes - 1)
        minute = max(0, min(cap, minute))
        return self.data.get(price_range, {}).get(minute)
