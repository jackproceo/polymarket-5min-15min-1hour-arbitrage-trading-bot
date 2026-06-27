#!/usr/bin/env python3
"""
Polymarket BTC 5分钟/15分钟 涨跌自动交易 - 工具函数模块
日志、价格获取、市场查询、钱包操作、状态持久化。
"""
import os
import sys
import time
import json
import threading
import requests
from datetime import datetime
from urllib.parse import urlencode

from config import (
    BASE_DIR, PROXIES, CRYPTO_PRICE_API, CRYPTO_PRICE_PTB_VARIANT,
    GAMMA_API, DATA_API, RTDS_WS, BINANCE_WSS, CLOB_API,
    _btc_market_minutes, _market_interval_sec, _normalize_btc_market_minutes,
    STATE_FILE, STOP_LOSS_PROB_PCT, TAKE_PROFIT_RR, TAKE_PROFIT_CAP,
    SIMULATION_MODE, AUTO_TRADE, TRADING_ANALYSIS_LOG, TRADE_AMOUNT,
    POLYGON_RPC_URL, USDC_E_CONTRACT, HAS_WEB3,
)
from state import price_data, dashboard_state, dashboard_version, dashboard_cond, dashboard_lock, _dashboard_set

try:
    import websocket
except ImportError:
    websocket = None

try:
    from web3 import Web3
except ImportError:
    Web3 = None

_market_found_log_state = {"slug": "", "kind": "", "last_ts": 0.0}
_price_refresh_lock = threading.Lock()
_price_refresh_running = False
_market_cache_lock = threading.Lock()
_market_cache = None
_market_refresh_running = False
_account_sync_lock = threading.Lock()
_account_sync_running = False
_trading_analysis_log_lock = threading.Lock()


# ═════════════════════════════════════════════════════════════════════════════
# 日志 / 刷新辅助
# ═════════════════════════════════════════════════════════════════════════════

def _log_market_found_throttled(kind, slug, remaining):
    """
    限频打印"发现市场"日志，避免同一市场重复刷屏。
    
    只在 slug 或 kind 发生变化时才实际输出一行日志，
    如果连续调用发现的是同一个市场，则静默跳过。
    
    参数
    ----------
    kind : str
        市场类型标识，如 "current"（当前窗口）或 "next"（下一窗口）。
    slug : str
        市场的唯一 slug，如 "btc-updown-5m-1765432100"。
    remaining : int
        市场剩余时间（秒），用于日志中展示 xxm xxs 格式。
    """


def _trigger_price_refresh():
    """
    异步触发 BTC 价格刷新（后台线程，防重入）。
    
    同时从两个渠道获取 BTC 价格并更新到全局 price_data：
      1. Chainlink（通过 Polymarket RTDS WebSocket）- 写入 price_data["btc"]
      2. Binance REST API - 写入 price_data["binance"]
    
    如果上次刷新尚未完成（_price_refresh_running == True），新的调用会被静默跳过。
    这避免了高频循环中重复请求导致 API 限流或 Websocket 堆积。
    
    使用场景
    -------
    主循环中每轮调用一次，确保 price_data 中的 BTC 价格保持最新。
    """
    global _price_refresh_running
    with _price_refresh_lock:
        if _price_refresh_running:
            return
        _price_refresh_running = True

    def worker():
        global _price_refresh_running
        try:
            chainlink_price = get_chainlink_btc_price()
            if chainlink_price:
                price_data["btc"] = chainlink_price
                ts = time.time()
                price_data["btc_update_ts"] = ts
                price_data["last_update"] = ts

            binance_price = get_binance_btc_price()
            if binance_price:
                price_data["binance"] = binance_price
        finally:
            with _price_refresh_lock:
                _price_refresh_running = False

    threading.Thread(target=worker, daemon=True).start()


def _trigger_market_refresh():
    """
    异步刷新活跃市场缓存（后台线程，防重入）。
    
    调用 get_active_market() 获取当前或下一时间窗口的市场数据，
    并将结果缓存在全局 _market_cache 中供主循环读取。
    使用 _market_cache_lock 保证线程安全。
    
    使用场景
    -------
    主循环中定期调用，确保 _market_cache 中的市场信息始终是最新的。
    也可在切换时间窗口（5m ↔ 15m）后强制刷新。
    """
    global _market_refresh_running, _market_cache
    with _market_cache_lock:
        if _market_refresh_running:
            return
        _market_refresh_running = True

    def worker():
        global _market_refresh_running, _market_cache
        try:
            market = get_active_market()
            with _market_cache_lock:
                _market_cache = dict(market) if isinstance(market, dict) else None
        finally:
            with _market_cache_lock:
                _market_refresh_running = False

    threading.Thread(target=worker, daemon=True).start()


def _get_market_cache():
    """
    获取市场缓存的线程安全快照副本。
    
    返回 _trigger_market_refresh 写入的最新市场数据（dict 副本），
    如果没有缓存则返回 None。调用方拿到的是独立副本，不会受后续
    _clear_market_cache 或 _trigger_market_refresh 的影响。
    
    返回
    -------
    dict or None
        市场数据字典，包含 slug / start / end / remaining / up_price 等字段。
    """
    with _market_cache_lock:
        return dict(_market_cache) if isinstance(_market_cache, dict) else None


def _clear_market_cache():
    """
    清空市场缓存，强制下一轮重新获取。
    
    通常在切换 BTC 时间窗口（5m ↔ 15m）或检测到数据过期时调用，
    确保 _get_market_cache 下一次返回 None，从而触发重新查询。
    """
    global _market_cache
    with _market_cache_lock:
        _market_cache = None


def _trigger_account_sync(user):
    """
    异步刷新仪表盘账户快照（后台线程，防重入）。
    
    调用 _sync_dashboard_account_snapshot 从链上和 Polymarket API 拉取
    钱包余额、持仓、交易历史和盈亏数据，然后写入 dashboard_state。
    
    防重入机制：如果上一次同步尚未完成，新的调用被静默跳过。
    
    参数
    ----------
    user : str
        钱包地址（checksummed hex string）。如果为空则直接返回。
    
    使用场景
    -------
    主循环中按 DASHBOARD_ACCOUNT_SYNC_SEC 间隔定期调用。
    """
    global _account_sync_running
    u = str(user or "").strip().lower()
    if not u:
        return
    with _account_sync_lock:
        if _account_sync_running:
            return
        _account_sync_running = True

    def worker():
        global _account_sync_running
        try:
            _sync_dashboard_account_snapshot(u)
        except Exception:
            pass
        finally:
            with _account_sync_lock:
                _account_sync_running = False

    threading.Thread(target=worker, daemon=True).start()


