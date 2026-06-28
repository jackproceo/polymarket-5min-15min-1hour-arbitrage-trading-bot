"""
多市场数据源：4 个币种的 Polymarket 订单簿
"""
import json
import time
import threading
import websocket
import subprocess
import requests
import os
import hmac
import hashlib
import base64
from typing import Optional, Dict
import trader as trader_module
from position_tracker import PositionTracker


class DataFeed:
    """BTC、ETH、SOL、XRP 的 Polymarket 订单簿（可配置 5m 或 15m 窗口）。"""
    
    def __init__(self, config: Dict):
        self.config = config
        
        # ✅ 仓位跟踪器 - 仓位数据的唯一真实来源！
        self.position_tracker = PositionTracker()
        
        # 经过身份验证的 WebSocket 所需的 API 凭据
        self.api_key = os.getenv('POLYMARKET_API_KEY')
        self.api_secret = os.getenv('POLYMARKET_API_SECRET')
        self.api_passphrase = os.getenv('POLYMARKET_API_PASSPHRASE')
        
        pm = config.get("data_sources", {}).get("polymarket", {})
        self.market_interval_sec = int(pm.get("market_interval_sec", 900))
        if self.market_interval_sec <= 0:
            self.market_interval_sec = 900
        # Slug 格式：{coin}-updown-5m-{slot} 或 {coin}-updown-15m-{slot}
        if self.market_interval_sec == 300:
            self.market_slug_suffix = "5m"
        elif self.market_interval_sec == 900:
            self.market_slug_suffix = "15m"
        else:
            self.market_slug_suffix = (
                f"{self.market_interval_sec // 60}m"
                if self.market_interval_sec % 60 == 0
                else "15m"
            )
            print(
                f"[DATA] Warning: market_interval_sec={self.market_interval_sec} "
                f"(standard Polymarket crypto up/down uses 300 or 900). Slug suffix={self.market_slug_suffix}"
            )
        
        iv = self.market_interval_sec
        tnow = int(time.time())
        self.markets = {}
        for coin in ["btc", "eth", "sol", "xrp"]:
            self.markets[coin] = {
                "slug": "",
                "up_ask": 0.5,
                "down_ask": 0.5,
                "up_bid": 0.5,
                "down_bid": 0.5,
                "up_ask_timestamp": 0.0,
                "down_ask_timestamp": 0.0,
                "up_bid_timestamp": 0.0,
                "down_bid_timestamp": 0.0,
                "up_bids_full": [],
                "down_bids_full": [],
                "up_asks_full": [],
                "down_asks_full": [],
                "tokens": {},
                "seconds_till_end": iv,
                "market_end_time": tnow + iv,
                "market_start_price": 0.0,
            }
        
        # 当前价格（仅 BTC 和 ETH 有价格数据源）
        self.btc_price = 0.0
        self.eth_price = 0.0
        
        # 线程安全 - 每币种独立锁，实现完全并行
        self.locks = {
            'btc': threading.Lock(),
            'eth': threading.Lock(),
            'sol': threading.Lock(),
            'xrp': threading.Lock()
        }
        self.stop_event = threading.Event()
        
        # 线程
        self.threads = []
        
        # 事件驱动的价格更新回调
        self.price_callbacks = []
    
    def start(self):
        """启动 BTC、ETH、SOL、XRP 的数据流 + 用户频道"""
        # 所有 4 个币种的 Polymarket WebSocket
        for coin in ['btc', 'eth', 'sol', 'xrp']:
            pm_thread = threading.Thread(target=self._polymarket_worker, args=(coin,), daemon=True)
            pm_thread.start()
            self.threads.append(pm_thread)
            print(f"[DATA] Started Polymarket feed for {coin.upper()}")
        
        # ❌ 用户频道已禁用 - WebSocket 认证无法使用
        # 改用 REST API takingAmount/makingAmount！
        print(f"[DATA] ℹ️  Position tracking via REST API responses")
        
        # 启动本地计时器更新（修复计时器冻结问题）
        timer_thread = threading.Thread(target=self._timer_worker, daemon=True)
        timer_thread.start()
        self.threads.append(timer_thread)
        
        print(
            f"[DATA] All feeds started: 4 Polymarket orderbooks "
            f"({self.market_slug_suffix} / {self.market_interval_sec}s windows)"
        )
    
    def stop(self):
        """停止所有数据流"""
        print("[DATA] Stopping feeds...")
        self.stop_event.set()
        
        # 给线程时间清理
        for t in self.threads:
            if t.is_alive():
                t.join(timeout=1)
        
        print("[DATA] Feeds stopped")
    
    def get_state(self, coin: str = 'btc') -> Dict:
        """获取指定币种的当前市场状态（线程安全）"""
        with self.locks[coin]:
            market = self.markets.get(coin)
            if not market:
                return None
            
            # 仅 BTC 和 ETH 有价格数据源（SOL/XRP 没有）
            if coin == 'btc':
                price = self.btc_price
            elif coin == 'eth':
                price = self.eth_price
            else:
                price = 0.0  # SOL 和 XRP 无需价格
            
            # 安全处理 None 值
            up_ask = market.get('up_ask') or 0.0
            down_ask = market.get('down_ask') or 0.0
            confidence = abs(down_ask - up_ask) if (up_ask > 0 and down_ask > 0) else 0.0
            
            return {
                'up_ask': up_ask,
                'down_ask': down_ask,
                'price': price,
                'market_start_price': market['market_start_price'],
                'seconds_till_end': market['seconds_till_end'],
                'market_slug': market['slug'],
                'confidence': confidence,
                'coin': coin,
                'market_interval_sec': self.market_interval_sec,
                'market_slug_suffix': self.market_slug_suffix,
            }
    
    def register_price_callback(self, callback):
        """注册价格更新回调函数（事件驱动）"""
        self.price_callbacks.append(callback)
    
    def _current_slug(self, coin: str) -> str:
        """计算当前市场标识（按配置为 5m 或 15m）。"""
        iv = self.market_interval_sec
        current_slot = int(time.time()) // iv * iv
        return f"{coin}-updown-{self.market_slug_suffix}-{current_slot}"
    
    def _fetch_tokens(self, coin: str) -> Optional[Dict]:
        """从 Polymarket 获取指定币种的当前市场 token"""
        try:
            gamma_api = self.config['data_sources']['polymarket']['gamma_api']
            slug = self._current_slug(coin)
            
            # 使用 events API 和特定 slug
            url = f"{gamma_api}/events?slug={slug}"
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            
            events = resp.json()
            if not events:
                # 市场未找到 - 可能尚未开放
                current_time = int(time.time())
                iv = self.market_interval_sec
                next_market = ((current_time // iv) + 1) * iv
                wait_time = next_market - current_time
                print(f"[PM-{coin.upper()}] Market {slug} not found (may not be open yet, next in {wait_time}s)")
                return None
            
            # 获取第一个市场
            market = events[0]["markets"][0]
            clob_token_ids = market.get("clobTokenIds", [])
            outcomes = market.get("outcomes", [])
            condition_id = market.get("conditionId", "")
            neg_risk = market.get("negRisk", True)
            
            # 如果是字符串格式则解析
            if isinstance(clob_token_ids, str):
                clob_token_ids = json.loads(clob_token_ids)
            if isinstance(outcomes, str):
                outcomes = json.loads(outcomes)
            
            # 查找 Up 和 Down 的索引
            up_idx = outcomes.index("Up") if "Up" in outcomes else 0
            down_idx = outcomes.index("Down") if "Down" in outcomes else 1
            
            return {
                'up': clob_token_ids[up_idx],
                'down': clob_token_ids[down_idx],
                'condition_id': condition_id,
                'neg_risk': neg_risk
            }
            
        except Exception as e:
            print(f"[PM-{coin.upper()}] Error fetching tokens: {e}")
        return None
    
    def _polymarket_worker(self, coin: str):
        """指定币种的 Polymarket WebSocket 工作线程"""
        while not self.stop_event.is_set():
            # 获取 token
            tokens = self._fetch_tokens(coin)
            if not tokens:
                time.sleep(5)
                continue
            
            with self.locks[coin]:
                self.markets[coin]['tokens'] = tokens
            
            # 保存 token ID 到 trader 模块，用于实盘交易
            market_slug = self._current_slug(coin)
            trader_module.set_token_ids(
                market_slug=market_slug,
                up_token_id=tokens['up'],
                down_token_id=tokens['down'],
                condition_id=tokens.get('condition_id', ''),
                neg_risk=tokens.get('neg_risk', True)
            )
            
            # 计算重连时间
            current_time = int(time.time())
            iv = self.market_interval_sec
            market_end = ((current_time // iv) * iv) + iv
            reconnect_in = market_end - current_time + 2
            
            # 获取市场标识
            market_slug = self._current_slug(coin)
            
            with self.locks[coin]:
                self.markets[coin]['slug'] = market_slug
                self.markets[coin]['market_end_time'] = market_end
                self.markets[coin]['tokens'] = tokens
                
                # ✅ 在 PositionTracker 中注册市场，用于通过 WebSocket 跟踪
                self.position_tracker.register_market(
                    market_slug=market_slug,
                    up_token_id=tokens['up'],
                    down_token_id=tokens['down']
                )
                
                # 仅 BTC/ETH 设置市场起始价格（SOL/XRP 不需要）
                if self.markets[coin]['market_start_price'] == 0.0:
                    if coin == 'btc':
                        self.markets[coin]['market_start_price'] = self.btc_price
                    elif coin == 'eth':
                        self.markets[coin]['market_start_price'] = self.eth_price
                    # SOL/XRP：保持为 0.0（无需价格数据源）
            
            print(f"[PM-{coin.upper()}] Connected to {market_slug}, reconnect in {reconnect_in}s")
            
            # 连接 WebSocket
            try:
                ws_url = self.config['data_sources']['polymarket']['ws_url']
                ws_ref = [None]  # 保存 ws 引用以便关闭
                
                ws = websocket.WebSocketApp(
                    ws_url,
                    on_message=lambda ws, msg: self._on_pm_message(msg, tokens, coin),
                    on_error=lambda ws, err: None,
                    on_close=lambda ws, code, reason: None
                )
                
                ws_ref[0] = ws
                
                def on_open(ws):
                    """WebSocket 连接建立后发送市场订阅请求。"""
                    sub_msg = {
                        "auth": {},
                        "type": "MARKET",
                        "assets_ids": [tokens["up"], tokens["down"]]
                    }
                    ws.send(json.dumps(sub_msg))
                
                ws.on_open = on_open
                
                # 自动重连计时器
                timer = threading.Timer(reconnect_in, lambda: ws.close())
                timer.start()
                
                # 停止检查线程
                def check_stop():
                    """监控停止事件，收到信号后关闭 WebSocket。"""
                    while not self.stop_event.is_set():
                        time.sleep(0.5)
                    if ws_ref[0]:
                        ws_ref[0].close()
                
                stop_checker = threading.Thread(target=check_stop, daemon=True)
                stop_checker.start()
                
                ws.run_forever(ping_interval=20, ping_timeout=10, skip_utf8_validation=True)
                timer.cancel()
                
                # 如果设置了停止事件，立即停止
                if self.stop_event.is_set():
                    break
                
            except Exception as e:
                print(f"[PM-{coin.upper()}] Error: {e}")
                time.sleep(5)
    
    def _on_pm_message(self, message: str, tokens: Dict, coin: str):
        """解析指定币种的 Polymarket 订单簿消息"""
        try:
            data = json.loads(message)
            
            if not isinstance(data, dict):
                return
            
            # 仅处理 "book" 事件（完整订单簿快照）
            event_type = data.get("event_type", "unknown")
            if event_type != "book":
                return
            
            # 解析订单簿
            asks_raw = data.get("asks", [])
            bids_raw = data.get("bids", [])
            
            # 解析 asks（价格，数量）元组
            asks = []
            for ask in asks_raw or []:
                if isinstance(ask, dict):
                    price = float(ask.get("price", 0))
                    size = float(ask.get("size", 0))
                else:
                    price = float(ask[0]) if len(ask) > 0 else 0
                    size = float(ask[1]) if len(ask) > 1 else 0
                if price > 0 and size > 0:
                    asks.append((price, size))
            
            # 解析 bids（价格，数量）元组
            bids = []
            for bid in bids_raw or []:
                if isinstance(bid, dict):
                    price = float(bid.get("price", 0))
                    size = float(bid.get("size", 0))
                else:
                    price = float(bid[0]) if len(bid) > 0 else 0
                    size = float(bid[1]) if len(bid) > 1 else 0
                if price > 0 and size > 0:
                    bids.append((price, size))
            
            # 按价格升序排序 asks（最低价优先）
            asks.sort(key=lambda x: x[0])
            
            # 按价格降序排序 bids（最高价优先）
            bids.sort(key=lambda x: x[0], reverse=True)
            
            # 获取最优 ask（最低价）和最优 bid（最高价）
            best_ask = asks[0] if asks else None
            best_bid = bids[0] if bids else None
            
            asset = data.get("asset_id", "")
            
            # 更新状态并触发回调（基于每个币种的锁——完全并行！）
            with self.locks[coin]:
                price_changed = False
                old_up_ask = self.markets[coin]['up_ask']
                old_down_ask = self.markets[coin]['down_ask']
                old_up_bid = self.markets[coin]['up_bid']
                old_down_bid = self.markets[coin]['down_bid']
                
                if best_ask:
                    price, size = best_ask
                    
                    if asset == tokens.get("up"):
                        self.markets[coin]['up_ask'] = price
                        self.markets[coin]['up_ask_timestamp'] = time.time()  # 跟踪更新时间
                        # 保存完整订单簿（1 档卖盘 + 5 档买盘）
                        self.markets[coin]['up_asks_full'] = asks[:1]  # 顶部 1 档卖盘
                        self.markets[coin]['up_bids_full'] = bids[:5]  # 顶部 5 档买盘
                        if price != old_up_ask:
                            price_changed = True
                    elif asset == tokens.get("down"):
                        self.markets[coin]['down_ask'] = price
                        self.markets[coin]['down_ask_timestamp'] = time.time()  # 跟踪更新时间
                        # 保存完整订单簿（1 档卖盘 + 5 档买盘）
                        self.markets[coin]['down_asks_full'] = asks[:1]  # 顶部 1 档卖盘
                        self.markets[coin]['down_bids_full'] = bids[:5]  # 顶部 5 档买盘
                        if price != old_down_ask:
                            price_changed = True
                
                if best_bid:
                    price, size = best_bid
                    
                    if asset == tokens.get("up"):
                        self.markets[coin]['up_bid'] = price
                        self.markets[coin]['up_bid_timestamp'] = time.time()  # 跟踪更新时间
                        # 如果卖盘未设置，则更新完整订单簿
                        if not self.markets[coin]['up_bids_full']:
                            self.markets[coin]['up_bids_full'] = bids[:5]
                        if price != old_up_bid:
                            price_changed = True
                    elif asset == tokens.get("down"):
                        self.markets[coin]['down_bid'] = price
                        self.markets[coin]['down_bid_timestamp'] = time.time()  # 跟踪更新时间
                        # 如果卖盘未设置，则更新完整订单簿
                        if not self.markets[coin]['down_bids_full']:
                            self.markets[coin]['down_bids_full'] = bids[:5]
                        if price != old_down_bid:
                            price_changed = True
                
                # 价格变化时触发回调
                if price_changed:
                    up_ask = self.markets[coin]['up_ask']
                    down_ask = self.markets[coin]['down_ask']
                    up_bid = self.markets[coin]['up_bid']
                    down_bid = self.markets[coin]['down_bid']
                    
                    # 价格未就绪则跳过
                    if up_ask is None or down_ask is None:
                        price_changed = False
                    else:
                        market_slug = self.markets[coin]['slug']
                        seconds_till_end = self.markets[coin]['seconds_till_end']
                        
                        # 仅 BTC/ETH 获取价格
                        if coin == 'btc':
                            market_price = self.btc_price
                        elif coin == 'eth':
                            market_price = self.eth_price
                        else:
                            market_price = 0.0  # SOL/XRP 无需价格
                        
                        market_start_price = self.markets[coin]['market_start_price']
                        
                        # 构建回调用的 market_state
                        market_state = {
                            'up_ask': up_ask,
                            'down_ask': down_ask,
                            'up_bid': up_bid,
                            'down_bid': down_bid,
                            'up_ask_timestamp': self.markets[coin]['up_ask_timestamp'],
                            'down_ask_timestamp': self.markets[coin]['down_ask_timestamp'],
                            'up_bid_timestamp': self.markets[coin]['up_bid_timestamp'],
                            'down_bid_timestamp': self.markets[coin]['down_bid_timestamp'],
                            'price': market_price,
                            'market_start_price': market_start_price,
                            'seconds_till_end': seconds_till_end,
                            'market_slug': market_slug,
                            'confidence': abs(down_ask - up_ask),
                            'coin': coin
                        }
                    
                    # 收集所有回调（在锁外调用以避免死锁）
                    callbacks_to_call = list(self.price_callbacks)
            
            # 在锁外调用回调
            # 🔥 异步：每个币种并行处理
            if price_changed and callbacks_to_call:
                for callback in callbacks_to_call:
                    try:
                        # 安全调用包装器
                        def safe_callback_wrapper():
                            """在独立线程中安全执行回调，捕获异常不崩溃。"""
                            try:
                                callback(coin, market_state)
                            except Exception as e:
                                # 记录日志但不崩溃
                                print(f"[CALLBACK ERROR] {coin}: {e}")
                                import traceback
                                traceback.print_exc()
                        
                        # 🛡️ 在独立线程中启动（不阻塞其他币种）
                        threading.Thread(
                            target=safe_callback_wrapper,
                            daemon=True,
                            name=f"cb_{coin}_{int(time.time()*1000)}"
                        ).start()
                    except Exception as e:
                        print(f"[CALLBACK ERROR] Failed to start callback for {coin}: {e}")
                
        except Exception as e:
            pass  # 忽略解析错误
    
    def _timer_worker(self):
        """每秒本地更新所有市场的计时器（基于每个币种的锁）"""
        while not self.stop_event.is_set():
            current_time = int(time.time())
            # 独立更新每个币种的计时器（完全并行）
            for coin in ['btc', 'eth', 'sol', 'xrp']:
                with self.locks[coin]:
                    market_end_time = self.markets[coin]['market_end_time']
                    self.markets[coin]['seconds_till_end'] = max(0, market_end_time - current_time)
            time.sleep(1)
    
    def _user_channel_worker(self):
        """
        WebSocket 用户频道——所有持仓数据的来源！
        
        连接经过身份验证的频道并接收：
        - ORDER 事件（含 size_matched——实际成交数量！）
        - TRADE 事件（交易确认）
        
        这是唯一的数据权威来源！
        """
        reconnect_delay = 5
        
        while not self.stop_event.is_set():
            try:
                ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
                
                print("[USER-WS] 🔌 Connecting to User Channel...")
                
                ws = websocket.WebSocketApp(
                    ws_url,
                    on_message=lambda ws, msg: self._on_user_message(msg),
                    on_error=lambda ws, err: print(f"[USER-WS] ❌ Error: {err}") if err else None,
                    on_close=lambda ws, code, reason: print(f"[USER-WS] 🔌 Disconnected (code={code})")
                )
                
                def on_open(ws):
                    """发送经过身份验证的订阅请求"""
                    try:
                        # 创建身份验证签名
                        timestamp = str(int(time.time()))
                        message = timestamp
                        signature = hmac.new(
                            self.api_secret.encode('utf-8'),
                            message.encode('utf-8'),
                            hashlib.sha256
                        ).digest()
                        signature_b64 = base64.b64encode(signature).decode('utf-8')
                        
                        sub_msg = {
                            "auth": {
                                "apikey": self.api_key,
                                "secret": signature_b64,
                                "passphrase": self.api_passphrase,
                                "timestamp": timestamp
                            },
                            "type": "user"
                        }
                        ws.send(json.dumps(sub_msg))
                        print("[USER-WS] ✅ Authenticated & subscribed to user channel")
                    except Exception as e:
                        print(f"[USER-WS] ⚠️  Auth failed: {e}")
                
                ws.on_open = on_open
                
                # 持续运行（阻塞调用）
                ws.run_forever()
                
            except Exception as e:
                print(f"[USER-WS] ⚠️  Exception: {e}")
            
            # 重连延迟
            if not self.stop_event.is_set():
                print(f"[USER-WS] ⏳ Reconnecting in {reconnect_delay}s...")
                time.sleep(reconnect_delay)
    
    def _on_user_message(self, message: str):
        """
        处理所有 USER 事件——唯一的数据权威来源！
        
        事件类型：
        - order：ORDER 事件（下单/更新/取消）
        - trade：TRADE 事件（成交/确认）
        
        所有事件都传递给 PositionTracker！
        """
        try:
            data = json.loads(message)
            event_type = data.get("event_type")
            
            if event_type == "order":
                # ✅ ORDER 事件——通过跟踪器更新持仓
                self.position_tracker.on_order_event(data)
            
            elif event_type == "trade":
                # ✅ TRADE 事件——确认交易
                self.position_tracker.on_trade_event(data)
            
            else:
                # 其他事件类型（例如心跳）
                pass
        
        except json.JSONDecodeError:
            # 非 JSON 消息（例如连接建立）
            pass
        except Exception as e:
            print(f"[USER-WS] ⚠️  Parse error: {e}")
