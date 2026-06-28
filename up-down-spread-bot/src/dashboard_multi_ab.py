"""
Meridian — 终端仪表盘（late_v3 × BTC、ETH、SOL、XRP）。
"""
import time
from typing import Dict
from multi_trader import MultiTrader


class DashboardMultiAB:
    """多市场仪表盘——按策略分组"""
    
    def __init__(self, width: int = 160, coins: list = None, config: dict = None):
        self.width = width
        self.start_time = time.time()
        self.coins = coins or ['btc', 'eth', 'sol', 'xrp']
        self.events_log = []  # 最近 N 个用于显示的事件
        self.max_events = 10
        self.config = config or {}
    
    def add_event(self, message: str, event_type: str = 'info'):
        """将事件添加到日志（仅在终端中显示关键错误）"""
        # 过滤：仅在终端中显示错误
        if event_type not in ['error']:
            return  # 忽略除错误外的所有内容
        
        timestamp = time.strftime('%H:%M:%S')
        
        # 为终端显示缩短消息
        if len(message) > 70:
            message = message[:67] + "..."
        
        emoji = '✗'  # 仅用于错误
        
        event = f"[{timestamp}] {emoji} {message}"
        self.events_log.append(event)
        
        # 仅保留最近 N 个事件
        if len(self.events_log) > self.max_events:
            self.events_log = self.events_log[-self.max_events:]
    
    def render(self, multi_trader: MultiTrader, strategies: Dict, data_feed, wallet_balance: float = None, pending_markets: Dict = None):
        """渲染仪表盘"""
        # 清屏
        print('\033[2J\033[H', end='')
        
        # 构建显示
        lines = self._build_display(multi_trader, strategies, data_feed, wallet_balance, pending_markets)
        print(lines, end='', flush=True)
    
    def _build_display(self, multi_trader: MultiTrader, strategies: Dict, data_feed, wallet_balance: float = None, pending_markets: Dict = None) -> str:
        """构建显示字符串"""
        output = []
        
        # 获取所有币种的市场状态
        market_states = {}
        for coin in self.coins:
            market_states[coin] = data_feed.get_state(coin)
        
        # 运行时间
        runtime = time.time() - self.start_time
        runtime_str = self._format_time(runtime)
        
        # 头部——所有币种使用订单簿数据
        header = f"⏱ {runtime_str} │ BTC │ ETH │ SOL │ XRP（Polymarket 订单簿）"
        
        output.append("=" * self.width)
        output.append(header.center(self.width))
        output.append("=" * self.width)
        output.append("")
        
        # 策略基础名称
        strategy_bases = [
            ('late_v3', 'LATE V3')
        ]
        
        # 显示每个策略（按基础分组）
        for base_name, display_name in strategy_bases:
            output.append(f"┌─ {display_name.upper()} {'─' * (self.width - len(display_name) - 5)}┐")
            
            # 计算此策略的总计（所有币种）
            traders = {}
            stats = {}
            for coin in self.coins:
                trader_name = f"{base_name}_{coin}"
                if trader_name in multi_trader.traders:
                    traders[coin] = multi_trader.traders[trader_name]
                    stats[coin] = traders[coin].get_performance_stats()
            
            if not traders:
                output.append(f"│ 错误：策略未找到交易者")
                output.append(f"└{'─' * (self.width - 2)}┘")
                output.append("")
                continue
            
            # 策略总计
            total_capital = sum(t.current_capital for t in traders.values())
            starting_capital = sum(t.starting_capital for t in traders.values())
            total_pnl = total_capital - starting_capital
            
            # 正确计算收益率——如果可用则使用 wallet_balance，否则使用 starting_capital
            # 收益率 = 盈亏 / 初始投资 * 100
            if wallet_balance and wallet_balance > 0:
                # 使用真实钱包余额计算初始投资
                initial_balance = wallet_balance - total_pnl
                total_roi = (total_pnl / initial_balance * 100) if initial_balance > 0 else 0
            elif starting_capital > 0:
                # 回退到 starting_capital（如果可用）
                total_roi = (total_pnl / starting_capital * 100)
            else:
                # 最后手段：从当前资本计算
                total_roi = (total_pnl / total_capital * 100) if total_capital > total_pnl and total_capital > 0 else 0
            total_trades = sum(s['total_trades'] for s in stats.values())
            total_wins = sum(s['wins'] for s in stats.values())
            total_losses = sum(s['losses'] for s in stats.values())
            total_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0
            
            # 为总盈亏着色
            pnl_color = '\033[92m' if total_pnl >= 0 else '\033[91m'
            pnl_reset = '\033[0m'
            pnl_sign = "+" if total_pnl >= 0 else ""
            
            # 策略汇总行（统一余额）
            balance_display = f"${wallet_balance:,.2f}" if wallet_balance else f"${total_capital:,.0f}"
            output.append(f"│ 余额：{balance_display} │ 盈亏：{pnl_color}{pnl_sign}${total_pnl:,.0f}（{pnl_sign}{total_roi:.1f}%）{pnl_reset} │ "
                         f"交易数：{total_trades} │ 胜/负：{total_wins}/{total_losses} │ 胜率：{total_wr:.1f}%")
            output.append(f"│")
            
            # 显示每个币种的市场
            for coin in self.coins:
                if coin in traders:
                    trader_name = f"{base_name}_{coin}"
                    self._add_market_info(output, coin.upper(), market_states[coin], trader_name, 
                                         traders[coin], strategies.get(trader_name), multi_trader)
            
            output.append(f"└{'─' * (self.width - 2)}┘")
            output.append("")
        
        # 近期活动（紧凑）
        output.append("📈 近期交易：")
        
        all_closed = []
        for name, trader in multi_trader.traders.items():
            for trade in trader.closed_trades[-1:]:
                trade['strategy'] = name
                all_closed.append(trade)
        
        all_closed.sort(key=lambda x: x.get('close_time', 0), reverse=True)
        
        for trade in all_closed[:4]:
            strategy = trade['strategy']
            # 从策略名称中提取币种（最后部分）
            coin = strategy.split('_')[-1].upper()
            # 提取基础名称（币种之前的部分）
            base = '_'.join(strategy.split('_')[:-1]).replace('late_v3', 'LV3')
            market = trade['market_slug'].split('-')[-1]
            pnl = trade['pnl']
            pnl_sign = "+" if pnl >= 0 else ""
            pnl_color = '\033[92m' if pnl >= 0 else '\033[91m'
            pnl_reset = '\033[0m'
            
            activity = f"  [{base:>3}/{coin:>3}] {market}：{pnl_color}{pnl_sign}${pnl:>5,.0f}{pnl_reset}"
            output.append(activity)
        
        if not all_closed:
            output.append("  （无）")
        
        output.append("")
        
        # 待处理市场
        if pending_markets:
            import time as time_module
            output.append("⏳ 待处理：")
            
            for market_slug_pending, info in pending_markets.items():
                elapsed = (time_module.time() - info['first_attempt']) / 60
                next_retry = (info['next_retry'] - time_module.time()) / 60
                
                # 从市场 slug 提取币种
                coin = market_slug_pending.split('-')[0].upper()
                market_short = market_slug_pending.split('-')[-1]
                
                if next_retry > 0:
                    status = f"~{next_retry:.0f}m（#{info['attempts']}）"
                else:
                    status = f"检查中...（#{info['attempts'] + 1}）"
                
                output.append(f"  • {coin}/{market_short}：{status}")
            
            output.append("")
        
        # 事件日志（如果有任何关键错误）
        if self.events_log:
            output.append("🚨 关键错误：")
            for event in self.events_log[-10:]:  # 最近 10 条
                output.append(f"  {event}")
            output.append("")
        
        # 添加键盘控制页脚
        output.append("─" * self.width)
        output.append("🎹 键盘：[M] 手动全部赎回  │  [Ctrl+C] 停止交易".center(self.width))
        
        return '\n'.join(output)
    
    def _add_market_info(self, output, coin_label, market_state, trader_name, trader, strategy, multi_trader):
        """为指定币种添加市场信息块"""
        market_slug = market_state['market_slug']
        seconds_left = market_state['seconds_till_end']
        up_ask = market_state.get('up_ask') or 0.0
        down_ask = market_state.get('down_ask') or 0.0
        confidence = market_state.get('confidence', 0.0)
        
        # 剩余时间
        time_left_str = self._format_time(seconds_left) if seconds_left > 0 else "已结束"
        market_short = market_slug.split('-')[-1] if market_slug else "N/A"
        
        # 做市商偏好（更高价格 = 偏好）
        mm_favorite = 'UP' if up_ask > down_ask else 'DOWN'
        fav_arrow = '↑' if mm_favorite == 'UP' else '↓'
        
        # 为信心着色
        conf_color = '\033[92m' if confidence >= 0.2 else '\033[93m'
        conf_reset = '\033[0m'
        
        # 策略统计
        stats = trader.get_performance_stats()
        pnl = trader.current_capital - trader.starting_capital
        pnl_sign = "+" if pnl >= 0 else ""
        pnl_color = '\033[92m' if pnl >= 0 else '\033[91m'
        pnl_reset = '\033[0m'
        
        # 胜率止损统计
        wr_stopped = 0
        recoveries = 0
        if strategy:
            strategy_stats = strategy.get_stats()
            wr_stopped = strategy_stats['skip_breakdown'].get('wr_stop_loss', 0)
            recoveries = strategy_stats.get('wr_recoveries', 0)
        
        # 交易状态指示器
        coin_lower = coin_label.lower()
        trading_enabled = self.config.get('trading', {}).get(coin_lower, {}).get('enabled', True)
        trading_status = "📈" if trading_enabled else "👁️"
        
        # 市场头部
        output.append(f"│ {trading_status} 【{coin_label}】 {market_short} │ ⏰ {time_left_str} │ "
                     f"UP:{up_ask:.3f} DN:{down_ask:.3f} {fav_arrow}{mm_favorite} │ "
                     f"信心：{conf_color}{confidence:.3f}{conf_reset}")
        
        # 统计行（无上限——头部使用统一余额）
        output.append(f"│   盈亏：{pnl_color}{pnl_sign}${pnl:,.0f}{pnl_reset} │ "
                     f"交易数：{stats['total_trades']} │ 胜/负：{stats['wins']}/{stats['losses']} │ "
                     f"胜率：{stats['win_rate']:.1f}% │ 止损：{wr_stopped} 恢复：{recoveries}")
        
        # 当前仓位
        pos = multi_trader.get_current_positions(trader_name, market_slug)
        
        if pos and (pos['up_shares'] > 0 or pos['down_shares'] > 0):
            # 获取详细统计
            detailed_stats = trader.get_market_detailed_stats(market_slug, up_ask, down_ask)
            
            if detailed_stats:
                up_shares = detailed_stats['up_shares']
                down_shares = detailed_stats['down_shares']
                up_invested = detailed_stats['up_invested']
                down_invested = detailed_stats['down_invested']
                total_invested = detailed_stats['total_invested']
                unrealized_pnl = detailed_stats['unrealized_pnl']
                unrealized_pct = detailed_stats['unrealized_pct']
                max_dd = detailed_stats['max_drawdown']
                max_dd_pct = detailed_stats['max_drawdown_pct']
                entries_count = detailed_stats['entries_count']
                
                # 计算盈亏场景
                if_up_wins = (up_shares * 1.0) - total_invested
                if_down_wins = (down_shares * 1.0) - total_invested
                
                # 确定我们的押注
                total_shares = up_shares + down_shares
                our_pct = (up_shares / total_shares * 100) if total_shares > 0 else 50
                our_favorite = 'UP' if up_shares > down_shares else 'DOWN'
                
                # 状态
                is_right = (our_favorite == mm_favorite)
                overall_status = '\033[92m✓\033[0m' if is_right else '\033[91m✗\033[0m'
                
                # 为未实现盈亏着色
                unreal_color = '\033[92m' if unrealized_pnl >= 0 else '\033[91m'
                unreal_reset = '\033[0m'
                
                # 仓位详情（紧凑 3 行格式）
                output.append(f"│   仓位：UP:{int(up_shares)}×{up_ask:.3f}=${up_invested:.0f} │ "
                             f"DN:{int(down_shares)}×{down_ask:.3f}=${down_invested:.0f} │ "
                             f"总计：${total_invested:.0f} │ 入场次数：{entries_count}")
                output.append(f"│   当前：{unreal_color}{unrealized_pnl:+.0f}（{unrealized_pct:+.0f}%）{unreal_reset} │ "
                             f"最大回撤：{max_dd:.0f}（{max_dd_pct:.0f}%）│ "
                             f"如涨：{if_up_wins:+.0f} 如跌：{if_down_wins:+.0f}")
                output.append(f"│   押注：{our_favorite}（{our_pct:.0f}%）vs 做市商：{mm_favorite} {overall_status}")
        else:
            output.append(f"│   仓位：无")
        
        output.append(f"│")
    
    def _format_time(self, seconds: float) -> str:
        """将秒格式化为 HH:MM:SS 或 MM:SS"""
        seconds = int(seconds)
        if seconds >= 3600:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            secs = seconds % 60
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        else:
            minutes = seconds // 60
            secs = seconds % 60
            return f"{minutes:02d}:{secs:02d}"
