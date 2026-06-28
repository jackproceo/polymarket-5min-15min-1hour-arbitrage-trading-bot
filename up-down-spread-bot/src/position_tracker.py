"""
仓位跟踪器——仓位数据的单一真实来源！

仅通过 WebSocket 用户频道事件更新。
无猜测或计算——仅来自 Polymarket API 的真实数据。
"""

import time
from typing import Dict, Optional, List
from dataclasses import dataclass
from threading import Lock


@dataclass
class TradeInfo:
    """已确认交易的信息"""
    trade_id: str
    side: str  # BUY/SELL
    contracts: float
    price: float
    usd_amount: float
    timestamp: float
    status: str  # MATCHED/MINED/CONFIRMED


class PositionTracker:
    """
    仓位数据的单一真实来源！
    
    仅通过 WebSocket 用户频道更新：
    - ORDER 事件（size_matched = 实际数量）
    - TRADE 事件（链上确认）
    
    无猜测！仅真实数据！
    """
    
    def __init__(self):
        self.positions = {}
        # 结构：
        # {
        #   'market_slug': {
        #     'UP': {
        #       'contracts': 120.5,    # 实际数量
        #       'invested': 85.32,     # 实际投资额
        #       'trades': [TradeInfo]  # 所有交易
        #     },
        #     'DOWN': {...}
        #   }
        # }
        
        self.pending_orders = {}  # order_id -> 订单数据
        self.confirmed_trades = {}  # trade_id -> TradeInfo
        
        self.asset_to_market = {}  # asset_id -> (market_slug, side_name)
        self.lock = Lock()
        
        print("[TRACKER] ✅ Position Tracker initialized - REAL DATA ONLY!")
    
    def register_market(self, market_slug: str, up_token_id: str, down_token_id: str):
        """
        注册市场及其代币
        
        用于映射 asset_id -> market_slug
        """
        with self.lock:
            self.asset_to_market[up_token_id] = (market_slug, 'UP')
            self.asset_to_market[down_token_id] = (market_slug, 'DOWN')
            
            if market_slug not in self.positions:
                self.positions[market_slug] = {
                    'UP': {'contracts': 0.0, 'invested': 0.0, 'trades': []},
                    'DOWN': {'contracts': 0.0, 'invested': 0.0, 'trades': []}
                }
            
            print(f"[TRACKER] 📋 Registered market: {market_slug}")
    
    def on_order_event(self, order_data: dict):
        """
        处理 WebSocket 的 ORDER 事件
        
        类型：
        - PLACEMENT: 订单已下达
        - UPDATE: 订单已成交（部分或全部）
        - CANCELLATION: 订单已取消
        """
        try:
            order_type = order_data.get('type')
            order_id = order_data.get('id')
            
            if order_type == 'PLACEMENT':
                # 保存待处理订单
                with self.lock:
                    self.pending_orders[order_id] = order_data
                print(f"[TRACKER] 📝 Order placed: {order_id[:16]}...")
            
            elif order_type == 'UPDATE':
                # ✅ 订单已成交！使用真实数据更新仓位！
                size_matched = float(order_data.get('size_matched', 0))
                original_size = float(order_data.get('original_size', 0))
                asset_id = order_data.get('asset_id')
                side = order_data.get('side')  # BUY/SELL
                price = float(order_data.get('price', 0))
                
                # 按 asset_id 查找市场
                market_info = self.asset_to_market.get(asset_id)
                if not market_info:
                    print(f"[TRACKER] ⚠ Unknown asset_id: {asset_id}")
                    return
                
                market_slug, side_name = market_info
                
                with self.lock:
                    # 确保市场已初始化
                    if market_slug not in self.positions:
                        self.positions[market_slug] = {
                            'UP': {'contracts': 0.0, 'invested': 0.0, 'trades': []},
                            'DOWN': {'contracts': 0.0, 'invested': 0.0, 'trades': []}
                        }
                    
                    pos = self.positions[market_slug][side_name]
                    
                    if side == 'BUY':
                        # ✅ 买入——增加仓位
                        pos['contracts'] += size_matched
                        pos['invested'] += (size_matched * price)
                        
                        print(f"[TRACKER] ✅ BUY {side_name}: +{size_matched:.2f} @ ${price:.4f}")
                        print(f"          Position now: {pos['contracts']:.2f} contracts, ${pos['invested']:.2f} invested")
                    
                    elif side == 'SELL':
                        # ✅ 卖出——减少仓位
                        pos['contracts'] -= size_matched
                        # 卖出时不动 invested（用于盈亏计算）
                        
                        received_usd = size_matched * price
                        print(f"[TRACKER] ✅ SELL {side_name}: -{size_matched:.2f} @ ${price:.4f} = ${received_usd:.2f}")
                        print(f"          Position now: {pos['contracts']:.2f} contracts")
            
            elif order_type == 'CANCELLATION':
                # 订单已取消
                with self.lock:
                    if order_id in self.pending_orders:
                        del self.pending_orders[order_id]
                print(f"[TRACKER] ❌ Order cancelled: {order_id[:16]}...")
        
        except Exception as e:
            print(f"[TRACKER] ⚠ Error processing order event: {e}")
    
    def on_trade_event(self, trade_data: dict):
        """
        处理 WebSocket 的 TRADE 事件
        
        状态进展：
        - MATCHED: 交易已匹配
        - MINED: 交易已上链
        - CONFIRMED: 交易已确认（最终状态！）
        - RETRYING/FAILED: 错误
        """
        try:
            trade_id = trade_data.get('id')
            status = trade_data.get('status')
            size = float(trade_data.get('size', 0))
            price = float(trade_data.get('price', 0))
            side = trade_data.get('side')  # BUY/SELL
            asset_id = trade_data.get('asset_id')
            
            if status == 'MATCHED':
                print(f"[TRACKER] 🔄 Trade matched: {trade_id[:16]}... ({side} {size:.2f})")
            
            elif status == 'MINED':
                print(f"[TRACKER] ⛏️  Trade mined: {trade_id[:16]}...")
            
            elif status == 'CONFIRMED':
                # ✅ 交易已在链上确认！
                market_info = self.asset_to_market.get(asset_id)
                if market_info:
                    market_slug, side_name = market_info
                    
                    trade_info = TradeInfo(
                        trade_id=trade_id,
                        side=side,
                        contracts=size,
                        price=price,
                        usd_amount=size * price,
                        timestamp=time.time(),
                        status=status
                    )
                    
                    with self.lock:
                        self.confirmed_trades[trade_id] = trade_info
                        
                        # 添加到仓位交易历史
                        if market_slug in self.positions:
                            self.positions[market_slug][side_name]['trades'].append(trade_info)
                    
                    print(f"[TRACKER] ✅ Trade CONFIRMED: {trade_id[:16]}...")
                    print(f"          {side} {size:.2f} @ ${price:.4f} = ${size * price:.2f}")
            
            elif status in ['RETRYING', 'FAILED']:
                print(f"[TRACKER] ⚠️  Trade {status}: {trade_id[:16]}...")
        
        except Exception as e:
            print(f"[TRACKER] ⚠ Error processing trade event: {e}")
    
    def get_position(self, market_slug: str, side: str) -> Dict:
        """
        获取来自 WebSocket 跟踪的真实仓位
        
        返回：
        {
            'contracts': 120.5,   # 准确数量
            'invested': 85.32,    # 准确投资额
            'avg_price': 0.71,    # 平均入场价格
            'trades_count': 10    # 交易次数
        }
        """
        with self.lock:
            if market_slug not in self.positions:
                return {
                    'contracts': 0.0,
                    'invested': 0.0,
                    'avg_price': 0.0,
                    'trades_count': 0
                }
            
            pos = self.positions[market_slug].get(side, {'contracts': 0.0, 'invested': 0.0, 'trades': []})
            contracts = pos['contracts']
            invested = pos['invested']
            avg_price = invested / contracts if contracts > 0 else 0.0
            
            return {
                'contracts': contracts,
                'invested': invested,
                'avg_price': avg_price,
                'trades_count': len(pos['trades'])
            }
    
    def get_total_position(self, market_slug: str) -> Dict:
        """
        获取按市场统计的总仓位（双边）
        """
        with self.lock:
            if market_slug not in self.positions:
                return {
                    'up_contracts': 0.0,
                    'down_contracts': 0.0,
                    'up_invested': 0.0,
                    'down_invested': 0.0,
                    'total_invested': 0.0,
                    'total_contracts': 0.0
                }
            
            up = self.positions[market_slug]['UP']
            down = self.positions[market_slug]['DOWN']
            
            return {
                'up_contracts': up['contracts'],
                'down_contracts': down['contracts'],
                'up_invested': up['invested'],
                'down_invested': down['invested'],
                'total_invested': up['invested'] + down['invested'],
                'total_contracts': up['contracts'] + down['contracts']
            }
    
    def calculate_pnl(self, market_slug: str, up_price: float, down_price: float) -> Dict:
        """
        基于真实仓位计算真实未实现盈亏
        
        返回：
        {
            'unrealized_pnl': -5.32,
            'unrealized_pnl_pct': -5.87,
            'current_value': 85.18,
            'total_invested': 90.50
        }
        """
        with self.lock:
            if market_slug not in self.positions:
                return {
                    'unrealized_pnl': 0.0,
                    'unrealized_pnl_pct': 0.0,
                    'current_value': 0.0,
                    'total_invested': 0.0
                }
            
            up = self.positions[market_slug]['UP']
            down = self.positions[market_slug]['DOWN']
            
            # 当前仓位价值
            up_value = up['contracts'] * up_price
            down_value = down['contracts'] * down_price
            current_value = up_value + down_value
            
            # 总投资额
            total_invested = up['invested'] + down['invested']
            
            # 盈亏
            unrealized_pnl = current_value - total_invested
            unrealized_pnl_pct = (unrealized_pnl / total_invested * 100) if total_invested > 0 else 0.0
            
            return {
                'unrealized_pnl': unrealized_pnl,
                'unrealized_pnl_pct': unrealized_pnl_pct,
                'current_value': current_value,
                'total_invested': total_invested
            }
    
    def has_position(self, market_slug: str) -> bool:
        """检查是否有未平仓仓位"""
        with self.lock:
            if market_slug not in self.positions:
                return False
            
            up_contracts = self.positions[market_slug]['UP']['contracts']
            down_contracts = self.positions[market_slug]['DOWN']['contracts']
            
            return up_contracts > 0.01 or down_contracts > 0.01
    
    def clear_position(self, market_slug: str):
        """清除仓位（市场关闭后）"""
        with self.lock:
            if market_slug in self.positions:
                print(f"[TRACKER] 🧹 Clearing position for {market_slug}")
                del self.positions[market_slug]
    
    def get_all_positions(self) -> Dict:
        """获取所有未平仓仓位"""
        with self.lock:
            return {
                slug: self.get_total_position(slug)
                for slug in self.positions.keys()
                if self.has_position(slug)
            }
