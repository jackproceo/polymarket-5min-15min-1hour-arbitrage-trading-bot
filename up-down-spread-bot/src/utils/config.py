"""
统一配置管理 - 从 config.json 和 .env 读取配置，提供类型安全的访问。

用法：
    cfg = Config.load()
    cfg.get("strategy.price_max", 0.92)
    cfg.dry_run
    cfg.rpc_endpoints
"""
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from utils.logging_setup import get_logger

log = get_logger("config")


class Config:
    """统一配置，合并 config.json + .env。"""

    # ── 合约地址常量 ──────────────────────────────────────────
    CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
    NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

    CTF_ABI = [
        {"inputs": [{"name": "_owner", "type": "address"}, {"name": "_id", "type": "uint256"}],
         "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}],
         "stateMutability": "view", "type": "function"}
    ]

    ERC20_ABI = [
        {'constant': True, 'inputs': [{'name': '_owner', 'type': 'address'}],
         'name': 'balanceOf', 'outputs': [{'name': 'balance', 'type': 'uint256'}], 'type': 'function'},
        {'constant': True, 'inputs': [], 'name': 'decimals',
         'outputs': [{'name': '', 'type': 'uint8'}], 'type': 'function'}
    ]

    def __init__(self, data: Dict[str, Any]):
        self._data = data

    # ── 工厂方法 ──────────────────────────────────────────────

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Config":
        """加载 config.json + .env，返回 Config 实例。"""
        if config_path is None:
            config_path = str(Path(__file__).resolve().parent.parent.parent / "config" / "config.json")

        # 加载 .env
        project_root = Path(config_path).resolve().parent.parent
        env_path = project_root / ".env"
        if env_path.exists():
            load_dotenv(str(env_path))

        # 加载 config.json
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 解析 market_window → market_interval_sec
        cls._apply_market_window(data)

        return cls(data)

    @staticmethod
    def _apply_market_window(data: dict) -> None:
        """将 human-friendly market_window 转为 market_interval_sec。"""
        pm = data.setdefault("data_sources", {}).setdefault("polymarket", {})
        if "market_interval_sec" not in pm or pm["market_interval_sec"] <= 0:
            raw = str(pm.get("market_window", "15m")).strip().lower()
            pm["market_interval_sec"] = 300 if raw.startswith("5") else 900

    # ── .env 值 ───────────────────────────────────────────────

    @property
    def private_key(self) -> str:
        return os.getenv("PRIVATE_KEY", "")

    @property
    def clob_host(self) -> str:
        return os.getenv("CLOB_HOST", "https://clob.polymarket.com")

    @property
    def chain_id(self) -> int:
        return int(os.getenv("CHAIN_ID", "137"))

    @property
    def signature_type(self) -> int:
        return int(os.getenv("SIGNATURE_TYPE", "0"))

    @property
    def funder_address(self) -> str:
        return os.getenv("FUNDER_ADDRESS", "")

    @property
    def telegram_bot_token(self) -> str:
        return os.getenv("TELEGRAM_BOT_TOKEN", "")

    @property
    def telegram_chat_id(self) -> str:
        return os.getenv("TELEGRAM_CHAT_ID", "")

    @property
    def polymarket_api_key(self) -> str:
        return os.getenv("POLYMARKET_API_KEY", "")

    @property
    def polymarket_api_secret(self) -> str:
        return os.getenv("POLYMARKET_API_SECRET", "")

    @property
    def polymarket_api_passphrase(self) -> str:
        return os.getenv("POLYMARKET_API_PASSPHRASE", "")

    @property
    def dashboard_password(self) -> str:
        return os.getenv("DASHBOARD_PASSWORD", "")

    # ── config.json 通用访问 ──────────────────────────────────

    def get(self, key_path: str, default: Any = None) -> Any:
        """点号路径访问，如 cfg.get('strategy.price_max')。"""
        parts = key_path.split(".")
        current = self._data
        for part in parts:
            if not isinstance(current, dict):
                return default
            current = current.get(part)
            if current is None:
                return default
        return current

    # ── 常用快捷属性 ──────────────────────────────────────────

    @property
    def dry_run(self) -> bool:
        return self.get("safety.dry_run", True)

    @property
    def max_order_size_usd(self) -> float:
        return float(self.get("safety.max_order_size_usd", 150))

    @property
    def max_total_investment(self) -> float:
        return float(self.get("safety.max_total_investment", 1000))

    @property
    def coins(self) -> list:
        return ["btc", "eth", "sol", "xrp"]

    @property
    def strategy_bases(self) -> list:
        return ["late_v3"]

    @property
    def market_interval_sec(self) -> int:
        return int(self.get("data_sources.polymarket.market_interval_sec", 900))

    @property
    def rpc_endpoints(self) -> list:
        fallback = [os.getenv("RPC_URL", "https://polygon-rpc.com")]
        return self.get("execution.rpc_config.endpoints", fallback)

    @property
    def rpc_single_timeout(self) -> int:
        return self.get("execution.rpc_config.single_request_timeout_sec", 3)

    @property
    def rpc_parallel_timeout(self) -> int:
        return self.get("execution.rpc_config.parallel_timeout_sec", 5)

    @property
    def rpc_retry_attempts(self) -> int:
        return self.get("execution.rpc_config.retry_attempts", 2)

    @property
    def rpc_retry_delay(self) -> float:
        return float(self.get("execution.rpc_config.retry_delay_sec", 0.3))

    @property
    def rpc_parallel_enabled(self) -> bool:
        return self.get("execution.rpc_config.enable_parallel_requests", True)

    @property
    def chart_interval(self) -> int:
        return self.get("notifications.chart_every_n_markets", 10)

    @property
    def log_dir(self) -> str:
        return "logs"

    # ── 兼容旧代码的 dict 式访问 ──────────────────────────────

    def __getitem__(self, key: str) -> Any:
        """支持 config['key'] 语法，委托给 _data。"""
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        """支持 'key in config' 语法。"""
        return key in self._data

    def to_dict(self) -> Dict[str, Any]:
        """返回原始配置字典（兼容旧代码）。"""
        return self._data
