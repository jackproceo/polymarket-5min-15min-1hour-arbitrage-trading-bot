"""
简易赎回收集器
定期通过 Polymarket API 收集所有未赎回的仓位
替代复杂系统 pending_markets 的简易方案
"""
import time
import threading
import requests
from typing import Dict, List, Optional


class SimpleRedeemCollector:
    """
    按定时器收集未赎回仓位的简易收集器
    
    使用 Polymarket API 自动检测所有
    可赎回仓位，并触发每个仓位的赎回。
    
    ✅ 不会阻塞主流程（交易）
    ✅ 在独立守护线程中运行
    ✅ 找到所有仓位（即使重启后）
    """
    
    def __init__(self, wallet_address: str, config: dict, order_executor, trader_module,
                 multi_trader=None, notifier=None):
        """
        参数：
            wallet_address: 钱包地址（0x...）
            config: 含参数的配置
            order_executor: 用于赎回的 OrderExecutor 实例
            trader_module: 用于获取代币 ID 的 Trader 模块
            multi_trader: 用于创建交易记录的 MultiTrader 实例（可选）
            notifier: 用于通知的 TelegramNotifier（可选）
        """
        self.wallet = wallet_address
        self.config = config
        self.executor = order_executor
        self.trader = trader_module
        self.multi_trader = multi_trader
        self.notifier = notifier
        
        # 从配置加载参数
        redeem_cfg = config.get('execution', {}).get('redeem', {})
        self.check_interval = redeem_cfg.get('check_interval_sec', 300)  # 5 分钟
        self.startup_delay = redeem_cfg.get('startup_check_delay_sec', 60)  # 1 分钟
        self.first_delay = redeem_cfg.get('first_check_delay_sec', 480)  # 8 分钟
        self.pause_between = redeem_cfg.get('pause_between_redeems_sec', 2)
        self.size_threshold = redeem_cfg.get('sizeThreshold', 0.1)
        
        # 速率限制保护
        self.api_max_retries = redeem_cfg.get('api_max_retries', 3)
        self.api_retry_delay = redeem_cfg.get('api_retry_delay_sec', 60)
        self.api_timeout = redeem_cfg.get('api_timeout_sec', 30)
        
        # 状态
        self.is_running = False
        self.last_check = 0
        self.stats = {
            'total_checks': 0,
            'total_redeemed': 0,
            'startup_check_done': False
        }
        
        print(f"[REDEEM COLLECTOR] 已初始化：")
        print(f"  钱包：{wallet_address[:10]}...{wallet_address[-8:]}")
        print(f"  启动检查：{self.startup_delay}s")
        print(f"  定期检查：每 {self.check_interval//60} 分钟")
    
    def start(self):
        """在后台线程中启动（守护线程——不阻塞关闭）"""
        if self.is_running:
            print("[REDEEM COLLECTOR] 已在运行！")
            return
        
        self.is_running = True
        self.thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="SimpleRedeemCollector"
        )
        self.thread.start()
        print(f"[REDEEM COLLECTOR] ✅ 已启动（守护线程）")
    
    def stop(self):
        """停止后台线程"""
        self.is_running = False
        if hasattr(self, 'thread') and self.thread:
            self.thread.join(timeout=5)
        print(f"[REDEEM COLLECTOR] 已停止")
    
    def _loop(self):
        """后台循环——在独立线程中运行"""
        print(f"\n[REDEEM COLLECTOR] 后台循环已启动")
        
        # 🔥 启动检查：启动后立即执行（startup_delay 后）
        # 目标：收集脚本启动前累积的所有仓位
        print(f"[REDEEM COLLECTOR] ⏰ {self.startup_delay}s 后执行启动检查...")
        print(f"[REDEEM COLLECTOR]    将收集重启前所有未赎回的仓位")
        time.sleep(self.startup_delay)
        
        print(f"\n[REDEEM COLLECTOR] 🚀 启动检查")
        try:
            self._check_and_redeem_all(check_type="STARTUP")
            self.stats['startup_check_done'] = True
        except Exception as e:
            print(f"[REDEEM COLLECTOR] ⚠️ 启动检查错误：{e}")
            import traceback
            traceback.print_exc()
        
        # 🔥 首次定期检查：启动后 first_delay 执行
        # （针对刚关闭的新市场）
        remaining_delay = max(0, self.first_delay - self.startup_delay)
        if remaining_delay > 0:
            print(f"\n[REDEEM COLLECTOR] ⏰ {remaining_delay//60} 分钟后执行首次定期检查...")
            time.sleep(remaining_delay)
        
        # 🔥 定期检查：每 check_interval 执行一次
        while self.is_running:
            try:
                self._check_and_redeem_all(check_type="PERIODIC")
            except Exception as e:
                print(f"[REDEEM COLLECTOR] ⚠️ 定期检查错误：{e}")
                import traceback
                traceback.print_exc()
            
            # 等待下次检查
            if self.is_running:
                print(f"[REDEEM COLLECTOR] ⏰ {self.check_interval//60} 分钟后下次检查...")
                time.sleep(self.check_interval)
    
    def _check_and_redeem_all(self, check_type: str = "PERIODIC"):
        """
        检查 API 并赎回所有
        
        参数：
            check_type: "STARTUP"（启动时）或 "PERIODIC"（定期）
        """
        print(f"\n{'='*80}")
        if check_type == "STARTUP":
            print(f"[REDEEM COLLECTOR] 🚀 启动检查")
            print(f"[REDEEM COLLECTOR] 收集重启前未赎回的仓位...")
        else:
            print(f"[REDEEM COLLECTOR] 🔍 定期检查 #{self.stats['total_checks'] + 1}")
        print(f"{'='*80}")
        
        self.stats['total_checks'] += 1
        self.last_check = time.time()
        
        # 第 1 步：查询 API
        positions = self._fetch_redeemable_positions()
        
        if positions is None:
            print(f"[REDEEM COLLECTOR] ⚠️ API 请求失败，跳过本轮")
            return
        
        print(f"[REDEEM COLLECTOR] 找到 {len(positions)} 个可赎回仓位")
        
        if not positions:
            print(f"[REDEEM COLLECTOR] ✓ 无需赎回")
            if check_type == "STARTUP":
                print(f"[REDEEM COLLECTOR] ✓ 重启前所有仓位已全部领取")
            return
        
        # 显示汇总
        total_size = sum(p.get('size', 0) for p in positions)
        total_value = sum(p.get('currentValue', 0) for p in positions)
        print(f"[REDEEM COLLECTOR] 汇总：")
        print(f"  总合约数：{total_size:.2f}")
        print(f"  预估价值：${total_value:.2f}")
        
        if check_type == "STARTUP":
            print(f"[REDEEM COLLECTOR] 💰 这些仓位在脚本重启前已累积")
        
        # 第 2 步：逐个赎回（顺序执行）
        print(f"\n[REDEEM COLLECTOR] 开始赎回流程...")
        success_count = 0
        failed_count = 0
        
        for i, pos in enumerate(positions, 1):
            result = self._redeem_one(i, len(positions), pos)
            if result:
                success_count += 1
            else:
                failed_count += 1
            
            # 赎回间暂停（来自配置）
            if i < len(positions):
                time.sleep(self.pause_between)
        
        print(f"\n[REDEEM COLLECTOR] ✅ 检查完成")
        print(f"  成功：{success_count}/{len(positions)}")
        print(f"  失败：{failed_count}/{len(positions)}")
        print(f"  本轮已赎回（会话）：{self.stats['total_redeemed']}")
        print(f"{'='*80}\n")
    
    def _fetch_redeemable_positions(self) -> Optional[List[Dict]]:
        """
        查询 Polymarket API 以获取可赎回仓位
        含速率限制处理和重试逻辑
        """
        url = "https://data-api.polymarket.com/positions"
        params = {
            'user': self.wallet,
            'redeemable': 'true',
            'sizeThreshold': self.size_threshold,
            'limit': 500
        }
        
        print(f"[REDEEM COLLECTOR] 正在请求 Polymarket API...")
        print(f"  URL: {url}")
        print(f"  过滤：redeemable=true, sizeThreshold={self.size_threshold}")
        
        for attempt in range(1, self.api_max_retries + 1):
            try:
                response = requests.get(url, params=params, timeout=self.api_timeout)
                
                # ✅ 成功
                if response.status_code == 200:
                    positions = response.json()
                    print(f"[REDEEM COLLECTOR] ✓ API 响应：{len(positions)} 个仓位")
                    return positions
                
                # ⚠️ 速率限制
                elif response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', self.api_retry_delay))
                    print(f"[REDEEM COLLECTOR] ⚠️ 达到速率限制（429）")
                    print(f"[REDEEM COLLECTOR]    等待重试：{retry_after}s")
                    
                    if attempt < self.api_max_retries:
                        print(f"[REDEEM COLLECTOR]    等待 {retry_after}s 后重试...")
                        time.sleep(retry_after)
                        continue
                    else:
                        print(f"[REDEEM COLLECTOR] ❌ 尝试 {self.api_max_retries} 次后速率限制仍存在")
                        return None
                
                # ❌ 其他错误
                else:
                    print(f"[REDEEM COLLECTOR] ❌ API 错误：{response.status_code}")
                    print(f"  响应：{response.text[:200]}")
                    
                    if attempt < self.api_max_retries:
                        wait_time = 5 * attempt  # 指数退避
                        print(f"[REDEEM COLLECTOR]    重试 {attempt}/{self.api_max_retries}，等待 {wait_time}s...")
                        time.sleep(wait_time)
                        continue
                    
                    return None
            
            except requests.exceptions.Timeout:
                print(f"[REDEEM COLLECTOR] ⚠️ 请求超时（尝试 {attempt}）")
                if attempt < self.api_max_retries:
                    time.sleep(5)
                    continue
            
            except Exception as e:
                print(f"[REDEEM COLLECTOR] ❌ 请求异常（尝试 {attempt}）：{e}")
                if attempt < self.api_max_retries:
                    time.sleep(5)
                    continue
        
        return None
    
    def _redeem_one(self, index: int, total: int, position: Dict) -> bool:
        """
        赎回一个仓位
        
        返回：
            True 表示成功，False 表示失败
        """
        slug = position.get('slug')
        condition_id = position.get('conditionId')
        size = position.get('size', 0)
        neg_risk = position.get('negativeRisk', True)
        current_value = position.get('currentValue', 0)
        outcome = position.get('outcome', '')
        
        print(f"\n[REDEEM COLLECTOR] [{index}/{total}] 正在处理：{slug}")
        print(f"  条件 ID：{condition_id[:20]}...")
        print(f"  数量：{size:.2f} 合约")
        print(f"  价值：${current_value:.2f}")
        print(f"  结果：{outcome}")
        
        try:
            # 从缓存获取代币 ID
            token_ids = self.trader.get_token_ids(slug)
            
            if not token_ids:
                print(f"[REDEEM COLLECTOR]   ️缓存中无代币 ID，正在获取元数据...")
                # 尝试获取元数据
                metadata = self.trader.get_market_metadata(slug)
                token_ids = self.trader.get_token_ids(slug)
            
            if not token_ids or not token_ids.get('UP') or not token_ids.get('DOWN'):
                print(f"[REDEEM COLLECTOR] ⚠️ 无 {slug} 的代币 ID，跳过")
                print(f"[REDEEM COLLECTOR]    无法在没有代币 ID 的情况下赎回此仓位")
                return False
            
            print(f"[REDEEM COLLECTOR]   UP 代币：{token_ids['UP'][:10]}...")
            print(f"[REDEEM COLLECTOR]   DOWN 代币：{token_ids['DOWN'][:10]}...")
            print(f"[REDEEM COLLECTOR]   正在调用 redeem_position()...")
            
            # 通过 order_executor 调用赎回
            success, amount = self.executor.redeem_position(
                market_slug=slug,
                condition_id=condition_id,
                up_token_id=token_ids['UP'],
                down_token_id=token_ids['DOWN'],
                neg_risk=neg_risk
            )
            
            if success:
                print(f"[REDEEM COLLECTOR] ✅ 已赎回 ${amount:.2f} USDC！")
                self.stats['total_redeemed'] += 1
                
                # 🔥 修复：为仪表盘创建交易记录（针对所有 4 个币种）
                if self.multi_trader:
                    try:
                        from polymarket_api import get_market_outcome
                        
                        # 从 Polymarket API 获取真实市场结果
                        print(f"[REDEEM COLLECTOR]   正在从 API 获取市场结果...")
                        api_result = get_market_outcome(slug)
                        
                        if api_result.get("success") and api_result.get("winner"):
                            winner = api_result["winner"]
                            print(f"[REDEEM COLLECTOR]   赢家：{winner}")
                            
                            # 从 market_slug 确定币种
                            coin = None
                            for c in ['btc', 'eth', 'sol', 'xrp']:
                                if f'{c}-updown-' in slug:
                                    coin = c
                                    break
                            
                            if coin:
                                strategy_name = f"late_v3_{coin}"
                                print(f"[REDEEM COLLECTOR]   正在为 {strategy_name} 创建交易记录...")
                                
                                # 通过 multi_trader 创建交易记录
                                result = self.multi_trader.close_market(
                                    strategy_name=strategy_name,
                                    market_slug=slug,
                                    winner=winner,
                                    btc_start=0.0,  # 赎回时未知
                                    btc_final=0.0
                                )
                                
                                if result:
                                    print(f"[REDEEM COLLECTOR]   ✅ 交易记录已创建！")
                                    print(f"[REDEEM COLLECTOR]      盈亏：${result['pnl']:+.2f}")
                                    print(f"[REDEEM COLLECTOR]      收益率：{result['roi_pct']:+.1f}%")
                                    
                                    # 发送 Telegram 通知
                                    if self.notifier:
                                        try:
                                            session_stats = self.multi_trader.get_session_stats(strategy_name, 0)
                                            
                                            # 为 Telegram 创建正确格式的 portfolio_stats
                                            portfolio_stats = {}
                                            for c in ['btc', 'eth', 'sol', 'xrp']:
                                                trader_name = f"late_v3_{c}"
                                                trader = self.multi_trader.traders.get(trader_name)
                                                if trader:
                                                    perf = trader.get_performance_stats()
                                                    portfolio_stats[f'{c}_pnl'] = trader.current_capital - trader.starting_capital
                                                    portfolio_stats[f'{c}_wr'] = perf['win_rate']
                                                    portfolio_stats[f'{c}_markets_played'] = perf['total_trades']
                                                else:
                                                    portfolio_stats[f'{c}_pnl'] = 0
                                                    portfolio_stats[f'{c}_wr'] = 0
                                                    portfolio_stats[f'{c}_markets_played'] = 0
                                            
                                            portfolio_stats['total_pnl'] = sum(portfolio_stats.get(f'{c}_pnl', 0) for c in ['btc', 'eth', 'sol', 'xrp'])
                                            portfolio_stats['uptime'] = 0  # 赎回时运行时间无关紧要
                                            
                                            self.notifier.send_market_closed(
                                                coin=coin,
                                                trade=result,
                                                session_stats=session_stats,
                                                portfolio_stats=portfolio_stats
                                            )
                                            print(f"[REDEEM COLLECTOR]      ✅ Telegram 通知已发送")
                                        except Exception as notify_err:
                                            print(f"[REDEEM COLLECTOR]      ⚠️ 通知失败：{notify_err}")
                                            import traceback
                                            traceback.print_exc()
                                else:
                                    print(f"[REDEEM COLLECTOR]   ⚠️ 交易记录创建返回 None")
                                    print(f"[REDEEM COLLECTOR]      （仓位可能为空）")
                            else:
                                print(f"[REDEEM COLLECTOR]   ⚠️ 无法从 slug 确定币种：{slug}")
                        else:
                            print(f"[REDEEM COLLECTOR]   ⚠️ 市场结果不可用")
                            print(f"[REDEEM COLLECTOR]      API 结果：{api_result}")
                    
                    except Exception as trade_err:
                        print(f"[REDEEM COLLECTOR]   ⚠️ 创建交易记录失败：{trade_err}")
                        import traceback
                        traceback.print_exc()
                
                # 重置安全卫士中的市场跟踪
                try:
                    if hasattr(self.trader, 'order_executor') and self.trader.order_executor:
                        self.trader.order_executor.safety.reset_market(slug)
                        print(f"[REDEEM COLLECTOR]   市场跟踪已重置")
                except Exception as reset_err:
                    print(f"[REDEEM COLLECTOR]   ⚠️ 重置跟踪失败：{reset_err}")
                
                return True
            else:
                print(f"[REDEEM COLLECTOR] ⚠️ 赎回失败")
                print(f"[REDEEM COLLECTOR]    原因：预言机未解析或无代币")
                return False
        
        except Exception as e:
            print(f"[REDEEM COLLECTOR] ❌ 处理 {slug} 时出错：{e}")
            import traceback
            traceback.print_exc()
            return False
    
    def get_stats(self) -> Dict:
        """获取收集器统计信息"""
        return {
            'total_checks': self.stats['total_checks'],
            'total_redeemed': self.stats['total_redeemed'],
            'startup_check_done': self.stats['startup_check_done'],
            'last_check_time': self.last_check,
            'is_running': self.is_running,
            'check_interval_min': self.check_interval // 60
        }
