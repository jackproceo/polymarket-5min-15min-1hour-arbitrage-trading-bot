#!/usr/bin/env python3
"""
Polymarket BTC 5分钟/15分钟 涨跌自动交易 - 交易模块
CLOB客户端和自动赎回。
"""
import os
import json
import time
import threading
import requests
from datetime import datetime

from config import (
    HAS_WEB3, AUTO_REDEEM, POLY_BUILDER_API_KEY, POLY_BUILDER_SECRET,
    POLY_BUILDER_PASSPHRASE, RELAYER_URL, RELAYER_TX_TYPE,
    REDEEM_SCAN_INTERVAL, REDEEM_RETRY_INTERVAL, REDEEM_MAX_PER_SCAN,
    REDEEM_PENDING_LOG_INTERVAL, DATA_API, PROXIES, CTF_CONTRACT, USDC_E_CONTRACT,
    POLYGON_RPC_URL,
)
from state import _dashboard_set
from utils import log, _sync_dashboard_account_snapshot

try:
    from web3 import Web3
except ImportError:
    Web3 = None


class Trader:
    def __init__(self):
        self.client = None
        self.connected = False
        self.address = None

    def connect(self):
        """连接py-clob客户端。"""
        pk = os.getenv("PRIVATE_KEY")
        if not pk:
            log("PRIVATE_KEY 未设置", "ERR")
            return False

        try:
            if not pk.startswith("0x"):
                pk = "0x" + pk

            log("正在连接 CLOB 客户端...")
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY, SELL
            temp = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=pk)
            self.address = temp.get_address()
            log(f"钱包地址: {self.address}")

            creds = temp.create_or_derive_api_creds()
            funder = os.getenv("FUNDER_ADDRESS") or self.address
            sig_type = int(os.getenv("SIGNATURE_TYPE", "2"))

            self.client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
                creds=creds,
                signature_type=sig_type,
                funder=funder
            )
            self.connected = True
            log("CLOB 客户端已连接", "OK")
            return True
        except Exception as e:
            log(f"连接失败: {e}", "ERR")
            return False

    def place_order(self, token_id, side, price, size):
        """下限价单。"""
        if not self.connected:
            log("CLOB 客户端未连接", "ERR")
            return None

        try:
            from py_clob_client.clob_types import OrderArgs
            from py_clob_client.order_builder.constants import BUY, SELL
            log(f"下单: {side} ${size} @ {price:.3f}", "TRADE")

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY if side == "BUY" else SELL
            )

            signed_order = self.client.create_order(order_args)
            resp = self.client.post_order(signed_order)

            if resp and resp.get("orderID"):
                order_id = resp.get("orderID")
                log(f"订单已提交，ID: {order_id}", "OK")
                return order_id
            else:
                log("订单被拒绝", "ERR")
                return None
        except Exception as e:
            log(f"下单错误: {e}", "ERR")
            return None

    def get_order_status(self, order_id):
        """轮询订单状态。"""
        if not self.connected or not order_id:
            return None

        try:
            order = self.client.get_order(order_id)
            if order:
                status = order.get("status", "").upper()
                original_size = float(order.get("original_size", 0) or 0)
                size_matched = float(order.get("size_matched", 0) or 0)

                return {
                    "status": status,
                    "original_size": original_size,
                    "size_matched": size_matched,
                    "filled": size_matched >= original_size if original_size > 0 else False
                }
        except Exception as e:
            log(f"查询订单失败: {e}", "WARN")
        return None

    def cancel_order(self, order_id):
        """取消未成交的订单。"""
        if not self.connected or not order_id:
            return False

        try:
            log(f"取消订单: {order_id}", "WARN")
            resp = self.client.cancel(order_id)
            if resp:
                log("订单已取消", "OK")
                return True
            else:
                log("取消失败", "ERR")
                return False
        except Exception as e:
            log(f"取消错误: {e}", "ERR")
            return False


