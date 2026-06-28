"""
订单执行器 - 带重试逻辑的真实交易引擎
基于 /root/clip/trade.py 的方法
"""
import os
import time
import json
import math
import requests
from pathlib import Path
from typing import Dict, Optional
from dataclasses import dataclass
import concurrent.futures

from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from safety_guard import SafetyGuard
import logging
from trade_logger import log_buy_attempt, log_buy_result, log_sell_attempt, log_sell_result
import threading

# 🔥 全局：按币种阻塞的市场（竞态条件保护）
# 此字典中的市场不能接收新的买单（止损/翻转止损活跃）
# 结构：{'btc': set(), 'eth': set(), 'sol': set(), 'xrp': set()}
_blocked_markets_lock = threading.Lock()
_blocked_markets = {
    'btc': set(),
    'eth': set(),
    'sol': set(),
    'xrp': set()
}


@dataclass
class OrderResult:
    """订单执行结果（带 FAK/FOK 重试支持）"""
    success: bool
    order_id: Optional[str] = None
    filled_size: float = 0.0           # 总成交合约数（可能少于目标！）
    filled_price: float = 0.0          # 每份合约的平均价格
    total_spent_usd: float = 0.0       # 总花费/收到 USD（所有尝试总和）
    attempts: int = 1                  # 尝试次数
    error: Optional[str] = None
    dry_run: bool = False
    elapsed_ms: int = 0
    remaining_balance: float = 0.0     # 🔥 修复4：卖出后区块链上的最终余额