def log(msg, level="INFO", force=False):
    """
    统一日志输出：控制台打印 + 仪表盘 SSE 推送 + 文件持久化。
    
    控制台输出带时间戳和 emoji 图标，按日志级别使用不同图标：
      INFO  → ℹ️,  OK  → ✅,  ERR  → ❌,  WARN → ⚠️,  TRADE → 💰
    
    仪表盘方面：将日志追加到 dashboard_state["activity"] 数组
    （最大保留 400 条），并触发 Condition 通知 SSE 客户端刷新。
    
    文件持久化方面：仅 TRADE 和 ERR 级别的日志会写入 trade.log 文件。
    
    参数
    ----------
    msg : str
        日志消息文本。
    level : str
        日志级别：INFO / OK / ERR / WARN / TRADE。
    force : bool
        如果为 True，无论什么级别都强制输出（默认仅 OK/ERR/WARN/TRADE 输出）。
    """
    if force or level in ["OK", "ERR", "WARN", "TRADE"]:
        icons = {"INFO": "\u2139\ufe0f", "OK": "\u2705", "ERR": "\u274c", "WARN": "\u26a0\ufe0f", "TRADE": "\ud83d\udcb0"}
        icon = icons.get(level, "\u2139\ufe0f")
        ts = datetime.now().strftime("%H:%M:%S")
        log_msg = f"[{ts}] {icon} {msg}"
        print(log_msg)

        global dashboard_version
        with dashboard_cond:
            arr = dashboard_state.get("activity") or []
            arr.append({
                "time": ts,
                "level": level,
                "message": str(msg),
            })
            if len(arr) > 400:
                arr = arr[-400:]
            dashboard_state["activity"] = arr
            dashboard_state["updated_at"] = datetime.now().isoformat()
            dashboard_version += 1
            dashboard_cond.notify_all()

        if level in ["TRADE", "ERR"]:
            try:
                log_dir = os.path.join(BASE_DIR, "logs")
                os.makedirs(log_dir, exist_ok=True)
                with open(os.path.join(log_dir, "trade.log"), "a", encoding="utf-8") as f:
                    f.write(log_msg + "\n")
            except:
                pass


def get_btc_market_minutes():
    """
    获取当前 BTC 时间窗口长度（分钟）。
    
    返回
    -------
    int
        5 或 15，表示当前使用 5 分钟还是 15 分钟 BTC Up/Down 市场。
    """
    return _btc_market_minutes


def set_btc_market_minutes(m):
    """
    动态切换 BTC 时间窗口（5 分钟 ↔ 15 分钟）。
    
    此函数在运行时切换策略的时间粒度，调用后：
      1. 更新全局 _btc_market_minutes 和 _market_interval_sec
      2. 清空 PTB 价格缓存（price_data["ptb"] = None）
      3. 清空市场缓存并触发异步刷新
      4. 更新仪表盘显示
      5. 打印确认日志
    
    参数
    ----------
    m : int or str
        目标分钟数，传入 5 或 15（字符串也可，会被归一化）。
    
    使用场景
    -------
    仪表盘 Web 界面的 "5m / 15m" 切换按钮。
    """
    global _btc_market_minutes, _market_interval_sec
    _btc_market_minutes = _normalize_btc_market_minutes(m)
    _market_interval_sec = _btc_market_minutes * 60
    price_data["ptb"] = None
    _clear_market_cache()
    _trigger_market_refresh()
    _dashboard_set(btc_market_minutes=_btc_market_minutes)
    log(
        f"BTC 市场窗口设为 {_btc_market_minutes}m "
        f"(slug btc-updown-{_btc_market_minutes}m-*, PTB 通过 {CRYPTO_PRICE_PTB_VARIANT!r} + 事件窗口)",
        "OK",
        force=True,
    )


def get_binance_btc_price():
    """
    从 Binance REST API 获取 BTC/USDT 最新价格。
    
    请求 Binance 公开市场数据接口 /api/v3/ticker/price，
    获取 BTCUSDT 交易对的当前市价。如果请求失败（网络错误、
    非 200 状态码），返回 None。
    
    支持通过 PROXIES 配置代理访问。
    
    返回
    -------
    float or None
        BTC/USDT 价格（美元），失败返回 None。
    
    使用场景
    -------
    作为 Chainlink 价格的辅助参考，写入 price_data["binance"]。
    """
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/price",
                        params={"symbol": "BTCUSDT"},
                        proxies=PROXIES if PROXIES else None,
                        timeout=5)
        if r.status_code == 200:
            return float(r.json().get("price"))
    except:
        pass
    return None


def get_chainlink_btc_price():
    """
    通过 Polymarket RTDS WebSocket 获取 Chainlink BTC/USD 预言机价格。
    
    建立临时 WebSocket 连接到 Polymarket 的实时数据服务（RTDS_WS），
    订阅 crypto_prices_chainlink 主题的 BTC/USD 数据。
    等待最多 3 秒获取最新价格后自动关闭连接。
    
    此路数据来自 Chainlink 去中心化预言机网络，相比 Binance 撮合价格
    更接近 Polymarket 市场定价所用参考源。
    
    返回
    -------
    float or None
        Chainlink BTC/USD 价格（美元），超时或失败返回 None。
    
    注意
    -------
    如果 websocket 库未安装，或 RTDS 连接失败，会静默返回 None。
    """
    result = {"price": None}

    def on_message(ws, message):
        try:
            data = json.loads(message)
            if data.get("topic") == "crypto_prices" and data.get("payload"):
                payload = data["payload"]
                if "data" in payload and payload.get("symbol") == "btc/usd":
                    prices = payload["data"]
                    if prices:
                        result["price"] = prices[-1]["value"]
                elif "value" in payload:
                    result["price"] = payload["value"]
            ws.close()
        except:
            pass

    def on_open(ws):
        sub_msg = {
            "action": "subscribe",
            "subscriptions": [{
                "topic": "crypto_prices_chainlink",
                "type": "*",
                "filters": "{\"symbol\":\"btc/usd\"}"
            }]
        }
        ws.send(json.dumps(sub_msg))

    def on_error(ws, error):
        pass

    try:
        ws = websocket.WebSocketApp(RTDS_WS,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error)

        def close_after():
            time.sleep(3)
            try:
                ws.close()
            except:
                pass
        threading.Thread(target=close_after, daemon=True).start()

        ws.run_forever()
        return result["price"]
    except:
        return None


def get_crypto_price_api(start_time, end_time):
    """
    从 Polymarket crypto-price API 获取时间窗口的 PTB（Price To Beat）。
    
    PTB 是 Polymarket 为每个 BTC Up/Down 市场定义的"基准价格"，
    即事件开始时的 BTC 价格。在窗口结束时，BTC 高于此价格则 UP 赢，
    低于则 DOWN 赢。
    
    API 请求参数包含 symbol（BTC）、variant（由 CRYPTO_PRICE_PTB_VARIANT
    配置，如 "fifteen"）、eventStartTime 和 endDate。
    
    参数
    ----------
    start_time : datetime or str
        事件开始时间。接受 datetime 对象或 ISO 8601 字符串。
    end_time : datetime or str
        事件结束时间（当前时间），格式同上。
    
    返回
    -------
    dict
        {"openPrice": float or None, "closePrice": float or None, "completed": bool}
        失败返回空 dict {}。
    
    使用场景
    -------
    主循环中获取当前活跃市场的 PTB，用于计算 BTC 与 PTB 的价差。
    """
    try:
        if isinstance(start_time, str):
            start_str = start_time.replace("Z", "+00:00")
            if "+" in start_str:
                start_str = start_str.split("+")[0] + "Z"
            else:
                start_str = start_time
        else:
            start_str = start_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        if isinstance(end_time, str):
            end_str = end_time.replace("Z", "+00:00")
            if "+" in end_str:
                end_str = end_str.split("+")[0] + "Z"
            else:
                end_str = end_time
        else:
            end_str = end_time.strftime("%Y-%m-%dT%H:%M:%SZ")

        params = {
            "symbol": "BTC",
            "eventStartTime": start_str,
            "variant": CRYPTO_PRICE_PTB_VARIANT,
            "endDate": end_str
        }

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://polymarket.com/"
        }

        log(f"PTB 请求: {CRYPTO_PRICE_API}?{urlencode(params)}", "INFO")
        r = requests.get(CRYPTO_PRICE_API, params=params, headers=headers,
                        proxies=PROXIES if PROXIES else None, timeout=10)

        log(f"PTB HTTP 状态: {r.status_code}", "INFO")

        if r.status_code == 200:
            data = r.json()
            log(f"PTB 返回数据: {data}", "INFO")
            return data
        else:
            log(f"PTB 请求失败: HTTP {r.status_code} - {r.text[:200]}", "ERR")
    except Exception as e:
        log(f"价格数据错误: {type(e).__name__}: {str(e)}", "ERR")
    return {}


