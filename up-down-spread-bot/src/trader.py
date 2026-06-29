"""
仓位管理，支持每个市场多次入场
"""
import time
import json
import threading
from typing import Dict, List, Optional
from pathlib import Path

from utils.logging_setup import get_logger
log = get_logger("trader")


class Trader:
    """管理交易仓位，支持详细的入场跟踪"""
    
    # ── 类级别共享状态（替代模块级全局变量）──
    order_executor = None
    data_feed = None
    token_ids_cache = {}
    market_metadata_cache = {}
    METADATA_FILE = Path("logs/market_metadata.json")

    @classmethod
    def set_order_executor(cls, executor):
        """注入 OrderExecutor 用于真实交易"""
        cls.order_executor = executor
        log.info("[TRADER] ✓ OrderExecutor injected")

    @classmethod
    def set_data_feed(cls, data_feed):
        """注入 DataFeed 以访问真实仓位"""
        cls.data_feed = data_feed
        log.info("[TRADER] ✅ DataFeed injected (REAL position tracking)")

    @classmethod
    def save_market_metadata_to_disk(cls):
        """将元数据保存到磁盘（重启后用于赎回，至关重要！）"""
        try:
            cls.METADATA_FILE.parent.mkdir(exist_ok=True)
            combined = {}
            for market_slug in cls.token_ids_cache:
                combined[market_slug] = {
                    'token_ids': cls.token_ids_cache[market_slug],
                    'metadata': cls.market_metadata_cache.get(market_slug, {})
                }
            with open(cls.METADATA_FILE, 'w') as f:
                json.dump(combined, f, indent=2)
        except Exception as e:
            log.warning(f"[TRADER] ⚠️ Failed to save metadata: {e}")

    @classmethod
    def load_market_metadata_from_disk(cls):
        """从磁盘加载元数据（重启后赎回用）"""
        if not cls.METADATA_FILE.exists():
            log.info("[TRADER] ℹ️ No metadata file found (first run or clean start)")
            return
        try:
            with open(cls.METADATA_FILE, 'r') as f:
                combined = json.load(f)
            for market_slug, data in combined.items():
                if 'token_ids' in data:
                    cls.token_ids_cache[market_slug] = data['token_ids']
                if 'metadata' in data:
                    cls.market_metadata_cache[market_slug] = data['metadata']
            log.info(f"[TRADER] ✅ Loaded metadata for {len(combined)} markets from disk")
        except Exception as e:
            log.warning(f"[TRADER] ⚠️ Failed to load metadata: {e}")

    @classmethod
    def set_token_ids(cls, market_slug: str, up_token_id: str, down_token_id: str,
                      condition_id: str = "", neg_risk: bool = True):
        """缓存市场 token ID 和元数据，并保存到磁盘！"""
        cls.token_ids_cache[market_slug] = {'UP': up_token_id, 'DOWN': down_token_id}
        cls.market_metadata_cache[market_slug] = {'condition_id': condition_id, 'neg_risk': neg_risk}
        cls.save_market_metadata_to_disk()

    @classmethod
    def get_token_ids(cls, market_slug: str) -> dict:
        """获取市场的 token ID"""
        return cls.token_ids_cache.get(market_slug, {})

    @classmethod
    def get_market_metadata(cls, market_slug: str) -> dict:
        """获取市场元数据（condition_id、neg_risk）"""
        return cls.market_metadata_cache.get(market_slug, {})

    def __init__(self, capital: float, log_dir: str = "logs", config: dict = None):
        self.starting_capital = capital
        self.current_capital = capital
        
        # 止损检查配置
        self.config = config
        
        # 仓位：{market_slug: {'UP': {...}, 'DOWN': {...}, 'entries': [...], ...}}
        self.positions = {}
        
        # 已平仓交易历史
        self.closed_trades = []
        
        # 跟踪已关闭市场，防止提前退出后重新入场
        self.closed_markets = set()  # 已关闭的市场（提前退出或正常退出）
        
        # 🛡️ 线程安全：异步操作锁
        self.lock = threading.RLock()  # 可重入锁（避免死锁）
        
        # 市场统计跟踪
        self.market_max_drawdown = {}  # {market_slug: max_dd_value}
        self.market_entries_count = {}  # {market_slug: count}
        
        # 日志
        self.log_dir = Path(log_dir)
        self.trades_file = self.log_dir / "trades.jsonl"
        self.session_file = self.log_dir / "session.json"
        
        log.info(f"[TRADER] Initialized with ${capital:,.2f} capital")
        
        # 加载以往交易以恢复统计
        self.load_previous_trades()
    
    def load_previous_trades(self):
        """
        从 trades.jsonl 加载以往交易以恢复统计
        允许机器人在重启后从中断处继续运行
        """
        if not self.trades_file.exists():
            log.info(f"[TRADER] No previous trades file found (this is OK for first run)")
            return
        
        try:
            loaded_count = 0
            corrupted_lines = 0
            
            with open(self.trades_file, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue  # 跳过空行
                    
                    try:
                        trade = json.loads(line)
                        
                        # 验证交易是否包含必填字段
                        if 'pnl' not in trade or 'market_slug' not in trade:
                            log.warning(f"[WARNING] Trade on line {line_num} missing required fields, skipping")
                            corrupted_lines += 1
                            continue
                        
                        self.closed_trades.append(trade)
                        loaded_count += 1
                        
                    except json.JSONDecodeError as e:
                        log.warning(f"[WARNING] Corrupted JSON on line {line_num}: {e}")
                        corrupted_lines += 1
                        continue
            
            if loaded_count > 0:
                # 从加载的交易中重新计算当前资金
                total_pnl = sum(t['pnl'] for t in self.closed_trades)
                self.current_capital = self.starting_capital + total_pnl
                
                # 获取统计信息
                wins = sum(1 for t in self.closed_trades if t['pnl'] > 0)
                win_rate = (wins / loaded_count * 100) if loaded_count > 0 else 0
                
                log.info(f"[TRADER] ✓ Loaded {loaded_count} previous trade(s)")
                log.info(f"[TRADER]   Cumulative PnL: ${total_pnl:+,.2f}")
                log.info(f"[TRADER]   Win Rate: {win_rate:.1f}% ({wins}/{loaded_count})")
                log.info(f"[TRADER]   Current Capital: ${self.current_capital:,.2f}")
                
                if corrupted_lines > 0:
                    log.warning(f"[TRADER] ⚠ Skipped {corrupted_lines} corrupted line(s)")
            else:
                log.info(f"[TRADER] No valid trades found in file")
                
        except Exception as e:
            log.warning(f"[TRADER] ⚠ Error loading previous trades: {e}")
            log.info(f"[TRADER] Starting fresh with capital ${self.starting_capital:,.2f}")
            # 出错时重置为全新状态
            self.closed_trades = []
            self.current_capital = self.starting_capital
    
    def enter_position_contracts(self, market_slug: str, side: str, price: float, contracts: int,
                                 up_ask: float = None, down_ask: float = None,
                                 winner_ratio: float = 0.0, is_recovery: bool = False,
                                 entry_reason: str = 'normal',
                                 seconds_till_end: int = 0, time_from_start: int = 0) -> bool:
        """
        通过指定合约/份额数量来建仓
        🛡️ 线程安全：可从不同线程调用
        
        参数：
            market_slug: 市场标识
            side: 'UP' 或 'DOWN'
            price: 入场价格
            contracts: 要购买的合约/份额数量
            up_ask: 当前 UP 卖单价（用于详细日志）
            down_ask: 当前 DOWN 卖单价（用于详细日志）
            winner_ratio: 当前赢家比率（用于详细日志）
            is_recovery: 是否为恢复入场？（用于详细日志）
            entry_reason: 入场原因（用于详细日志）
            seconds_till_end: 距离市场结束的秒数（用于详细日志）
            time_from_start: 从市场开始的秒数（用于详细日志）
            
        返回：
            成功建仓时返回 True
        """
        # 如果合约为 0 则跳过（无仓位的对冲）
        if contracts == 0:
            return True  # 成功，只是没有建任何仓位
        
        # 注意：市场关闭检查现在由 main.py 处理（market_start_prices）
        # 这提供了单一真实来源，并在市场切换时自动清理
        
        # 以美元计算仓位大小
        size_usd = contracts * price
        shares = float(contracts)
        
        # 跟踪入场次数，用于比例计算
        if not hasattr(self, '_entry_count'):
            self._entry_count = 0
        self._entry_count += 1
        
        # 🔥 先尝试买入（如果是实盘模式）
        actual_contracts = shares
        actual_cost = size_usd
        
        if Trader.order_executor and market_slug in Trader.token_ids_cache:
            token_id = Trader.token_ids_cache[market_slug][side]
            ask_price = up_ask if side == 'UP' else down_ask
            
            if token_id and ask_price:
                log.info(f"[TRADER] ▶ {side:4s} @ ${price:.3f}  {shares:6.1f} contracts = ${size_usd:6.2f}  ({market_slug})")
                
                result = Trader.order_executor.place_buy_order(
                    market_slug=market_slug,
                    token_id=token_id,
                    side=side,
                    contracts=contracts,
                    ask_price=ask_price
                )
                
                if result.success:
                    # ✅ 成功！使用实际成交数量
                    actual_contracts = result.filled_size
                    actual_cost = result.total_spent_usd
                    
                    if actual_contracts != contracts:
                        log.warning(f"[TRADER] ⚠ FAK partial fill: {actual_contracts:.2f}/{contracts} contracts")
                    
                    log.info(f"[TRADER] ✓ Order filled: {actual_contracts:.2f} contracts for ${actual_cost:.2f}")
                    
                elif not result.dry_run:
                    # ❌ 失败！不要创建仓位！
                    log.error(f"[TRADER] ❌ Order FAILED for {side}: {result.error} - position NOT created")
                    return False
        else:
            # DRY_RUN 或无执行器 - 仅打印
            log.info(f"[TRADER] ▶ {side:4s} @ ${price:.3f}  {shares:6.1f} shares = ${size_usd:6.2f}  ({market_slug})")
        
        # 现在使用实际值（或 DRY_RUN 下的模拟值）创建仓位
        if market_slug not in self.positions:
            self.positions[market_slug] = {
                'UP': {
                    'entries': [],
                    'total_invested': 0.0,
                    'total_shares': 0.0
                },
                'DOWN': {
                    'entries': [],
                    'total_invested': 0.0,
                    'total_shares': 0.0
                },
                'all_entries': [],
                'start_time': time.time(),
                'status': 'OPEN'
            }
        
        # 使用实际值创建入场记录
        entry = {
            'side': side,
            'price': price,
            'size_usd': actual_cost,
            'shares': actual_contracts,
            'time': time.time(),
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'actual_fill': (Trader.order_executor is not None)  # 标记是否为真实订单
        }
        
        # 添加到仓位
        pos = self.positions[market_slug]
        pos['all_entries'].append(entry)
        pos[side]['entries'].append(entry)
        pos[side]['total_invested'] += actual_cost
        pos[side]['total_shares'] += actual_contracts
        
        # 更新市场统计
        self._update_market_stats(market_slug)
        
        # 回测的详细日志
        if up_ask is not None and down_ask is not None and market_slug in self.positions:
            try:
                self.log_entry_detailed(
                    market_slug=market_slug,
                    side=side,
                    contracts=actual_contracts,  # 记录实际数量
                    price=price,
                    up_ask=up_ask,
                    down_ask=down_ask,
                    winner_ratio=winner_ratio,
                    is_recovery=is_recovery,
                    entry_reason=entry_reason,
                    seconds_till_end=seconds_till_end,
                    time_from_start=time_from_start
                )
            except Exception as e:
                # 日志记录失败时不要影响交易
                log.warning(f"[WARNING] Detailed logging failed: {e}")
        
        return True
    
    def enter_position(self, market_slug: str, side: str, price: float, size_pct: float) -> bool:
        """
        建仓
        
        参数：
            market_slug: 市场标识
            side: 'UP' 或 'DOWN'
            price: 入场价格
            size_pct: 仓位大小（资金百分比）
            
        返回：
            成功建仓时返回 True
        """
        # 计算仓位大小
        size_usd = self.current_capital * (size_pct / 100.0)
        shares = size_usd / price if price > 0 else 0
        
        # 如果市场不存在则创建
        if market_slug not in self.positions:
            self.positions[market_slug] = {
                'UP': {
                    'entries': [],
                    'total_invested': 0.0,
                    'total_shares': 0.0
                },
                'DOWN': {
                    'entries': [],
                    'total_invested': 0.0,
                    'total_shares': 0.0
                },
                'all_entries': [],
                'start_time': time.time(),
                'status': 'OPEN'
            }
        
        # 创建入场记录
        entry = {
            'side': side,
            'price': price,
            'size_usd': size_usd,
            'shares': shares,
            'time': time.time(),
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # 添加到仓位
        pos = self.positions[market_slug]
        pos['all_entries'].append(entry)
        pos[side]['entries'].append(entry)
        pos[side]['total_invested'] += size_usd
        pos[side]['total_shares'] += shares
        
        # 更新市场统计
        self._update_market_stats(market_slug)
        
        # 计算本次入场后的当前比例
        up_shares = pos['UP']['total_shares']
        down_shares = pos['DOWN']['total_shares']
        total_shares = up_shares + down_shares
        
        if total_shares > 0 and self._entry_count % 5 == 1:
            up_ratio = (up_shares / total_shares) * 100
            down_ratio = (down_shares / total_shares) * 100
            log.info(f"[TRADER] After entry: UP {up_shares:.1f} ({up_ratio:.1f}%) | DOWN {down_shares:.1f} ({down_ratio:.1f}%)")
        
        log.info(f"[TRADER] ▶ {side:4s} @ ${price:.3f}  {shares:6.1f} shares = ${size_usd:6.2f}  ({market_slug})")
        
        return True
    
    def close_market(self, market_slug: str, winner: str, btc_start: float, btc_final: float) -> Optional[Dict]:
        """
        关闭市场的所有仓位
        
        参数：
            market_slug: 市场标识
            winner: 'UP' 或 'DOWN'
            btc_start: 起始 BTC 价格
            btc_final: 最终 BTC 价格
            
        返回：
            交易结果字典
        """
        if market_slug not in self.positions:
            return None
        
        pos = self.positions[market_slug]
        
        # 计算盈亏
        winner_side = pos[winner]
        loser_side = pos['UP' if winner == 'DOWN' else 'DOWN']
        
        # 赢家每份额支付 1 美元
        payout = winner_side['total_shares'] * 1.0
        
        # 总成本
        total_cost = pos['UP']['total_invested'] + pos['DOWN']['total_invested']
        
        # 盈亏
        pnl = payout - total_cost
        roi_pct = (pnl / total_cost * 100) if total_cost > 0 else 0
        
        # 赢家比例
        total_shares = pos['UP']['total_shares'] + pos['DOWN']['total_shares']
        winner_ratio = (winner_side['total_shares'] / total_shares * 100) if total_shares > 0 else 50
        
        # 更新资金
        self.current_capital += pnl
        
        # 创建交易记录
        trade = {
            'market_slug': market_slug,
            'winner': winner,
            'btc_start': btc_start,
            'btc_final': btc_final,
            'pnl': pnl,
            'roi_pct': roi_pct,
            'total_cost': total_cost,
            'payout': payout,
            'winner_ratio': winner_ratio,
            'total_entries': len(pos['all_entries']),
            'up_entries': len(pos['UP']['entries']),
            'down_entries': len(pos['DOWN']['entries']),
            'up_invested': pos['UP']['total_invested'],
            'down_invested': pos['DOWN']['total_invested'],
            'up_shares': pos['UP']['total_shares'],
            'down_shares': pos['DOWN']['total_shares'],
            'duration': time.time() - pos['start_time'],
            'close_time': time.time(),
            'close_timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
        }
        
        # ═══════════════════════════════════════════════════════════
        # 关键修复：先记录交易，再删除仓位！
        # 这可以防止 _log_trade() 失败时数据丢失
        # ═══════════════════════════════════════════════════════════
        
        try:
            # 1. 先将交易记录到磁盘（最重要！）
            self._log_trade(trade)
            
            # 2. 添加到内存（即使磁盘写入失败也是安全的）
            self.closed_trades.append(trade)
            
            # 3. 将市场标记为已关闭，防止重新入场
            self.closed_markets.add(market_slug)
            
            # 4. 现在可以安全地删除仓位
            del self.positions[market_slug]
            
            # 5. 清理市场统计
            if market_slug in self.market_max_drawdown:
                del self.market_max_drawdown[market_slug]
            if market_slug in self.market_entries_count:
                del self.market_entries_count[market_slug]
                
        except Exception as e:
            # 关键：如果日志记录失败，不要删除仓位！
            # 仓位将保持打开状态，可以再次关闭
            log.warning(f"[TRADER] ⚠️ FAILED TO CLOSE MARKET {market_slug}: {e}")
            log.warning(f"[TRADER] ⚠️ Position kept open for retry!")
            return None
        
        # 打印结果
        status = "✓" if pnl > 0 else "✗"
        log.info(f"[TRADER] {status} CLOSED {market_slug}: {pnl:+.2f} ({roi_pct:+.1f}%) | " f"{trade['total_entries']} entries, ${total_cost:.0f} invested, {winner_ratio:.1f}% {winner}")
        
        # ═══════════════════════════════════════════════════════════
        # 🔥 关键：重置该市场的投资跟踪！
        # 现在可以无限制地交易新市场！
        # ═══════════════════════════════════════════════════════════
        try:
            if order_executor and hasattr(order_executor, 'safety'):
                order_executor.safety.reset_market(market_slug)
        except Exception as reset_err:
            log.warning(f"[TRADER] ⚠ Failed to reset market tracking: {reset_err}")
        
        return trade
    
    def close_market_early_exit(self, market_slug: str, exit_price: float, exit_reason: str = 'early_exit',
                                up_bid: float = None, down_bid: float = None) -> Optional[Dict]:
        """
        提前退出：按当前热门价格平仓
        🛡️ 线程安全：可从不同线程调用
        
        参数：
            market_slug: 市场标识
            exit_price: 当前热门价格（例如 0.52）
            exit_reason: 退出原因（'stop_loss'、'flip_stop'、'early_exit'）
            up_bid: 当前 UP 出价（用于卖出 UP token）
            down_bid: 当前 DOWN 出价（用于卖出 DOWN token）
        
        返回：
            交易结果字典
        """
        with self.lock:
            # ✅ 保护措施 #1：检查仓位是否存在
            if market_slug not in self.positions:
                return None
            
            # ✅ 保护措施 #2：检查市场是否已关闭（另一个线程可能已关闭）
            if market_slug in self.closed_markets:
                return None  # 已关闭，静默跳过
            
            pos = self.positions[market_slug]
            
            # 获取合约数量
            up_contracts = pos['UP']['total_shares']
            down_contracts = pos['DOWN']['total_shares']
            
            # 判断热门方（哪边合约更多）
            if up_contracts > down_contracts:
                # UP 是热门方 - 按 exit_price 卖出 UP，按 (1 - exit_price) 卖出 DOWN
                payout = up_contracts * exit_price + down_contracts * (1 - exit_price)
                winner = 'UP'
            else:
                # DOWN 是热门方 - 按 exit_price 卖出 DOWN，按 (1 - exit_price) 卖出 UP
                payout = down_contracts * exit_price + up_contracts * (1 - exit_price)
                winner = 'DOWN'
            
            # 总成本
            total_cost = pos['UP']['total_invested'] + pos['DOWN']['total_invested']
            
            # 盈亏 = 支出 - 成本
            pnl = payout - total_cost
            roi_pct = (pnl / total_cost * 100) if total_cost > 0 else 0
            
            # 赢家比例
            total_shares = up_contracts + down_contracts
            winner_ratio = (up_contracts / total_shares * 100) if winner == 'UP' else (down_contracts / total_shares * 100)
            
            # 更新资金
            self.current_capital += pnl
            
            # ═══════════════════════════════════════════════════════════
            # 📊 在卖出前记录完整订单簿（用于分析）
            # ═══════════════════════════════════════════════════════════
            if exit_reason in ['stop_loss', 'flip_stop']:
                try:
                    # 从 data_feed 获取当前卖单价
                    up_ask = 0.5
                    down_ask = 0.5
                    if Trader.data_feed:
                        market_state = Trader.data_feed.get_state(self.coin)
                        up_ask = market_state.get('up_ask', 0.5)
                        down_ask = market_state.get('down_ask', 0.5)
                    
                    self._last_orderbook_snapshot = self._capture_orderbook_snapshot(
                        market_slug, exit_reason,
                        up_bid if up_bid else (1 - exit_price),
                        down_bid if down_bid else exit_price,
                        up_ask, down_ask
                    )
                    self._log_exit_orderbook(self._last_orderbook_snapshot)
                except Exception as e:
                    log.warning(f"[TRADER] ⚠ Failed to log orderbook: {e}")
                    self._last_orderbook_snapshot = None
            
            # 创建交易记录
            trade = {
                'market_slug': market_slug,
                'winner': winner,
                'exit_type': 'early_exit',
                'exit_reason': exit_reason,
                'exit_price': exit_price,
                'pnl': pnl,
                'roi_pct': roi_pct,
                'total_cost': total_cost,
                'payout': payout,
                'winner_ratio': winner_ratio,
                'total_entries': len(pos['all_entries']),
                'up_entries': len(pos['UP']['entries']),
                'down_entries': len(pos['DOWN']['entries']),
                'up_invested': pos['UP']['total_invested'],
                'down_invested': pos['DOWN']['total_invested'],
                'up_shares': up_contracts,
                'down_shares': down_contracts,
                'duration': time.time() - pos['start_time'],
                'close_time': time.time(),
                'close_timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            }
            
            # ═══════════════════════════════════════════════════════════
            # 关键修复：先记录交易，再删除仓位！
            # 这可以防止 _log_trade() 失败时数据丢失
            # ═══════════════════════════════════════════════════════════
            
            try:
                # 1. 先将交易记录到磁盘（最重要！）
                self._log_trade(trade)
                
                # 2. 添加到内存（即使磁盘写入失败也是安全的）
                self.closed_trades.append(trade)
                
                # 3. 将市场标记为已关闭，防止重新入场
                self.closed_markets.add(market_slug)
                
                # 4. 现在可以安全地删除仓位
                del self.positions[market_slug]
                
                # 5. 清理市场统计
                if market_slug in self.market_max_drawdown:
                    del self.market_max_drawdown[market_slug]
                if market_slug in self.market_entries_count:
                    del self.market_entries_count[market_slug]
                    
            except Exception as e:
                # 关键：如果日志记录失败，不要删除仓位！
                # 仓位将保持打开状态，可以再次关闭
                log.warning(f"[TRADER] ⚠️ FAILED TO CLOSE MARKET {market_slug}: {e}")
                log.warning(f"[TRADER] ⚠️ Position kept open for retry!")
                return None
            
            # 打印结果
            status = "🚨" if pnl < 0 else "✓"
            log.info(f"[TRADER] {status} EARLY EXIT {market_slug} @ ${exit_price:.2f}: {pnl:+.2f} ({roi_pct:+.1f}%) | " f"{trade['total_entries']} entries, ${total_cost:.0f} invested")
            
            # 🔥 实盘卖出（如果已连接执行器）
            # 📊 收集实际收益，用于精确的盈亏计算
            real_payout = 0.0
            real_sells_executed = False
            
            if Trader.order_executor and market_slug in Trader.token_ids_cache:
                token_ids = Trader.token_ids_cache[market_slug]
                
                # 使用跟踪的合约数量卖出两侧（UP 和 DOWN）
                for side in ['UP', 'DOWN']:
                    token_id = token_ids[side]
                    # 获取跟踪的合约数量
                    side_contracts = up_contracts if side == 'UP' else down_contracts
                    
                    # 如果没有合约则跳过
                    if side_contracts <= 0:
                        continue
                    
                    # 获取出价
                    bid = up_bid if side == 'UP' else down_bid
                    if bid is None:
                        # 回退方案
                        bid = exit_price if side == 'UP' else (1 - exit_price)
                    
                    result = Trader.order_executor.sell_position(
                        market_slug=market_slug,
                        token_id=token_id,
                        side=side,
                        contracts=side_contracts,  # 跟踪的数量！
                        bid_price=bid
                    )
                    
                    if result.success:
                        # 累加实际收益
                        real_payout += result.total_spent_usd
                        real_sells_executed = True
                    elif not result.dry_run:
                        log.warning(f"[TRADER] ⚠ Failed to sell {side}: {result.error}")
                
                # ═══════════════════════════════════════════════════════════
                # 📊 滑点分析：预期 vs 实际
                # 比较预期收益（按最佳出价）与实际收益
                # ═══════════════════════════════════════════════════════════
                if real_sells_executed and real_payout > 0:
                    # 获取卖出前捕获的订单簿快照
                    try:
                        if hasattr(self, '_last_orderbook_snapshot') and self._last_orderbook_snapshot:
                            snapshot = self._last_orderbook_snapshot
                            expected_payout = snapshot.get('expected_sale', {}).get('expected_payout_usd', payout)
                            expected_price = snapshot.get('expected_sale', {}).get('best_bid_price', exit_price)
                            
                            # 计算滑点
                            slippage_usd = real_payout - expected_payout
                            slippage_pct = (slippage_usd / expected_payout * 100) if expected_payout > 0 else 0
                            
                            actual_avg_price = real_payout / (up_contracts + down_contracts) if (up_contracts + down_contracts) > 0 else 0
                            price_diff = actual_avg_price - expected_price
                            price_diff_pct = (price_diff / expected_price * 100) if expected_price > 0 else 0
                            
                            log.info(f"\n{'='*80}")
                            log.info(f"[SLIPPAGE ANALYSIS] {self.coin.upper()} - {exit_reason}")
                            log.info(f"{'='*80}")
                            log.info(f"📊 EXPECTED (based on BID at trigger):")
                            log.info(f"   Best BID price: ${expected_price:.4f}")
                            log.info(f"   Expected payout: ${expected_payout:.2f}")
                            log.info(f"   Expected PnL: ${pnl:.2f}")
                            log.info(f"")
                            log.info(f"💰 ACTUAL (from API response):")
                            log.info(f"   Avg fill price: ${actual_avg_price:.4f}")
                            log.info(f"   Actual payout: ${real_payout:.2f}")
                            log.info(f"   Actual PnL: ${real_pnl:.2f}")
                            log.info(f"")
                            log.info(f"📉 SLIPPAGE:")
                            log.info(f"   Payout difference: ${slippage_usd:+.2f} ({slippage_pct:+.1f}%)")
                            log.info(f"   Price difference: ${price_diff:+.4f} ({price_diff_pct:+.1f}%)")
                            
                            if slippage_usd < -1.0:
                                log.warning(f"   ⚠️ NEGATIVE SLIPPAGE > $1 - investigating...")
                            elif abs(slippage_usd) < 0.5:
                                log.info(f"   ✅ Minimal slippage")
                            
                            log.info(f"{'='*80}\n")
                            
                            # 添加到快照用于日志记录
                            snapshot['actual_sale'] = {
                                'actual_payout': real_payout,
                                'actual_avg_price': actual_avg_price,
                                'actual_pnl': real_pnl,
                                'slippage_usd': slippage_usd,
                                'slippage_pct': slippage_pct,
                                'price_diff': price_diff,
                                'price_diff_pct': price_diff_pct
                            }
                            
                            # 用实际数据覆盖快照
                            self._log_exit_orderbook(snapshot)
                            
                    except Exception as e:
                        log.warning(f"[TRADER] ⚠ Slippage analysis error: {e}")
                
                # ═══════════════════════════════════════════════════════════
                # 📊 用实际数据更新交易记录
                # 基于区块链的实际收益重新计算盈亏
                # ═══════════════════════════════════════════════════════════
                if real_sells_executed and real_payout > 0:
                    # 用实际收益重新计算盈亏
                    real_pnl = real_payout - total_cost
                    real_roi_pct = (real_pnl / total_cost * 100) if total_cost > 0 else 0
                    
                    # 更新交易记录（返回值和内存中的记录）
                    trade['payout'] = real_payout
                    trade['pnl'] = real_pnl
                    trade['roi_pct'] = real_roi_pct
                    
                    # 重要：同时更新 closed_trades 中最后一个元素
                    # （在卖出前已添加）
                    if self.closed_trades and self.closed_trades[-1]['market_slug'] == market_slug:
                        self.closed_trades[-1]['payout'] = real_payout
                        self.closed_trades[-1]['pnl'] = real_pnl
                        self.closed_trades[-1]['roi_pct'] = real_roi_pct
                    
                    # 用实际数据记录更新后的交易
                    # （添加第二个条目，带 updated=True 标志，用于事后分析）
                    updated_trade = trade.copy()
                    updated_trade['updated'] = True
                    updated_trade['estimated_pnl'] = pnl
                    updated_trade['estimated_payout'] = payout
                    self._log_trade(updated_trade)
                    
                    # 用实际盈亏更新资金（而非估算值）
                    self.current_capital = self.current_capital - pnl + real_pnl
                    
                    log.info(f"[TRADER] 💰 Real payout: ${real_payout:.2f} (estimated: ${payout:.2f})")
                    if abs(real_pnl - pnl) > 0.5:
                        diff = real_pnl - pnl
                        log.warning(f"[TRADER] ⚠️  PnL correction: {diff:+.2f} (real: {real_pnl:+.2f} vs estimated: {pnl:+.2f})")
            
            # ═══════════════════════════════════════════════════════════
            # 🔥 关键：重置该市场的投资跟踪！
            # 现在可以无限制地交易新市场！
            # ═══════════════════════════════════════════════════════════
            try:
                if order_executor and hasattr(order_executor, 'safety'):
                    order_executor.safety.reset_market(market_slug)
            except Exception as reset_err:
                log.warning(f"[TRADER] ⚠ Failed to reset market tracking: {reset_err}")
            
            return trade
    
    def _capture_orderbook_snapshot(self, market_slug: str, exit_reason: str, 
                                    up_bid: float, down_bid: float, up_ask: float, down_ask: float) -> Dict:
        """
        捕获完整的订单簿快照，用于退出分析
        
        返回包含仓位 + 订单簿数据的字典
        """
        pos = self.positions.get(market_slug, {})
        
        # 确定我们要卖出哪一侧
        up_shares = pos.get('UP', {}).get('total_shares', 0)
        down_shares = pos.get('DOWN', {}).get('total_shares', 0)
        
        if up_shares > down_shares:
            our_side = 'UP'
            sell_contracts = up_shares
            sell_bid_price = up_bid
        elif down_shares > 0:
            our_side = 'DOWN'
            sell_contracts = down_shares
            sell_bid_price = down_bid
        else:
            our_side = None
            sell_contracts = 0
            sell_bid_price = 0
        
        total_invested = pos.get('UP', {}).get('total_invested', 0) + pos.get('DOWN', {}).get('total_invested', 0)
        
        # 从 data_feed 获取完整订单簿
        up_bids_full = []
        down_bids_full = []
        up_asks_full = []
        down_asks_full = []
        
        if Trader.data_feed:
            market_state = Trader.data_feed.get_state(self.coin)
            up_bids_full = market_state.get('up_bids_full', [])
            down_bids_full = market_state.get('down_bids_full', [])
            up_asks_full = market_state.get('up_asks_full', [])
            down_asks_full = market_state.get('down_asks_full', [])
        
        snapshot = {
            'timestamp': time.time(),
            'datetime': time.strftime('%Y-%m-%d %H:%M:%S'),
            'coin': self.coin,
            'market_slug': market_slug,
            'exit_reason': exit_reason,
            'position': {
                'up_shares': up_shares,
                'down_shares': down_shares,
                'up_invested': pos.get('UP', {}).get('total_invested', 0),
                'down_invested': pos.get('DOWN', {}).get('total_invested', 0),
                'total_invested': total_invested,
                'our_side': our_side
            },
            'orderbook': {
                'UP': {
                    'best_bid': up_bid,
                    'best_ask': up_ask,
                    'spread': up_ask - up_bid if (up_ask and up_bid) else 0,
                    'bids_top5': [{'price': p, 'size': s} for p, s in up_bids_full[:5]],
                    'asks_top1': [{'price': p, 'size': s} for p, s in up_asks_full[:1]]
                },
                'DOWN': {
                    'best_bid': down_bid,
                    'best_ask': down_ask,
                    'spread': down_ask - down_bid if (down_ask and down_bid) else 0,
                    'bids_top5': [{'price': p, 'size': s} for p, s in down_bids_full[:5]],
                    'asks_top1': [{'price': p, 'size': s} for p, s in down_asks_full[:1]]
                }
            },
            'expected_sale': {
                'side': our_side,
                'contracts': sell_contracts,
                'best_bid_price': sell_bid_price,
                'expected_payout_usd': sell_contracts * sell_bid_price if sell_bid_price else 0,
                'invested_usd': total_invested,
                'expected_loss_usd': (sell_contracts * sell_bid_price - total_invested) if sell_bid_price else -total_invested
            }
        }
        
        return snapshot
    
    def _log_exit_orderbook(self, snapshot: Dict):
        """将订单簿快照写入日志文件，用于分析"""
        import os
        
        log_dir = f"logs/{self.strategy_name}"
        os.makedirs(log_dir, exist_ok=True)
        
        log_file = f"{log_dir}/exit_orderbooks.jsonl"
        
        with open(log_file, 'a') as f:
            f.write(json.dumps(snapshot) + '\n')
        
        # 打印摘要到控制台
        log.info(f"\n{'='*80}")
        log.info(f"[EXIT ORDERBOOK] {snapshot['coin'].upper()} - {snapshot['exit_reason']}")
        log.info(f"Market: {snapshot['market_slug']}")
        log.info(f"Our side: {snapshot['position']['our_side']}")
        log.info(f"Invested: ${snapshot['position']['total_invested']:.2f}")
        log.info(f"Best bid (sell price): {snapshot['expected_sale']['best_bid_price']:.4f}")
        log.info(f"Expected payout: ${snapshot['expected_sale']['expected_payout_usd']:.2f}")
        log.info(f"Expected loss: ${snapshot['expected_sale']['expected_loss_usd']:.2f}")
        log.info(f"UP: BID={snapshot['orderbook']['UP']['best_bid']:.4f} ASK={snapshot['orderbook']['UP']['best_ask']:.4f} SPREAD={snapshot['orderbook']['UP']['spread']:.4f}")
        log.info(f"DOWN: BID={snapshot['orderbook']['DOWN']['best_bid']:.4f} ASK={snapshot['orderbook']['DOWN']['best_ask']:.4f} SPREAD={snapshot['orderbook']['DOWN']['spread']:.4f}")
        
        # 打印卖出侧的完整订单簿
        our_side = snapshot['position']['our_side']
        if our_side:
            log.info(f"\n{our_side} Orderbook (we're selling here):")
            ob = snapshot['orderbook'][our_side]
            log.info(f"  Asks (top 1):")
            for level in ob['asks_top1']:
                log.info(f"    ${level['price']:.4f} × {level['size']:.2f}")
            log.info(f"  Bids (top 5):")
            for level in ob['bids_top5']:
                log.info(f"    ${level['price']:.4f} × {level['size']:.2f}")
        
        log.info(f"{'='*80}\n")
    
    def get_market_stats(self, market_slug: str, up_current: float = 0.5, down_current: float = 0.5) -> Optional[Dict]:
        """
        获取特定市场的统计信息，包括未实现盈亏
        
        ✅ 使用 trader.positions 中的真实数据（通过 REST API takingAmount 更新）！
        """
        if market_slug not in self.positions:
            return None
        
        pos = self.positions[market_slug]
        
        total_entries = len(pos['all_entries'])
        
        # ✅ 使用 trader.positions 中的真实数据（通过 REST API 更新）
        total_invested = pos['UP']['total_invested'] + pos['DOWN']['total_invested']
        up_shares = pos['UP']['total_shares']
        down_shares = pos['DOWN']['total_shares']
        up_invested = pos['UP']['total_invested']
        down_invested = pos['DOWN']['total_invested']
        
        up_avg_price = (pos['UP']['total_invested'] / pos['UP']['total_shares']) if pos['UP']['total_shares'] > 0 else 0
        down_avg_price = (pos['DOWN']['total_invested'] / pos['DOWN']['total_shares']) if pos['DOWN']['total_shares'] > 0 else 0
        
        # 使用当前价格计算未实现盈亏
        up_value = pos['UP']['total_shares'] * up_current
        down_value = pos['DOWN']['total_shares'] * down_current
        total_value = up_value + down_value
        unrealized_pnl = total_value - total_invested
        
        up_entries = len(pos['UP']['entries'])
        down_entries = len(pos['DOWN']['entries'])
        
        total_shares = up_shares + down_shares
        up_ratio = (up_shares / total_shares * 100) if total_shares > 0 else 0
        down_ratio = (down_shares / total_shares * 100) if total_shares > 0 else 0
        
        return {
            'total_entries': total_entries,
            'total_invested': total_invested,
            'total_cost': total_invested,  # 兼容性别名
            'avg_per_entry': total_invested / total_entries if total_entries > 0 else 0,
            'up_entries': up_entries,
            'down_entries': down_entries,
            'up_invested': up_invested,  # ✅ 真实数据
            'down_invested': down_invested,  # ✅ 真实数据
            'up_shares': up_shares,  # ✅ 真实数据
            'down_shares': down_shares,  # ✅ 真实数据
            'up_avg_price': up_avg_price,
            'down_avg_price': down_avg_price,
            'up_ratio': up_ratio,
            'down_ratio': down_ratio,
            'unrealized_pnl': unrealized_pnl,  # ✅ 来自 WebSocket 的真实盈亏！
            'exposure_pct': (total_invested / self.current_capital * 100) if self.current_capital > 0 else 0.0
        }
    
    def get_performance_stats(self) -> Dict:
        """获取整体表现统计"""
        total_trades = len(self.closed_trades)
        wins = sum(1 for t in self.closed_trades if t['pnl'] > 0)
        losses = total_trades - wins
        
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        
        total_pnl = sum(t['pnl'] for t in self.closed_trades)
        avg_pnl = total_pnl / total_trades if total_trades > 0 else 0
        
        winning_trades = [t for t in self.closed_trades if t['pnl'] > 0]
        losing_trades = [t for t in self.closed_trades if t['pnl'] <= 0]
        
        best_win = max(winning_trades, key=lambda t: t['pnl']) if winning_trades else None
        worst_loss = min(losing_trades, key=lambda t: t['pnl']) if losing_trades else None
        
        total_wins = sum(t['pnl'] for t in winning_trades)
        total_losses = abs(sum(t['pnl'] for t in losing_trades))
        profit_factor = (total_wins / total_losses) if total_losses > 0 else 0
        
        avg_entries = sum(t.get('total_entries', 0) for t in self.closed_trades) / total_trades if total_trades > 0 else 0
        avg_invested = sum(t.get('total_cost', 0) for t in self.closed_trades) / total_trades if total_trades > 0 else 0
        
        return {
            'total_trades': total_trades,
            'wins': wins,
            'losses': losses,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'avg_pnl': avg_pnl,
            'best_win': best_win,
            'worst_loss': worst_loss,
            'profit_factor': profit_factor,
            'avg_entries': avg_entries,
            'avg_invested': avg_invested
        }
    
    def _update_market_stats(self, market_slug: str):
        """入场后更新市场统计"""
        # 更新入场次数
        if market_slug not in self.market_entries_count:
            self.market_entries_count[market_slug] = 0
        self.market_entries_count[market_slug] += 1
        
        # 按需初始化最大回撤
        if market_slug not in self.market_max_drawdown:
            self.market_max_drawdown[market_slug] = 0.0
    
    def update_market_drawdown(self, market_slug: str, unrealized_pnl: float):
        """如果当前值更差，更新市场最大回撤"""
        if market_slug not in self.market_max_drawdown:
            self.market_max_drawdown[market_slug] = 0.0
        
        if unrealized_pnl < self.market_max_drawdown[market_slug]:
            self.market_max_drawdown[market_slug] = unrealized_pnl
    
    def get_market_detailed_stats(self, market_slug: str, up_ask: float = 0.5, down_ask: float = 0.5) -> Optional[Dict]:
        """
        获取市场的详细统计信息
        
        参数：
            market_slug: 市场标识
            up_ask: 当前 UP 卖单价
            down_ask: 当前 DOWN 卖单价
            
        返回：
            包含详细统计信息的字典，或 None
        """
        if market_slug not in self.positions:
            return None
        
        pos = self.positions[market_slug]
        
        up_shares = pos['UP']['total_shares']
        down_shares = pos['DOWN']['total_shares']
        up_invested = pos['UP']['total_invested']
        down_invested = pos['DOWN']['total_invested']
        total_invested = up_invested + down_invested
        
        # 当前价值（未实现）
        current_value = (up_shares * up_ask) + (down_shares * down_ask)
        unrealized_pnl = current_value - total_invested
        unrealized_pct = (unrealized_pnl / total_invested * 100) if total_invested > 0 else 0
        
        # ═══════════════════════════════════════════════════════════
        # 🚨 在此处检查止损（计算盈亏的地方！）
        # ═══════════════════════════════════════════════════════════
        stop_loss_triggered = False
        stop_loss_threshold = None
        stop_loss_type = None
        
        # 从 market_slug 获取币种（例如 "btc-updown-15m-1768060800" -> "btc"）
        coin = market_slug.split('-')[0] if '-' in market_slug else ''
        
        # 检查是否有止损配置
        if self.config and coin and total_invested > 0:
            sl_config = self.config.get('exit', {}).get('stop_loss', {}).get('per_coin', {}).get(coin, {})
            sl_enabled = sl_config.get('enabled', False)
            sl_type = sl_config.get('type', 'none')
            sl_value = sl_config.get('value', None)
            
            if sl_enabled and sl_value is not None:
                if sl_type == 'fixed':
                    # 固定美元金额（例如 -$10）
                    stop_loss_threshold = sl_value
                    stop_loss_triggered = unrealized_pnl <= stop_loss_threshold
                    stop_loss_type = 'fixed'
                elif sl_type == 'percent':
                    # 投资资金的百分比（例如 -15%）
                    stop_loss_threshold = total_invested * (sl_value / 100.0)
                    stop_loss_triggered = unrealized_pnl <= stop_loss_threshold
                    stop_loss_type = 'percent'
        
        # ═══════════════════════════════════════════════════════════
        # 🚨 检查翻转止损（价格反转保护）
        # ═══════════════════════════════════════════════════════════
        flip_stop_triggered = False
        flip_stop_price = None
        
        if self.config and coin and (up_shares > 0 or down_shares > 0):
            flip_cfg = self.config.get('exit', {}).get('flip_stop', {})
            flip_stop_price = flip_cfg.get('price_threshold', 0.48)
            
            # 确定我们的方向
            our_side = 'UP' if up_shares > down_shares else 'DOWN'
            our_price = up_ask if our_side == 'UP' else down_ask
            
            # 检查我们的方向价格是否跌得太低
            if our_price <= flip_stop_price:
                flip_stop_triggered = True
                log.error(f"[FLIP-STOP] 🚨 {coin.upper()} {our_side} @ ${our_price:.4f} <= ${flip_stop_price:.4f} TRIGGERED!")
            else:
                # 如果价格接近翻转止损（25% 以内），记录警告
                if our_price < flip_stop_price * 1.25:
                    log.warning(f"[FLIP-STOP] ⚠️  {coin.upper()} {our_side} @ ${our_price:.4f} close to ${flip_stop_price:.4f}")
        
        # 用当前未实现盈亏更新回撤
        self.update_market_drawdown(market_slug, unrealized_pnl)
        
        # 最大回撤
        max_dd = self.market_max_drawdown.get(market_slug, 0.0)
        max_dd_pct = (max_dd / total_invested * 100) if total_invested > 0 else 0
        
        # 平均入场价格
        avg_up_price = up_invested / up_shares if up_shares > 0 else 0
        avg_down_price = down_invested / down_shares if down_shares > 0 else 0
        
        # 入场次数
        entries_count = self.market_entries_count.get(market_slug, len(pos['all_entries']))
        
        return {
            'up_shares': up_shares,
            'down_shares': down_shares,
            'up_invested': up_invested,
            'down_invested': down_invested,
            'total_invested': total_invested,
            'unrealized_pnl': unrealized_pnl,
            'unrealized_pct': unrealized_pct,
            'max_drawdown': max_dd,
            'max_drawdown_pct': max_dd_pct,
            'avg_up_price': avg_up_price,
            'avg_down_price': avg_down_price,
            'entries_count': entries_count,
            'stop_loss_triggered': stop_loss_triggered,
            'stop_loss_threshold': stop_loss_threshold,
            'stop_loss_type': stop_loss_type,
            'flip_stop_triggered': flip_stop_triggered,
            'flip_stop_price': flip_stop_price
        }
    
    def _log_trade(self, trade: Dict):
        """
        将交易记录到文件，具有最大容错性
        
        关键：此函数必须成功或抛出异常！
        如果静默失败，我们将丢失交易数据！
        """
        try:
            # 确保目录存在
            self.trades_file.parent.mkdir(parents=True, exist_ok=True)
            
            # 写入文件并显式刷新
            with open(self.trades_file, 'a') as f:
                f.write(json.dumps(trade) + '\n')
                f.flush()  # 立即强制写入磁盘
                
        except PermissionError as e:
            log.warning(f"[TRADER] ⚠️ PERMISSION ERROR logging trade: {e}")
            log.warning(f"[TRADER] ⚠️ Trade data: {trade}")
            log.warning(f"[TRADER] ⚠️ File: {self.trades_file}")
            raise  # 重新抛出，防止删除仓位
            
        except OSError as e:
            log.warning(f"[TRADER] ⚠️ DISK ERROR logging trade: {e}")
            log.warning(f"[TRADER] ⚠️ Trade data: {trade}")
            log.warning(f"[TRADER] ⚠️ Check disk space: df -h")
            raise  # 重新抛出，防止删除仓位
            
        except Exception as e:
            log.warning(f"[TRADER] ⚠️ UNKNOWN ERROR logging trade: {e}")
            log.warning(f"[TRADER] ⚠️ Trade data: {trade}")
            import traceback
            traceback.print_exc()
            raise  # 重新抛出，防止删除仓位

        # 同时写入 SQLite 数据库（非阻塞，容错）
        try:
            import db_manager
            total_cost = trade.get('total_cost', 0)
            up_shares = trade.get('up_shares', 0)
            down_shares = trade.get('down_shares', 0)
            total_shares = up_shares + down_shares
            entry_price = (total_cost / total_shares) if total_cost > 0 and total_shares > 0 else None
            pnl_val = trade.get('pnl', 0) or 0
            db_manager.get_db().save_trade({
                'market_slug': trade.get('market_slug', ''),
                'coin': getattr(self, 'coin', None),
                'side': trade.get('winner'),
                'entry_price': entry_price,
                'contracts': total_shares,
                'size_usd': total_cost,
                'pnl': pnl_val,
                'roi_pct': trade.get('roi_pct', 0),
                'winner': trade.get('winner'),
                'exit_type': trade.get('exit_reason', 'market_resolution'),
                'exit_price': trade.get('exit_price'),
                'total_entries': trade.get('total_entries', 0),
                'up_invested': trade.get('up_invested', 0),
                'down_invested': trade.get('down_invested', 0),
                'up_shares': trade.get('up_shares', 0),
                'down_shares': trade.get('down_shares', 0),
                'duration_sec': trade.get('duration', 0),
                'close_time': trade.get('close_timestamp'),
                'status': 'closed',
            })
            # 同时记录模拟余额变动（实盘/模拟均记录）
            try:
                old_capital = self.current_capital - pnl_val
                db_manager.get_db().save_balance_change(
                    amount=pnl_val,
                    balance_before=old_capital,
                    balance_after=self.current_capital,
                    operation_type='trade',
                    market_slug=trade.get('market_slug', ''),
                    coin=getattr(self, 'coin', None),
                    note=f"Trade close: {trade.get('winner', '?')} PnL={pnl_val:+.2f}"
                )
            except Exception:
                pass
        except Exception:
            pass  # 数据库日志记录失败不应影响交易流程
    
    def save_session(self):
        """保存当前会话状态"""
        try:
            session = {
                'starting_capital': self.starting_capital,
                'current_capital': self.current_capital,
                'total_pnl': self.current_capital - self.starting_capital,
                'roi_pct': ((self.current_capital / self.starting_capital) - 1) * 100,
                'open_positions': len(self.positions),
                'closed_trades': len(self.closed_trades),
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S')
            }
            
            with open(self.session_file, 'w') as f:
                json.dump(session, f, indent=2)
                
        except Exception as e:
            log.info(f"[TRADER] Error saving session: {e}")
    
    def log_entry_detailed(self, market_slug: str, side: str, contracts: int, 
                           price: float, up_ask: float, down_ask: float,
                           winner_ratio: float, is_recovery: bool, 
                           entry_reason: str, seconds_till_end: int,
                           time_from_start: int):
        """
        记录详细入场信息，用于回测分析
        
        参数：
            market_slug: 完整市场标识
            side: 'UP' 或 'DOWN'
            contracts: 合约数量
            price: 入场价格
            up_ask: 当前 UP 卖单价
            down_ask: 当前 DOWN 卖单价
            winner_ratio: 当前赢家比例（0.0-1.0）
            is_recovery: 是否在 WR < 40% 后的恢复入场？
            entry_reason: 'normal' 或 'recovery'
            seconds_till_end: 距离市场结束的秒数
            time_from_start: 从市场开始的秒数
        """
        import os
        
        # 创建详细日志目录
        detailed_dir = str(self.log_dir).replace('/logs/', '/logs_detailed/')
        Path(detailed_dir).mkdir(parents=True, exist_ok=True)
        
        # 获取仓位数据
        if market_slug not in self.positions:
            return
        
        pos = self.positions[market_slug]
        
        # 计算当前指标
        up_contracts = pos['UP']['total_shares']
        down_contracts = pos['DOWN']['total_shares']
        up_invested = pos['UP']['total_invested']
        down_invested = pos['DOWN']['total_invested']
        total_invested = up_invested + down_invested
        total_contracts = up_contracts + down_contracts
        entries_count = len(pos['all_entries'])
        
        # 基于当前市场价格计算正确的未实现盈亏
        current_value = (up_contracts * up_ask) + (down_contracts * down_ask)
        unrealized_pnl = current_value - total_invested
        unrealized_pnl_pct = (unrealized_pnl / total_invested * 100) if total_invested > 0 else 0
        
        # 在读取最大回撤之前，用当前未实现盈亏更新它
        self.update_market_drawdown(market_slug, unrealized_pnl)
        
        # 计算市场结算时的盈亏情景
        if_up_wins = (up_contracts * 1.0) - total_invested
        if_down_wins = (down_contracts * 1.0) - total_invested
        
        # 平均价格
        avg_up_price = (up_invested / up_contracts) if up_contracts > 0 else 0
        avg_down_price = (down_invested / down_contracts) if down_contracts > 0 else 0
        
        # 获取该市场的最大回撤（在上面的更新之后）
        max_dd = self.market_max_drawdown.get(market_slug, 0.0)
        max_dd_pct = (max_dd / total_invested * 100) if total_invested > 0 else 0
        
        # 构建入场数据
        entry_data = {
            "timestamp": int(time.time()),
            "market_slug": market_slug,
            "seconds_till_end": seconds_till_end,
            "time_from_start": time_from_start,
            
            "market_prices": {
                "up_ask": round(up_ask, 3),
                "down_ask": round(down_ask, 3),
                "confidence": round(abs(down_ask - up_ask), 3)
            },
            
            "entry": {
                "side": side,
                "contracts": contracts,
                "price": round(price, 3),
                "cost": round(contracts * price, 2)
            },
            
            "position_after": {
                "up_contracts": int(up_contracts),
                "down_contracts": int(down_contracts),
                "up_invested": round(up_invested, 2),
                "down_invested": round(down_invested, 2),
                "total_invested": round(total_invested, 2),
                "total_contracts": int(total_contracts),
                "entries_count": entries_count
            },
            
            "pnl_metrics": {
                "unrealized_pnl": round(unrealized_pnl, 2),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                "max_drawdown": round(max_dd, 2),
                "max_drawdown_pct": round(max_dd_pct, 2),
                "if_up_wins": round(if_up_wins, 2),
                "if_down_wins": round(if_down_wins, 2),
                "avg_up_price": round(avg_up_price, 3),
                "avg_down_price": round(avg_down_price, 3)
            },
            
            "strategy_state": {
                "winner_ratio": round(winner_ratio, 3),
                "is_recovery": is_recovery,
                "entry_reason": entry_reason
            }
        }
        
        # 基于市场标识的文件名
        filename = f"{market_slug}_entries.jsonl"
        filepath = os.path.join(detailed_dir, filename)
        
        # 追加入场记录
        with open(filepath, 'a') as f:
            f.write(json.dumps(entry_data) + '\n')


# ── 向后兼容的模块级函数（委托给 Trader 类方法）──

def set_order_executor(executor):
    return Trader.set_order_executor(executor)

def set_data_feed(data_feed):
    return Trader.set_data_feed(data_feed)

def save_market_metadata_to_disk():
    return Trader.save_market_metadata_to_disk()

def load_market_metadata_from_disk():
    return Trader.load_market_metadata_from_disk()

def set_token_ids(market_slug, up_token_id, down_token_id, condition_id="", neg_risk=True):
    return Trader.set_token_ids(market_slug, up_token_id, down_token_id, condition_id, neg_risk)

def get_token_ids(market_slug):
    return Trader.get_token_ids(market_slug)

def get_market_metadata(market_slug):
    return Trader.get_market_metadata(market_slug)


