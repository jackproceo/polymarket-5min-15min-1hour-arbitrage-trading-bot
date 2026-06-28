"""
Polymarket 行情 WebSocket 客户端：
连接到 Polymarket CLOB WebSocket，接收市场深度和成交数据。
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets

from .core_types import MarketState, TokenData, Trade

logger = logging.getLogger("btc_live")

# Polymarket 行情 WebSocket 地址
WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class WebSocketClient:
    """Polymarket 行情数据 WebSocket 客户端"""

    def __init__(self, state: MarketState):
        self.state = state
        self.running = False
        self._tokens_validated = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None

    def _validate_tokens(self):
        """
        在首次收到 WebSocket 数据后记录代币价格。

        注意：已移除代币交换逻辑（因为有 bug），应信任 API 的代币分配。
        """
        if self._tokens_validated:
            return

        up = self.state.up_token
        down = self.state.down_token

        if not up or not down:
            return

        up_price = up.best_bid or up.best_ask or up.last_price
        down_price = down.best_bid or down.best_ask or down.last_price

        # 仅在获得有效价格后记录
        if up_price > 0.05 and down_price > 0.05:
            price_sum = up_price + down_price
            logger.info(
                f"代币验证: UP={up_price:.2f}, DOWN={down_price:.2f}, sum={price_sum:.2f}"
            )
            self._tokens_validated = True

    async def connect(self):
        """连接到 Polymarket WebSocket 并订阅 UP/DOWN 代币数据"""
        self.running = True

        while self.running:
            try:
                async with websockets.connect(WSS_URL) as ws:
                    self._ws = ws
                    self.state.connected = True

                    token_ids = []
                    if self.state.up_token:
                        token_ids.append(self.state.up_token.token_id)
                    if self.state.down_token:
                        token_ids.append(self.state.down_token.token_id)

                    # 记录正在订阅的代币 ID
                    logger.info("WebSocket 正在订阅代币:")
                    logger.info(
                        f"  UP: {self.state.up_token.token_id[:40]}..."
                        if self.state.up_token
                        else "  UP: None"
                    )
                    logger.info(
                        f"  DOWN: {self.state.down_token.token_id[:40]}..."
                        if self.state.down_token
                        else "  DOWN: None"
                    )

                    await ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))

                    async for message in ws:
                        if not self.running:
                            break
                        await self._handle_message(message)

                    self._ws = None

            except websockets.ConnectionClosed:
                self._ws = None
                self.state.connected = False
                if self.running:
                    await asyncio.sleep(1)
            except Exception:
                self._ws = None
                self.state.connected = False
                if self.running:
                    await asyncio.sleep(2)

    async def disconnect(self):
        """优雅关闭 WebSocket 连接（code 1000 正常关闭）"""
        self.running = False
        if self._ws:
            try:
                await self._ws.close(code=1000, reason="正常关闭")
                logger.info("WebSocket 已优雅关闭 (code 1000)")
            except Exception as e:
                logger.warning(f"WebSocket 关闭时出错: {e}")
            finally:
                self._ws = None
        self.state.connected = False

    async def _handle_message(self, message: str):
        """处理收到的 WebSocket 消息"""
        try:
            data = json.loads(message)

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        await self._process_item(item)
            elif isinstance(data, dict):
                await self._process_item(data)

            self.state.last_update = time.time()

            # 收到价格数据后验证代币
            if not self._tokens_validated:
                self._validate_tokens()
        except Exception:
            pass

    async def _process_item(self, data: dict):
        """处理单条 WebSocket 数据项"""
        event_type = data.get("event_type", "")

        if event_type == "last_trade_price":
            asset_id = data.get("asset_id")
            token = self._get_token(asset_id)

            if not token and asset_id:
                # 代币 ID 不匹配 - 可能表示订阅问题
                logger.warning(f"收到未知代币的价格: {asset_id[:30]}...")
                logger.warning(
                    f"  我们的 UP 代币: {self.state.up_token.token_id[:30] if self.state.up_token else 'None'}..."
                )
                logger.warning(
                    f"  我们的 DOWN 代币: {self.state.down_token.token_id[:30] if self.state.down_token else 'None'}..."
                )

            if token:
                price = float(data.get("price", 0))
                size = float(data.get("size", 0))
                side = data.get("side", "BUY")

                if price > 0 and size > 0:
                    token.last_price = price
                    token.last_trade_time = time.time()
                    token.trades.append(Trade(time.time(), price, size, side))
                    token.trade_count += 1
                    token.volume_total += size
                    if side == "BUY":
                        token.volume_buy += size
                    else:
                        token.volume_sell += size

        elif event_type == "price_change":
            for change in data.get("price_changes", []):
                token = self._get_token(change.get("asset_id"))
                if token:
                    if change.get("best_bid"):
                        token.best_bid = float(change["best_bid"])
                    if change.get("best_ask"):
                        token.best_ask = float(change["best_ask"])

        elif event_type == "book":
            token = self._get_token(data.get("asset_id"))
            if token:
                bids = data.get("bids", [])
                if bids:
                    bids.sort(key=lambda x: float(x["price"]), reverse=True)
                    token.best_bid = float(bids[0]["price"])
                    token.best_bid_size = float(bids[0]["size"])
                asks = data.get("asks", [])
                if asks:
                    asks.sort(key=lambda x: float(x["price"]))
                    token.best_ask = float(asks[0]["price"])
                    token.best_ask_size = float(asks[0]["size"])

    def _get_token(self, asset_id: str) -> Optional[TokenData]:
        """根据 asset_id 获取对应的代币数据"""
        if self.state.up_token and asset_id == self.state.up_token.token_id:
            return self.state.up_token
        elif self.state.down_token and asset_id == self.state.down_token.token_id:
            return self.state.down_token
        return None

    def stop(self):
        """停止 WebSocket（同步版本，仅设置标志位）"""
        self.running = False

    async def stop_graceful(self):
        """优雅停止 WebSocket，发送正常关闭帧"""
        await self.disconnect()