class AutoRedeemer:
    def __init__(self, private_key, funder_address):
        self.enabled = bool(AUTO_REDEEM)
        self.private_key = (private_key or "").strip()
        if self.private_key and not self.private_key.startswith("0x"):
            self.private_key = "0x" + self.private_key
        self.funder_address = (funder_address or "").strip()
        self.scan_addresses = []
        self.last_try_by_condition = {}
        self.last_pending_signature = ""
        self.last_pending_log_ts = 0.0
        self.running = False
        self.thread = None
        self.relayer_client = None
        self.relayer_error = ""
        self.last_pending_count = 0
        self.last_claimable_count = 0
        self.last_result = {}
        self.last_error = ""

        if not self.enabled:
            _dashboard_set(auto_redeem={"enabled": False, "pending_count": 0, "claimable_count": 0, "last_result": {}, "last_error": ""})
            return
        if not HAS_WEB3 or not Web3:
            log("自动赎回已禁用：未安装 web3", "WARN", force=True)
            self.enabled = False
            _dashboard_set(auto_redeem={"enabled": False, "pending_count": 0, "claimable_count": 0, "last_result": {}, "last_error": "web3 missing"})
            return
        if not self.private_key:
            log("自动赎回已禁用：PRIVATE_KEY 缺失", "WARN", force=True)
            self.enabled = False
            _dashboard_set(auto_redeem={"enabled": False, "pending_count": 0, "claimable_count": 0, "last_result": {}, "last_error": "PRIVATE_KEY missing"})
            return
        if not self.funder_address:
            log("自动赎回已禁用：FUNDER_ADDRESS 缺失（代理钱包）", "WARN", force=True)
            self.enabled = False
            _dashboard_set(auto_redeem={"enabled": False, "pending_count": 0, "claimable_count": 0, "last_result": {}, "last_error": "FUNDER_ADDRESS missing"})
            return
        if not (POLY_BUILDER_API_KEY and POLY_BUILDER_SECRET and POLY_BUILDER_PASSPHRASE):
            log("自动赎回已禁用：POLY_BUILDER_API_KEY/SECRET/PASSPHRASE 缺失", "WARN", force=True)
            self.enabled = False
            _dashboard_set(auto_redeem={"enabled": False, "pending_count": 0, "claimable_count": 0, "last_result": {}, "last_error": "Builder API creds missing"})
            return

        self.scan_addresses = [self.funder_address]

        client, err = self._create_relayer_client()
        if client is None:
            log(f"自动赎回已禁用：中继器初始化失败 {err}", "ERR", force=True)
            self.enabled = False
            _dashboard_set(auto_redeem={"enabled": False, "pending_count": 0, "claimable_count": 0, "last_result": {}, "last_error": str(err)})
            return
        self.relayer_client = client

    def _normalize_condition_id(self, value):
        s = str(value or "").strip().lower()
        if not s:
            return ""
        if s.startswith("0x"):
            s = s[2:]
        if len(s) != 64:
            return ""
        try:
            int(s, 16)
        except Exception:
            return ""
        return "0x" + s

    def _fetch_positions(self, user):
        try:
            r = requests.get(
                f"{DATA_API}/positions",
                params={"user": user, "sizeThreshold": 0},
                proxies=PROXIES if PROXIES else None,
                timeout=12,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    def _create_relayer_client(self):
        try:
            import inspect
            import py_builder_relayer_client.client as rel_mod
            from py_builder_relayer_client.client import RelayClient
            try:
                from py_builder_signing_sdk import BuilderConfig, BuilderApiKeyCreds
            except Exception:
                from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

            cfg = BuilderConfig(
                local_builder_creds=BuilderApiKeyCreds(
                    key=POLY_BUILDER_API_KEY,
                    secret=POLY_BUILDER_SECRET,
                    passphrase=POLY_BUILDER_PASSPHRASE,
                )
            )

            args = [RELAYER_URL, 137, self.private_key, cfg]
            init_params = inspect.signature(RelayClient.__init__).parameters
            if len(init_params) >= 6:
                tx_enum = getattr(rel_mod, "RelayerTxType", None) or getattr(rel_mod, "TransactionType", None)
                tx_value = None
                if tx_enum is not None:
                    if RELAYER_TX_TYPE == "PROXY" and hasattr(tx_enum, "PROXY"):
                        tx_value = getattr(tx_enum, "PROXY")
                    elif hasattr(tx_enum, "SAFE"):
                        tx_value = getattr(tx_enum, "SAFE")
                    elif hasattr(tx_enum, "SAFE_CREATE"):
                        tx_value = getattr(tx_enum, "SAFE_CREATE")
                if tx_value is not None:
                    args.append(tx_value)

            return RelayClient(*args), ""
        except Exception as e:
            return None, str(e)

    def _collect_redeemable(self):
        pending = []
        seen = set()
        claimable = []

        for owner in self.scan_addresses:
            rows = self._fetch_positions(owner)
            owner_l = owner.lower()
            for row in rows:
                if not isinstance(row, dict):
                    continue
                size = row.get("size")
                try:
                    size_f = float(size or 0)
                except Exception:
                    size_f = 0.0
                if size_f <= 0:
                    continue

                redeemable = bool(row.get("redeemable") or row.get("mergeable"))
                if not redeemable:
                    continue

                cid = self._normalize_condition_id(
                    row.get("conditionId") or row.get("condition_id")
                )
                if not cid:
                    continue

                key = owner_l + "|" + cid
                if key in seen:
                    continue
                seen.add(key)
                pending.append({"owner": owner, "condition_id": cid})

                if owner_l == self.funder_address.lower() and cid not in claimable:
                    claimable.append(cid)

        return pending, claimable

    def _redeem_condition(self, condition_id):
        try:
            from py_builder_relayer_client.models import SafeTransaction, OperationType

            ctf_addr = Web3.to_checksum_address(CTF_CONTRACT)
            usdc_addr = Web3.to_checksum_address(USDC_E_CONTRACT)
            contract = Web3().eth.contract(
                address=ctf_addr,
                abi=[{
                    "name": "redeemPositions",
                    "type": "function",
                    "stateMutability": "nonpayable",
                    "inputs": [
                        {"name": "collateralToken", "type": "address"},
                        {"name": "parentCollectionId", "type": "bytes32"},
                        {"name": "conditionId", "type": "bytes32"},
                        {"name": "indexSets", "type": "uint256[]"},
                    ],
                    "outputs": [],
                }],
            )
            cond_bytes = bytes.fromhex(condition_id[2:])
            data = contract.encode_abi(
                abi_element_identifier="redeemPositions",
                args=[usdc_addr, b"\x00" * 32, cond_bytes, [1, 2]],
            )
            op_call = getattr(OperationType, "Call", None)
            if op_call is None:
                op_call = list(OperationType)[0]
            tx = SafeTransaction(to=str(ctf_addr), operation=op_call, data=str(data), value="0")

            def execute_once():
                resp = self.relayer_client.execute([tx], f"Redeem {condition_id}")
                result = resp.wait()
                txh = str(getattr(resp, "transaction_hash", "") or "")
                state = ""
                if isinstance(result, dict):
                    txh = str(result.get("transaction_hash") or result.get("transactionHash") or txh)
                    state = str(result.get("state") or "")
                else:
                    txh = str(getattr(result, "transaction_hash", "") or getattr(result, "transactionHash", "") or txh)
                    state = str(getattr(result, "state", "") or "")
                if result is None:
                    return False, txh, "relayer_not_confirmed"
                if state and state not in ["STATE_CONFIRMED", "STATE_MINED", "STATE_EXECUTED"]:
                    return False, txh, f"state={state}"
                return True, txh, ""

            try:
                return execute_once()
            except Exception as e:
                msg = str(e)
                low = msg.lower()
                if "expected safe" in low and "not deployed" in low:
                    dep = self.relayer_client.deploy()
                    dep.wait()
                    return execute_once()
                return False, "", msg
        except Exception as e:
            return False, "", str(e)

    def scan_once(self):
        if not self.enabled:
            return

        pending, claimable = self._collect_redeemable()
        now = time.time()
        self.last_pending_count = len(pending)
        self.last_claimable_count = len(claimable)
        _dashboard_set(auto_redeem={
            "enabled": self.enabled,
            "pending_count": self.last_pending_count,
            "claimable_count": self.last_claimable_count,
            "last_result": dict(self.last_result or {}),
            "last_error": self.last_error,
            "scan_interval": REDEEM_SCAN_INTERVAL,
        })

        if pending:
            signature = "|".join([f"{x['owner']}:{x['condition_id']}" for x in pending])
            if signature != self.last_pending_signature or (now - self.last_pending_log_ts) >= REDEEM_PENDING_LOG_INTERVAL:
                self.last_pending_signature = signature
                self.last_pending_log_ts = now
                owners = sorted(list({x["owner"] for x in pending}))
                owner_text = ", ".join(owners[:3])
                if len(owners) > 3:
                    owner_text += f" +{len(owners) - 3} more"
                log(f"可赎回待处理: {len(pending)}, 中继器可领取: {len(claimable)}, 拥有者: {owner_text}", "WARN", force=True)

        if not claimable:
            return

        processed = 0
        for cid in claimable:
            t0 = self.last_try_by_condition.get(cid, 0)
            if now - t0 < REDEEM_RETRY_INTERVAL:
                continue
            self.last_try_by_condition[cid] = now

            ok, tx_hash, err = self._redeem_condition(cid)
            if ok:
                log(f"中继器兑换成功: {cid} | 交易 {tx_hash}", "TRADE", force=True)
                self.last_error = ""
                self.last_result = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ok": True,
                    "condition_id": cid,
                    "tx": tx_hash,
                    "message": "ok",
                }
            else:
                log(f"中继器兑换失败: {cid} | {err}", "ERR", force=True)
                self.last_error = str(err)
                self.last_result = {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "ok": False,
                    "condition_id": cid,
                    "tx": tx_hash,
                    "message": str(err),
                }

            _dashboard_set(auto_redeem={
                "enabled": self.enabled,
                "pending_count": self.last_pending_count,
                "claimable_count": self.last_claimable_count,
                "last_result": dict(self.last_result or {}),
                "last_error": self.last_error,
                "scan_interval": REDEEM_SCAN_INTERVAL,
            })
            _sync_dashboard_account_snapshot(self.funder_address)

            processed += 1
            if processed >= REDEEM_MAX_PER_SCAN:
                break

    def _loop(self):
        while self.running:
            try:
                self.scan_once()
            except Exception as e:
                log(f"自动赎回扫描错误: {e}", "ERR", force=True)
            for _ in range(REDEEM_SCAN_INTERVAL):
                if not self.running:
                    break
                time.sleep(1)

    def start(self):
        if not self.enabled:
            return
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        log(f"自动赎回已启动：每 {REDEEM_SCAN_INTERVAL}s 扫描一次", "OK", force=True)
        _dashboard_set(auto_redeem={
            "enabled": self.enabled,
            "pending_count": self.last_pending_count,
            "claimable_count": self.last_claimable_count,
            "last_result": dict(self.last_result or {}),
            "last_error": self.last_error,
            "scan_interval": REDEEM_SCAN_INTERVAL,
        })

    def stop(self):
        self.running = False
