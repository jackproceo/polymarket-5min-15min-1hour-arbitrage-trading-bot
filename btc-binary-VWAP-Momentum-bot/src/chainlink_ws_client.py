"""
Chainlink BTC/USD 价格客户端：
通过 Polymarket RTDS WebSocket 订阅 Chainlink BTC/USD 价格流。
自动检测市场边界（按配置的时间间隔对齐到 epoch），
在边界跨越时自动截取锚定价格。
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import websockets

from .core_types import MarketState

logger = logging.getLogger("btc_live")

# Polymarket 实时数据流 WebSocket 地址
RTDS_URL = "wss://ws-live-data.polymarket.com"


class ChainlinkPriceClient:
    """
    持续在线的 BTC/USD 价格流（来自 Polymarket RTDS 的 Chainlink 数据源）。

    连接到 wss://ws-live-data.polymarket.com 并订阅 crypto_prices_chainlink 的 btc/usd。

    自主追踪市场边界（按配置的时间间隔对齐到 epoch），在边界跨越的精确时刻
    截取锚定价格，独立于 bot 的 market finding 流程。这确保锚定价格在真实边界
    约 1 秒内被捕获，而非 5-15 秒后。
    """

    # 无数据超时（秒）- 超过此时间强制重连
    DATA_TIMEOUT = 30

    def __init__(self, state: MarketState, market_duration_sec: int):
        self.state = state
        self._market_duration = int(market_duration_sec)
        if self._market_duration <= 0:
            self._market_duration = 900
        self.running = False
        self._ws = None
        self._ping_task: Optional[asyncio.Task] = None
        # 追踪当前锚定价格属于哪个时间窗口
        self._current_window: int = 0
        # 边界前的最后价格缓冲（用于最精确的锚定）
        self._last_price_before_boundary: float = 0.0
        self._last_price_ts: float = 0.0
        self._last_msg_time: float = 0.0

    def _get_window(self, ts: float) -> int:
        """返回时间戳对应的窗口起始时间（epoch）"""
        d = self._market_duration
        return int(ts) // d * d

    async def connect(self):
        """连接到 RTDS 并订阅 Chainlink BTC/USD 价格。始终在线。"""
        self.running = True
        self._last_msg_time = time.time()

        while self.running:
            try:
                async with websockets.connect(RTDS_URL) as ws:
                    self._ws = ws
                    self.state.btc_connected = True
                    self._last_msg_time = time.time()
                    logger.info("RTDS Chainlink 已连接")

                    # 订阅 chainlink 价格（所有交易对，在代码中过滤）
                    subscribe_msg = json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": "",
                        }],
                    })
                    await ws.send(subscribe_msg)

                    # 启动 ping 循环和看门狗
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))
                    watchdog_task = asyncio.create_task(self._watchdog(ws))

                    try:
                        async for message in ws:
                            if not self.running:
                                break
                            self._last_msg_time = time.time()
                            self._handle_message(message)
                    finally:
                        watchdog_task.cancel()
                        try:
                            await watchdog_task
                        except asyncio.CancelledError:
                            pass

                    self._ws = None

            except websockets.ConnectionClosed:
                self._ws = None
                self.state.btc_connected = False
                if self.running:
                    logger.warning("RTDS Chainlink 已断开，2 秒后重连...")
                    await asyncio.sleep(2)
            except Exception as e:
                self._ws = None
                self.state.btc_connected = False
                if self.running:
                    logger.warning(f"RTDS Chainlink 错误: {e}，5 秒后重连...")
                    await asyncio.sleep(5)
            finally:
                if self._ping_task and not self._ping_task.done():
                    self._ping_task.cancel()
                    try:
                        await self._ping_task
                    except Exception:
                        pass
                    self._ping_task = None

    async def _watchdog(self, ws):
        """看门狗：如果超过 DATA_TIMEOUT 秒未收到任何消息，强制关闭 WebSocket"""
        try:
            while self.running:
                await asyncio.sleep(5)
                silence = time.time() - self._last_msg_time
                if silence > self.DATA_TIMEOUT:
                    logger.warning(
                        f"RTDS Chainlink 看门狗: {silence:.0f} 秒无数据，强制重连"
                    )
                    self.state.btc_connected = False
                    await ws.close()
                    break
        except asyncio.CancelledError:
            pass

    def _handle_message(self, message: str):
        """解析收到的 Chainlink 价格消息并自动检测市场边界"""
        try:
            if not isinstance(message, str) or not message.strip():
                return

            data = json.loads(message)
            topic = data.get("topic", "")

            if topic != "crypto_prices_chainlink":
                return

            payload = data.get("payload", {})
            symbol = payload.get("symbol", "")

            if symbol != "btc/usd":
                return

            price = float(payload.get("value", 0))
            if price <= 0:
                return

            # 使用 Chainlink 自身的时间戳（毫秒）进行精确边界检测
            chainlink_ts_ms = payload.get("timestamp", 0)
            if chainlink_ts_ms:
                price_ts = chainlink_ts_ms / 1000.0
            else:
                price_ts = time.time()

            now = time.time()

            # 始终更新当前价格
            self.state.btc_current_price = price
            self.state.btc_last_update = now

            # === 校准日志：在任意边界 [-15s..+5s] 范围内的每个 tick ===
            price_window = self._get_window(price_ts)
            next_boundary = price_window + self._market_duration
            secs_to_next = next_boundary - price_ts
            secs_from_prev = price_ts - price_window

            # 如果在下一个边界前 15 秒内，或在当前边界开始后 5 秒内，则记录日志
            if secs_to_next <= 15.0 or secs_from_prev <= 5.0:
                cl_time = datetime.fromtimestamp(
                    price_ts, tz=timezone.utc
                ).strftime('%H:%M:%S.%f')[:-3]
                local_time = datetime.fromtimestamp(
                    now, tz=timezone.utc
                ).strftime('%H:%M:%S.%f')[:-3]
                if secs_from_prev <= 5.0:
                    boundary_time = datetime.fromtimestamp(
                        price_window, tz=timezone.utc
                    ).strftime('%H:%M:%S')
                    offset_str = f"+{secs_from_prev:.3f}s after {boundary_time}"
                else:
                    boundary_time = datetime.fromtimestamp(
                        next_boundary, tz=timezone.utc
                    ).strftime('%H:%M:%S')
                    offset_str = f"-{secs_to_next:.3f}s before {boundary_time}"
                logger.info(
                    f"BTC_TICK {cl_time} (local {local_time}) ${price:,.2f} [{offset_str}]"
                )

            # 检测窗口边界跨越

            if self._current_window == 0:
                # 首次收到价格 — 初始化
                self._current_window = price_window
                self.state.btc_anchor_price = price
                logger.info(
                    f"BTC Chainlink 初始化: ${price:,.2f} "
                    f"(窗口 {self._current_window}, "
                    f"ts={datetime.fromtimestamp(price_ts, tz=timezone.utc).strftime('%H:%M:%S.%f')[:-3]})"
                )
            elif price_window != self._current_window:
                # === 新窗口 === 使用新窗口的第一个 tick 作为锚定价格
                # 校准：参考程序使用边界时刻或之后的第一个 tick
                old_anchor = self.state.btc_anchor_price
                old_window = self._current_window

                self.state.btc_anchor_price = price  # 新窗口的第一个 tick
                self._current_window = price_window

                boundary_time = datetime.fromtimestamp(
                    price_window, tz=timezone.utc
                ).strftime('%H:%M:%S')
                price_time = datetime.fromtimestamp(
                    price_ts, tz=timezone.utc
                ).strftime('%H:%M:%S.%f')[:-3]
                delay_ms = (price_ts - price_window) * 1000

                logger.info(
                    f"BTC 锚定重置: ${self.state.btc_anchor_price:,.2f} "
                    f"(边界 {boundary_time}, 首个 tick 于 {price_time}, "
                    f"延迟 {delay_ms:.0f}ms, 上一锚定 ${old_anchor:,.2f})"
                )

            # 始终缓存最新价格以便下次边界跨越
            self._last_price_before_boundary = price
            self._last_price_ts = price_ts

        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    async def _ping_loop(self, ws):
        """每 5 秒发送一次 ping 以保持连接活跃"""
        try:
            while self.running:
                await asyncio.sleep(5)
                try:
                    await ws.ping()
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def disconnect(self):
        """优雅关闭 RTDS WebSocket 连接"""
        self.running = False

        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except Exception:
                pass
            self._ping_task = None

        if self._ws:
            try:
                # 关闭前先取消订阅
                unsub_msg = json.dumps({
                    "action": "unsubscribe",
                    "subscriptions": [{
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": "",
                    }],
                })
                await self._ws.send(unsub_msg)
                await self._ws.close(code=1000, reason="正常关闭")
                logger.info("RTDS Chainlink 已优雅关闭")
            except Exception as e:
                logger.warning(f"RTDS 关闭时出错: {e}")
            finally:
                self._ws = None

        self.state.btc_connected = False
