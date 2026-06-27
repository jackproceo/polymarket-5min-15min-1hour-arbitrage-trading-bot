#!/usr/bin/env python3
"""
Polymarket BTC 5分钟/15分钟 涨跌自动交易 - 配置模块
所有配置常量、环境变量读取、API端点。
"""
import os
import sys
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 加载环境变量
load_dotenv(os.path.join(BASE_DIR, "config.env"))

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY, SELL
    HAS_CLOB = True
except:
    HAS_CLOB = False
    print("请安装: pip install py-clob-client")
    sys.exit(1)

try:
    import websocket
    HAS_WS = True
except:
    HAS_WS = False
    print("请安装: pip install websocket-client")
    sys.exit(1)

try:
    from web3 import Web3
    HAS_WEB3 = True
except:
    HAS_WEB3 = False

# ============== API端点 ==============
GAMMA_API = "https://gamma-api.polymarket.com"
CRYPTO_PRICE_API = "https://polymarket.com/api/crypto/crypto-price"
CRYPTO_PRICE_PTB_VARIANT = "fifteen"
BINANCE_WSS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
POLYMARKET_WSS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_API = "https://clob.polymarket.com"
RTDS_WS = "wss://ws-live-data.polymarket.com"  # Chainlink价格WebSocket
DATA_API = "https://data-api.polymarket.com"
CTF_CONTRACT = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
USDC_E_CONTRACT = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"

# 代理（可选）
HTTP_PROXY = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")

PROXIES = {}
if HTTP_PROXY:
    PROXIES["http"] = HTTP_PROXY
if HTTPS_PROXY:
    PROXIES["https"] = HTTPS_PROXY

# ============== 交易设置 ==============
AUTO_TRADE = os.getenv("AUTO_TRADE", "false").lower() == "true"
TRADE_AMOUNT = float(os.getenv("TRADE_AMOUNT", "5"))
SIMULATION_MODE = os.getenv("SIMULATION_MODE", "false").lower() == "true"

_tal_raw = (os.getenv("TRADING_ANALYSIS_LOG", "") or "").strip()
if not _tal_raw:
    TRADING_ANALYSIS_LOG = os.path.join(BASE_DIR, "trading_analysis.jsonl")
elif os.path.isabs(_tal_raw):
    TRADING_ANALYSIS_LOG = os.path.normpath(_tal_raw)
else:
    TRADING_ANALYSIS_LOG = os.path.normpath(os.path.join(BASE_DIR, _tal_raw))
TRADING_ANALYSIS_LOG = os.path.abspath(TRADING_ANALYSIS_LOG)

# ============== 触发规则 ==============
C1_TIME = int(os.getenv("CONDITION_1_TIME", "120"))
C1_DIFF = float(os.getenv("CONDITION_1_DIFF", "30"))
C1_MIN_PROB = float(os.getenv("CONDITION_1_MIN_PROB", "0.80"))
C1_MAX_PROB = float(os.getenv("CONDITION_1_MAX_PROB", "0.92"))

C2_TIME = int(os.getenv("CONDITION_2_TIME", "120"))
C2_DIFF = float(os.getenv("CONDITION_2_DIFF", "30"))
C2_MIN_PROB = float(os.getenv("CONDITION_2_MIN_PROB", "0.80"))
C2_MAX_PROB = float(os.getenv("CONDITION_2_MAX_PROB", "0.92"))

C3_TIME = int(os.getenv("CONDITION_3_TIME", "60"))
C3_DIFF = float(os.getenv("CONDITION_3_DIFF", "50"))
C3_MIN_PROB = float(os.getenv("CONDITION_3_MIN_PROB", "0.80"))
C3_MAX_PROB = float(os.getenv("CONDITION_3_MAX_PROB", "0.92"))

C4_TIME = int(os.getenv("CONDITION_4_TIME", "60"))
C4_DIFF = float(os.getenv("CONDITION_4_DIFF", "50"))
C4_MIN_PROB = float(os.getenv("CONDITION_4_MIN_PROB", "0.80"))
C4_MAX_PROB = float(os.getenv("CONDITION_4_MAX_PROB", "0.92"))