def get_current_slug():
    """
    获取当前 UTC 时间所在窗口的 Slug（如 "btc-updown-5m-1765432100"）。
    
    Slug 按 _market_interval_sec（300 或 900 秒）对齐 UTC 时间戳计算，
    同一窗口内的所有调用返回相同 Slug。
    
    返回
    -------
    str
        格式：f"btc-updown-{minutes}m-{window_start_timestamp}"
    """
    ts = int(time.time())
    step = _market_interval_sec
    window_start = (ts // step) * step
    return f"btc-updown-{_btc_market_minutes}m-{window_start}"


def get_next_slug():
    """
    获取下一个 UTC 时间窗口的 Slug。
    
    在 get_current_slug 的基础上加一个窗口步长，用于预取即将到来的市场。
    
    返回
    -------
    str
        下一个窗口的 Slug。
    """
    ts = int(time.time())
    step = _market_interval_sec
    window_start = ((ts // step) + 1) * step
    return f"btc-updown-{_btc_market_minutes}m-{window_start}"


def get_active_market():
    """
    获取当前 BTC 时间窗口对应的活跃 Up/Down 市场。
    
    先尝试获取当前窗口的市场（get_current_slug），如果该市场不存在
    或已结束，则尝试获取下一个窗口的市场（get_next_slug）。
    两个窗口都无活跃市场时打印 WARN 日志。
    
    返回
    -------
    dict or None
        市场数据字典（包含 slug / start / end / remaining /
        up_price / down_price / up_token / down_token），
        无活跃市场时返回 None。
    """
    try:
        current_slug = get_current_slug()
        market = fetch_market_by_slug(current_slug)
        if market and market["remaining"] > 0:
            _log_market_found_throttled("current", current_slug, market["remaining"])
            return market

        next_slug = get_next_slug()
        market = fetch_market_by_slug(next_slug)
        if market and market["remaining"] > 0:
            _log_market_found_throttled("next", next_slug, market["remaining"])
            return market

        log("当前或下一窗口无活跃市场", "WARN")

    except Exception as e:
        log(f"市场获取失败: {e}", "ERR")
        import traceback
        traceback.print_exc()
    return None


def fetch_market_by_slug(slug):
    """
    通过 Polymarket Gamma API 查询指定 Slug 的市场数据。
    
    从 Gamma API 的 /events 端点获取事件信息，解析后返回：
    - 事件剩余时间（秒）
    - UP 和 DOWN 代币的当前价格
    - UP 和 DOWN 代币的 CLOB token ID（用于下单）
    
    参数
    ----------
    slug : str
        市场 Slug，如 "btc-updown-5m-1765432100"。
    
    返回
    -------
    dict or None
        {
            "slug": str,          # 市场 Slug
            "start": str,         # 事件开始时间
            "end": str,           # 事件结束时间
            "remaining": int,     # 剩余秒数
            "up_price": float,    # UP 代币价格（0~1）
            "down_price": float,  # DOWN 代币价格（0~1）
            "up_token": str,      # UP 代币 CLOB Token ID
            "down_token": str     # DOWN 代币 CLOB Token ID
        }
        事件已关闭、已结束或 API 失败时返回 None。
    """
    try:
        r = requests.get(f"{GAMMA_API}/events", params={"slug": slug},
                        proxies=PROXIES if PROXIES else None, timeout=10)
        data = r.json()

        if not data:
            return None

        event = data[0]

        if event.get("closed", False):
            return None

        end_str = event.get("endDate", "")
        start_str = event.get("startTime", "")
        if not end_str or not start_str:
            return None

        now = datetime.now().timestamp()
        end_ts = datetime.fromisoformat(end_str.replace("Z", "+00:00")).timestamp()
        remaining_time = int(end_ts - now)

        if remaining_time <= 0:
            return None

        markets = event.get("markets", [])
        if not markets:
            return None

        m = markets[0]
        outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
        prices = json.loads(m.get("outcomePrices", "[]")) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
        tokens = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])

        up_price = float(prices[0]) if len(prices) > 0 else None
        down_price = float(prices[1]) if len(prices) > 1 else None
        up_token = tokens[0] if len(tokens) > 0 else None
        down_token = tokens[1] if len(tokens) > 1 else None

        return {
            "slug": slug,
            "start": start_str,
            "end": end_str,
            "remaining": remaining_time,
            "up_price": up_price,
            "down_price": down_price,
            "up_token": up_token,
            "down_token": down_token
        }
    except Exception as e:
        return None


def get_ptb(start_time, end_time):
    """
    获取时间窗口的 PTB（Price To Beat，开盘参考价）。
    
    PTB 即事件开始时的 BTC 价格，交易者需判断窗口结束时的 BTC 价格
    相对于 PTB 是涨还是跌。此函数相比 get_crypto_price_api 更轻量，
    直接返回 openPrice 的 float 值。
    
    参数
    ----------
    start_time : datetime or str
        事件开始时间。
    end_time : datetime or str
        当前时间（事件结束查询时间）。
    
    返回
    -------
    float or None
        PTB 价格（美元），失败返回 None。
    """
    try:
        params = {
            "symbol": "BTC",
            "eventStartTime": start_time,
            "variant": CRYPTO_PRICE_PTB_VARIANT,
            "endDate": end_time
        }
        r = requests.get(CRYPTO_PRICE_API, params=params, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return float(data.get("openPrice")) if data.get("openPrice") else None
    except:
        pass
    return None


def _normalize_state(state):
    """
    确保机器人状态字典包含所有必要字段，缺失字段用默认值填充。
    
    状态持久化/加载后，某些字段可能不存在或类型不正确，
    此函数保证返回的 dict 结构完整，避免后续代码因 KeyError 崩溃。
    
    处理的字段：
    - position / pending_order / last_order / take_profit_order : dict
    - trade_history : list
    - cumulative_realized_pnl : float
    
    参数
    ----------
    state : any
        原始状态数据（可能是 None、dict 或其他类型）。
    
    返回
    -------
    dict
        确保包含所有必需字段的规范化状态字典。
    """
    if not isinstance(state, dict):
        state = {}
    if not isinstance(state.get("position"), dict):
        state["position"] = {}
    if not isinstance(state.get("pending_order"), dict):
        state["pending_order"] = {}
    if not isinstance(state.get("last_order"), dict):
        state["last_order"] = {}
    if not isinstance(state.get("take_profit_order"), dict):
        state["take_profit_order"] = {}
    if not isinstance(state.get("trade_history"), list):
        state["trade_history"] = []
    if state.get("cumulative_realized_pnl") is None or not isinstance(
        state.get("cumulative_realized_pnl"), (int, float)
    ):
        try:
            state["cumulative_realized_pnl"] = float(state.get("cumulative_realized_pnl") or 0.0)
        except (TypeError, ValueError):
            state["cumulative_realized_pnl"] = 0.0
    return state


def _dashboard_pending_order_from_state(state):
    """
    从状态中提取当前待处理订单，用于仪表盘显示。
    
    优先返回 pending_order（开仓中的订单），如果没有则返回
    take_profit_order（止盈中的订单），都没有时返回空 dict。
    
    参数
    ----------
    state : dict
        机器人状态字典。
    
    返回
    -------
    dict
        待处理订单信息，包含 action / price / size / order_id 等字段。
    """
    state = _normalize_state(state)
    pending = dict(state.get("pending_order") or {})
    if pending:
        return pending
    tp = dict(state.get("take_profit_order") or {})
    if tp:
        tp.setdefault("action", "SELL")
        tp.setdefault("reason", "take_profit")
    return tp


def _append_trade_history(state, item):
    """
    追加一条交易记录到历史列表，同步更新仪表盘。
    
    历史记录最多保留最近 300 条，超出时自动裁减最早的记录。
    每次追加后立即同步到 dashboard_state["trade_history"]。
    
    参数
    ----------
    state : dict
        机器人状态字典（会被就地修改）。
    item : dict
        单条交易记录，包含 time / action / slug / price / shares / pnl 等。
    
    返回
    -------
    dict
        更新后的状态字典。
    """
    state = _normalize_state(state)
    hist = list(state.get("trade_history") or [])
    hist.append(item)
    if len(hist) > 300:
        hist = hist[-300:]
    state["trade_history"] = hist
    _dashboard_set(trade_history=list(hist))
    return state


def _planned_take_profit_stop_loss(entry_prob):
    """
    计算开仓后的止盈（TP）和止损（SL）概率目标值。
    
    基于入场概率（entry_prob）和配置参数（STOP_LOSS_PROB_PCT、
    TAKE_PROFIT_RR、TAKE_PROFIT_CAP）计算：
    
    1. 止损概率 = entry_prob × (1 - STOP_LOSS_PROB_PCT)
       （例如入场 0.90, STOP_LOSS_PROB_PCT=0.15 → 止损 0.765）
    
    2. 止盈概率 = min(TAKE_PROFIT_CAP, entry_prob + 风险敞口 × TAKE_PROFIT_RR)
       （例如入场 0.90, 风险 0.135, RR=2.0, 上限 0.99 → 止盈 0.99）
    
    如果止盈概率不高于入场概率，则止盈返回 None（表示无法设置合理的止盈）。
    
    参数
    ----------
    entry_prob : float or None
        开仓时的买入概率价格（0~1 范围）。
    
    返回
    -------
    tuple
        (take_profit_prob, stop_loss_prob)
        - take_profit_prob: float or None（无法计算时返回 None）
        - stop_loss_prob: float or None
        
    使用场景
    -------
    每次买入成交后调用，记录计划中的止盈止损价位供后续监控。
    """
    if entry_prob is None or entry_prob <= 0:
        return None, None
    try:
        ep = float(entry_prob)
    except (TypeError, ValueError):
        return None, None
    stop_prob = max(0.0, ep * (1.0 - STOP_LOSS_PROB_PCT))
    risk_abs = max(0.0, ep - stop_prob)
    tp_trigger = min(TAKE_PROFIT_CAP, ep + risk_abs * TAKE_PROFIT_RR)
    if tp_trigger <= ep:
        return None, stop_prob
    balanced_risk = (tp_trigger - ep) / TAKE_PROFIT_RR
    balanced_stop = max(0.0, ep - balanced_risk)
    if balanced_stop > stop_prob:
        stop_prob = balanced_stop
    return tp_trigger, stop_prob


def _emit_trading_analysis(event, **fields):
    """
    向交易分析日志文件追加一行结构化 JSON（schema_version=2）。
    
    此函数接收一个事件名称和一系列关键字参数，将其归一化为统一的
    JSON 行格式写入 TRADING_ANALYSIS_LOG 文件。每行包含稳定的字段集：
    schema_version / event / slug / shares_type / share_price /
    share_amount / ptb / btc_price / difference / status /
    take_profit / stop_loss / time / pnl_trade_usd / pnl_total_usd /
    simulation / btc_market_minutes 等。
    
    参数会自动归一化：
    - difference 优先取传入值，否则自动计算 btc - ptb
    - status 从 action 自动推导（BUY→buy, SELL→sell）
    - tp/sl 如果未传入且不是 SELL 类事件，自动调用 _planned_take_profit_stop_loss 计算
    
    额外传入的字段（reason / order_id / slug 等）会原样透传。
    
    参数
    ----------
    event : str
        事件类型，如 "BUY_FILL" / "SELL_FILL" / "SESSION_START" 等。
    **fields : dict
        可选字段，支持 price / shares / pnl / slug / reason 等。
    
    文件格式
    -------
    JSON Lines（每行一个完整 JSON 对象），追加写入。
    """
    ts = fields.get("time") or datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
    ts = fields.get("time") or datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")

    btc = fields.get("btc_price")
    if btc is None:
        btc = fields.get("chainlink_btc")
    ptb = fields.get("ptb")

    diff = fields.get("difference")
    if diff is None:
        diff = fields.get("diff_rule", fields.get("diff"))
    if diff is None and btc is not None and ptb is not None:
        try:
            diff = float(btc) - float(ptb)
        except (TypeError, ValueError):
            diff = None

    st = fields.get("status")
    if not st:
        act = str(fields.get("action") or "").upper()
        if act == "BUY":
            st = "buy"
        elif act == "SELL":
            st = "sell"

    shares_type = fields.get("shares_type") or fields.get("side")

    share_price = fields.get("share_price")
    if share_price is None:
        share_price = fields.get("price")
    if share_price is None:
        share_price = fields.get("exit_share_price")

    share_amount = fields.get("share_amount")
    if share_amount is None:
        share_amount = fields.get("shares")

    pnl_trade = fields.get("pnl_trade_usd")
    if pnl_trade is None:
        pnl_trade = fields.get("realized_pnl_usd")

    pnl_total = fields.get("pnl_total_usd")
    if pnl_total is None:
        pnl_total = fields.get("cumulative_realized_pnl_usd")

    tp = fields.get("take_profit")
    sl = fields.get("stop_loss")
    if tp is None and sl is None:
        entry_plan = fields.get("entry_share_price")
        if entry_plan is None:
            entry_plan = share_price
        _no_auto_plan = (
            "SELL_CLOSE",
            "SELL_SUBMIT",
            "SELL_FAILED",
            "SELL_ALERT",
            "BUY_CANCEL_TIMEOUT",
        )
        if entry_plan is not None and event not in _no_auto_plan:
            tp, sl = _planned_take_profit_stop_loss(entry_plan)

    def _nf(x):
        if x is None:
            return None
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    row = {
        "schema_version": 2,
        "event": event,
        "slug": fields.get("slug"),
        "shares_type": shares_type,
        "share_price": _nf(share_price),
        "share_amount": _nf(share_amount),
        "ptb": _nf(ptb),
        "btc_price": _nf(btc),
        "difference": _nf(diff),
        "difference_note": "Chainlink BTC minus PTB (USD); same as diff in bot logic.",
        "status": st,
        "take_profit": _nf(tp),
        "stop_loss": _nf(sl),
        "time": ts,
        "pnl_trade_usd": _nf(pnl_trade),
        "pnl_total_usd": _nf(pnl_total),
        "simulation": SIMULATION_MODE,
        "btc_market_minutes": _btc_market_minutes,
    }

    passthrough = (
        "reason",
        "order_id",
        "order_size_usdc",
        "remaining_sec",
        "entry_share_price",
        "exit_share_price",
        "notional_exit_usd",
        "action",
        "chainlink_btc",
        "btc_minus_ptb",
        "diff_rule",
    )
    for k in passthrough:
        if k in fields and fields[k] is not None:
            row[k] = fields[k]

    try:
        log_dir = os.path.dirname(TRADING_ANALYSIS_LOG)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with _trading_analysis_log_lock:
            with open(TRADING_ANALYSIS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        try:
            log(f"交易分析日志写入失败 ({TRADING_ANALYSIS_LOG}): {e}", "ERR", force=True)
        except Exception:
            print(f"Trading analysis log write failed ({TRADING_ANALYSIS_LOG}): {e}", file=sys.stderr)


def _init_trading_analysis_session():
    """
    初始化交易分析日志会话，写入 SESSION_START 标记行。
    
    在机器人启动时调用，确保日志文件路径在发生任何交易之前即已创建，
    方便运维人员确认日志配置正确。写入的行包含 schema_version / simulation /
    auto_trade / btc_market_minutes / trade_amount_usdc 等会话级元数据。
    
    如果写入失败（如目录不可写），打印错误到 stderr 但不中断启动。
    """
    row = {
        "schema_version": 2,
        "event": "SESSION_START",
        "log_path": TRADING_ANALYSIS_LOG,
        "slug": None,
        "shares_type": None,
        "share_price": None,
        "share_amount": None,
        "ptb": None,
        "btc_price": None,
        "difference": None,
        "difference_note": "Chainlink BTC minus PTB (USD).",
        "status": None,
        "take_profit": None,
        "stop_loss": None,
        "time": None,
        "pnl_trade_usd": None,
        "pnl_total_usd": None,
        "simulation": SIMULATION_MODE,
        "auto_trade": AUTO_TRADE,
        "btc_market_minutes": _btc_market_minutes,
        "trade_amount_usdc": TRADE_AMOUNT,
        "note": "Trade rows use the same keys as SESSION_START; pnl_total_usd is cumulative realized.",
    }
    row["logged_at"] = row["time"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S.%f")
    try:
        log_dir = os.path.dirname(TRADING_ANALYSIS_LOG)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        with _trading_analysis_log_lock:
            with open(TRADING_ANALYSIS_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"FATAL: cannot write trading log at {TRADING_ANALYSIS_LOG}: {e}", file=sys.stderr)
        try:
            log(f"无法初始化交易分析日志: {e}", "ERR", force=True)
        except Exception:
            pass


def _shares_from_usdc_buy(usdc, share_price):
    """
    根据 USDC 金额和代币单价计算可买入的代币数量。
    
    参数
    ----------
    usdc : float
        用于买入的 USDC 金额。
    share_price : float
        每个代币的单价（0~1 范围，如 0.85 表示 0.85 USDC/share）。
    
    返回
    -------
    float
        可购买的 shares 数量，参数无效时返回 0.0。
    """
    if share_price and share_price > 0 and usdc and usdc > 0:
        return float(usdc) / float(share_price)
    return 0.0


def _btc_ptb_snapshot(btc, ptb):
    """
    计算当前 BTC 价格与 PTB 的差值快照。
    
    返回值 = BTC - PTB（美元），正数表示 BTC 高于 PTB。
    
    参数
    ----------
    btc : float or None
        当前 BTC 价格。
    ptb : float or None
        当前 PTB（Price To Beat）。
    
    返回
    -------
    float or None
        差值（美元），任一参数为 None 时返回 None。
    """
    if btc is None or ptb is None:
        return None
    try:
        return float(btc) - float(ptb)
    except (TypeError, ValueError):
        return None


def _to_float(value, default=0.0):
    """
    安全地将任意值转换为 float，转换失败返回默认值。
    
    参数
    ----------
    value : any
        要转换的值。
    default : float
        转换失败时的默认值（默认 0.0）。
    
    返回
    -------
    float
    """
    try:
        return float(value)
    except Exception:
        return float(default)


def _maybe_float(value):
    """
    尝试将任意值转换为 float，失败返回 None。
    
    与 _to_float 的区别在于失败时返回 None 而非默认值，
    适用于需要区分"有效 0"和"无效值"的场景。
    
    参数
    ----------
    value : any
    
    返回
    -------
    float or None
    """
    try:
        return float(value)
    except Exception:
        return None


def _to_bool(value):
    """
    将任意值安全转换为布尔值。
    
    支持字符串 "1"/"true"/"yes"/"y"/"on"（不区分大小写）
    以及 Python bool 类型。
    
    参数
    ----------
    value : any
    
    返回
    -------
    bool
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in ("1", "true", "yes", "y", "on")


def _data_api_get(path, params=None):
    """
    向 Polymarket Data API 发送 GET 请求并返回 JSON 结果。
    
    自动拼接 DATA_API 基础 URL 和路径，支持代理和 12 秒超时。
    
    参数
    ----------
    path : str
        API 路径，如 "/activity" 或 "/positions"。
    params : dict or None
        查询参数字典。
    
    返回
    -------
    dict or list or None
        JSON 解析后的响应数据，失败返回 None。
    """
    try:
        r = requests.get(
            f"{DATA_API}{path}",
            params=params or {},
            proxies=PROXIES if PROXIES else None,
            timeout=12,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        return None
    return None


def _text_scalar(v):
    """
    将值安全转换为纯文本字符串。
    
    仅接受 str / int / float / bool 类型，其他类型返回空字符串。
    避免将 None 或复杂对象转换为字符串 "None" / "{...}"。
    
    参数
    ----------
    v : any
    
    返回
    -------
    str
    """
    if isinstance(v, (str, int, float, bool)):
        return str(v).strip()
    return ""


def _normalize_outcome_label(v):
    """
    归一化交易方向标签为统一格式（UP / DOWN / -）。
    
    参数
    ----------
    v : any
        原始方向值，可能为 "UP" / "DOWN" / "YES" / "NO" / None 等。
    
    返回
    -------
    str
        "UP"（包含 UP 或 YES）、"DOWN"（包含 DOWN 或 NO）、"-"（其他）。
    """
    s = str(v or "").upper()
    if "UP" in s or s == "YES":
        return "UP"
    if "DOWN" in s or s == "NO":
        return "DOWN"
    return s or "-"


def _trade_pick_field(tr, *keys):
    """
    从交易记录中按优先级顺序提取第一个非空字段值。
    
    同时在交易记录本身、其 market 子对象和 event 子对象中查找，
    返回第一个匹配成功的非空值。
    
    参数
    ----------
    tr : dict
        交易记录（可能包含嵌套的 market / event 对象）。
    *keys : str
        要搜索的字段名，按优先级排列。
    
    返回
    -------
    str
        第一个找到的非空值，全部未找到则返回空字符串。
    """
    if not isinstance(tr, dict):
        return ""
    sources = [tr]
    market = tr.get("market")
    if isinstance(market, dict):
        sources.append(market)
    event = tr.get("event")
    if isinstance(event, dict):
        sources.append(event)
    for src in sources:
        for k in keys:
            if k not in src:
                continue
            s = _text_scalar(src.get(k))
            if s:
                return s
    return ""


def _trade_event_kind(tr):
    """
    判断交易记录的事件类型（BUY / SELL / REDEEM / IGNORE）。
    
    根据 type 和 side 字段判定：
    - type=REDEEM → 赎回
    - type=DEPOSIT/WITHDRAW/TRANSFER → 忽略的资金操作
    - side=BUY/SELL → 买卖
    - 其他 → 忽略
    
    参数
    ----------
    tr : dict
        交易记录。
    
    返回
    -------
    str
        "BUY" / "SELL" / "REDEEM" / "IGNORE"
    """
    typ = str((tr or {}).get("type") or "").upper().strip()
    side = str((tr or {}).get("side") or "").upper().strip()
    if typ == "REDEEM":
        return "REDEEM"
    if typ in ["DEPOSIT", "WITHDRAW", "WITHDRAWAL", "TRANSFER"]:
        return "IGNORE"
    if side in ["BUY", "SELL"]:
        return side
    return "IGNORE"


def _trade_ts_ms(tr):
    """
    从交易记录中提取时间戳（毫秒）。
    
    支持多种字段名（matchtime / match_time / timestamp / created_at / time），
    以及多种格式：
    - 数字时间戳（秒或毫秒，>1e12 视为毫秒）
    - ISO 8601 字符串（含 Z 或时区偏移）
    - 纯数字字符串
    
    参数
    ----------
    tr : dict
        交易记录。
    
    返回
    -------
    int
        毫秒时间戳，无法提取时返回 0。
    """
    v = (tr or {}).get("matchtime") or (tr or {}).get("match_time") or (tr or {}).get("timestamp") or (tr or {}).get("created_at") or (tr or {}).get("time")
    if isinstance(v, (int, float)):
        n = float(v)
        return int(n if n > 1e12 else n * 1000)
    s = str(v or "").strip()
    if not s:
        return 0
    if s.isdigit():
        n = int(s)
        return n if n > 1e12 else n * 1000
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


def _trade_usdc_size(tr):
    """
    计算交易记录的 USDC 名义金额。
    
    优先使用 usdcSize / usdc_size 字段，如果不存在则
    通过 price × size_matched 推算。
    
    参数
    ----------
    tr : dict
        交易记录。
    
    返回
    -------
    float
        USDC 金额（绝对值），无法计算时返回 0.0。
    """
    usdc = _maybe_float((tr or {}).get("usdcSize") or (tr or {}).get("usdc_size"))
    if usdc is not None:
        return abs(usdc)
    price = _maybe_float((tr or {}).get("price"))
    size = _maybe_float((tr or {}).get("size_matched") or (tr or {}).get("size") or (tr or {}).get("original_size"))
    if price is not None and size is not None:
        return abs(price * size)
    return 0.0


def _trade_market_key(tr):
    """
    提取交易的唯一市场标识（用于分组聚合）。
    
    按优先级查找：conditionId > eventSlug/slug > asset_id/asset/token_id。
    
    参数
    ----------
    tr : dict
        交易记录。
    
    返回
    -------
    str
        市场标识符，兜底返回 "market"。
    """
    cond = _trade_pick_field(tr, "conditionId", "condition_id", "market", "market_id")
    slug = _trade_pick_field(tr, "eventSlug", "slug")
    if cond:
        return cond
    if slug:
        return slug
    asset = _trade_pick_field(tr, "asset_id", "asset", "token_id")
    return asset or "market"


def _resolve_trade_reason(tr):
    """
    解析交易的人类可读描述。
    
    按优先级查找：title / eventTitle / name / question > eventSlug / slug。
    
    参数
    ----------
    tr : dict
        交易记录。
    
    返回
    -------
    str
        交易描述文本，兜底返回 "market"。
    """
    title = _trade_pick_field(tr, "title", "eventTitle", "name", "question")
    if title:
        return title
    slug = _trade_pick_field(tr, "eventSlug", "slug")
    if slug:
        return slug
    return "market"


def _fetch_trade_activity(user, limit=500):
    """
    从 Polymarket Data API 拉取用户的交易活动记录。
    
    使用多组参数尝试请求 /activity 端点：
    1. {user, limit, offset}
    2. {user}（默认 limit）
    3. {address, limit, offset}
    4. {wallet, limit, offset}
    
    只要任一参数组合返回结果就停止尝试。结果会去重（按 id / tradeID /
    transaction_hash 去重），按时间戳排序。
    
    参数
    ----------
    user : str
        钱包地址。
    limit : int
        最大拉取数量（50~1000，默认 500）。
    
    返回
    -------
    list[dict]
        按时间排序的交易活动记录列表，失败返回 []。
    """
    if not user:
        return []
    lim = min(max(int(limit), 50), 1000)
    param_sets = [
        {"user": user, "limit": lim, "offset": 0},
        {"user": user},
        {"address": user, "limit": lim, "offset": 0},
        {"wallet": user, "limit": lim, "offset": 0},
    ]

    rows = []
    seen = set()
    for params in param_sets:
        data = _data_api_get("/activity", params)
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            kind = _trade_event_kind(item)
            if kind == "IGNORE":
                continue
            tid = _text_scalar(item.get("id") or item.get("tradeID") or item.get("transaction_hash") or item.get("transactionHash"))
            if not tid:
                tid = f"act-{kind}-{_trade_ts_ms(item)}-{_trade_usdc_size(item):.6f}-{_trade_market_key(item)}"
            if tid in seen:
                continue
            seen.add(tid)
            norm = dict(item)
            if norm.get("type") is not None:
                norm["type"] = str(norm.get("type")).upper()
            if norm.get("side") is not None:
                norm["side"] = str(norm.get("side")).upper()
            norm["id"] = tid
            rows.append(norm)
        if rows:
            break

    rows.sort(key=_trade_ts_ms)
    return rows


def _build_market_aggregated_trades(raw_trades):
    """
    将原始交易活动记录按市场聚合为汇总行。
    
    每个市场的聚合信息包含：
    - 买卖次数、总份额、总金额
    - 平均入场/出场价格
    - 已实现盈亏（卖+赎 - 买）
    - 持仓状态（OPEN=仅有买入, CLOSED=有卖出或赎回）
    - 方向推断（UP / DOWN / MIX）
    
    去重逻辑：按 _trade_market_key 分组，同一市场的 BUY/SELL/REDEEM
    汇总到同一条记录。
    
    参数
    ----------
    raw_trades : list[dict]
        原始交易活动记录列表（来自 _fetch_trade_activity）。
    
    返回
    -------
    list[dict]
        按 settle_time 排序的聚合行列表，每行包含：
        id / pair_id / direction / reason / buy_count / sell_count /
        redeem_count / buy_usdc / sell_usdc / redeem_usdc / size /
        entry_price_quote / exit_price_quote / order_time / settle_time /
        profit / result / status
    """
    groups = {}
    for tr in sorted((raw_trades or []), key=_trade_ts_ms):
        if not isinstance(tr, dict):
            continue
        kind = _trade_event_kind(tr)
        if kind == "IGNORE":
            continue

        price = _maybe_float(tr.get("price"))
        size = _maybe_float(tr.get("size_matched") or tr.get("size") or tr.get("original_size"))
        usdc_size = _trade_usdc_size(tr)
        if kind in ["BUY", "SELL"] and (price is None or size is None or size <= 0):
            continue
        if kind == "REDEEM" and usdc_size <= 0:
            continue

        key = _trade_market_key(tr)
        g = groups.get(key)
        if g is None:
            g = {
                "id": f"agg-{key}",
                "direction": _normalize_outcome_label(tr.get("outcome") or tr.get("direction")),
                "outcomes": set(),
                "reason": _resolve_trade_reason(tr),
                "buy_count": 0,
                "sell_count": 0,
                "redeem_count": 0,
                "buy_size": 0.0,
                "sell_size": 0.0,
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "redeem_notional": 0.0,
                "first_ts": tr.get("matchtime") or tr.get("match_time") or tr.get("timestamp") or tr.get("created_at") or tr.get("time"),
                "last_ts": tr.get("matchtime") or tr.get("match_time") or tr.get("timestamp") or tr.get("created_at") or tr.get("time"),
                "first_ts_ms": _trade_ts_ms(tr),
                "last_ts_ms": _trade_ts_ms(tr),
            }
            groups[key] = g

        ts_ms = _trade_ts_ms(tr)
        if ts_ms and ts_ms < g["first_ts_ms"]:
            g["first_ts_ms"] = ts_ms
            g["first_ts"] = tr.get("matchtime") or tr.get("match_time") or tr.get("timestamp") or tr.get("created_at") or tr.get("time")
        if ts_ms and ts_ms >= g["last_ts_ms"]:
            g["last_ts_ms"] = ts_ms
            g["last_ts"] = tr.get("matchtime") or tr.get("match_time") or tr.get("timestamp") or tr.get("created_at") or tr.get("time")

        outcome = _normalize_outcome_label(tr.get("outcome") or tr.get("direction"))
        if outcome and outcome != "-":
            g["outcomes"].add(outcome)

        if kind == "BUY":
            g["buy_count"] += 1
            g["buy_size"] += float(size)
            g["buy_notional"] += float(usdc_size)
        elif kind == "SELL":
            g["sell_count"] += 1
            g["sell_size"] += float(size)
            g["sell_notional"] += float(usdc_size)
        elif kind == "REDEEM":
            g["redeem_count"] += 1
            g["redeem_notional"] += float(usdc_size)

    rows = []
    for g in groups.values():
        if (g["buy_count"] + g["sell_count"] + g["redeem_count"]) <= 0:
            continue
        buy_avg = (g["buy_notional"] / g["buy_size"]) if g["buy_size"] > 1e-9 else None
        sell_avg = (g["sell_notional"] / g["sell_size"]) if g["sell_size"] > 1e-9 else None
        matched_size = min(g["buy_size"], g["sell_size"])
        pnl = g["sell_notional"] + g["redeem_notional"] - g["buy_notional"]

        if len(g["outcomes"]) == 1:
            g["direction"] = list(g["outcomes"])[0]
        elif len(g["outcomes"]) > 1:
            g["direction"] = "MIX"

        result = "CLOSED" if (g["sell_count"] > 0 or g["redeem_count"] > 0) else "OPEN"
        rows.append({
            "id": g["id"],
            "pair_id": g["id"],
            "direction": g["direction"],
            "reason": g["reason"],
            "buy_count": g["buy_count"],
            "sell_count": g["sell_count"],
            "redeem_count": g["redeem_count"],
            "buy_usdc": g["buy_notional"],
            "sell_usdc": g["sell_notional"],
            "redeem_usdc": g["redeem_notional"],
            "size": matched_size if matched_size > 1e-9 else max(g["buy_size"], g["sell_size"]),
            "entry_price_quote": buy_avg,
            "exit_price_quote": sell_avg,
            "order_time": g["first_ts"],
            "settle_time": g["last_ts"],
            "profit": pnl,
            "result": result,
            "status": "AGG",
        })

    rows.sort(key=lambda x: _trade_ts_ms({"timestamp": x.get("settle_time")}) if isinstance(x, dict) else 0)
    return rows


def _compute_wallet_realized_pnl(rows):
    """
    计算钱包维度已实现的累计盈亏。
    
    对每个已平仓仓位行累加 realizedPnl / realized_pnl 字段。
    
    参数
    ----------
    rows : list[dict]
        已平仓仓位列表（来自 _fetch_wallet_closed_positions）。
    
    返回
    -------
    float
        累计已实现盈亏（USDC）。
    """
    realized = 0.0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rp = _maybe_float(row.get("realizedPnl") if row.get("realizedPnl") is not None else row.get("realized_pnl"))
        if rp is not None:
            realized += rp
    return float(realized)


def _compute_wallet_unrealized_pnl(rows):
    """
    计算钱包维度未实现浮动盈亏。
    
    对每个持仓行计算 (当前市价 - 均价) × 持仓量 并累加。
    
    参数
    ----------
    rows : list[dict]
        当前持仓列表（来自 _fetch_wallet_positions）。
    
    返回
    -------
    float
        未实现盈亏（USDC），正数为浮盈。
    """
    unrealized = 0.0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        mark = _maybe_float(row.get("curPrice") if row.get("curPrice") is not None else row.get("cur_price"))
        avg = _maybe_float(row.get("avgPrice") if row.get("avgPrice") is not None else row.get("avg_price"))
        size = _maybe_float(row.get("size"))
        if mark is None or avg is None or size is None:
            continue
        unrealized += (mark - avg) * size
    return float(unrealized)


def _fetch_wallet_usdc_balance(user):
    """
    通过链上 RPC 查询钱包的 USDC.e 余额。
    
    使用 USDC.e 合约（在 Polygon 上的桥接 USDC）的 balanceOf 方法
    查询指定地址的余额，然后除以 decimals（通常为 6）得到 USDC 数额。
    
    依赖 web3 库和 POLYGON_RPC_URL 配置。
    
    参数
    ----------
    user : str
        钱包地址（checksummed hex string）。
    
    返回
    -------
    float or None
        USDC 余额（美元），查询失败或 web3 不可用时返回 None。
    """
    if not HAS_WEB3 or not Web3:
        return None
    rpc_url = (POLYGON_RPC_URL or "").strip()
    if not rpc_url or not user:
        return None
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 8}))
        if not w3.is_connected():
            return None
        usdc_addr = Web3.to_checksum_address(USDC_E_CONTRACT)
        user_addr = Web3.to_checksum_address(user)
        contract = w3.eth.contract(
            address=usdc_addr,
            abi=[
                {
                    "name": "balanceOf",
                    "type": "function",
                    "stateMutability": "view",
                    "inputs": [{"name": "account", "type": "address"}],
                    "outputs": [{"name": "", "type": "uint256"}],
                },
                {
                    "name": "decimals",
                    "type": "function",
                    "stateMutability": "view",
                    "inputs": [],
                    "outputs": [{"name": "", "type": "uint8"}],
                },
            ],
        )
        raw = contract.functions.balanceOf(user_addr).call()
        decimals = contract.functions.decimals().call()
        return float(raw) / (10 ** int(decimals))
    except Exception:
        return None


def _sync_dashboard_account_snapshot(user):
    """
    同步钱包全部账户快照到仪表盘（余额 + 持仓 + 历史 + 盈亏）。
    
    综合调用多个数据源：
    1. _fetch_wallet_positions - 当前持仓
    2. _fetch_wallet_closed_positions - 已平仓记录
    3. _fetch_trade_activity - 原始交易活动
    4. _fetch_wallet_usdc_balance - 链上 USDC 余额
    
    将所有数据归一化后写入 dashboard_state，包括：
    wallet_balance / wallet_positions / wallet_history / live_trades /
    live_positions_count / live_realized_pnl / live_unrealized_pnl /
    live_total_pnl
    
    参数
    ----------
    user : str
        钱包地址。
    
    返回
    -------
    bool
        同步成功返回 True，地址为空返回 False。
        注意：内部异常会被静默捕获，返回 True 不代表数据完全有效。
    """
    u = str(user or "").strip().lower()
    if not u:
        return False
    wallet_positions = _fetch_wallet_positions(u)
    wallet_closed = _fetch_wallet_closed_positions(u)
    wallet_history = _build_wallet_history_items(wallet_closed)
    raw_activity = _fetch_trade_activity(u, limit=500)
    agg_trades = _build_market_aggregated_trades(raw_activity)
    realized_pnl = _compute_wallet_realized_pnl(wallet_closed)
    unrealized_pnl = _compute_wallet_unrealized_pnl(wallet_positions)
    wallet_balance = _fetch_wallet_usdc_balance(u)
    # 模拟模式：用累计 PnL 推算模拟余额，替代链上真实余额
    if SIMULATION_MODE:
        from database import INITIAL_BALANCE_USDC
        sim_pnl = float(dashboard_state.get("cumulative_realized_pnl") or 0.0)
        wallet_balance = max(0.0, INITIAL_BALANCE_USDC + sim_pnl)
    _dashboard_set(
        wallet_balance=wallet_balance,
        wallet_positions=list(wallet_positions)[:120],
        wallet_history=list(wallet_history)[:200],
        live_trades=list(agg_trades)[-300:],
        live_positions_count=len(wallet_positions),
        live_realized_pnl=float(realized_pnl),
        live_unrealized_pnl=float(unrealized_pnl),
        live_total_pnl=float(realized_pnl + unrealized_pnl),
    )
    try:
        from database import insert_account_snapshot
        from config import _btc_market_minutes, SIMULATION_MODE
        live_open_count = len(wallet_positions)
        insert_account_snapshot(
            balance=float(wallet_balance) if wallet_balance is not None else None,
            available_balance=float(wallet_balance) if wallet_balance is not None else None,
            locked_balance=0.0,
            position_value=float(sum(p.get("price", 0) * p.get("size", 0) for p in wallet_positions if isinstance(p, dict))),
            total_equity=float(wallet_balance) + float(sum(p.get("price", 0) * p.get("size", 0) for p in wallet_positions if isinstance(p, dict))) if wallet_balance is not None else None,
            realized_pnl=float(realized_pnl),
            unrealized_pnl=float(unrealized_pnl),
            cumulative_pnl=float(realized_pnl + unrealized_pnl) if (realized_pnl is not None and unrealized_pnl is not None) else None,
            open_positions_count=live_open_count,
            btc_market_minutes=_btc_market_minutes,
            simulation=SIMULATION_MODE,
        )
    except Exception:
        pass
    return True


def _fetch_wallet_positions(user):
    """
    从 Polymarket Data API 获取钱包当前的未平仓持仓。
    
    请求 /positions 端点，过滤掉以下类型：
    - size <= 0 的空持仓
    - redeemable（已到期可赎回）的持仓
    - mergeable（可合并）的持仓
    
    参数
    ----------
    user : str
        钱包地址。
    
    返回
    -------
    list[dict]
        有效的持仓列表，失败返回 []。
    """
    if not user:
        return []
    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": user, "sizeThreshold": 0},
            proxies=PROXIES if PROXIES else None,
            timeout=12,
        )
        if r.status_code == 200:
            rows = r.json()
            if isinstance(rows, list):
                out = []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    size = _to_float(row.get("size"), 0)
                    if size <= 0:
                        continue
                    if _to_bool(row.get("redeemable")) or _to_bool(row.get("mergeable")):
                        continue
                    out.append(row)
                return out
    except Exception:
        pass
    return []


def _fetch_wallet_closed_positions(user):
    """
    从 Polymarket Data API 获取钱包已平仓/已结束的持仓历史。
    
    请求 /closed-positions 端点，按时间戳降序排列，
    最多返回 200 条记录。
    
    参数
    ----------
    user : str
        钱包地址。
    
    返回
    -------
    list[dict]
        已平仓记录列表，包含 realizedPnl / avgPrice / size 等字段，失败返回 []。
    """
    if not user:
        return []
    try:
        r = requests.get(
            f"{DATA_API}/closed-positions",
            params={
                "user": user,
                "limit": 200,
                "offset": 0,
                "sortBy": "TIMESTAMP",
                "sortDirection": "DESC",
            },
            proxies=PROXIES if PROXIES else None,
            timeout=12,
        )
        if r.status_code == 200:
            rows = r.json()
            if isinstance(rows, list):
                return rows
    except Exception:
        pass
    return []


def _build_wallet_history_items(rows):
    """
    将已平仓记录转为钱包历史条目列表（用于仪表盘展示）。
    
    每条记录包含：time / slug / action（固定 CLOSE）/ side / price /
    amount / order_id / status / reason / pnl。
    结果最多 200 条。
    
    参数
    ----------
    rows : list[dict]
        已平仓记录（来自 _fetch_wallet_closed_positions）。
    
    返回
    -------
    list[dict]
        归一化的历史条目列表。
    """
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        side = row.get("outcome") or row.get("side") or row.get("positionSide") or "-"
        item = {
            "time": row.get("endDate") or row.get("timestamp") or row.get("updatedAt") or "-",
            "slug": row.get("slug") or row.get("marketSlug") or row.get("question") or "-",
            "action": "CLOSE",
            "side": side,
            "price": row.get("avgPrice") if row.get("avgPrice") is not None else row.get("avg_price"),
            "amount": row.get("size"),
            "order_id": row.get("transactionHash") or row.get("id") or "",
            "status": "closed",
            "reason": "wallet_sync",
            "pnl": row.get("realizedPnl") if row.get("realizedPnl") is not None else row.get("realized_pnl"),
        }
        items.append(item)
    return items[:200]


def load_state():
    """
    从磁盘加载持久化的机器人状态（state.json）。
    
    如果状态文件不存在或损坏，返回一个空的规范化状态字典。
    加载后会自动调用 _normalize_state 确保所有必要字段存在。
    
    返回
    -------
    dict
        包含 position / pending_order / trade_history /
        cumulative_realized_pnl 等字段的完整状态字典。
    
    使用场景
    -------
    机器人启动时调用，从中断处恢复上次的运行状态。
    """
    if not os.path.exists(STATE_FILE):
        return _normalize_state({})
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return _normalize_state(json.load(f))
    except:
        return _normalize_state({})


def save_state(state):
    """
    将机器人状态和最新价格持久化写入磁盘（state.json）。
    
    保存内容包括：
    - 状态字典（持仓、订单、历史、盈亏）
    - 最新价格数据（ptb / chainlink / binance / up_price / down_price）
    - 最后更新时间戳
    
    写入前会自动调用 _normalize_state 确保数据完整性。
    
    参数
    ----------
    state : dict
        当前机器人状态。
    
    使用场景
    -------
    主循环中定期调用（通常每轮循环末尾），异常时重启可从中断点恢复。
    """
    try:
        state = _normalize_state(state)
        state["ptb"] = price_data.get("ptb")
        state["chainlink"] = price_data.get("btc")
        state["binance"] = price_data.get("binance")
        state["up_price"] = price_data.get("up_price")
        state["down_price"] = price_data.get("down_price")
        state["last_update"] = datetime.now().isoformat()

        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        log(f"保存状态失败: {e}", "ERR")
