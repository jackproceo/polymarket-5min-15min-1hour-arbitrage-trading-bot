#!/usr/bin/env python3
"""
Polymarket BTC 5分钟/15分钟 涨跌自动交易 - WebSocket馈送模块
BTC价格监听器和市场订单簿监听器。
"""
import json
import time
import threading

from config import BINANCE_WSS, POLYMARKET_WSS
from state import price_data
from utils import log

try:
    import websocket
except ImportError:
    websocket = None


class BTCPriceListener:
    """Binance BTC交易WebSocket监听器。"""
    def __init__(self):
        self.ws = None
        self.running = False

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            if "p" in data:
                price_data["btc"] = float(data["p"])
                ts = time.time()
                price_data["btc_update_ts"] = ts
                price_data["last_update"] = ts
        except:
            pass

    def on_error(self, ws, error):
        pass

    def on_close(self, ws, *args):
        if self.running:
            log("BTC feed disconnected, reconnecting in 5s...", "WARN")
            time.sleep(5)
            self.start()

    def on_open(self, ws):
        log("BTC WebSocket connected", "OK")

    def start(self):
        self.running = True
        self.ws = websocket.WebSocketApp(
            BINANCE_WSS,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()


class MarketPriceListener:
    """CLOB市场订单簿/价格变化监听器（涨/跌）。"""
    def __init__(self, up_token, down_token):
        self.up_token = up_token
        self.down_token = down_token
        self.ws = None
        self.running = False

    def on_message(self, ws, message):
        try:
            data = json.loads(message)

            items = data if isinstance(data, list) else [data]

            for item in items:
                if not isinstance(item, dict):
                    continue

                event_type = item.get("event_type")
                asset_id = item.get("asset_id")

                if event_type == "book":
                    bids = item.get("bids") or []
                    asks = item.get("asks") or []

                    if bids and asks:
                        best_bid = max([float(b["price"]) for b in bids], default=0)
                        best_ask = min([float(a["price"]) for a in asks], default=0)
                        mid_price = (best_bid + best_ask) / 2
                        ts = time.time()

                        if asset_id == self.up_token:
                            price_data["up_bid"] = best_bid
                            price_data["up_ask"] = best_ask
                            price_data["up_price"] = mid_price
                            price_data["up_update_ts"] = ts
                            price_data["last_update"] = ts
                        elif asset_id == self.down_token:
                            price_data["down_bid"] = best_bid
                            price_data["down_ask"] = best_ask
                            price_data["down_price"] = mid_price
                            price_data["down_update_ts"] = ts
                            price_data["last_update"] = ts

                elif event_type == "price_change":
                    price_changes = item.get("price_changes", [])
                    if price_changes:
                        pc = price_changes[0]
                        best_bid = float(pc.get("best_bid", 0))
                        best_ask = float(pc.get("best_ask", 0))

                        if best_bid > 0 and best_ask > 0:
                            mid_price = (best_bid + best_ask) / 2
                            ts = time.time()

                            if asset_id == self.up_token:
                                price_data["up_bid"] = best_bid
                                price_data["up_ask"] = best_ask
                                price_data["up_price"] = mid_price
                                price_data["up_update_ts"] = ts
                                price_data["last_update"] = ts
                            elif asset_id == self.down_token:
                                price_data["down_bid"] = best_bid
                                price_data["down_ask"] = best_ask
                                price_data["down_price"] = mid_price
                                price_data["down_update_ts"] = ts
                                price_data["last_update"] = ts
        except:
            pass

    def on_error(self, ws, error):
        pass

    def on_close(self, ws, *args):
        if self.running:
            log("Market feed disconnected, reconnecting in 5s...", "WARN")
            time.sleep(5)
            self.start()

    def on_open(self, ws):
        ws.send(json.dumps({
            "assets_ids": [self.up_token, self.down_token],
            "type": "market"
        }))
        log("Market WebSocket connected", "OK")

    def start(self):
        self.running = True
        self.ws = websocket.WebSocketApp(
            POLYMARKET_WSS,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()