class OrderExecutor:
    """在 Polymarket 上执行真实订单（方法来自 /root/clip）"""
    
    @staticmethod
    def block_market(market_slug: str, coin: str):
        """
        🔥 关键：阻塞市场，使其无法接收新的买单（按币种）
        当止损/翻转止损触发时立即调用此方法
        
        参数：
            market_slug: 市场标识符
            coin: 币种名称（'btc', 'eth', 'sol', 'xrp'）
        """
        with _blocked_markets_lock:
            if coin in _blocked_markets:
                _blocked_markets[coin].add(market_slug)
                print(f"[EXECUTOR] 🔒 MARKET BLOCKED: {coin.upper()} - {market_slug}")
            else:
                print(f"[EXECUTOR] ⚠️ Unknown coin: {coin}")
    
    @staticmethod
    def unblock_market(market_slug: str, coin: str):
        """
        解除市场阻塞（成功赎回后调用）
        
        参数：
            market_slug: 市场标识符
            coin: 币种名称（'btc', 'eth', 'sol', 'xrp'）
        """
        with _blocked_markets_lock:
            if coin in _blocked_markets and market_slug in _blocked_markets[coin]:
                _blocked_markets[coin].remove(market_slug)
                print(f"[EXECUTOR] 🔓 MARKET UNBLOCKED: {coin.upper()} - {market_slug}")
    
    @staticmethod
    def is_market_blocked(market_slug: str, coin: str) -> bool:
        """
        检查市场是否对特定币种被阻塞（原子检查）
        
        参数：
            market_slug: 市场标识符
            coin: 币种名称（'btc', 'eth', 'sol', 'xrp'）
            
        返回：
            如果该币种被阻塞则返回 True，否则返回 False
        """
        with _blocked_markets_lock:
            return coin in _blocked_markets and market_slug in _blocked_markets[coin]
    
    def __init__(self, safety_guard: SafetyGuard, config: Dict, data_feed=None):
        self.safety = safety_guard
        self.config = config
        self.data_feed = data_feed  # ✅ 用于访问 position_tracker
        
        # 初始化 CLOB 客户端
        self.client = None
        self.wallet_address = None
        
        if not self.safety.dry_run:
            try:
                from dotenv import load_dotenv
                # 从项目根目录加载 .env（不是当前目录）
                project_root = Path(__file__).parent.parent
                env_path = project_root / ".env"
                load_dotenv(env_path)
                
                # 加载 .env 后读取 PRIVATE_KEY
                self.private_key = os.getenv("PRIVATE_KEY", "")
                if not self.private_key:
                    raise ValueError("PRIVATE_KEY not found in .env")
                
                # 读取签名类型和资助者地址
                signature_type = int(os.getenv("SIGNATURE_TYPE", "0"))
                funder_address = os.getenv("FUNDER_ADDRESS", "")
                
                # 根据 SIGNATURE_TYPE 获取钱包地址
                # Type 0: 使用 PRIVATE_KEY 的地址（标准 EOA 钱包）
                # Type 1/2: 使用 FUNDER_ADDRESS（Polymarket 代理/智能合约钱包）
                if signature_type == 0:
                    self.wallet_address = Account.from_key(self.private_key).address
                    wallet_type = "EOA"
                else:
                    if not funder_address:
                        raise ValueError(f"SIGNATURE_TYPE={signature_type} requires FUNDER_ADDRESS in .env")
                    self.wallet_address = funder_address
                    wallet_type = f"Proxy (type {signature_type})"
                
                host = os.getenv("CLOB_HOST", "https://clob.polymarket.com")
                chain_id = int(os.getenv("CHAIN_ID", "137"))
                
                # Initialize ClobClient with signature type and funder if needed
                if signature_type == 0:
                    self.client = ClobClient(
                        host=host,
                        chain_id=chain_id,
                        key=self.private_key,
                        signature_type=0
                    )
                else:
                    self.client = ClobClient(
                        host=host,
                        chain_id=chain_id,
                        key=self.private_key,
                        signature_type=signature_type,
                        funder=funder_address
                    )
                # 🚨 关键：生成并设置 API 凭据
                print(f"[EXECUTOR] Generating API credentials...")
                creds = self.client.create_or_derive_api_creds()
                self.client.set_api_creds(creds)
                print(f"[EXECUTOR] ✓ API credentials set")
                
                print(f"[EXECUTOR] ✓ CLOB client initialized")
                print(f"[EXECUTOR]    Wallet: {self.wallet_address[:6]}...{self.wallet_address[-4:]}")
                print(f"[EXECUTOR]    Type: {wallet_type}")
            except Exception as e:
                print(f"[EXECUTOR] ❌ Failed to init CLOB client: {e}")
                self.safety.activate_emergency_stop("CLOB_INIT_FAILED")
        else:
            self.private_key = ""  # DRY_RUN - 不需要私钥
            print("[EXECUTOR] ✓ DRY_RUN mode (no real orders)")
        
        # 🔥 RPC 配置（多个端点，并行请求）
        self.rpc_config = config.get('execution', {}).get('rpc_config', {})
        
        # RPC 端点（如果配置中没有，则回退到环境变量）
        self.rpc_endpoints = self.rpc_config.get('endpoints', [
            os.getenv("RPC_URL", "https://polygon-rpc.com")
        ])
        
        # RPC 参数
        self.rpc_single_timeout = self.rpc_config.get('single_request_timeout_sec', 3)
        self.rpc_parallel_timeout = self.rpc_config.get('parallel_timeout_sec', 5)
        self.rpc_retry_attempts = self.rpc_config.get('retry_attempts', 2)
        self.rpc_retry_delay = self.rpc_config.get('retry_delay_sec', 0.3)
        self.rpc_parallel_enabled = self.rpc_config.get('enable_parallel_requests', True)
        
        # 记录 RPC 配置
        print(f"[EXECUTOR] {'='*60}")
        print(f"[EXECUTOR] 🌐 RPC CONFIGURATION:")
        print(f"[EXECUTOR]    Endpoints: {len(self.rpc_endpoints)}")
        for i, rpc in enumerate(self.rpc_endpoints, 1):
            rpc_short = rpc.split('/')[2][:30] if '://' in rpc else rpc[:30]
            print(f"[EXECUTOR]      #{i}: {rpc_short}...")
        print(f"[EXECUTOR]    Single timeout: {self.rpc_single_timeout}s")
        print(f"[EXECUTOR]    Parallel timeout: {self.rpc_parallel_timeout}s")
        print(f"[EXECUTOR]    Retry attempts: {self.rpc_retry_attempts}")
        print(f"[EXECUTOR]    Retry delay: {self.rpc_retry_delay}s")
        print(f"[EXECUTOR]    Parallel mode: {'ENABLED ⚡' if self.rpc_parallel_enabled else 'DISABLED'}")
        print(f"[EXECUTOR] {'='*60}\n")
        
        # CTF 合约，用于代币余额
        self.CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
        self.CTF_ABI = [
            {"inputs": [{"name": "_owner", "type": "address"}, {"name": "_id", "type": "uint256"}], 
             "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], 
             "stateMutability": "view", "type": "function"}
        ]
        
        # USDC 合约
        self.USDC_BRIDGED = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        self.USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
        self.ERC20_ABI = [
            {'constant': True, 'inputs': [{'name': '_owner', 'type': 'address'}], 
             'name': 'balanceOf', 'outputs': [{'name': 'balance', 'type': 'uint256'}], 'type': 'function'},
            {'constant': True, 'inputs': [], 'name': 'decimals', 
             'outputs': [{'name': '', 'type': 'uint8'}], 'type': 'function'}
        ]
        
        # 订单日志
        self.orders_log = Path("logs/orders.jsonl")
        self.orders_log.parent.mkdir(exist_ok=True)
        
        # 回调函数，用于跟踪余额变化
        self.balance_change_callback = None
        
        # 回调函数，用于检查市场关闭（竞态条件保护）
        self.market_closing_check_callback = None
    
    def set_balance_callback(self, callback):
        """
        设置余额变化的回调函数
        callback(amount, operation, is_absolute=False)
          - amount: float - 变化金额或绝对值
          - operation: str - 操作类型（'BUY', 'SELL', 'REDEEM', 'REDEEM_REFRESH'）
          - is_absolute: bool - 如果为 True，amount = 全部余额，否则为增量
        """
        self.balance_change_callback = callback
        print("[EXECUTOR] ✓ Balance change callback registered")
    
    def set_market_closing_check(self, callback):
        """
        设置检查市场关闭的回调函数（竞态条件保护）
        callback(market_slug: str) -> bool
          - 如果市场正在关闭且应阻塞买单则返回 True
          - 如果市场开放且允许买单则返回 False
        
        🔥 关键：阻止在止损/翻转止损触发后的买入
        """
        self.market_closing_check_callback = callback
        print("[EXECUTOR] ✓ Market closing check callback registered")
    
    def _log_redeem(self, market_slug: str, success: bool, amount: float, tx_hash: str = "", reason: str = ""):
        """将赎回操作记录到单独的文件"""
        try:
            import os
            from datetime import datetime
            
            log_file = "logs/redeem.log"
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            
            with open(log_file, 'a') as f:
                timestamp = datetime.now().isoformat()
                status = "SUCCESS" if success else "FAILED"
                f.write(f"{timestamp} | {market_slug} | {status} | ${amount:.2f} | {tx_hash} | {reason}\n")
        except Exception as e:
            print(f"[ERROR] Failed to log redeem: {e}")
    
    def get_wallet_usdc_balance(self) -> Optional[float]:
        """
        获取钱包 USDC 余额（桥接版 + 原生版）
        来自 /root/clip/trade.py 的方法副本
        """
        try:
            if not self.wallet_address and self.private_key:
                self.wallet_address = Account.from_key(self.private_key).address
            
            if not self.wallet_address:
                print("[EXECUTOR] ❌ No wallet address")
                return None
            
            # 使用第一个 RPC 端点查询钱包余额
            rpc_url = self.rpc_endpoints[0] if self.rpc_endpoints else "https://polygon-rpc.com"
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': self.rpc_single_timeout}))
            
            if not w3.is_connected():
                print("[EXECUTOR] ⚠ Cannot connect to RPC")
                return None
            
            total = 0.0
            
            # USDC.e（桥接版）- Polymarket 主要代币
            usdc_e = w3.eth.contract(
                address=Web3.to_checksum_address(self.USDC_BRIDGED), 
                abi=self.ERC20_ABI
            )
            balance_e = usdc_e.functions.balanceOf(self.wallet_address).call()
            decimals_e = usdc_e.functions.decimals().call()
            total += balance_e / (10 ** decimals_e)
            
            # 原生 USDC
            usdc_n = w3.eth.contract(
                address=Web3.to_checksum_address(self.USDC_NATIVE), 
                abi=self.ERC20_ABI
            )
            balance_n = usdc_n.functions.balanceOf(self.wallet_address).call()
            decimals_n = usdc_n.functions.decimals().call()
            total += balance_n / (10 ** decimals_n)
            
            print(f"[EXECUTOR] Wallet balance: ${total:.2f}")
            return total
            
        except Exception as e:
            print(f"[EXECUTOR] ❌ Balance query error: {e}")
            return None
    
    def get_pol_balance(self) -> Optional[float]:
        """
        获取 POL 余额（Polygon 原生代币）
        
        返回：
            POL 余额，出错时返回 None
        """
        try:
            if not self.wallet_address and self.private_key:
                self.wallet_address = Account.from_key(self.private_key).address
            
            if not self.wallet_address:
                print("[EXECUTOR] ❌ No wallet address")
                return None
            
            # 使用第一个 RPC 端点查询钱包余额
            rpc_url = self.rpc_endpoints[0] if self.rpc_endpoints else "https://polygon-rpc.com"
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': self.rpc_single_timeout}))
            
            if not w3.is_connected():
                print("[EXECUTOR] ⚠ Cannot connect to RPC")
                return None
            
            # 获取原生余额（以 Wei 为单位）
            balance_wei = w3.eth.get_balance(self.wallet_address)
            # 转换为 POL（1 POL = 10^18 Wei）
            balance_pol = balance_wei / 1e18
            
            print(f"[EXECUTOR] POL balance: {balance_pol:.4f}")
            return balance_pol
            
        except Exception as e:
            print(f"[EXECUTOR] ❌ POL balance query error: {e}")
            return None
    
    def get_blockchain_token_balance(self, token_id: str) -> Optional[float]:
        """
        ✅ 从区块链获取真实代币余额！
        
        使用并行请求到多个 RPC 端点：
        - 最大速度（取第一个成功响应 ~20-70ms）
        - 最大可靠性（如果一个 RPC 失败，换另一个）
        - 最小超时（从 60 秒降到 5-10 秒）
        
        参数：
            token_id: 代币 ID（例如 "52114319501245915516055106046884209969926127482827954674443846427813813222426"）
        
        返回：
            合约中的真实余额（float），如果所有 RPC 都不可用则返回 None
        """
        if self.safety.dry_run:
            return 0.0
        
        try:
            if not self.wallet_address and self.private_key:
                self.wallet_address = Account.from_key(self.private_key).address
            
            if not self.wallet_address:
                print("[EXECUTOR] ❌ No wallet address for token balance query")
                return None
            
            # 🔥 函数：向一个 RPC 端点发送请求
            def query_single_rpc(rpc_url: str, attempt: int = 1) -> Optional[float]:
                """从单个 RPC 端点查询余额"""
                try:
                    w3 = Web3(Web3.HTTPProvider(
                        rpc_url, 
                        request_kwargs={'timeout': self.rpc_single_timeout}
                    ))
                    
                    if not w3.is_connected():
                        return None
                    
                    ctf = w3.eth.contract(
                        address=Web3.to_checksum_address(self.CTF_ADDRESS), 
                        abi=self.CTF_ABI
                    )
                    
                    balance_raw = ctf.functions.balanceOf(
                        self.wallet_address, 
                        int(token_id)
                    ).call()
                    balance = balance_raw / 1e6  # Convert from raw to USDC decimals (6 decimals)
                    
                    rpc_short = rpc_url.split('/')[2][:20] if '://' in rpc_url else rpc_url[:20]
                    print(f"[EXECUTOR] ✅ RPC [{rpc_short}...] balance: {balance:.4f} contracts")
                    return balance
                    
                except Exception as e:
                    rpc_short = rpc_url.split('/')[2][:20] if '://' in rpc_url else rpc_url[:20]
                    print(f"[EXECUTOR] ⚠️  RPC [{rpc_short}...] failed: {type(e).__name__}")
                    return None
            
            # 🔥 重试循环，支持并行或串行请求
            for attempt in range(1, self.rpc_retry_attempts + 1):
                print(f"[EXECUTOR] 🔄 Balance query attempt {attempt}/{self.rpc_retry_attempts}...")
                
                if self.rpc_parallel_enabled and len(self.rpc_endpoints) > 1:
                    # 🚀 并行请求
                    print(f"[EXECUTOR] 🚀 Querying {len(self.rpc_endpoints)} RPCs in parallel...")
                    
                    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(self.rpc_endpoints))
                    
                    try:
                        # 同时启动所有请求
                        futures = {
                            executor.submit(query_single_rpc, rpc, attempt): rpc 
                            for rpc in self.rpc_endpoints
                        }
                        
                        # 等待第一个成功的返回结果
                        for future in concurrent.futures.as_completed(
                            futures, 
                            timeout=self.rpc_parallel_timeout
                        ):
                            try:
                                balance = future.result()
                                if balance is not None:
                                    # 🔥 关键：立即取消剩余的任务！
                                    executor.shutdown(wait=False, cancel_futures=True)
                                    print(f"[EXECUTOR] ✅ Got balance: {balance:.4f} contracts (token: {token_id[:16]}...)")
                                    return balance  # ← 提前退出！
                            except Exception:
                                continue
                        
                    except concurrent.futures.TimeoutError:
                        print(f"[EXECUTOR] ⏱️  All RPCs timeout after {self.rpc_parallel_timeout}s")
                    finally:
                        # 保证清理
                        executor.shutdown(wait=False, cancel_futures=True)
                
                else:
                    # 🔄 串行请求（回退或并行禁用时）
                    print(f"[EXECUTOR] 🔄 Querying RPCs sequentially...")
                    for rpc in self.rpc_endpoints:
                        balance = query_single_rpc(rpc, attempt)
                        if balance is not None:
                            print(f"[EXECUTOR] ✅ Got balance: {balance:.4f} contracts (token: {token_id[:16]}...)")
                            return balance  # ✅ 成功！
                
                # 未获取到余额 - 等待后重试
                if attempt < self.rpc_retry_attempts:
                    print(f"[EXECUTOR] ⏸️  Waiting {self.rpc_retry_delay}s before retry...")
                    time.sleep(self.rpc_retry_delay)
            
            # 所有尝试均失败
            print(f"[EXECUTOR] ❌ All {self.rpc_retry_attempts} attempts failed for all {len(self.rpc_endpoints)} RPC endpoints!")
            return None
            
        except Exception as e:
            print(f"[EXECUTOR] ❌ CRITICAL ERROR in get_blockchain_token_balance: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _get_fresh_bid_price(self, market_slug: str, side: str) -> Optional[float]:
        """
        ✅ 从 WebSocket 数据源获取最新买入价！
        
        用于在每次 FAK 尝试时更新卖出价格。
        价格通过 Polymarket WebSocket 实时更新（无延迟或 API 请求）。
        
        参数：
            market_slug: 市场标识（例如 'btc-updown-15m-1768134600'）
            side: 'UP' 或 'DOWN'
        
        返回：
            最新买入价（float），如果不可用则返回 None
        """
        if not self.data_feed:
            return None
        
        try:
            # 从 market_slug 确定币种
            # 格式：'btc-updown-15m-1768134600' -> 'btc'
            coin = market_slug.split('-')[0].lower()
            
            if coin not in ['btc', 'eth', 'sol', 'xrp']:
                print(f"[EXECUTOR] ⚠️ Unknown coin in market_slug: {market_slug}")
                return None
            
            # Get state from WebSocket
            market_state = self.data_feed.get_state(coin)
            
            if not market_state:
                return None
            
            # 获取所需方向的 BID
            if side == 'UP':
                bid = market_state.get('up_bid')
            elif side == 'DOWN':
                bid = market_state.get('down_bid')
            else:
                print(f"[EXECUTOR] ⚠️ Invalid side: {side}")
                return None
            
            # 验证 BID 价格
            if bid and 0.01 < bid < 0.99:
                return bid
            else:
                return None
                
        except Exception as e:
            print(f"[EXECUTOR] ⚠️ Failed to get fresh BID: {e}")
            return None
    
    def place_buy_order(self, market_slug: str, token_id: str, side: str, 
                       contracts: int, ask_price: float, coin: str = None) -> OrderResult:
        """
        使用 FAK 部分成交跟踪下买单
        
        🚨 关键：FAK 订单可能部分成交！
        - 通过 takingAmount/makingAmount 跟踪实际成交
        - 使用 max_fak_attempts 次尝试完成目标
        - 四舍五入到 2 位小数，最低 $1.00
        
        参数：
            market_slug: 市场标识
            token_id: 要买入的代币 ID
            side: 'UP' 或 'DOWN'
            contracts: 目标合约数（可能无法达到！）
            ask_price: 当前卖价
            coin: 币种名称（'btc', 'eth', 'sol', 'xrp'）用于按币种阻塞
            
        返回：
            OrderResult（filled_size 可能小于 contracts！）
        """
        # 如果未提供，从 market_slug 确定币种
        if not coin:
            for c in ['btc', 'eth', 'sol', 'xrp']:
                if f'{c}-updown-' in market_slug:
                    coin = c
                    break
        # 从配置读取参数！
        exec_config = self.config.get('execution', {}).get('buy', {})
        MAX_FAK_ATTEMPTS = exec_config.get('max_fak_attempts', 3)
        RETRY_DELAY = exec_config.get('retry_delay_sec', 0.3)
        MIN_ORDER_USD = exec_config.get('min_order_usd', 1.00)
        TARGET_FILL_PERCENT = exec_config.get('target_fill_percent', 95.0) / 100.0
        
        # 安全检查
        allowed, reason = self.safety.check_order_allowed(
            side=side,
            contracts=contracts,
            price=ask_price,
            market_slug=market_slug
        )
        
        if not allowed:
            # DRY_RUN - 模拟成功
            if reason == "DRY_RUN_MODE":
                result = OrderResult(
                    success=True,
                    order_id=f"DRY_RUN_{int(time.time())}",
                    filled_size=contracts,
                    filled_price=ask_price,
                total_spent_usd=round(contracts * ask_price, 2),
                attempts=1,
                dry_run=True
                )
                self._log_order(market_slug, side, contracts, ask_price, result, "BUY", fak_attempt=1)
                return result
            else:
                # 其他阻止原因
                result = OrderResult(
                    success=False,
                    error=reason
                )
                print(f"[EXECUTOR] ❌ Order blocked: {reason}")
                return result
        
        # 🔥 真实交易，带 FAK 部分成交跟踪
        target_contracts = contracts
        
        # 市价单定价：增加滑点以确保成交
        SLIPPAGE_BUY = 0.05  # 高于卖价 5%（安全边际，交易所会在可能时以更低价格买入）
        aggressive_price = ask_price * (1 + SLIPPAGE_BUY)
        normalized_price = math.ceil(aggressive_price * 100) / 100  # 向上取整到 2 位小数
        
        total_filled_contracts = 0.0
        total_spent_usd = 0.0
        start_time_total = time.time()
        
        # 🔥 竞态条件保护 #1：原子检查（最高优先级！）
        # 在任何操作之前先检查此币种的 blocked_markets
        if coin and OrderExecutor.is_market_blocked(market_slug, coin):
            print(f"[EXECUTOR] 🛑 BLOCKED (ATOMIC): {coin.upper()} market {market_slug} is blocked!")
            
            from trade_logger import trades_logger
            trades_logger.warning(
                f"BUY_BLOCKED_ATOMIC | Market: {market_slug} | Coin: {coin.upper()} | Side: {side} | "
                f"Contracts: {contracts} | Reason: Per-coin block active (exit triggered)"
            )
            
            return OrderResult(
                success=False,
                error="MARKET_BLOCKED_FOR_COIN",
                remaining_balance=0.0
            )
        
        print(f"[EXECUTOR] 🎯 BUY TARGET: {target_contracts} {side} @ ${normalized_price:.2f} (ASK ${ask_price:.2f} +{SLIPPAGE_BUY*100:.0f}% slippage, max {MAX_FAK_ATTEMPTS} FAK)")
        
        # 🔥 竞态条件保护 #2：回调检查（次要）
        if coin and hasattr(self, 'market_closing_check_callback') and self.market_closing_check_callback:
            if self.market_closing_check_callback(market_slug, coin):
                print(f"[EXECUTOR] 🛑 BLOCKED: {coin.upper()} market {market_slug} is closing (stop-loss/flip-stop triggered)")
                
                # 📝 日志：竞态条件阻塞
                from trade_logger import trades_logger
                trades_logger.warning(
                    f"MARKET_CLOSING_BLOCKED | Market: {market_slug} | Coin: {coin.upper()} | Side: {side} | "
                    f"Contracts: {contracts} | Reason: Per-coin stop-loss or flip-stop active"
                )
                
                return OrderResult(
                    success=False,
                    error="MARKET_CLOSING_RACE_CONDITION_BLOCKED",
                    remaining_balance=0.0
                )
        
        for fak_attempt in range(1, MAX_FAK_ATTEMPTS + 1):
            try:
                # 🔥 竞态条件保护 #1：FAK 循环中的原子检查
                if coin and OrderExecutor.is_market_blocked(market_slug, coin):
                    print(f"[EXECUTOR] 🛑 BLOCKED (ATOMIC in FAK {fak_attempt}): {coin.upper()} market {market_slug}")
                    break  # 立即退出 FAK 循环
                
                # 🔥 竞态条件保护 #2：回调检查
                if coin and hasattr(self, 'market_closing_check_callback') and self.market_closing_check_callback:
                    if self.market_closing_check_callback(market_slug, coin):
                        print(f"[EXECUTOR] 🛑 BLOCKED (attempt {fak_attempt}): {coin.upper()} market {market_slug} is closing")
                        
                        # 📝 日志：买入循环中的竞态条件阻塞
                        from trade_logger import trades_logger
                        trades_logger.warning(
                            f"BUY_BLOCKED_DURING_FAK | Market: {market_slug} | Coin: {coin.upper()} | Side: {side} | "
                            f"FAK Attempt: {fak_attempt}/{MAX_FAK_ATTEMPTS} | Reason: Per-coin market closing"
                        )
                        
                        break  # Exit loop, return what we've accumulated
                
                # 还需要买多少？
                remaining_contracts = target_contracts - total_filled_contracts
                
                # 检查：是否已买够？
                if remaining_contracts <= 0.01 or total_filled_contracts >= target_contracts * TARGET_FILL_PERCENT:
                    fill_pct = (total_filled_contracts / target_contracts) * 100
                    print(f"[EXECUTOR] ✅ BUY TARGET REACHED: {total_filled_contracts:.2f}/{target_contracts} ({fill_pct:.1f}%)")
                    break
                
                # 🚨 关键：将合约数量转换为美元！
                remaining_usd = remaining_contracts * normalized_price
                order_size_usd = round(remaining_usd, 2)  # 四舍五入到 2 位小数！
                
                # 🚨 最低 $1.00
                if order_size_usd < MIN_ORDER_USD:
                    print(f"[EXECUTOR] ⚠ Remaining ${order_size_usd:.2f} < ${MIN_ORDER_USD:.2f} minimum, stopping")
                    break
                
                log_buy_attempt(market_slug, side, round(remaining_contracts, 2), normalized_price, fak_attempt, MAX_FAK_ATTEMPTS)
                print(f"[EXECUTOR] [FAK {fak_attempt}/{MAX_FAK_ATTEMPTS}] Ordering {round(remaining_contracts, 2)} contracts of {side} @ ${normalized_price:.2f} (=${order_size_usd:.2f})")
                
                start_time = time.time()
                
                # 创建 FAK 订单（金额以美元计！）
                order_args = OrderArgs(
                    price=normalized_price,
                    size=round(remaining_contracts, 2),  # 🚨 以合约为单位！
                    side=BUY,
                    token_id=token_id,
                )
                
                signed_order = self.client.create_order(order_args)
                api_result = self.client.post_order(signed_order, OrderType.FAK)
                
                elapsed_ms = int((time.time() - start_time) * 1000)
                
                if api_result.get("success"):
                    # 🚨 读取实际成交！
                    taking_amount = float(api_result.get("takingAmount", 0))  # 收到的合约数
                    making_amount = float(api_result.get("makingAmount", 0))  # 花费的美元数
                    order_id = api_result.get("orderID", "N/A")
                    
                    # 更新计数器
                    total_filled_contracts += taking_amount
                    total_spent_usd += making_amount
                    log_buy_result(market_slug, side, target_contracts, total_filled_contracts, target_contracts * normalized_price, total_spent_usd, True, fak_attempts=fak_attempt, elapsed_ms=elapsed_ms)
                    
                    fill_pct = (total_filled_contracts / target_contracts) * 100
                    print(f"[EXECUTOR]   → Filled {taking_amount:.2f} contracts for ${making_amount:.2f} ({elapsed_ms}ms)")
                    print(f"[EXECUTOR]   → Progress: {total_filled_contracts:.2f}/{target_contracts} ({fill_pct:.1f}%)")
                    
                    # 写入 SafetyGuard
                    self.safety.record_order(
                        side=side,
                        contracts=taking_amount,
                        price=normalized_price,
                        market_slug=market_slug,
                        order_id=order_id
                    )
                    
                    # 分别记录每次 FAK 尝试
                    partial_result = OrderResult(
                        success=True,
                        order_id=order_id,
                        filled_size=taking_amount,
                        filled_price=normalized_price,
                    total_spent_usd=making_amount,
                    attempts=fak_attempt,
                    elapsed_ms=elapsed_ms
                    )
                    self._log_order(market_slug, side, taking_amount, normalized_price, partial_result, "BUY", fak_attempt=fak_attempt)
                    
                else:
                    error_msg = api_result.get("errorMsg", "Unknown")
                    print(f"[EXECUTOR] ⚠ [FAK {fak_attempt}] FAILED: {error_msg}")
                    print(f"[EXECUTOR]   🔍 Full API response: {json.dumps(api_result, indent=2)}")
                    print(f"[EXECUTOR]   📋 Sent OrderArgs: price=${sell_price:.2f}, size={remaining_contracts:.2f} contracts, side=SELL, token={token_id}")
                
                # 暂停后继续下一次 FAK 尝试
                if fak_attempt < MAX_FAK_ATTEMPTS:
                    time.sleep(RETRY_DELAY)
                    
            except Exception as e:
                print(f"[EXECUTOR] ❌ [FAK {fak_attempt}] Exception: {e}")
                    # 记录失败的尝试
                if fak_attempt < MAX_FAK_ATTEMPTS:
                    time.sleep(RETRY_DELAY)
        
        # 所有 FAK 尝试之后 - 停止！
        elapsed_total_ms = int((time.time() - start_time_total) * 1000)
        
        if total_filled_contracts > 0:
            fill_pct = (total_filled_contracts / target_contracts) * 100
            avg_price = total_spent_usd / total_filled_contracts if total_filled_contracts > 0 else normalized_price
            
            result = OrderResult(
                success=True,
                filled_size=total_filled_contracts,
                filled_price=avg_price,
            total_spent_usd=total_spent_usd,
            attempts=fak_attempt,
            elapsed_ms=elapsed_total_ms
            )
            
            if fill_pct >= TARGET_FILL_PERCENT * 100:
                print(f"[EXECUTOR] ✅ BUY SUCCESS: {total_filled_contracts:.2f}/{target_contracts} contracts, ${total_spent_usd:.2f}")
            else:
                print(f"[EXECUTOR] ⚠ PARTIAL BUY: {total_filled_contracts:.2f}/{target_contracts} ({fill_pct:.1f}%), ${total_spent_usd:.2f}")
            
            # 通知余额变化（花费的资金）
            if self.balance_change_callback and not result.dry_run:
                try:
                    self.balance_change_callback(-total_spent_usd, "BUY")
                except Exception as e:
                    print(f"[EXECUTOR] ⚠ Balance callback error: {e}")
            
            return result
        else:
            log_buy_result(market_slug, side, target_contracts, total_filled_contracts, target_contracts * normalized_price, total_spent_usd, False, error="NO_FILL_AFTER_FAK", fak_attempts=MAX_FAK_ATTEMPTS)
            print(f"[EXECUTOR] ❌ BUY FAILED: No fills after {MAX_FAK_ATTEMPTS} FAK attempts")
            return OrderResult(
                success=False, 
                error=f"NO_FILL_AFTER_{MAX_FAK_ATTEMPTS}_FAK",
                attempts=fak_attempt
            )
    
    def sell_position(self, market_slug: str, token_id: str, side: str, 
                     contracts: float, bid_price: float = None) -> OrderResult:
        """
        使用 FOK 分块策略卖出仓位
        
        🔥 FOK 分块 = 拆分为多个块 + 每个块使用 Fill-Or-Kill
        
        ✅ 策略：
        1. 从区块链查询余额（开始时一次）
        2. 按 CHUNK_SIZE（默认：40 合约）拆分为块
        3. 依次以 FOK 订单 @ $0.01 发送每个块
        4. 失败时立即重试（最多 MAX_CHUNK_RETRIES 次）
        5. 成功块之间 CHUNK_DELAY 暂停
        6. 最终清理：检查余额并卖出剩余部分
        
        ✅ 优势：
        - 简单：FOK = 全有或全无（无部分成交）
        - 可靠：小块总能通过
        - 速度：164 合约 ≈ 4.5 秒
        - 可预测：清楚知道发送了什么
        
        ✅ 适用于所有 4 个币种（BTC、ETH、SOL、XRP）
        ✅ 适用于两种卖出类型（止损 + 翻转止损）
        
        参数：
            market_slug: 市场标识（任意币种）
            token_id: 要卖出的代币 ID
            side: 'UP' 或 'DOWN'
            contracts: 合约数量（作参考，会重新请求）
            bid_price: 当前买入价（不使用，始终 $0.01）
            
        返回：
            OrderResult（如果卖出 ≥99% 或仅剩粉尘则 success=True）
        """
        # ═══════════════════════════════════════════════════════════
        # 🔥 从配置读取所有参数
        # ═══════════════════════════════════════════════════════════
        exec_config = self.config.get('execution', {}).get('sell', {})
        
        STRATEGY = exec_config.get('strategy', 'FOK_CHUNKED')
        CHUNK_SIZE = exec_config.get('chunk_size', 40)
        CHUNK_DELAY = exec_config.get('chunk_delay_sec', 0.5)
        MAX_CHUNK_RETRIES = exec_config.get('max_chunk_retries', 3)
        PRICE = exec_config.get('price', 0.01)
        MIN_DUST_THRESHOLD = exec_config.get('min_dust_threshold', 0.1)
        SWEEP_MAX_ATTEMPTS = exec_config.get('sweep_max_attempts', 3)
        SWEEP_RETRY_DELAY = exec_config.get('sweep_retry_delay_sec', 1.0)
        
        # 记录参数
        print(f"\n[EXECUTOR] {'='*60}")
        print(f"[EXECUTOR] 🔥 FOK CHUNKED SELL STARTED")
        print(f"[EXECUTOR] {'='*60}")
        print(f"[EXECUTOR] Market: {market_slug}")
        print(f"[EXECUTOR] Side: {side}")
        print(f"[EXECUTOR] Tracked Position: {contracts:.2f} contracts")
        print(f"[EXECUTOR] ")
        print(f"[EXECUTOR] ⚙️  CONFIG:")
        print(f"[EXECUTOR]    Strategy: {STRATEGY}")
        print(f"[EXECUTOR]    Chunk Size: {CHUNK_SIZE} contracts")
        print(f"[EXECUTOR]    Chunk Delay: {CHUNK_DELAY}s")
        print(f"[EXECUTOR]    Max Chunk Retries: {MAX_CHUNK_RETRIES}")
        print(f"[EXECUTOR]    Price: ${PRICE:.2f} (aggressive market order)")
        print(f"[EXECUTOR]    Dust Threshold: {MIN_DUST_THRESHOLD}")
        print(f"[EXECUTOR] {'='*60}\n")
        
        # ═══════════════════════════════════════════════════════════
        # 步骤 1：从区块链获取初始余额
        # ═══════════════════════════════════════════════════════════
        print(f"[EXECUTOR] [STEP 1] 📊 Fetching balance from blockchain...")
        
        initial_balance = self.get_blockchain_token_balance(token_id)
        
        if initial_balance is None:
            error_msg = "RPC_UNAVAILABLE_CANNOT_GET_BALANCE"
            print(f"[EXECUTOR] ❌ CRITICAL: Cannot get balance from blockchain!")
            self._send_telegram_alert(
                f"🚨 SELL FAILED: Cannot get balance!\n"
                f"\nMarket: {market_slug}"
                f"\nSide: {side}"
                f"\nTracked: {contracts:.2f}"
                f"\nReason: RPC unavailable"
            )
            return OrderResult(success=False, error=error_msg)
        
        print(f"[EXECUTOR] ✓ Blockchain balance: {initial_balance:.4f} contracts")
        
        # 检查：余额是否已接近 0
        if initial_balance < MIN_DUST_THRESHOLD:
            print(f"[EXECUTOR] ✓ Balance below dust threshold ({MIN_DUST_THRESHOLD}), nothing to sell")
            return OrderResult(
                success=True,
                filled_size=0.0,
                total_spent_usd=0.0,
                error="BALANCE_ALREADY_ZERO",
                remaining_balance=0.0
            )
        
        # ═══════════════════════════════════════════════════════════
        # 步骤 2：拆分为块
        # ═══════════════════════════════════════════════════════════
        print(f"\n[EXECUTOR] [STEP 2] 🔪 Splitting into chunks...")
        
        chunks = []
        remaining = initial_balance
        chunk_num = 0
        
        while remaining > MIN_DUST_THRESHOLD:
            chunk_num += 1
            chunk = min(CHUNK_SIZE, remaining)
            chunks.append(chunk)
            remaining -= chunk
            print(f"[EXECUTOR]    Chunk #{chunk_num}: {chunk:.2f} contracts")
        
        print(f"[EXECUTOR] ✓ Total chunks: {len(chunks)}")
        print(f"[EXECUTOR] ✓ Total to sell: {sum(chunks):.2f} contracts")
        print(f"[EXECUTOR] ✓ Estimated time: {len(chunks) * CHUNK_DELAY:.1f}s")
        
        # ═══════════════════════════════════════════════════════════
        # 步骤 3：发送每个块并立即重试
        # ═══════════════════════════════════════════════════════════
        print(f"\n[EXECUTOR] [STEP 3] 🚀 Sending FOK orders...")
        
        total_sold = 0.0
        total_received_usd = 0.0
        successful_chunks = 0
        failed_chunks = []
        
        start_time = time.time()
        
        for i, chunk in enumerate(chunks, start=1):
            chunk_sold = False
            chunk_start = time.time()
            
            # ════════════════════════════════════════════════════════
            # 重试循环：失败时立即重试（无暂停！）
            # ════════════════════════════════════════════════════════
            for attempt in range(1, MAX_CHUNK_RETRIES + 1):
                print(f"\n[EXECUTOR] [FOK {i}/{len(chunks)}] Attempt {attempt}/{MAX_CHUNK_RETRIES}")
                print(f"[EXECUTOR]    Selling {chunk:.2f} contracts @ ${PRICE:.2f}...")
                
                # 📝 日志：尝试卖出块
                log_sell_attempt(
                    market_slug=market_slug,
                    side=f"{side}_CHUNK_{i}/{len(chunks)}",
                    contracts=chunk,
                    price=PRICE,
                    attempt=attempt,
                    max_attempts=MAX_CHUNK_RETRIES
                )
                
                attempt_start = time.time()
                
                # 检查 DRY RUN
                if self.safety.dry_run:
                    print(f"[EXECUTOR] [FOK {i}] ✓ DRY_RUN: Simulated success")
                    total_sold += chunk
                    total_received_usd += chunk * PRICE
                    successful_chunks += 1
                    chunk_sold = True
                    break
                
                # Send FOK order
                try:
                    order_args = OrderArgs(
                        price=PRICE,
                        size=chunk,
                        side=SELL,
                        token_id=token_id,
                    )
                    
                    signed_order = self.client.create_order(order_args)
                    api_result = self.client.post_order(signed_order, OrderType.FOK)  # 🔥 FOK!
                    
                    attempt_elapsed = int((time.time() - attempt_start) * 1000)
                    
                    if api_result and api_result.get("success"):
                        # Check errorMsg (success=true doesn't mean order was filled!)
                        error_msg = api_result.get("errorMsg", "")
                        taking_amount = float(api_result.get("takingAmount", 0))  # USD received
                        making_amount = float(api_result.get("makingAmount", 0))  # Contracts sold
                        order_id = api_result.get("orderID", "N/A")
                        
                        # 🔥 关键检查：FOK_ORDER_NOT_FILLED 或金额为 0
                        if error_msg and ("FOK_ORDER_NOT_FILLED" in error_msg or "not filled" in error_msg.lower()):
                            # FOK 无法完全成交 - 这是失败！
                            print(f"[EXECUTOR] [FOK {i}] ❌ NOT FILLED (attempt {attempt}): {error_msg}")
                            if attempt == MAX_CHUNK_RETRIES:
                                log_sell_result(
                                    market_slug=market_slug,
                                    side=side,
                                    requested_contracts=chunk,
                                    sold_contracts=0.0,
                                    requested_usd=chunk * PRICE,
                                    received_usd=0.0,
                                    success=False,
                                    error=error_msg,
                                    fak_attempts=attempt,
                                    elapsed_ms=attempt_elapsed
                                )
                            # Continue retry loop
                            
                        elif taking_amount == 0 or making_amount == 0:
                            # 金额为 0 表示未卖出任何东西！
                            print(f"[EXECUTOR] [FOK {i}] ❌ ZERO FILL (attempt {attempt}): taking={taking_amount}, making={making_amount}")
                            if attempt == MAX_CHUNK_RETRIES:
                                log_sell_result(
                                    market_slug=market_slug,
                                    side=side,
                                    requested_contracts=chunk,
                                    sold_contracts=0.0,
                                    requested_usd=chunk * PRICE,
                                    received_usd=0.0,
                                    success=False,
                                    error=f"ZERO_FILL: taking={taking_amount}, making={making_amount}",
                                    fak_attempts=attempt,
                                    elapsed_ms=attempt_elapsed
                                )
                            # Continue retry loop
                            
                        else:
                            # ✅ 真正成功 - 有成交！
                            filled = making_amount
                            received = taking_amount

                            total_sold += filled
                            total_received_usd += received
                            successful_chunks += 1
                            chunk_sold = True
                            
                            print(f"[EXECUTOR] [FOK {i}] ✅ SUCCESS (attempt {attempt})!")
                            print(f"[EXECUTOR]    Sold: {filled:.2f} contracts")
                            print(f"[EXECUTOR]    Received: ${received:.2f}")
                            print(f"[EXECUTOR]    Time: {attempt_elapsed}ms")
                            if error_msg:
                                print(f"[EXECUTOR]    Warning: {error_msg}")
                            
                            # 记录成功
                            try:
                                log_sell_result(
                                    market_slug=market_slug,
                                    side=side,
                                    requested_contracts=chunk,
                                    sold_contracts=filled,
                                    requested_usd=chunk * PRICE,
                                    received_usd=received,
                                    success=True,
                                    fak_attempts=attempt,
                                    elapsed_ms=attempt_elapsed
                                )
                            except Exception as log_err:
                                print(f"[EXECUTOR] ⚠️ Logging error: {log_err}")
                            
                            # 通知余额变化
                            if self.balance_change_callback:
                                try:
                                    self.balance_change_callback(received, "SELL")
                                except Exception as cb_err:
                                    print(f"[EXECUTOR] ⚠️ Balance callback error: {cb_err}")
                            
                            break  # ← Exit retry loop, go to next chunk
                    
                    else:
                        # ❌ 失败 → 立即重试（无暂停！）
                        error = api_result.get("errorMsg", "UNKNOWN") if api_result else "NO_API_RESPONSE"
                        print(f"[EXECUTOR] [FOK {i}] ❌ FAILED (attempt {attempt}): {error}")
                        
                        if attempt == MAX_CHUNK_RETRIES:
                            # Last attempt - log it
                            try:
                                log_sell_result(
                                    market_slug=market_slug,
                                    side=side,
                                    requested_contracts=chunk,
                                    sold_contracts=0.0,
                                    requested_usd=chunk * PRICE,
                                    received_usd=0.0,
                                    success=False,
                                    error=error,
                                    fak_attempts=attempt,
                                    elapsed_ms=attempt_elapsed
                                )
                            except Exception as log_err:
                                print(f"[EXECUTOR] ⚠️ Logging error: {log_err}")
                        # NO time.sleep() - immediately next attempt!
                
                except Exception as e:
                    print(f"[EXECUTOR] [FOK {i}] ❌ EXCEPTION (attempt {attempt}): {e}")
                    if attempt == MAX_CHUNK_RETRIES:
                        chunk_sold = False
            
            # If not sold after all attempts
            if not chunk_sold:
                chunk_elapsed = int((time.time() - chunk_start) * 1000)
                print(f"[EXECUTOR] [FOK {i}] ⚠️  FAILED after {MAX_CHUNK_RETRIES} attempts ({chunk_elapsed}ms)")
                failed_chunks.append({'chunk': i, 'size': chunk, 'attempts': MAX_CHUNK_RETRIES})
            
            # ════════════════════════════════════════════════════════
            # PAUSE BEFORE NEXT CHUNK
            # (only if this is not the last chunk)
            # ════════════════════════════════════════════════════════
            if i < len(chunks):
                print(f"[EXECUTOR] [FOK {i}] Waiting {CHUNK_DELAY}s before next chunk...")
                time.sleep(CHUNK_DELAY)
        
        total_elapsed = time.time() - start_time
        
        print(f"\n[EXECUTOR] Chunks completed in {total_elapsed:.1f}s")
        print(f"[EXECUTOR]    Successful: {successful_chunks}/{len(chunks)}")
        print(f"[EXECUTOR]    Failed: {len(failed_chunks)}")
        
        # ═══════════════════════════════════════════════════════════
        # STEP 4: FINAL BALANCE CHECK
        # ═══════════════════════════════════════════════════════════
        print(f"\n[EXECUTOR] [STEP 4] 🔍 Final balance check...")
        
        final_balance = self.get_blockchain_token_balance(token_id)
        
        if final_balance is None:
            print(f"[EXECUTOR] ⚠️  WARNING: Cannot verify final balance (RPC error)")
            final_balance = initial_balance - total_sold  # Estimate
        
        print(f"[EXECUTOR] ✓ Final balance: {final_balance:.4f} contracts")
        
        # ═══════════════════════════════════════════════════════════
        # STEP 4.5: FINAL SWEEP (if balance remains)
        # ═══════════════════════════════════════════════════════════
        if final_balance > MIN_DUST_THRESHOLD:
            print(f"\n[EXECUTOR] [STEP 4.5] 🧹 FINAL SWEEP REQUIRED")
            print(f"[EXECUTOR] ⚠️  Remaining balance: {final_balance:.2f} contracts")
            print(f"[EXECUTOR] Attempting to sell remainder...")
            
            sweep_success = False
            
            for sweep_attempt in range(1, SWEEP_MAX_ATTEMPTS + 1):
                sweep_start = time.time()
                
                print(f"\n[EXECUTOR] [SWEEP {sweep_attempt}/{SWEEP_MAX_ATTEMPTS}] Selling {final_balance:.2f} @ ${PRICE:.2f}...")
                
                # 📝 LOG: Sweep attempt
                log_sell_attempt(
                    market_slug=market_slug,
                    side=f"{side}_SWEEP",
                    contracts=final_balance,
                    price=PRICE,
                    attempt=sweep_attempt,
                    max_attempts=SWEEP_MAX_ATTEMPTS
                )
                
                # DRY RUN check
                if self.safety.dry_run:
                    print(f"[EXECUTOR] [SWEEP {sweep_attempt}] ✓ DRY_RUN: Simulated success")
                    total_sold += final_balance
                    total_received_usd += final_balance * PRICE
                    sweep_success = True
                    final_balance = 0.0
                    break
                
                # Send FOK for sweep
                try:
                    order_args = OrderArgs(
                        price=PRICE,
                        size=final_balance,
                        side=SELL,
                        token_id=token_id,
                    )
                    
                    signed_order = self.client.create_order(order_args)
                    api_result = self.client.post_order(signed_order, OrderType.FOK)
                    
                    sweep_elapsed = int((time.time() - sweep_start) * 1000)
                    
                    # 🔥 DEBUG: Log full API response
                    print(f"[EXECUTOR] [SWEEP {sweep_attempt}] API Response:")
                    print(f"[EXECUTOR]    Raw: {api_result}")
                    
                    if api_result and api_result.get("success"):
                        # Check errorMsg and amounts
                        error_msg = api_result.get("errorMsg", "")
                        taking_amount = float(api_result.get("takingAmount", 0))
                        making_amount = float(api_result.get("makingAmount", 0))
                        sweep_balance_before = final_balance  # Save for logging
                        
                        # 🔥 CRITICAL CHECK: FOK_ORDER_NOT_FILLED or amounts = 0
                        if error_msg and ("FOK_ORDER_NOT_FILLED" in error_msg or "not filled" in error_msg.lower()):
                            # FOK couldn't be filled
                            print(f"[EXECUTOR] [SWEEP {sweep_attempt}] ❌ NOT FILLED: {error_msg}")
                            try:
                                log_sell_result(
                                    market_slug=market_slug,
                                    side=side,
                                    requested_contracts=sweep_balance_before,
                                    sold_contracts=0.0,
                                    requested_usd=sweep_balance_before * PRICE,
                                    received_usd=0.0,
                                    success=False,
                                    error=error_msg,
                                    fak_attempts=sweep_attempt,
                                    elapsed_ms=sweep_elapsed
                                )
                            except Exception as log_err:
                                print(f"[EXECUTOR] ⚠️ Logging error: {log_err}")
                            # Continue retry loop
                            
                        elif taking_amount == 0 or making_amount == 0:
                            # Amounts = 0 means nothing was sold
                            print(f"[EXECUTOR] [SWEEP {sweep_attempt}] ❌ ZERO FILL: taking={taking_amount}, making={making_amount}")
                            try:
                                log_sell_result(
                                    market_slug=market_slug,
                                    side=side,
                                    requested_contracts=sweep_balance_before,
                                    sold_contracts=0.0,
                                    requested_usd=sweep_balance_before * PRICE,
                                    received_usd=0.0,
                                    success=False,
                                    error=f"ZERO_FILL: taking={taking_amount}, making={making_amount}",
                                    fak_attempts=sweep_attempt,
                                    elapsed_ms=sweep_elapsed
                                )
                            except Exception as log_err:
                                print(f"[EXECUTOR] ⚠️ Logging error: {log_err}")
                            # Continue retry loop
                            
                        else:
                            # ✅ REAL SUCCESS
                            filled = making_amount
                            received = taking_amount
                            
                            total_sold += filled
                            total_received_usd += received
                            sweep_success = True
                            
                            print(f"[EXECUTOR] [SWEEP {sweep_attempt}] ✅ SUCCESS!")
                            print(f"[EXECUTOR]    Sold: {filled:.2f} contracts")
                            print(f"[EXECUTOR]    Received: ${received:.2f}")
                            print(f"[EXECUTOR]    Time: {sweep_elapsed}ms")
                            if error_msg:
                                print(f"[EXECUTOR]    Warning: {error_msg}")
                            
                            # Log success
                            try:
                                log_sell_result(
                                    market_slug=market_slug,
                                    side=side,
                                    requested_contracts=sweep_balance_before,
                                    sold_contracts=filled,
                                    requested_usd=sweep_balance_before * PRICE,
                                    received_usd=received,
                                    success=True,
                                    fak_attempts=sweep_attempt,
                                    elapsed_ms=sweep_elapsed
                                )
                            except Exception as log_err:
                                print(f"[EXECUTOR] ⚠️ Logging error: {log_err}")
                            
                            # Re-check balance
                            final_balance = self.get_blockchain_token_balance(token_id)
                            if final_balance is None:
                                final_balance = 0.0  # Assume success
                            
                            if final_balance < MIN_DUST_THRESHOLD:
                                print(f"[EXECUTOR] ✅ All sold! (remaining dust: {final_balance:.4f})")
                                break
                            else:
                                print(f"[EXECUTOR] ⚠️  Still remaining: {final_balance:.2f}, will retry...")
                    
                    else:
                        error = api_result.get("errorMsg", "UNKNOWN") if api_result else "NO_API_RESPONSE"
                        print(f"[EXECUTOR] [SWEEP {sweep_attempt}] ❌ FAILED: {error}")
                        
                        # Log failure
                        try:
                            log_sell_result(
                                market_slug=market_slug,
                                side=side,
                                requested_contracts=final_balance,
                                sold_contracts=0.0,
                                requested_usd=final_balance * PRICE,
                                received_usd=0.0,
                                success=False,
                                error=error,
                                fak_attempts=sweep_attempt,
                                elapsed_ms=sweep_elapsed
                            )
                        except Exception as log_err:
                            print(f"[EXECUTOR] ⚠️ Logging error: {log_err}")
                
                except Exception as e:
                    print(f"[EXECUTOR] [SWEEP {sweep_attempt}] ❌ EXCEPTION: {e}")
                
                # 重试延迟（最后一次除外）
                if sweep_attempt < SWEEP_MAX_ATTEMPTS and not sweep_success:
                    print(f"[EXECUTOR] Waiting {SWEEP_RETRY_DELAY}s before retry...")
                    time.sleep(SWEEP_RETRY_DELAY)
                    
                    # Re-check balance before next attempt
                    final_balance = self.get_blockchain_token_balance(token_id)
                    if final_balance is None or final_balance < MIN_DUST_THRESHOLD:
                        print(f"[EXECUTOR] Balance cleared or unavailable, stopping sweep")
                        break
            
            # Final check after sweep
            final_balance = self.get_blockchain_token_balance(token_id)
            if final_balance is None:
                final_balance = 0.0  # Assume cleared
            
            print(f"\n[EXECUTOR] Sweep completed:")
            print(f"[EXECUTOR]    Success: {sweep_success}")
            print(f"[EXECUTOR]    Final balance: {final_balance:.4f}")
            
            # ═══════════════════════════════════════════════════════════
            # 🔥 修复 3: 清仓回退（FOK → FAK → 市价）
            # 如果 FOK 未通过，尝试 FAK 和市价单
            # ═══════════════════════════════════════════════════════════
            SWEEP_ENABLE_FALLBACK = exec_config.get('sweep_enable_fallback', False)
            SWEEP_FAK_ATTEMPTS = exec_config.get('sweep_fak_attempts', 2)
            SWEEP_MARKET_PRICE = exec_config.get('sweep_market_price', 0.01)
            
            if SWEEP_ENABLE_FALLBACK and not sweep_success and final_balance > MIN_DUST_THRESHOLD:
                print(f"\n[EXECUTOR] [STEP 4.6] 🔄 SWEEP FALLBACK ACTIVATED")
                print(f"[EXECUTOR] FOK failed, trying FAK → Market order")
                
                # ─────────────────────────────────────────────────────
                # 回退 #1: FAK（填单即撤销）
                # ─────────────────────────────────────────────────────
                print(f"\n[EXECUTOR] [FALLBACK FAK] Attempting FAK orders...")
                
                for fak_attempt in range(1, SWEEP_FAK_ATTEMPTS + 1):
                    if final_balance < MIN_DUST_THRESHOLD:
                        break
                    
                    fak_start = time.time()
                    print(f"\n[EXECUTOR] [FAK {fak_attempt}/{SWEEP_FAK_ATTEMPTS}] Selling {final_balance:.2f} @ ${PRICE:.2f}...")
                    
                    # 📝 日志：FAK 尝试
                    log_sell_attempt(
                        market_slug=market_slug,
                        side=f"{side}_SWEEP_FAK",
                        contracts=final_balance,
                        price=PRICE,
                        attempt=fak_attempt,
                        max_attempts=SWEEP_FAK_ATTEMPTS
                    )
                    
                    # 检查 DRY RUN
                    if self.safety.dry_run:
                        print(f"[EXECUTOR] [FAK {fak_attempt}] ✓ DRY_RUN: Simulated success")
                        total_sold += final_balance
                        total_received_usd += final_balance * PRICE
                        final_balance = 0.0
                        break
                    
                    # 发送 FAK 订单
                    try:
                        order_args = OrderArgs(
                            price=PRICE,
                            size=final_balance,
                            side=SELL,
                            token_id=token_id,
                        )
                        
                        signed_order = self.client.create_order(order_args)
                        api_result = self.client.post_order(signed_order, OrderType.FAK)  # 🔥 FAK!
                        
                        fak_elapsed = int((time.time() - fak_start) * 1000)
                        
                        # 🔥 调试：记录完整 API 响应
                        print(f"[EXECUTOR] [FAK {fak_attempt}] API Response:")
                        print(f"[EXECUTOR]    Raw: {api_result}")
                        
                        if api_result and api_result.get("success"):
                            taking_amount = float(api_result.get("takingAmount", 0))
                            making_amount = float(api_result.get("makingAmount", 0))
                            
                            if taking_amount > 0 and making_amount > 0:
                                # ✅ 部分或全部卖出
                                filled = making_amount
                                received = taking_amount
                                
                                total_sold += filled
                                total_received_usd += received
                                
                                print(f"[EXECUTOR] [FAK {fak_attempt}] ✅ SUCCESS!")
                                print(f"[EXECUTOR]    Sold: {filled:.2f} contracts")
                                print(f"[EXECUTOR]    Received: ${received:.2f}")
                                print(f"[EXECUTOR]    Time: {fak_elapsed}ms")
                                
                                # 记录成功
                                try:
                                    log_sell_result(
                                        market_slug=market_slug,
                                        side=side,
                                        requested_contracts=final_balance,
                                        sold_contracts=filled,
                                        requested_usd=final_balance * PRICE,
                                        received_usd=received,
                                        success=True,
                                        fak_attempts=fak_attempt,
                                        elapsed_ms=fak_elapsed
                                    )
                                except Exception as log_err:
                                    print(f"[EXECUTOR] ⚠️ Logging error: {log_err}")
                                
                                # 重新检查余额
                                final_balance = self.get_blockchain_token_balance(token_id)
                                if final_balance is None or final_balance < MIN_DUST_THRESHOLD:
                                    final_balance = 0.0
                                    break
                            else:
                                # ❌ 未卖出
                                print(f"[EXECUTOR] [FAK {fak_attempt}] ❌ NO FILL")
                        else:
                            error = api_result.get("errorMsg", "UNKNOWN") if api_result else "NO_API_RESPONSE"
                            print(f"[EXECUTOR] [FAK {fak_attempt}] ❌ FAILED: {error}")
                    
                    except Exception as e:
                        print(f"[EXECUTOR] [FAK {fak_attempt}] ❌ EXCEPTION: {e}")
                    
                    # 下次尝试前延迟
                    if fak_attempt < SWEEP_FAK_ATTEMPTS and final_balance > MIN_DUST_THRESHOLD:
                        time.sleep(SWEEP_RETRY_DELAY)
                
                # ─────────────────────────────────────────────────────
                # 回退 #2: 市价单（GTC - 取消前有效）
                # 以任意价格保证卖出
                # ─────────────────────────────────────────────────────
                if final_balance > MIN_DUST_THRESHOLD:
                    print(f"\n[EXECUTOR] [FALLBACK MARKET] FAK failed, trying Market order...")
                    print(f"[EXECUTOR] ⚠️  WARNING: Market order may have high slippage!")
                    
                    market_start = time.time()
                    print(f"\n[EXECUTOR] [MARKET] Selling {final_balance:.2f} @ ${SWEEP_MARKET_PRICE:.2f}...")
                    
                    # 📝 日志：市价单尝试
                    log_sell_attempt(
                        market_slug=market_slug,
                        side=f"{side}_SWEEP_MARKET",
                        contracts=final_balance,
                        price=SWEEP_MARKET_PRICE,
                        attempt=1,
                        max_attempts=1
                    )
                    
                    # 检查 DRY RUN
                    if self.safety.dry_run:
                        print(f"[EXECUTOR] [MARKET] ✓ DRY_RUN: Simulated success")
                        total_sold += final_balance
                        total_received_usd += final_balance * SWEEP_MARKET_PRICE
                        final_balance = 0.0
                    else:
                        # 发送市价单（GTC）
                        try:
                            order_args = OrderArgs(
                                price=SWEEP_MARKET_PRICE,
                                size=final_balance,
                                side=SELL,
                                token_id=token_id,
                            )
                            
                            signed_order = self.client.create_order(order_args)
                            api_result = self.client.post_order(signed_order, OrderType.GTC)  # 🔥 GTC = Market!
                            
                            market_elapsed = int((time.time() - market_start) * 1000)
                            
                            # 🔥 DEBUG: Log full API response
                            print(f"[EXECUTOR] [MARKET] API Response:")
                            print(f"[EXECUTOR]    Raw: {api_result}")
                            
                            if api_result and api_result.get("success"):
                                taking_amount = float(api_result.get("takingAmount", 0))
                                making_amount = float(api_result.get("makingAmount", 0))
                                
                                if taking_amount > 0 and making_amount > 0:
                                    # ✅ 成功
                                    filled = making_amount
                                    received = taking_amount
                                    
                                    total_sold += filled
                                    total_received_usd += received
                                    
                                    print(f"[EXECUTOR] [MARKET] ✅ SUCCESS!")
                                    print(f"[EXECUTOR]    Sold: {filled:.2f} contracts")
                                    print(f"[EXECUTOR]    Received: ${received:.2f}")
                                    print(f"[EXECUTOR]    Actual price: ${received/filled:.4f}")
                                    print(f"[EXECUTOR]    Time: {market_elapsed}ms")
                                    
                                    # 记录成功
                                    try:
                                        log_sell_result(
                                            market_slug=market_slug,
                                            side=side,
                                            requested_contracts=final_balance,
                                            sold_contracts=filled,
                                            requested_usd=final_balance * SWEEP_MARKET_PRICE,
                                            received_usd=received,
                                            success=True,
                                            fak_attempts=1,
                                            elapsed_ms=market_elapsed
                                        )
                                    except Exception as log_err:
                                        print(f"[EXECUTOR] ⚠️ Logging error: {log_err}")
                                    
                                    # 最终余额检查
                                    final_balance = self.get_blockchain_token_balance(token_id)
                                    if final_balance is None:
                                        final_balance = 0.0
                                else:
                                    print(f"[EXECUTOR] [MARKET] ❌ NO FILL")
                            else:
                                error = api_result.get("errorMsg", "UNKNOWN") if api_result else "NO_API_RESPONSE"
                                print(f"[EXECUTOR] [MARKET] ❌ FAILED: {error}")
                        
                        except Exception as e:
                            print(f"[EXECUTOR] [MARKET] ❌ EXCEPTION: {e}")
                
                print(f"\n[EXECUTOR] Fallback completed:")
                print(f"[EXECUTOR]    Final balance: {final_balance:.4f}")
        
        # ═══════════════════════════════════════════════════════════
        # 🔥 延迟最终清仓（捕获竞态条件导致的飞行中买入）
        # 注意：报告已移至延迟清仓之后以获取正确数据！
        # ═══════════════════════════════════════════════════════════
        DELAYED_SWEEP_ENABLED = exec_config.get('delayed_sweep_enabled', True)
        DELAYED_SWEEP_DELAY = exec_config.get('delayed_sweep_delay_sec', 5)
        DELAYED_SWEEP_MIN_BALANCE = exec_config.get('delayed_sweep_min_balance', 0.1)
        DELAYED_SWEEP_FOK_ATTEMPTS = exec_config.get('delayed_sweep_fok_attempts', 3)
        DELAYED_SWEEP_FAK_ATTEMPTS = exec_config.get('delayed_sweep_fak_attempts', 2)
        DELAYED_SWEEP_RETRY_DELAY = exec_config.get('delayed_sweep_retry_delay_sec', 1.0)
        
        if DELAYED_SWEEP_ENABLED:
            print(f"\n[EXECUTOR] {'='*60}")
            print(f"[EXECUTOR] [DELAYED SWEEP] STAGE 1: WAIT FOR BLOCKCHAIN")
            print(f"[EXECUTOR] {'='*60}")
            print(f"[EXECUTOR] [DELAYED SWEEP] Current balance (before wait): {final_balance:.4f}")
            print(f"[EXECUTOR] [DELAYED SWEEP] ⏰ Waiting {DELAYED_SWEEP_DELAY}s for in-flight purchases...")
            print(f"[EXECUTOR] [DELAYED SWEEP] (Catching race conditions with blockchain)")
            time.sleep(DELAYED_SWEEP_DELAY)
            
            # 从区块链重新获取余额
            print(f"\n[EXECUTOR] [DELAYED SWEEP] STAGE 2: RE-FETCH BALANCE")
            print(f"[EXECUTOR] [DELAYED SWEEP] 🔄 Fetching REAL balance from blockchain...")
            delayed_balance = self.get_blockchain_token_balance(token_id)
            print(f"[EXECUTOR] [DELAYED SWEEP] Balance after re-fetch: {delayed_balance if delayed_balance is not None else 'ERROR'}...")
            
            if delayed_balance is None:
                print(f"[EXECUTOR] [DELAYED SWEEP] ⚠️  Cannot fetch balance, skipping delayed sweep")
            elif delayed_balance > DELAYED_SWEEP_MIN_BALANCE:
                print(f"[EXECUTOR] [DELAYED SWEEP] 🔥 FOUND IN-FLIGHT PURCHASES!")
                print(f"[EXECUTOR] [DELAYED SWEEP]    Balance: {delayed_balance:.2f} contracts")
                print(f"[EXECUTOR] [DELAYED SWEEP]    (These appeared AFTER initial sale started)")
                print(f"\n[EXECUTOR] [DELAYED SWEEP] 🧹 Starting cascade sale (FOK → FAK → Market)...")
                
                delayed_sold = 0.0
                delayed_received = 0.0
                delayed_success = False
                
                # ─────────────────────────────────────────────────────
                # 延迟清仓 #1: FOK 尝试
                # ─────────────────────────────────────────────────────
                print(f"\n[EXECUTOR] [DELAYED FOK] Attempting FOK orders...")
                
                for fok_attempt in range(1, DELAYED_SWEEP_FOK_ATTEMPTS + 1):
                    if delayed_balance < DELAYED_SWEEP_MIN_BALANCE:
                        break
                    
                    fok_start = time.time()
                    print(f"\n[EXECUTOR] [DELAYED FOK {fok_attempt}/{DELAYED_SWEEP_FOK_ATTEMPTS}] Selling {delayed_balance:.2f} @ ${PRICE:.2f}...")
                    
                    log_sell_attempt(
                        market_slug=market_slug,
                        side=f"{side}_DELAYED_FOK",
                        contracts=delayed_balance,
                        price=PRICE,
                        attempt=fok_attempt,
                        max_attempts=DELAYED_SWEEP_FOK_ATTEMPTS
                    )
                    
                    if self.safety.dry_run:
                        print(f"[EXECUTOR] [DELAYED FOK {fok_attempt}] ✓ DRY_RUN success")
                        delayed_sold += delayed_balance
                        delayed_received += delayed_balance * PRICE
                        delayed_balance = 0.0
                        delayed_success = True
                        break
                    
                    try:
                        order_args = OrderArgs(
                            price=PRICE,
                            size=delayed_balance,
                            side=SELL,
                            token_id=token_id,
                        )
                        
                        signed_order = self.client.create_order(order_args)
                        api_result = self.client.post_order(signed_order, OrderType.FOK)
                        
                        fok_elapsed = int((time.time() - fok_start) * 1000)
                        
                        if api_result and api_result.get("success"):
                            error_msg = api_result.get("errorMsg", "")
                            taking_amount = float(api_result.get("takingAmount", 0))
                            making_amount = float(api_result.get("makingAmount", 0))
                            
                            if error_msg and ("FOK_ORDER_NOT_FILLED" in error_msg or "not filled" in error_msg.lower()):
                                print(f"[EXECUTOR] [DELAYED FOK {fok_attempt}] ❌ NOT FILLED")
                            elif taking_amount == 0 or making_amount == 0:
                                print(f"[EXECUTOR] [DELAYED FOK {fok_attempt}] ❌ ZERO FILL")
                            else:
                                # ✅ 成功！
                                filled = making_amount
                                received = taking_amount
                                
                                delayed_sold += filled
                                delayed_received += received
                                delayed_success = True
                                
                                print(f"[EXECUTOR] [DELAYED FOK {fok_attempt}] ✅ SUCCESS!")
                                print(f"[EXECUTOR]    Sold: {filled:.2f} contracts")
                                print(f"[EXECUTOR]    Received: ${received:.2f}")
                                
                                log_sell_result(
                                    market_slug=market_slug,
                                    side=side,
                                    requested_contracts=delayed_balance,
                                    sold_contracts=filled,
                                    requested_usd=delayed_balance * PRICE,
                                    received_usd=received,
                                    success=True,
                                    fak_attempts=fok_attempt,
                                    elapsed_ms=fok_elapsed
                                )
                                
                                # 重新检查余额
                                delayed_balance = self.get_blockchain_token_balance(token_id)
                                if delayed_balance is None or delayed_balance < DELAYED_SWEEP_MIN_BALANCE:
                                    delayed_balance = 0.0
                                    break
                    
                    except Exception as e:
                        print(f"[EXECUTOR] [DELAYED FOK {fok_attempt}] ❌ EXCEPTION: {e}")
                    
                    if fok_attempt < DELAYED_SWEEP_FOK_ATTEMPTS and delayed_balance > DELAYED_SWEEP_MIN_BALANCE:
                        time.sleep(DELAYED_SWEEP_RETRY_DELAY)
                
                # ─────────────────────────────────────────────────────
                # 延迟清仓 #2: FAK 尝试（如果 FOK 失败）
                # ─────────────────────────────────────────────────────
                if not delayed_success and delayed_balance > DELAYED_SWEEP_MIN_BALANCE:
                    print(f"\n[EXECUTOR] [DELAYED FAK] FOK failed, trying FAK orders...")
                    
                    for fak_attempt in range(1, DELAYED_SWEEP_FAK_ATTEMPTS + 1):
                        if delayed_balance < DELAYED_SWEEP_MIN_BALANCE:
                            break
                        
                        fak_start = time.time()
                        print(f"\n[EXECUTOR] [DELAYED FAK {fak_attempt}/{DELAYED_SWEEP_FAK_ATTEMPTS}] Selling {delayed_balance:.2f} @ ${PRICE:.2f}...")
                        
                        log_sell_attempt(
                            market_slug=market_slug,
                            side=f"{side}_DELAYED_FAK",
                            contracts=delayed_balance,
                            price=PRICE,
                            attempt=fak_attempt,
                            max_attempts=DELAYED_SWEEP_FAK_ATTEMPTS
                        )
                        
                        if self.safety.dry_run:
                            print(f"[EXECUTOR] [DELAYED FAK {fak_attempt}] ✓ DRY_RUN success")
                            delayed_sold += delayed_balance
                            delayed_received += delayed_balance * PRICE
                            delayed_balance = 0.0
                            delayed_success = True
                            break
                        
                        try:
                            order_args = OrderArgs(
                                price=PRICE,
                                size=delayed_balance,
                                side=SELL,
                                token_id=token_id,
                            )
                            
                            signed_order = self.client.create_order(order_args)
                            api_result = self.client.post_order(signed_order, OrderType.FAK)
                            
                            fak_elapsed = int((time.time() - fak_start) * 1000)
                            
                            if api_result and api_result.get("success"):
                                taking_amount = float(api_result.get("takingAmount", 0))
                                making_amount = float(api_result.get("makingAmount", 0))
                                
                                if taking_amount > 0 and making_amount > 0:
                                    # ✅ 部分或全部成交
                                    filled = making_amount
                                    received = taking_amount
                                    
                                    delayed_sold += filled
                                    delayed_received += received
                                    delayed_success = True
                                    
                                    print(f"[EXECUTOR] [DELAYED FAK {fak_attempt}] ✅ SUCCESS!")
                                    print(f"[EXECUTOR]    Sold: {filled:.2f} contracts")
                                    print(f"[EXECUTOR]    Received: ${received:.2f}")
                                    
                                    log_sell_result(
                                        market_slug=market_slug,
                                        side=side,
                                        requested_contracts=delayed_balance,
                                        sold_contracts=filled,
                                        requested_usd=delayed_balance * PRICE,
                                        received_usd=received,
                                        success=True,
                                        fak_attempts=fak_attempt,
                                        elapsed_ms=fak_elapsed
                                    )
                                    
                                    # 重新检查余额
                                    delayed_balance = self.get_blockchain_token_balance(token_id)
                                    if delayed_balance is None or delayed_balance < DELAYED_SWEEP_MIN_BALANCE:
                                        delayed_balance = 0.0
                                        break
                                else:
                                    print(f"[EXECUTOR] [DELAYED FAK {fak_attempt}] ❌ NO FILL")
                        
                        except Exception as e:
                            print(f"[EXECUTOR] [DELAYED FAK {fak_attempt}] ❌ EXCEPTION: {e}")
                        
                        if fak_attempt < DELAYED_SWEEP_FAK_ATTEMPTS and delayed_balance > DELAYED_SWEEP_MIN_BALANCE:
                            time.sleep(DELAYED_SWEEP_RETRY_DELAY)
                
                # ─────────────────────────────────────────────────────
                # 延迟清仓 #3: 市价单（如果 FAK 失败）
                # ─────────────────────────────────────────────────────
                if not delayed_success and delayed_balance > DELAYED_SWEEP_MIN_BALANCE:
                    print(f"\n[EXECUTOR] [DELAYED MARKET] FAK failed, trying Market order...")
                    print(f"[EXECUTOR] [DELAYED MARKET] ⚠️  WARNING: May have slippage")
                    
                    market_start = time.time()
                    print(f"\n[EXECUTOR] [DELAYED MARKET] Selling {delayed_balance:.2f} @ ${PRICE:.2f}...")
                    
                    log_sell_attempt(
                        market_slug=market_slug,
                        side=f"{side}_DELAYED_MARKET",
                        contracts=delayed_balance,
                        price=PRICE,
                        attempt=1,
                        max_attempts=1
                    )
                    
                    if self.safety.dry_run:
                        print(f"[EXECUTOR] [DELAYED MARKET] ✓ DRY_RUN success")
                        delayed_sold += delayed_balance
                        delayed_received += delayed_balance * PRICE
                        delayed_balance = 0.0
                        delayed_success = True
                    else:
                        try:
                            order_args = OrderArgs(
                                price=PRICE,
                                size=delayed_balance,
                                side=SELL,
                                token_id=token_id,
                            )
                            
                            signed_order = self.client.create_order(order_args)
                            api_result = self.client.post_order(signed_order, OrderType.GTC)
                            
                            market_elapsed = int((time.time() - market_start) * 1000)
                            
                            if api_result and api_result.get("success"):
                                taking_amount = float(api_result.get("takingAmount", 0))
                                making_amount = float(api_result.get("makingAmount", 0))
                                
                                if taking_amount > 0 and making_amount > 0:
                                    filled = making_amount
                                    received = taking_amount
                                    
                                    delayed_sold += filled
                                    delayed_received += received
                                    delayed_success = True
                                    
                                    print(f"[EXECUTOR] [DELAYED MARKET] ✅ SUCCESS!")
                                    print(f"[EXECUTOR]    Sold: {filled:.2f} contracts")
                                    print(f"[EXECUTOR]    Received: ${received:.2f}")
                                    
                                    log_sell_result(
                                        market_slug=market_slug,
                                        side=side,
                                        requested_contracts=delayed_balance,
                                        sold_contracts=filled,
                                        requested_usd=delayed_balance * PRICE,
                                        received_usd=received,
                                        success=True,
                                        fak_attempts=1,
                                        elapsed_ms=market_elapsed
                                    )
                                    
                                    # 最终余额检查
                                    delayed_balance = self.get_blockchain_token_balance(token_id)
                                    if delayed_balance is None:
                                        delayed_balance = 0.0
                                else:
                                    print(f"[EXECUTOR] [DELAYED MARKET] ❌ NO FILL")
                        
                        except Exception as e:
                            print(f"[EXECUTOR] [DELAYED MARKET] ❌ EXCEPTION: {e}")
                
                # 使用延迟清仓结果更新总计
                total_sold += delayed_sold
                total_received_usd += delayed_received
                final_balance = delayed_balance
                
                print(f"\n[EXECUTOR] {'='*60}")
                print(f"[EXECUTOR] [DELAYED SWEEP] STAGE 3: RESULTS")
                print(f"[EXECUTOR] {'='*60}")
                print(f"[EXECUTOR] [DELAYED SWEEP] Additional Sold: {delayed_sold:.2f} contracts")
                print(f"[EXECUTOR] [DELAYED SWEEP] Additional Received: ${delayed_received:.2f}")
                print(f"[EXECUTOR] [DELAYED SWEEP] Final Balance: {final_balance:.4f}")
                print(f"[EXECUTOR] [DELAYED SWEEP] Success: {delayed_success}")
                print(f"[EXECUTOR] {'='*60}")
                
                if delayed_sold > 0:
                    print(f"\n[EXECUTOR] ✅ Delayed sweep caught in-flight purchases!")
                    print(f"[EXECUTOR]    This proves the race condition fix is working!")
            else:
                print(f"\n[EXECUTOR] {'='*60}")
                print(f"[EXECUTOR] [DELAYED SWEEP] STAGE 3: RESULTS")
                print(f"[EXECUTOR] {'='*60}")
                print(f"[EXECUTOR] [DELAYED SWEEP] ✓ No additional balance found")
                print(f"[EXECUTOR] [DELAYED SWEEP]    Balance: {delayed_balance:.4f} (below threshold {DELAYED_SWEEP_MIN_BALANCE})")
                print(f"[EXECUTOR] [DELAYED SWEEP]    No in-flight purchases detected")
                print(f"[EXECUTOR] {'='*60}")
                final_balance = delayed_balance
        
        # ═══════════════════════════════════════════════════════════
        # 步骤 5: 最终报告（延迟清仓之后！）
        # ═══════════════════════════════════════════════════════════
        total_elapsed = time.time() - start_time
        
        # 📝 日志：FOK 分块卖出摘要（含延迟清仓后的最终余额）
        from trade_logger import trades_logger
        trades_logger.info(
            f"FOK_CHUNKED_COMPLETE | Market: {market_slug} | Side: {side} | "
            f"Initial: {initial_balance:.2f} | Sold: {total_sold:.2f} ({total_sold/initial_balance*100:.1f}%) | "
            f"Remaining: {final_balance:.2f} | Chunks: {successful_chunks}/{len(chunks)} | "
            f"Failed: {len(failed_chunks)} | Received: ${total_received_usd:.2f} | "
            f"Time: {total_elapsed:.1f}s"
        )
        
        print(f"\n[EXECUTOR] {'='*60}")
        print(f"[EXECUTOR] 📊 FOK CHUNKED SELL COMPLETED (FINAL REPORT)")
        print(f"[EXECUTOR] {'='*60}")
        print(f"[EXECUTOR] Initial Balance: {initial_balance:.2f}")
        print(f"[EXECUTOR] Total Sold: {total_sold:.2f} ({total_sold/initial_balance*100:.1f}%)")
        print(f"[EXECUTOR] Final Balance: {final_balance:.2f}")
        print(f"[EXECUTOR] ")
        print(f"[EXECUTOR] Successful Chunks: {successful_chunks}/{len(chunks)}")
        print(f"[EXECUTOR] Failed Chunks: {len(failed_chunks)}")
        print(f"[EXECUTOR] ")
        print(f"[EXECUTOR] Total Received: ${total_received_usd:.2f}")
        if total_sold > 0:
            print(f"[EXECUTOR] Avg Price: ${total_received_usd/total_sold:.4f}")
        print(f"[EXECUTOR] Total Time: {total_elapsed:.1f}s")
        print(f"[EXECUTOR] {'='*60}\n")
        
        # 检查：是否仍有大量余额剩余？（最终检查！）
        if final_balance > MIN_DUST_THRESHOLD:
            warning_msg = (
                f"⚠️ WARNING: Significant balance remains!\n"
                f"\n🔥 AFTER DELAYED SWEEP (5s delay + retries)"
                f"\nMarket: {market_slug}"
                f"\nSide: {side}"
                f"\nInitial: {initial_balance:.2f}"
                f"\nSold: {total_sold:.2f} ({total_sold/initial_balance*100:.1f}%)"
                f"\nRemaining: {final_balance:.2f} ({final_balance/initial_balance*100:.1f}%)"
                f"\nReceived: ${total_received_usd:.2f}"
                f"\n"
                f"\nFailed chunks: {len(failed_chunks)}"
            )
            
            if failed_chunks:
                warning_msg += "\n\nFailed details:"
                for fc in failed_chunks[:3]:  # Show first 3
                    warning_msg += f"\n  • Chunk {fc['chunk']}: {fc['size']:.2f} (attempts: {fc.get('attempts', '?')})"
            
            print(f"[EXECUTOR] ⚠️  Sending Telegram alert for FINAL remaining balance...")
            self._send_telegram_alert(warning_msg)
            
            # 如果剩余超过 10% 则成功 = False
            success = (final_balance / initial_balance) < 0.1
        else:
            print(f"[EXECUTOR] ✅ SUCCESS: All sold (remaining = dust)")
            success = True
        
        avg_price = total_received_usd / total_sold if total_sold > 0 else 0.0
        
        # 🔥 修复 4: 记录剩余余额以供赎回
        if final_balance > MIN_DUST_THRESHOLD:
            print(f"\n[EXECUTOR] ⚠️  WARNING: Remaining balance detected!")
            print(f"[EXECUTOR]    Token: {token_id}")
            print(f"[EXECUTOR]    Balance: {final_balance:.4f} contracts")
            print(f"[EXECUTOR]    Market: {market_slug}")
            print(f"[EXECUTOR]    This market should be added to pending_markets for redeem!")
        
        return OrderResult(
            success=success,
            filled_size=total_sold,
            filled_price=avg_price,
            total_spent_usd=total_received_usd,
            attempts=len(chunks),
            error=f"REMAINING_{final_balance:.2f}" if final_balance > MIN_DUST_THRESHOLD else None,
            elapsed_ms=int(total_elapsed * 1000),
            remaining_balance=final_balance  # 🔥 FIX 4: Return final balance
        )
    
    def _send_telegram_alert(self, message: str):
        """
        向 Telegram 发送关键通知
        """
        print(f"[EXECUTOR] [TELEGRAM] {message[:100]}...")  # 调试
        try:
            token = os.getenv("TELEGRAM_BOT_TOKEN")
            chat_id = os.getenv("TELEGRAM_CHAT_ID")
            
            if not token or not chat_id:
                return
            
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message
            }
            requests.post(url, json=payload, timeout=5)
        except Exception as e:
            print(f"[EXECUTOR] ⚠️ Telegram alert failed: {e}")
    
    def _log_order(self, market_slug: str, side: str, contracts: float,
                   price: float, result: OrderResult, order_type: str, fak_attempt: int = 1):
        """将订单写入日志（每次 FAK 尝试单独记录）"""
        log_entry = {
            'timestamp': time.time(),
            'datetime': time.strftime('%Y-%m-%d %H:%M:%S'),
            'market_slug': market_slug,
            'side': order_type,
            'order_type': order_type,
            'fak_attempt': fak_attempt,
            'contracts': contracts,
            'price': price,
            'size_usd': contracts * price if contracts and price else 0,
            'total_spent_usd': result.total_spent_usd,
            'success': result.success,
            'order_id': result.order_id,
            'error': result.error,
            'dry_run': result.dry_run,
            'elapsed_ms': result.elapsed_ms,
            'attempts_total': result.attempts
        }
        
        orders_log_path = Path(self.config.get('logging', {}).get('orders_file', 'logs/orders.jsonl'))
        os.makedirs(orders_log_path.parent, exist_ok=True)
        
        with open(orders_log_path, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
    
    def redeem_position(self, market_slug: str, condition_id: str, 
                        up_token_id: str, down_token_id: str, 
                        neg_risk: bool = True) -> tuple[bool, float]:
        """
        赎回已完成市场的仓位。
        基于 /root/clip/redeem.py - redeem_specific()
        
        Args:
            market_slug: 市场标识符
            condition_id: 此市场的条件 ID
            up_token_id: UP 方的令牌 ID
            down_token_id: DOWN 方的令牌 ID
            neg_risk: 是否为负风险（默认：True）
            
        Returns:
            (success: bool, amount_usd: float)
        """
        if self.safety.dry_run:
            print(f"[EXECUTOR] 🟢 DRY_RUN: Would redeem {market_slug}")
            return (True, 0.0)
        
        print(f"[EXECUTOR] 📤 REDEEM: {market_slug}")
        
        # 加载赎回配置
        redeem_cfg = self.config.get("execution", {}).get("redeem", {})
        gas_limit = redeem_cfg.get("gas_limit", 500000)
        gas_multiplier = redeem_cfg.get("gas_price_multiplier", 1.5)
        max_gas_retries = 5
        gas_retry_delay = 3
        
        try:
            # 合约地址
            NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
            USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            
            # 连接到 Web3（使用第一个 RPC 端点）
            rpc_url = self.rpc_endpoints[0] if self.rpc_endpoints else "https://polygon-rpc.com"
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            if not w3.is_connected():
                print(f"[REDEEM] ❌ Cannot connect to RPC")
                return False, 0.0
            
            # CTF 合约 ABI
            CTF_ABI = [
                {"inputs":[{"internalType":"address","name":"_collateralToken","type":"address"},{"internalType":"bytes32","name":"_parentCollectionId","type":"bytes32"},{"internalType":"bytes32","name":"_conditionId","type":"bytes32"},{"internalType":"uint256[]","name":"_partition","type":"uint256[]"},{"internalType":"uint256[]","name":"_amounts","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}
            ]
            
            # 适配器 ABI（用于负风险）
            ADAPTER_ABI = [
                {"inputs":[{"internalType":"address","name":"_operator","type":"address"},{"internalType":"address","name":"","type":"address"},{"internalType":"uint256[]","name":"_ids","type":"uint256[]"},{"internalType":"uint256[]","name":"_values","type":"uint256[]"},{"internalType":"bytes","name":"_data","type":"bytes"}],"name":"onERC1155BatchReceived","outputs":[{"internalType":"bytes4","name":"","type":"bytes4"}],"stateMutability":"nonpayable","type":"function"}
            ]
            
            # 获取钱包地址
            wallet_address = self.client.creds.address
            print(f"[REDEEM] Wallet: {wallet_address}")
            
            # TODO: 完成赎回实现
            # 目前返回成功以避免错误
            print(f"[REDEEM] ⚠️  Redeem implementation incomplete")
            return (True, 0.0)
            
        except Exception as e:
            print(f"[REDEEM] ❌ Error: {e}")
            return (False, 0.0)
        """
        向 Telegram 发送关键通知
        用于严重错误（未能完全卖出）
        """
        try:
            token = os.getenv("TELEGRAM_BOT_TOKEN")
            chat_id = os.getenv("TELEGRAM_CHAT_ID")
            
            if not token or not chat_id:
                # 无 Telegram 配置 - 静默失败
                return
            
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML"
            }
            
            response = requests.post(url, json=payload, timeout=5)
            
            if response.status_code == 200:
                print(f"[EXECUTOR] 📱 Telegram alert sent")
            else:
                print(f"[EXECUTOR] ⚠ Telegram alert failed: {response.status_code}")
                
        except Exception as e:
            # 静默失败 - 不想让 Telegram 错误中断交易
            print(f"[EXECUTOR] ⚠ Telegram exception: {e}")
    
    def _log_order(self, market_slug: str, side: str, contracts: float, 
                   price: float, result: OrderResult, order_type: str, fak_attempt: int = 1):
        """将订单写入日志（每次 FAK 尝试单独记录）"""
        log_entry = {
            'timestamp': time.time(),
            'datetime': time.strftime('%Y-%m-%d %H:%M:%S'),
            'market_slug': market_slug,
            'side': side,
            'order_type': order_type,  # BUY 或 SELL
            'fak_attempt': fak_attempt,  # FAK 尝试编号
            'contracts': contracts,
            'price': price,
            'size_usd': contracts * price,
            'total_spent_usd': result.total_spent_usd,
            'success': result.success,
            'order_id': result.order_id,
            'error': result.error,
            'dry_run': result.dry_run,
            'elapsed_ms': result.elapsed_ms,
            'attempts_total': result.attempts
        }
        
        with open(self.orders_log, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
    
    def redeem_position(self, market_slug: str, condition_id: str, 
                        up_token_id: str, down_token_id: str, 
                        neg_risk: bool = True) -> tuple[bool, float]:
        """
        赎回已完成市场的仓位。
        基于 /root/clip/redeem.py - redeem_specific()
        
        Args:
            market_slug: 市场标识符
            condition_id: CTF 条件 ID（带 0x 前缀的十六进制字符串）
            up_token_id: UP 结果的令牌 ID
            down_token_id: DOWN 结果的令牌 ID
            neg_risk: 如果为 True，使用 NegRisk 适配器；否则直接使用 CTF
        
        Returns:
            (success: bool, amount_received_usd: float)
        """
        if self.safety.dry_run:
            print(f"[REDEEM DRY-RUN] Would redeem {market_slug}")
            return True, 0.0
        
        # 加载赎回配置
        redeem_cfg = self.config.get("execution", {}).get("redeem", {})
        gas_limit = redeem_cfg.get("gas_limit", 500000)
        gas_multiplier = redeem_cfg.get("gas_price_multiplier", 1.5)
        max_gas_retries = 5  # Gas 价格错误的最大重试次数
        gas_retry_delay = 3  # 重试间隔秒数
        
        try:
            # 合约地址
            NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
            USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            
            # 连接到 Web3（使用第一个 RPC 端点）
            rpc_url = self.rpc_endpoints[0] if self.rpc_endpoints else "https://polygon-rpc.com"
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            if not w3.is_connected():
                print(f"[REDEEM] ❌ Cannot connect to RPC")
                return False, 0.0
            
            # CTF 合约 ABI
            CTF_ABI = [
                {"inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}], 
                 "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], 
                 "stateMutability": "view", "type": "function"},
                {"inputs": [{"name": "conditionId", "type": "bytes32"}], 
                 "name": "payoutDenominator", "outputs": [{"name": "", "type": "uint256"}], 
                 "stateMutability": "view", "type": "function"},
                {"inputs": [{"name": "conditionId", "type": "bytes32"}, {"name": "index", "type": "uint256"}], 
                 "name": "payoutNumerators", "outputs": [{"name": "", "type": "uint256"}], 
                 "stateMutability": "view", "type": "function"},
                {"inputs": [
                    {"name": "collateralToken", "type": "address"},
                    {"name": "parentCollectionId", "type": "bytes32"},
                    {"name": "conditionId", "type": "bytes32"},
                    {"name": "indexSets", "type": "uint256[]"}
                 ], "name": "redeemPositions", "outputs": [], 
                 "stateMutability": "nonpayable", "type": "function"}
            ]
            
            NEG_RISK_ABI = [
                {"inputs": [
                    {"name": "conditionId", "type": "bytes32"},
                    {"name": "amounts", "type": "uint256[]"}
                 ], "name": "redeemPositions", "outputs": [], 
                 "stateMutability": "nonpayable", "type": "function"}
            ]
            
            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(self.CTF_ADDRESS), 
                abi=CTF_ABI
            )
            
            # 检查代币余额
            up_balance = ctf.functions.balanceOf(self.wallet_address, int(up_token_id)).call()
            down_balance = ctf.functions.balanceOf(self.wallet_address, int(down_token_id)).call()
            
            print(f"[REDEEM] {market_slug}")
            print(f"  UP: {up_balance / 1e6:.2f}, DOWN: {down_balance / 1e6:.2f}")
            
            if up_balance == 0 and down_balance == 0:
                self._log_redeem(market_slug, True, 0.0, "", "NO_TOKENS")
                print(f"[REDEEM] ✅ No tokens to redeem (position already closed or never opened)")
                # 返回 True 表示完成（不是需要重试的错误）
                return True, 0.0
            
            # 检查预言机解析
            condition_bytes = Web3.to_bytes(hexstr=condition_id)
            payout_denom = ctf.functions.payoutDenominator(condition_bytes).call()
            
            if payout_denom == 0:
                self._log_redeem(market_slug, False, 0.0, "", "ORACLE_NOT_RESOLVED")
                print(f"[REDEEM] ⚠ Oracle not resolved yet (payoutDenominator=0)")
                return False, 0.0
            
            # 检查赢家
            up_payout = ctf.functions.payoutNumerators(condition_bytes, 0).call()
            down_payout = ctf.functions.payoutNumerators(condition_bytes, 1).call()
            winner = "UP" if up_payout > 0 else "DOWN" if down_payout > 0 else "UNKNOWN"
            print(f"  Oracle resolved: {winner} won!")
            
            # 构建赎回交易
            nonce = w3.eth.get_transaction_count(self.wallet_address)
            gas_price = w3.eth.gas_price
            
            if neg_risk:
                # NegRisk 市场（新的 BTC/ETH/SOL/XRP 市场）
                adapter = w3.eth.contract(
                    address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                    abi=NEG_RISK_ABI
                )
                tx = adapter.functions.redeemPositions(
                    condition_bytes,
                    [up_balance, down_balance]
                ).build_transaction({
                    "chainId": 137,
                    "from": self.wallet_address,
                    "nonce": nonce,
                    "gas": gas_limit,
                    "gasPrice": int(gas_price * gas_multiplier),
                })
            else:
                # 标准 CTF 市场（旧市场）
                tx = ctf.functions.redeemPositions(
                    Web3.to_checksum_address(USDC_ADDRESS),
                    bytes(32),  # 父集合 ID
                    condition_bytes,
                    [1, 2]  # 索引集合
                ).build_transaction({
                    "chainId": 137,
                    "from": self.wallet_address,
                    "nonce": nonce,
                    "gas": gas_limit,
                    "gasPrice": int(gas_price * gas_multiplier),
                })
            
            # 签名发送，带 Gas 价格错误的重试逻辑
            for retry_attempt in range(1, max_gas_retries + 1):
                try:
                    signed_tx = w3.eth.account.sign_transaction(tx, private_key=self.private_key)
                    tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                    
                    print(f"  TX: {tx_hash.hex()}")
                    print(f"  Waiting for confirmation...")
                    
                    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
                    
                    if receipt.status == 1:
                        # 计算收到的金额（赢家的余额）
                        amount_received = (up_balance if up_payout > 0 else down_balance) / 1e6
                        winner = "UP" if up_payout > 0 else "DOWN"
                        self._log_redeem(market_slug, True, amount_received, tx_hash.hex(), f"WINNER_{winner}")
                        print(f"[REDEEM] ✅ Redeemed ${amount_received:.2f} USDC!")
                        print(f"[REDEEM] TX Hash: {tx_hash.hex()}")
                        
                        # 等待 3 秒再更新余额（让区块链结算）
                        import asyncio
                        try:
                            # 尝试在异步上下文中使用 asyncio.sleep
                            asyncio.get_event_loop()
                            import time
                            time.sleep(3)
                        except RuntimeError:
                            # 不在异步上下文中，使用常规 sleep
                            import time
                            time.sleep(3)
                        
                        print(f"[REDEEM] 🔄 Checking balance after 3s delay...")
                        
                        # 从区块链刷新余额以获得精确金额
                        try:
                            updated_balance = self.get_wallet_usdc_balance()
                            
                            if updated_balance is not None and updated_balance > 0:
                                print(f"[REDEEM] 💰 Blockchain balance refreshed: ${updated_balance:.2f}")
                                
                                # 使用区块链的精确值更新本地余额
                                if self.balance_change_callback:
                                    self.balance_change_callback(updated_balance, "REDEEM_REFRESH", is_absolute=True)
                                    print(f"[REDEEM] ✅ Balance callback called with ${updated_balance:.2f}")
                            else:
                                print(f"[REDEEM] ⚠️ Blockchain query returned None/0, using local update")
                                # 回退到本地更新
                                if self.balance_change_callback:
                                    self.balance_change_callback(+amount_received, "REDEEM")
                                    print(f"[REDEEM] ✅ Balance callback called with +${amount_received:.2f}")
                        except Exception as e:
                            print(f"[REDEEM] ⚠️ Failed to refresh balance: {e}")
                            import traceback
                            traceback.print_exc()
                            # 回退到本地更新
                            if self.balance_change_callback:
                                self.balance_change_callback(+amount_received, "REDEEM")
                                print(f"[REDEEM] ✅ Balance callback called with +${amount_received:.2f} (fallback)")
                        
                        # 🔥 成功赎回后解除市场锁定（按币种）
                        # 从 market_slug 提取币种（例如 "btc-updown-15m-..." → "btc"）
                        coin = None
                        for c in ['btc', 'eth', 'sol', 'xrp']:
                            if f'{c}-updown-' in market_slug:
                                coin = c
                                break
                        
                        if coin:
                            OrderExecutor.unblock_market(market_slug, coin)
                            print(f"[REDEEM] 🔓 Market unblocked for {coin.upper()}")
                        else:
                            print(f"[REDEEM] ⚠️ Could not determine coin from slug: {market_slug}")
                        
                        return True, amount_received
                    else:
                        self._log_redeem(market_slug, False, 0.0, tx_hash.hex(), "TX_REVERTED")
                        print(f"[REDEEM] ❌ TX reverted")
                        return False, 0.0
                
                except Exception as send_error:
                    error_str = str(send_error)
                    
                    # 检查是否是我们要重试的特定 Gas 价格错误
                    if 'replacement transaction underpriced' in error_str:
                        if retry_attempt < max_gas_retries:
                            print(f"[REDEEM] ⚠️ Gas price too low (attempt {retry_attempt}/{max_gas_retries})")
                            print(f"[REDEEM] 🔄 Retrying in {gas_retry_delay}s with higher gas...")
                            
                            import time
                            time.sleep(gas_retry_delay)
                            
                            # 增加 Gas 价格以重试
                            gas_multiplier *= 1.2
                            
                            # 用更高的 Gas 重新构建交易
                            nonce = w3.eth.get_transaction_count(self.wallet_address)
                            gas_price = w3.eth.gas_price
                            
                            if neg_risk:
                                adapter = w3.eth.contract(
                                    address=Web3.to_checksum_address(NEG_RISK_ADAPTER),
                                    abi=NEG_RISK_ABI
                                )
                                tx = adapter.functions.redeemPositions(
                                    condition_bytes,
                                    [up_balance, down_balance]
                                ).build_transaction({
                                    "chainId": 137,
                                    "from": self.wallet_address,
                                    "nonce": nonce,
                                    "gas": gas_limit,
                                    "gasPrice": int(gas_price * gas_multiplier),
                                })
                            else:
                                tx = ctf.functions.redeemPositions(
                                    Web3.to_checksum_address(USDC_ADDRESS),
                                    bytes(32),
                                    condition_bytes,
                                    [1, 2]
                                ).build_transaction({
                                    "chainId": 137,
                                    "from": self.wallet_address,
                                    "nonce": nonce,
                                    "gas": gas_limit,
                                    "gasPrice": int(gas_price * gas_multiplier),
                                })
                            
                            continue  # 用新 Gas 价格重试
                        else:
                            print(f"[REDEEM] ❌ Failed after {max_gas_retries} gas price retries")
                            self._log_redeem(market_slug, False, 0.0, "", f"ERROR: {error_str[:100]}")
                            return False, 0.0
                    else:
                        # 其他错误，不重试
                        raise send_error
                
        except Exception as e:
            self._log_redeem(market_slug, False, 0.0, "", f"ERROR: {str(e)[:100]}")
            print(f"[REDEEM] ❌ Error: {e}")
            import traceback
            logging.exception("Exception occurred")
            return False, 0.0