# ============== 订单/风控参数 ==============
ORDER_TIMEOUT_SEC = int(os.getenv("ORDER_TIMEOUT_SEC", "8"))
SLIPPAGE_THRESHOLD = float(os.getenv("SLIPPAGE_THRESHOLD", "0.05"))
MAX_RETRY_PER_MARKET = int(os.getenv("MAX_RETRY_PER_MARKET", "2"))
BUY_RETRY_STEP = max(0.001, float(os.getenv("BUY_RETRY_STEP", "0.01")))
STOP_LOSS_PROB_PCT = float(os.getenv("STOP_LOSS_PROB_PCT", "0.15"))
TAKE_PROFIT_RR = max(0.2, float(os.getenv("TAKE_PROFIT_RR", "1.0")))
TAKE_PROFIT_CAP = min(0.995, max(0.55, float(os.getenv("TAKE_PROFIT_CAP", "0.99"))))
TAKE_PROFIT_RETRY_STEP = max(0.001, float(os.getenv("TAKE_PROFIT_RETRY_STEP", "0.005")))
TAKE_PROFIT_RETRY_MAX = max(1, int(os.getenv("TAKE_PROFIT_RETRY_MAX", "3")))
MARKET_DATA_MAX_LAG_SEC = max(0.2, float(os.getenv("MARKET_DATA_MAX_LAG_SEC", "1.2")))
LOOP_INTERVAL_SEC = max(0.1, float(os.getenv("LOOP_INTERVAL_SEC", "0.25")))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "2"))

# ============== 自动赎回 ==============
AUTO_REDEEM = os.getenv("AUTO_REDEEM", "true").lower() == "true"
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "")
REDEEM_SCAN_INTERVAL = max(3, int(os.getenv("REDEEM_SCAN_INTERVAL", "15")))
REDEEM_RETRY_INTERVAL = max(10, int(os.getenv("REDEEM_RETRY_INTERVAL", "120")))
REDEEM_MAX_PER_SCAN = max(1, int(os.getenv("REDEEM_MAX_PER_SCAN", "2")))
REDEEM_PENDING_LOG_INTERVAL = max(10, int(os.getenv("REDEEM_PENDING_LOG_INTERVAL", "30")))
POLY_BUILDER_API_KEY = os.getenv("POLY_BUILDER_API_KEY", "")
POLY_BUILDER_SECRET = os.getenv("POLY_BUILDER_SECRET", "")
POLY_BUILDER_PASSPHRASE = os.getenv("POLY_BUILDER_PASSPHRASE", "")
RELAYER_URL = os.getenv("RELAYER_URL", "https://relayer-v2.polymarket.com")
RELAYER_TX_TYPE = os.getenv("RELAYER_TX_TYPE", "SAFE").upper()

# ============== 仪表盘 ==============
DASHBOARD_ACCOUNT_SYNC_SEC = max(10, int(os.getenv("DASHBOARD_ACCOUNT_SYNC_SEC", "20")))
MARKET_FOUND_LOG_INTERVAL = max(10, int(os.getenv("MARKET_FOUND_LOG_INTERVAL", "30")))
MARKET_META_REFRESH_SEC = max(2, int(os.getenv("MARKET_META_REFRESH_SEC", "5")))
WEB_ENABLED = os.getenv("WEB_ENABLED", "true").lower() == "true"
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "5080"))

# ============== BTC市场间隔 ==============
def _normalize_btc_market_minutes(m):
    """Polymarket支持5分钟和15分钟的BTC涨跌事件。"""
    try:
        n = int(float(str(m).strip()))
    except (TypeError, ValueError):
        return 15
    return 5 if n == 5 else 15

_btc_market_minutes = _normalize_btc_market_minutes(os.getenv("BTC_MARKET_MINUTES", "15"))
_market_interval_sec = _btc_market_minutes * 60

# 持久化状态文件
STATE_FILE = os.path.join(BASE_DIR, "state.json")
