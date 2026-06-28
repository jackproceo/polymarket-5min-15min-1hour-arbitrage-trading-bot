"""
多交易器管理器
管理 4 个独立的交易器实例（2 个策略 × 2 个币种），完全隔离
"""
from typing import Dict, Optional
from pathlib import Path
from trader import Trader


class MultiTrader:
    """管理多个独立交易策略"""
    
    def __init__(self, capital_per_strategy: float = 10000, strategy_names: list = None, config: dict = None):
        """
        初始化独立交易器
        
        参数：
            capital_per_strategy: 每个策略的起始资金
            strategy_names: 策略名称列表（如果为 None，则使用默认 6 个）
            config: 配置字典（用于止损检查）
        """
        self.capital_per_strategy = capital_per_strategy
        self.config = config
        
        # 使用提供的策略名称或默认 6 个
        if strategy_names is None:
            strategy_names = [
                'v1_current',
                'v11_extreme',
                'v9_sqrt',
                'v10_hedge_reduction',
                'v12_balanced',
                'v8_high_base'
            ]
        
        self.traders = {}
        # 获取项目根目录（src 的父目录）
        project_root = Path(__file__).parent.parent
        
        for name in strategy_names:
            log_dir = project_root / "logs" / name
            log_dir.mkdir(parents=True, exist_ok=True)
            self.traders[name] = Trader(capital=capital_per_strategy, log_dir=str(log_dir), config=config)
            print(f"[MULTI-TRADER] Initialized {name} with ${capital_per_strategy:,.0f}")
        
        print(f"[MULTI-TRADER] Total portfolio: ${len(self.traders) * capital_per_strategy:,.0f}")
    
    def enter_position(self, strategy_name: str, market_slug: str, side: str, 
                      price: float, contracts: int,
                      up_ask: float = None, down_ask: float = None,
                      winner_ratio: float = 0.0, is_recovery: bool = False,
                      entry_reason: str = 'normal',
                      seconds_till_end: int = 0, time_from_start: int = 0) -> bool:
        """
        为特定策略开仓（隔离）
        
        参数：
            strategy_name: 使用哪个策略的交易器
            market_slug: 市场标识符
            side: 'UP' 或 'DOWN'
            price: 入场价格
            contracts: 合约数量
            up_ask: 当前 UP 卖价（用于详细日志）
            down_ask: 当前 DOWN 卖价（用于详细日志）
            winner_ratio: 当前胜率（用于详细日志）
            is_recovery: 是否为恢复性入场？（用于详细日志）
            entry_reason: 入场原因（用于详细日志）
            seconds_till_end: 距离市场结束的秒数（用于详细日志）
            time_from_start: 距市场开始的秒数（用于详细日志）
            
        返回：
            成功入场返回 True
        """
        if strategy_name not in self.traders:
            print(f"[ERROR] Unknown strategy: {strategy_name}")
            return False
        
        try:
            trader = self.traders[strategy_name]
            return trader.enter_position_contracts(
                market_slug=market_slug,
                side=side,
                price=price,
                contracts=contracts,
                up_ask=up_ask,
                down_ask=down_ask,
                winner_ratio=winner_ratio,
                is_recovery=is_recovery,
                entry_reason=entry_reason,
                seconds_till_end=seconds_till_end,
                time_from_start=time_from_start
            )
        except Exception as e:
            print(f"[ERROR] {strategy_name} entry failed: {e}")
            return False
    
    def close_market(self, strategy_name: str, market_slug: str, 
                     winner: str, btc_start: float, btc_final: float) -> Optional[Dict]:
        """
        为特定策略平仓（隔离）
        
        参数：
            strategy_name: 使用哪个策略的交易器
            market_slug: 市场标识符
            winner: 'UP' 或 'DOWN'
            btc_start: 起始 BTC 价格
            btc_final: 最终 BTC 价格
            
        返回：
            交易结果字典或 None
        """
        if strategy_name not in self.traders:
            print(f"[ERROR] Unknown strategy: {strategy_name}")
            return None
        
        try:
            trader = self.traders[strategy_name]
            return trader.close_market(
                market_slug=market_slug,
                winner=winner,
                btc_start=btc_start,
                btc_final=btc_final
            )
        except Exception as e:
            print(f"[ERROR] {strategy_name} close failed: {e}")
            return None
    
    def close_market_early_exit(self, strategy_name: str, market_slug: str, 
                                exit_price: float, exit_reason: str = 'early_exit',
                                up_bid: float = None, down_bid: float = None) -> Optional[Dict]:
        """
        为特定策略提前平仓
        
        参数：
            strategy_name: 使用哪个策略的交易器
            market_slug: 市场标识符
            exit_price: 当前优势方价格
            exit_reason: 退出原因（'stop_loss'、'flip_stop'、'early_exit'）
            up_bid: 当前 UP 买价（用于卖出）
            down_bid: 当前 DOWN 买价（用于卖出）
        
        返回：
            交易结果字典或 None
        """
        if strategy_name not in self.traders:
            print(f"[ERROR] Unknown strategy: {strategy_name}")
            return None
        
        try:
            trader = self.traders[strategy_name]
            return trader.close_market_early_exit(
                market_slug=market_slug,
                exit_price=exit_price,
                exit_reason=exit_reason,
                up_bid=up_bid,
                down_bid=down_bid
            )
        except Exception as e:
            print(f"[ERROR] {strategy_name} early exit failed: {e}")
            return None
    
    def get_trader(self, strategy_name: str) -> Optional[Trader]:
        """获取特定交易器实例"""
        return self.traders.get(strategy_name)
    
    def get_all_traders(self) -> Dict[str, Trader]:
        """获取所有交易器实例"""
        return self.traders
    
    def get_portfolio_stats(self) -> Dict:
        """获取投资组合汇总统计"""
        total_capital = 0
        total_pnl = 0
        total_trades = 0
        total_wins = 0
        total_losses = 0
        
        strategy_stats = {}
        
        for name, trader in self.traders.items():
            stats = trader.get_performance_stats()
            
            total_capital += trader.current_capital
            pnl = trader.current_capital - trader.starting_capital
            total_pnl += pnl
            total_trades += stats['total_trades']
            total_wins += stats['wins']
            total_losses += stats['losses']
            
            strategy_stats[name] = {
                'capital': trader.current_capital,
                'pnl': pnl,
                'stats': stats
            }
        
        total_starting = len(self.traders) * self.capital_per_strategy
        portfolio_roi = (total_pnl / total_starting * 100) if total_starting > 0 else 0
        
        return {
            'total_capital': total_capital,
            'total_pnl': total_pnl,
            'total_trades': total_trades,
            'total_wins': total_wins,
            'total_losses': total_losses,
            'portfolio_roi': portfolio_roi,
            'strategy_stats': strategy_stats,
            'num_strategies': len(self.traders)
        }
    
    def get_market_stats(self, strategy_name: str, market_slug: str, up_current: float = 0.5, down_current: float = 0.5) -> Optional[Dict]:
        """
        获取特定策略的市场统计
        
        参数：
            strategy_name: 使用哪个策略的交易器
            market_slug: 市场标识符
            up_current: 当前 UP 卖价（用于未实现盈亏计算）
            down_current: 当前 DOWN 卖价（用于未实现盈亏计算）
            
        返回：
            市场统计字典，若无持仓则返回 None
        """
        if strategy_name not in self.traders:
            return None
        
        trader = self.traders[strategy_name]
        return trader.get_market_stats(market_slug, up_current, down_current)
    
    def get_current_positions(self, strategy_name: str, market_slug: str) -> Optional[Dict]:
        """获取特定策略和市场的当前持仓"""
        if strategy_name not in self.traders:
            return None
        
        trader = self.traders[strategy_name]
        if market_slug not in trader.positions:
            return None
        
        pos = trader.positions[market_slug]
        return {
            'up_shares': pos['UP']['total_shares'],
            'down_shares': pos['DOWN']['total_shares'],
            'up_invested': pos['UP']['total_invested'],
            'down_invested': pos['DOWN']['total_invested'],
            'num_entries': len(pos['all_entries'])
        }
    
    def get_session_stats(self, strategy_name: str, markets_skipped: int = 0) -> Dict:
        """
        获取策略/币种的会话统计
        
        参数：
            strategy_name: 策略标识符（例如 'late_v3_btc'）
            markets_skipped: 跳过的市场数量（外部跟踪）
        
        返回：
            包含会话统计的字典
        """
        if strategy_name not in self.traders:
            return {
                'markets_played': 0,
                'markets_skipped': 0,
                'wins': 0,
                'losses': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'stop_losses': 0,
                'flip_stops': 0,
            }
        
        trader = self.traders[strategy_name]
        stats = trader.get_performance_stats()
        
        # 统计退出类型
        stop_losses = sum(1 for t in trader.closed_trades 
                         if t.get('exit_reason') == 'stop_loss')
        flip_stops = sum(1 for t in trader.closed_trades 
                        if t.get('exit_reason') == 'flip_stop')
        
        return {
            'markets_played': stats['total_trades'],
            'markets_skipped': markets_skipped,
            'wins': stats['wins'],
            'losses': stats['losses'],
            'win_rate': stats['win_rate'],
            'total_pnl': trader.current_capital - trader.starting_capital,
            'stop_losses': stop_losses,
            'flip_stops': flip_stops,
        }
