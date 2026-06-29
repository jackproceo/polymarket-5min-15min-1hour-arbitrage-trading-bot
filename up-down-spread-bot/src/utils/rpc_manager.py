"""
RPC 多节点管理 - 并行查询、超时控制、自动重试。

从 order_executor.py 提取，职责单一。
"""
import concurrent.futures
import time
from typing import Callable, Optional

from web3 import Web3

from utils.config import Config
from utils.logging_setup import get_logger

log = get_logger("rpc")


class RpcManager:
    """多 RPC 节点管理，支持并行请求和自动故障转移。"""

    def __init__(self, config: Config):
        self.endpoints: list[str] = config.rpc_endpoints
        self.single_timeout: int = config.rpc_single_timeout
        self.parallel_timeout: int = config.rpc_parallel_timeout
        self.retry_attempts: int = config.rpc_retry_attempts
        self.retry_delay: float = config.rpc_retry_delay
        self.parallel_enabled: bool = config.rpc_parallel_enabled

        log.info(
            "RPC: %d endpoints, parallel=%s, timeout=%ds/%ds, retry=%d/%ds",
            len(self.endpoints), self.parallel_enabled,
            self.single_timeout, self.parallel_timeout,
            self.retry_attempts, self.retry_delay,
        )

    def get_web3(self, rpc_url: str) -> Optional[Web3]:
        """创建 Web3 实例，验证连接。"""
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={'timeout': self.single_timeout}))
            if w3.is_connected():
                return w3
        except Exception:
            pass
        return None

    def query(self, query_fn: Callable[[Web3, str], Optional[float]], token_id: str = "") -> Optional[float]:
        """
        通用 RPC 查询：并行/串行调用多个节点，返回第一个成功结果。

        Args:
            query_fn: 签名 (w3: Web3, rpc_url: str) -> Optional[float]
            token_id: 仅用于日志

        Returns:
            查询结果 float，全部失败返回 None
        """
        for attempt in range(1, self.retry_attempts + 1):
            log.info("RPC query attempt %d/%d", attempt, self.retry_attempts)

            if self.parallel_enabled and len(self.endpoints) > 1:
                result = self._query_parallel(query_fn, token_id)
            else:
                result = self._query_sequential(query_fn, token_id)

            if result is not None:
                return result

            if attempt < self.retry_attempts:
                log.info("Retrying in %.1fs...", self.retry_delay)
                time.sleep(self.retry_delay)

        log.error("All %d RPC endpoints failed after %d attempts", len(self.endpoints), self.retry_attempts)
        return None

    def _query_parallel(self, query_fn: Callable, token_id: str) -> Optional[float]:
        """并行查询所有 RPC 节点。"""
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(self.endpoints))
        try:
            futures = {executor.submit(self._safe_query, query_fn, rpc): rpc for rpc in self.endpoints}
            for future in concurrent.futures.as_completed(futures, timeout=self.parallel_timeout):
                try:
                    result = future.result()
                    if result is not None:
                        executor.shutdown(wait=False, cancel_futures=True)
                        return result
                except Exception:
                    continue
        except concurrent.futures.TimeoutError:
            log.warning("Parallel RPC timeout after %ds", self.parallel_timeout)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return None

    def _query_sequential(self, query_fn: Callable, token_id: str) -> Optional[float]:
        """串行查询 RPC 节点。"""
        for rpc in self.endpoints:
            result = self._safe_query(query_fn, rpc)
            if result is not None:
                return result
        return None

    @staticmethod
    def _safe_query(query_fn: Callable, rpc_url: str) -> Optional[float]:
        """安全执行单次 RPC 查询。"""
        try:
            return query_fn(rpc_url)
        except Exception as e:
            rpc_short = rpc_url.split('/')[2][:20] if '://' in rpc_url else rpc_url[:20]
            log.debug("RPC [%s...] failed: %s", rpc_short, e)
            return None
