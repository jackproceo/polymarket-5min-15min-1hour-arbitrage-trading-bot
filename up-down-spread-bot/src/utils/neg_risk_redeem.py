"""
NegRisk 赎回模块 - 从 order_executor.py 提取。

职责：检查 CTF 代币余额、预言机解析、构建/签名/发送赎回交易。
不依赖 OrderExecutor 实例，通过参数注入所有依赖。
"""
import logging
import time
import traceback
from typing import Callable, Optional

from web3 import Web3

from utils.logging_setup import get_logger

log = get_logger("redeem")

# ── 合约地址 ──
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# CTF ABI（余额 + 预言机解析 + 赎回）
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


def redeem_position(
    w3: Web3,
    wallet_address: str,
    private_key: str,
    ctf_address: str,
    market_slug: str,
    condition_id: str,
    up_token_id: str,
    down_token_id: str,
    neg_risk: bool = True,
    dry_run: bool = False,
    gas_limit: int = 500000,
    gas_multiplier: float = 1.5,
    balance_callback: Optional[Callable] = None,
    unblock_callback: Optional[Callable] = None,
    log_callback: Optional[Callable] = None,
) -> tuple[bool, float]:
    """
    赎回已完成市场的仓位。

    Args:
        w3: 已连接的 Web3 实例
        wallet_address: 钱包地址
        private_key: 私钥（十六进制字符串）
        ctf_address: CTF 合约地址
        market_slug: 市场标识符
        condition_id: CTF 条件 ID（带 0x 前缀）
        up_token_id: UP 令牌 ID
        down_token_id: DOWN 令牌 ID
        neg_risk: True=使用 NegRisk 适配器，False=直接使用 CTF
        dry_run: 模拟模式，不提交交易
        gas_limit: Gas 上限
        gas_multiplier: Gas 价格倍数（初始值）
        balance_callback: 可选，签名 (new_balance: float, reason: str, is_absolute: bool)
        unblock_callback: 可选，签名 (market_slug: str, coin: str)
        log_callback: 可选，签名 (market_slug, success: bool, amount: float, tx_hash, reason)

    Returns:
        (success: bool, amount_received_usd: float)
    """
    max_gas_retries = 5
    gas_retry_delay = 3

    if dry_run:
        log.info("[DRY-RUN] Would redeem %s", market_slug)
        return True, 0.0

    log.info("Redemption: %s", market_slug)

    try:
        ctf = w3.eth.contract(address=Web3.to_checksum_address(ctf_address), abi=CTF_ABI)

        # 检查代币余额
        up_balance = ctf.functions.balanceOf(wallet_address, int(up_token_id)).call()
        down_balance = ctf.functions.balanceOf(wallet_address, int(down_token_id)).call()
        log.info("  UP: %.2f, DOWN: %.2f", up_balance / 1e6, down_balance / 1e6)

        if up_balance == 0 and down_balance == 0:
            if log_callback:
                log_callback(market_slug, True, 0.0, "", "NO_TOKENS")
            log.info("No tokens to redeem")
            return True, 0.0

        # 检查预言机解析
        condition_bytes = Web3.to_bytes(hexstr=condition_id)
        payout_denom = ctf.functions.payoutDenominator(condition_bytes).call()

        if payout_denom == 0:
            if log_callback:
                log_callback(market_slug, False, 0.0, "", "ORACLE_NOT_RESOLVED")
            log.warning("Oracle not resolved yet")
            return False, 0.0

        # 确定赢家
        up_payout = ctf.functions.payoutNumerators(condition_bytes, 0).call()
        down_payout = ctf.functions.payoutNumerators(condition_bytes, 1).call()
        winner = "UP" if up_payout > 0 else "DOWN" if down_payout > 0 else "UNKNOWN"
        log.info("  Oracle: %s won", winner)

        # 构建交易
        nonce = w3.eth.get_transaction_count(wallet_address)
        gas_price = w3.eth.gas_price
        current_multiplier = gas_multiplier

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
                "from": wallet_address,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": int(gas_price * current_multiplier),
            })
        else:
            tx = ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                bytes(32),
                condition_bytes,
                [1, 2]
            ).build_transaction({
                "chainId": 137,
                "from": wallet_address,
                "nonce": nonce,
                "gas": gas_limit,
                "gasPrice": int(gas_price * current_multiplier),
            })

        # 签名发送，带 Gas 价格重试
        for retry_attempt in range(1, max_gas_retries + 1):
            try:
                signed_tx = w3.eth.account.sign_transaction(tx, private_key=private_key)
                tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                log.info("  TX: %s", tx_hash.hex())

                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)

                if receipt.status == 1:
                    amount_received = (up_balance if up_payout > 0 else down_balance) / 1e6
                    win_side = "UP" if up_payout > 0 else "DOWN"
                    if log_callback:
                        log_callback(market_slug, True, amount_received, tx_hash.hex(), f"WINNER_{win_side}")
                    log.info("Redeemed $%.2f USDC! TX: %s", amount_received, tx_hash.hex())

                    # 等待区块链结算
                    time.sleep(3)

                    # 回调：余额更新
                    if balance_callback:
                        try:
                            balance_callback(+amount_received, "REDEEM")
                        except Exception as e:
                            log.warning("Balance callback failed: %s", e)

                    # 回调：解除市场锁定
                    if unblock_callback:
                        try:
                            coin = _coin_from_slug(market_slug)
                            if coin:
                                unblock_callback(market_slug, coin)
                        except Exception as e:
                            log.warning("Unblock callback failed: %s", e)

                    return True, amount_received
                else:
                    if log_callback:
                        log_callback(market_slug, False, 0.0, tx_hash.hex(), "TX_REVERTED")
                    log.error("TX reverted")
                    return False, 0.0

            except Exception as send_error:
                error_str = str(send_error)
                if 'replacement transaction underpriced' in error_str:
                    if retry_attempt < max_gas_retries:
                        current_multiplier *= 1.2
                        log.warning("Gas retry %d/%d (multiplier=%.1f)",
                                    retry_attempt, max_gas_retries, current_multiplier)
                        time.sleep(gas_retry_delay)

                        nonce = w3.eth.get_transaction_count(wallet_address)
                        gas_price = w3.eth.gas_price

                        if neg_risk:
                            tx = adapter.functions.redeemPositions(
                                condition_bytes,
                                [up_balance, down_balance]
                            ).build_transaction({
                                "chainId": 137,
                                "from": wallet_address,
                                "nonce": nonce,
                                "gas": gas_limit,
                                "gasPrice": int(gas_price * current_multiplier),
                            })
                        else:
                            tx = ctf.functions.redeemPositions(
                                Web3.to_checksum_address(USDC_ADDRESS),
                                bytes(32),
                                condition_bytes,
                                [1, 2]
                            ).build_transaction({
                                "chainId": 137,
                                "from": wallet_address,
                                "nonce": nonce,
                                "gas": gas_limit,
                                "gasPrice": int(gas_price * current_multiplier),
                            })
                        continue
                    else:
                        if log_callback:
                            log_callback(market_slug, False, 0.0, "", f"GAS_RETRIES_EXCEEDED: {error_str[:100]}")
                        log.error("Failed after %d gas retries", max_gas_retries)
                        return False, 0.0
                else:
                    raise send_error

    except Exception as e:
        if log_callback:
            log_callback(market_slug, False, 0.0, "", f"ERROR: {str(e)[:100]}")
        log.error("Redeem error: %s", e)
        traceback.print_exc()
        return False, 0.0


def _coin_from_slug(market_slug: str) -> Optional[str]:
    """从 market_slug 提取币种。"""
    for c in ['btc', 'eth', 'sol', 'xrp']:
        if f'{c}-updown-' in market_slug:
            return c
    return None
