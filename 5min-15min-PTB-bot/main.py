#!/usr/bin/env python3
"""
Polymarket BTC 5分钟/15分钟 涨跌自动交易 - 主循环模块
主循环、规则评估、订单管理、止盈止损。
"""
import os
import time
import json
import threading
from datetime import datetime

from config import (
    BASE_DIR, _btc_market_minutes, _market_interval_sec,
    WEB_ENABLED, WEB_HOST, WEB_PORT, SIMULATION_MODE, AUTO_TRADE,
    C1_TIME, C1_DIFF, C1_MIN_PROB, C1_MAX_PROB,
    C2_TIME, C2_DIFF, C2_MIN_PROB, C2_MAX_PROB,
    C3_TIME, C3_DIFF, C3_MIN_PROB, C3_MAX_PROB,
    C4_TIME, C4_DIFF, C4_MIN_PROB, C4_MAX_PROB,
    TRADE_AMOUNT, ORDER_TIMEOUT_SEC, SLIPPAGE_THRESHOLD,
    MAX_RETRY_PER_MARKET, BUY_RETRY_STEP,
    STOP_LOSS_PROB_PCT, TAKE_PROFIT_RR, TAKE_PROFIT_CAP,
    TAKE_PROFIT_RETRY_STEP, TAKE_PROFIT_RETRY_MAX,
    MARKET_DATA_MAX_LAG_SEC, LOOP_INTERVAL_SEC,
    DASHBOARD_ACCOUNT_SYNC_SEC, MARKET_META_REFRESH_SEC,
    AUTO_REDEEM, TRADING_ANALYSIS_LOG,
)
from state import price_data, dashboard_state
from utils import (
    log, _trigger_price_refresh, _trigger_market_refresh, _clear_market_cache,
    _get_market_cache, _trigger_account_sync, load_state, save_state,
    _dashboard_pending_order_from_state, _append_trade_history,
    _normalize_state, get_crypto_price_api,
    _shares_from_usdc_buy, _btc_ptb_snapshot,
    _to_float, _maybe_float,
    _planned_take_profit_stop_loss, _emit_trading_analysis,
    _init_trading_analysis_session, _sync_dashboard_account_snapshot,
    reset_ptb_backoff,
)
from state import _dashboard_set
from websocket_feeds import MarketPriceListener
from trader import Trader, AutoRedeemer
from dashboard import start_web_server
from database import init_db, insert_trade, insert_trade_close, insert_account_snapshot, get_trade_stats


def main():
    start_web_server()
    if WEB_ENABLED:
        log(f"仪表盘: http://{WEB_HOST}:{WEB_PORT}", "OK", force=True)

    print("\n" + "="*60)
    print(f"  Polymarket BTC {_btc_market_minutes}m auto-trader")
    print("="*60)
    print(f"  Simulation mode: {'on (paper, no CLOB)' if SIMULATION_MODE else 'off'}")
    print(f"  Auto trade: {'on' if AUTO_TRADE else 'off'}")
    print(f"  BTC market window: {_btc_market_minutes}m (config BTC_MARKET_MINUTES or dashboard)")
    print(f"  Trading analysis log: {TRADING_ANALYSIS_LOG}")
    print(f"  Auto redeem: {'on' if AUTO_REDEEM else 'off'}")
    _init_trading_analysis_session()
    log(f"交易分析日志就绪（追加模式）: {TRADING_ANALYSIS_LOG}", "OK", force=True)
    print(f"  Order size: ${TRADE_AMOUNT}")
    print(f"  Rule 1: time\u2264{C1_TIME}s and diff\u2265${C1_DIFF} (UP prob {C1_MIN_PROB*100:.0f}-{C1_MAX_PROB*100:.0f}%)")
    print(f"  Rule 2: time\u2264{C2_TIME}s and diff\u2264-${C2_DIFF} (DOWN prob {C2_MIN_PROB*100:.0f}-{C2_MAX_PROB*100:.0f}%)")
    print(f"  Rule 3: time\u2264{C3_TIME}s and diff\u2265${C3_DIFF} (UP prob {C3_MIN_PROB*100:.0f}-{C3_MAX_PROB*100:.0f}%)")
    print(f"  Rule 4: time\u2264{C4_TIME}s and diff\u2264-${C4_DIFF} (DOWN prob {C4_MIN_PROB*100:.0f}-{C4_MAX_PROB*100:.0f}%)")
    print(f"  TP/SL: prob-based (SL {STOP_LOSS_PROB_PCT*100:.0f}%, RR\u2248{TAKE_PROFIT_RR:.2f}, TP cap {TAKE_PROFIT_CAP*100:.1f}%)")
    print(f"  Cancel after: {ORDER_TIMEOUT_SEC}s unfilled")
    print(f"  Slippage cap: {SLIPPAGE_THRESHOLD*100:.0f}%")
    print(f"  Max retries / market: {MAX_RETRY_PER_MARKET}")
    print(f"  Chase step: +{BUY_RETRY_STEP*100:.1f}% per retry")
    print(f"  TP retries: up to {TAKE_PROFIT_RETRY_MAX}, step +{TAKE_PROFIT_RETRY_STEP*100:.1f}%")
    print(f"  Stop: entry prob down {STOP_LOSS_PROB_PCT*100:.0f}%")
    print(f"  Stale data skip: >{MARKET_DATA_MAX_LAG_SEC:.1f}s")
    print(f"  Loop interval: {LOOP_INTERVAL_SEC:.2f}s")
    print("="*60 + "\n")

    trader = Trader()
    redeemer = AutoRedeemer(os.getenv("PRIVATE_KEY"), os.getenv("FUNDER_ADDRESS"))
    if SIMULATION_MODE:
        log("模拟模式：纸面交易 — 即时成交，不下真实订单", "OK", force=True)
    elif AUTO_TRADE:
        if not trader.connect():
            log("无法连接 CLOB 客户端，退出", "ERR", force=True)
            return
    redeemer.start()

    init_state = load_state()
    _dashboard_set(
        position=dict(init_state.get("position") or {}),
        pending_order=_dashboard_pending_order_from_state(init_state),
        last_order=dict(init_state.get("last_order") or {}),
        trade_history=list(init_state.get("trade_history") or []),
        wallet_balance=None,
        wallet_positions=[],
        wallet_history=[],
        live_trades=[],
        live_positions_count=0,
        live_realized_pnl=0.0,
        live_unrealized_pnl=0.0,
        live_total_pnl=0.0,
        cumulative_realized_pnl=float(init_state.get("cumulative_realized_pnl") or 0.0),
        simulation_mode=SIMULATION_MODE,
    )

    log("正在启动价格数据源...", "INFO", force=True)

    last_slug = None
    market_listener = None
    first_display = True
    last_chainlink_update = 0
    last_account_sync = 0.0
    last_market_fetch = 0.0
    last_stale_log_ts = 0.0
    dashboard_user = (os.getenv("FUNDER_ADDRESS", "") or "").strip().lower()
    if not dashboard_user:
        dashboard_user = (os.getenv("PRIVATE_KEY_ADDRESS", "") or "").strip().lower()
    if not SIMULATION_MODE and AUTO_TRADE and trader.address:
        dashboard_user = ((os.getenv("FUNDER_ADDRESS", "") or trader.address) or "").strip().lower()
    _trigger_market_refresh()
    _trigger_account_sync(dashboard_user)
    init_db()

    try:
        while True:
            now = time.time()

            if now - last_chainlink_update > 5:
                _trigger_price_refresh()
                last_chainlink_update = now

            if now - last_market_fetch >= MARKET_META_REFRESH_SEC:
                _trigger_market_refresh()
                last_market_fetch = now

            market = None
            market_data_cache = _get_market_cache()
            if market_data_cache:
                try:
                    end_ts = datetime.fromisoformat(str(market_data_cache.get("end", "")).replace("Z", "+00:00")).timestamp()
                    remaining_live = int(end_ts - now)
                except Exception:
                    remaining_live = 0
                if remaining_live <= 0:
                    _clear_market_cache()
                    last_market_fetch = 0.0
                    reset_ptb_backoff()
                else:
                    market = dict(market_data_cache)
                    market["remaining"] = remaining_live

            if now - last_account_sync >= DASHBOARD_ACCOUNT_SYNC_SEC:
                _trigger_account_sync(dashboard_user)
                last_account_sync = now

            if not market:
                state_snapshot = load_state()
                _dashboard_set(
                    market={"slug": "", "remaining": 0, "status": "waiting"},
                    prices={
                        "ptb": price_data.get("ptb"),
                        "chainlink_btc": price_data.get("btc"),
                        "binance_btc": price_data.get("binance"),
                        "up_price": price_data.get("up_price"),
                        "down_price": price_data.get("down_price"),
                        "diff": None,
                        "diff_abs": None,
                    },
                    position=dict(state_snapshot.get("position") or {}),
                    pending_order=_dashboard_pending_order_from_state(state_snapshot),
                    last_order=dict(state_snapshot.get("last_order") or {}),
                    trade_history=list(state_snapshot.get("trade_history") or []),
                    btc_market_minutes=_btc_market_minutes,
                    cumulative_realized_pnl=float(state_snapshot.get("cumulative_realized_pnl") or 0.0),
                    simulation_mode=SIMULATION_MODE,
                )
                if first_display:
                    print("\nWaiting for active market...")
                    if price_data["btc"]:
                        print(f"BTC (Chainlink): ${price_data['btc']:,.2f}")
                time.sleep(LOOP_INTERVAL_SEC)
                continue

            slug = market["slug"]
            remaining = market["remaining"]

            if last_slug and slug != last_slug:
                reset_ptb_backoff(slug)
                if market_listener:
                    market_listener.stop()

                state = load_state()
                state.pop("position", None)
                state.pop("last_order", None)
                state.pop("take_profit_order", None)
                save_state(state)

                market_listener = MarketPriceListener(market["up_token"], market["down_token"])
                market_listener.start()

                price_data["ptb"] = None

                first_display = True

                time.sleep(2)

            elif not last_slug:
                market_listener = MarketPriceListener(market["up_token"], market["down_token"])
                market_listener.start()
                time.sleep(2)

            last_slug = slug

            if not price_data["ptb"]:
                crypto_data = get_crypto_price_api(market["start"], market["end"])
                if crypto_data.get("openPrice"):
                    price_data["ptb"] = crypto_data["openPrice"]
                elif crypto_data.get("closePrice"):
                    price_data["ptb"] = crypto_data["closePrice"]
                    log(f"使用上一窗口收盘价作为 PTB: {price_data['ptb']}", "INFO")

            btc = _to_float(price_data.get("btc"), 0.0)
            ptb = _to_float(price_data.get("ptb"), 0.0)
            up_price = _to_float(price_data.get("up_price") if price_data.get("up_price") is not None else market.get("up_price"), 0.0)
            down_price = _to_float(price_data.get("down_price") if price_data.get("down_price") is not None else market.get("down_price"), 0.0)
            up_bid = _maybe_float(price_data.get("up_bid"))
            up_ask = _maybe_float(price_data.get("up_ask"))
            down_bid = _maybe_float(price_data.get("down_bid"))
            down_ask = _maybe_float(price_data.get("down_ask"))

            up_entry_price = up_ask if (up_ask is not None and up_ask > 0) else up_price
            down_entry_price = down_ask if (down_ask is not None and down_ask > 0) else down_price

            diff = btc - ptb if (btc > 0 and ptb > 0) else 0
            diff_abs = abs(diff)
            _dashboard_set(
                market={
                    "slug": slug,
                    "remaining": remaining,
                    "remaining_text": f"{remaining//60}m {remaining%60}s",
                    "start": market.get("start"),
                    "end": market.get("end"),
                    "status": "active",
                },
                prices={
                    "ptb": ptb if ptb > 0 else None,
                    "chainlink_btc": btc if btc > 0 else None,
                    "binance_btc": (price_data.get("binance") or None),
                    "up_price": up_price,
                    "down_price": down_price,
                    "up_bid": up_bid,
                    "up_ask": up_ask,
                    "down_bid": down_bid,
                    "down_ask": down_ask,
                    "diff": diff if (btc > 0 and ptb > 0) else None,
                    "diff_abs": diff_abs if (btc > 0 and ptb > 0) else None,
                    "updated_ts": time.time(),
                },
                btc_market_minutes=_btc_market_minutes,
            )

            state_snapshot = load_state()
            _dashboard_set(
                position=dict(state_snapshot.get("position") or {}),
                pending_order=_dashboard_pending_order_from_state(state_snapshot),
                last_order=dict(state_snapshot.get("last_order") or {}),
                trade_history=list(state_snapshot.get("trade_history") or []),
                cumulative_realized_pnl=float(state_snapshot.get("cumulative_realized_pnl") or 0.0),
                simulation_mode=SIMULATION_MODE,
            )

            if first_display:
                print("\n" + "="*90)
                print(f"Market: {slug}")
                print(f"Time left: {remaining//60}m {remaining%60}s")
                print()
                print("\u250c" + "\u2500"*24 + "\u252c" + "\u2500"*24 + "\u252c" + "\u2500"*24 + "\u2510")
                print("\u2502 PTB                    \u2502 Chainlink (ref)        \u2502 Binance (ref)          \u2502")
                ptb_display = f"${ptb:,.2f}" if ptb > 0 else "fetching..."
                btc_display = f"${btc:,.2f}" if btc > 0 else "fetching..."
                binance = price_data.get("binance") or 0
                binance_display = f"${binance:,.2f}" if binance > 0 else "fetching..."
                print(f"\u2502 {ptb_display:22s} \u2502 {btc_display:22s} \u2502 {binance_display:22s} \u2502")
                print("\u251c" + "\u2500"*24 + "\u2534" + "\u2500"*24 + "\u2534" + "\u2500"*24 + "\u2524")
                print("\u2502 Market mid                                                               \u2502")
                print(f"\u2502 UP: {up_price*100:.2f}%  DOWN: {down_price*100:.2f}%                                                \u2502")
                print("\u251c" + "\u2500"*74 + "\u2524")
                print("\u2502 Live diff (Chainlink - PTB)                                              \u2502")
                if btc > 0 and ptb > 0:
                    diff_display = f"{diff:+.0f} USD"
                else:
                    diff_display = "waiting for prices..."
                print(f"\u2502 {diff_display:72s} \u2502")
                print("\u2514" + "\u2500"*74 + "\u2518")
                print()
                print("="*90)
                print("Live log:")
                print("="*90)
                first_display = False

            ptb_str = f"${ptb:,.0f}" if ptb > 0 else "..."
            btc_str = f"${btc:,.0f}" if btc > 0 else "..."
            binance = price_data.get("binance") or 0
            binance_str = f"${binance:,.0f}" if binance > 0 else "N/A"
            diff_str = f"{diff:+.0f}" if (btc > 0 and ptb > 0) else "N/A"
            status = f"[{datetime.now().strftime('%H:%M:%S')}] left {remaining//60:02d}m{remaining%60:02d}s | CL:{btc_str} | BN:{binance_str} | PTB:{ptb_str} | diff:{diff_str} | UP:{up_price*100:.1f}% DOWN:{down_price*100:.1f}%"
            print(f"\r{status}" + " "*10, end="", flush=True)

            triggered = False
            condition = None
            side = None
            desired_side = None
            price = None
            token = None

            if remaining <= C1_TIME and diff >= C1_DIFF:
                prob = up_entry_price
                if C1_MIN_PROB <= prob <= C1_MAX_PROB:
                    triggered = True
                    desired_side = "UP"
                    condition = f"R1: time\u2264{C1_TIME}s & diff\u2265${C1_DIFF} (UP {prob*100:.0f}%)"
                else:
                    log(f"条件1 跳过：上涨概率 {prob*100:.1f}% 不在 {C1_MIN_PROB*100:.0f}-{C1_MAX_PROB*100:.0f}% 范围", "INFO")

            elif remaining <= C2_TIME and diff <= -C2_DIFF:
                prob = down_entry_price
                if C2_MIN_PROB <= prob <= C2_MAX_PROB:
                    triggered = True
                    desired_side = "DOWN"
                    condition = f"R2: time\u2264{C2_TIME}s & diff\u2264-${C2_DIFF} (DOWN {prob*100:.0f}%)"
                else:
                    log(f"条件2 跳过：下跌概率 {prob*100:.1f}% 不在 {C2_MIN_PROB*100:.0f}-{C2_MAX_PROB*100:.0f}% 范围", "INFO")

            elif remaining <= C3_TIME and diff >= C3_DIFF:
                prob = up_entry_price
                if C3_MIN_PROB <= prob <= C3_MAX_PROB:
                    triggered = True
                    desired_side = "UP"
                    condition = f"R3: time\u2264{C3_TIME}s & diff\u2265${C3_DIFF} (UP {prob*100:.0f}%)"
                else:
                    log(f"条件3 跳过：上涨概率 {prob*100:.1f}% 不在 {C3_MIN_PROB*100:.0f}-{C3_MAX_PROB*100:.0f}% 范围", "INFO")

            elif remaining <= C4_TIME and diff <= -C4_DIFF:
                prob = down_entry_price
                if C4_MIN_PROB <= prob <= C4_MAX_PROB:
                    triggered = True
                    desired_side = "DOWN"
                    condition = f"R4: time\u2264{C4_TIME}s & diff\u2264-${C4_DIFF} (DOWN {prob*100:.0f}%)"
                else:
                    log(f"条件4 跳过：下跌概率 {prob*100:.1f}% 不在 {C4_MIN_PROB*100:.0f}-{C4_MAX_PROB*100:.0f}% 范围", "INFO")

            if triggered:
                side = desired_side or ("UP" if diff > 0 else "DOWN")
                price = up_entry_price if side == "UP" else down_entry_price
                token = market["up_token"] if side == "UP" else market["down_token"]

                side_ts = _to_float(price_data.get("up_update_ts" if side == "UP" else "down_update_ts"), 0.0)
                btc_ts = _to_float(price_data.get("btc_update_ts"), 0.0)
                side_age = now - side_ts if side_ts > 0 else 999.0
                btc_age = now - btc_ts if btc_ts > 0 else 999.0
                if price <= 0:
                    triggered = False
                    condition = None
                elif side_age > MARKET_DATA_MAX_LAG_SEC or btc_age > MARKET_DATA_MAX_LAG_SEC:
                    if now - last_stale_log_ts >= 2:
                        log(f"陈旧数据跳过：{side} 订单簿年龄 {side_age:.2f}s，BTC 年龄 {btc_age:.2f}s（上限 {MARKET_DATA_MAX_LAG_SEC:.1f}s）",
                            "WARN",
                        )
                        last_stale_log_ts = now
                    triggered = False
                    condition = None

                state = load_state()
                last_order = state.get("last_order", {})
                order_key = f"{slug}|{side}"

                pending_order = state.get("pending_order")
                _dashboard_set(
                    position=dict(state.get("position") or {}),
                    pending_order=_dashboard_pending_order_from_state(state),
                    last_order=dict(last_order or {}),
                )
                if pending_order and (not SIMULATION_MODE) and AUTO_TRADE and trader.connected:
                    order_id = pending_order.get("order_id")
                    order_time = pending_order.get("time")

                    if order_time:
                        elapsed = (datetime.now() - datetime.fromisoformat(order_time)).total_seconds()
                        if elapsed > ORDER_TIMEOUT_SEC:
                            order_status = trader.get_order_status(order_id)
                            if order_status and not order_status.get("filled"):
                                log(f"订单超时，取消并重试（ID {order_id}）", "TRADE")
                                trader.cancel_order(order_id)
                                state.pop("pending_order", None)
                                save_state(state)
                                _emit_trading_analysis(
                                    "BUY_CANCEL_TIMEOUT",
                                    slug=slug,
                                    order_id=order_id,
                                    status="buy",
                                    shares_type=pending_order.get("side"),
                                    share_price=float(pending_order.get("price") or 0) or None,
                                    btc_price=btc if btc > 0 else None,
                                    chainlink_btc=btc if btc > 0 else None,
                                    ptb=ptb if ptb > 0 else None,
                                    btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                    remaining_sec=remaining,
                                    pnl_trade_usd=None,
                                    pnl_total_usd=_to_float(state.get("cumulative_realized_pnl"), 0.0),
                                )
                                _dashboard_set(
                                    position=dict(state.get("position") or {}),
                                    pending_order=_dashboard_pending_order_from_state(state),
                                    last_order=dict(state.get("last_order") or {}),
                                )
                            elif order_status and order_status.get("filled"):
                                filled_side = pending_order.get("side") or side
                                filled_price = float(pending_order.get("price") or price or 0)
                                filled_slug = pending_order.get("slug") or slug
                                log(f"已成交 {filled_side} @ {filled_price*100:.2f}%（{filled_slug}）", "TRADE")
                                state.pop("pending_order", None)
                                filled_size = float(order_status.get("size_matched") or order_status.get("original_size") or TRADE_AMOUNT)
                                state["position"] = {
                                    "slug": filled_slug,
                                    "side": filled_side,
                                    "entry_price": filled_price,
                                    "entry_diff": diff_abs,
                                    "size": filled_size,
                                }
                                cum = _to_float(state.get("cumulative_realized_pnl"), 0.0)
                                state = _append_trade_history(state, {
                                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                    "slug": filled_slug,
                                    "action": "BUY",
                                    "side": filled_side,
                                    "price": filled_price,
                                    "amount": TRADE_AMOUNT,
                                    "shares": filled_size,
                                    "order_size_usdc": TRADE_AMOUNT,
                                    "order_id": order_id,
                                    "status": "filled",
                                    "reason": "pending_filled",
                                    "diff": diff,
                                    "btc": btc if btc > 0 else None,
                                    "ptb": ptb if ptb > 0 else None,
                                    "btc_minus_ptb": _btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                    "remaining_sec": remaining,
                                    "cumulative_realized_pnl_usd": cum,
                                })
                                _emit_trading_analysis(
                                    "BUY_FILL",
                                    action="BUY",
                                    slug=filled_slug,
                                    status="buy",
                                    shares_type=filled_side,
                                    share_price=filled_price,
                                    share_amount=filled_size,
                                    order_size_usdc=TRADE_AMOUNT,
                                    order_id=order_id,
                                    reason="pending_filled",
                                    btc_price=btc if btc > 0 else None,
                                    chainlink_btc=btc if btc > 0 else None,
                                    ptb=ptb if ptb > 0 else None,
                                    btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                    diff_rule=diff,
                                    remaining_sec=remaining,
                                    pnl_trade_usd=0.0,
                                    pnl_total_usd=cum,
                                )
                                insert_trade(
                                    polymarket_slug=filled_slug,
                                    order_id=order_id,
                                    side=filled_side,
                                    action="BUY",
                                    open_reason="pending_filled",
                                    entry_price=filled_price,
                                    amount_usdc=TRADE_AMOUNT,
                                    shares=filled_size,
                                    diff_at_trade=diff if (btc > 0 and ptb > 0) else None,
                                    btc_price=btc if btc > 0 else None,
                                    ptb_price=ptb if ptb > 0 else None,
                                    remaining_sec=remaining,
                                    status="filled",
                                    result="pending",
                                    btc_market_minutes=_btc_market_minutes,
                                    simulation=SIMULATION_MODE,
                                )
                                save_state(state)
                                _dashboard_set(
                                    position=dict(state.get("position") or {}),
                                    pending_order=_dashboard_pending_order_from_state(state),
                                    last_order=dict(state.get("last_order") or {}),
                                    trade_history=list(state.get("trade_history") or []),
                                    cumulative_realized_pnl=cum,
                                )
                                _sync_dashboard_account_snapshot(dashboard_user)

                has_position = bool(state.get("position"))
                retry_count = int(last_order.get("retry_count", 0) or 0)
                same_key_retry = (last_order.get("key") == order_key)
                can_place = (not pending_order) and (not has_position) and ((not same_key_retry) or (retry_count < MAX_RETRY_PER_MARKET))
                if can_place:
                    if same_key_retry and retry_count > 0:
                        last_price = _to_float(last_order.get("last_price"), price)
                        retry_cap_price = min(0.995, last_price + BUY_RETRY_STEP)
                        if price > retry_cap_price:
                            log(
                                f"追价上限：{price*100:.2f}% > 上次 {last_price*100:.2f}%+{BUY_RETRY_STEP*100:.2f}%，使用 {retry_cap_price*100:.2f}%",
                                "INFO",
                            )
                        price = min(price, retry_cap_price)

                    current_price = up_entry_price if side == "UP" else down_entry_price
                    if price > 0:
                        slippage = abs(current_price - price) / price
                        if slippage > SLIPPAGE_THRESHOLD:
                            log(f"滑点过高：{slippage*100:.1f}% > {SLIPPAGE_THRESHOLD*100:.0f}%，跳过", "WARN")
                            triggered = False
                            condition = None

                    if triggered:
                        if same_key_retry and retry_count >= MAX_RETRY_PER_MARKET:
                            log(f"最大重试次数（{MAX_RETRY_PER_MARKET}）已达，跳过 {order_key}", "WARN")
                            triggered = False
                            condition = None

                    if triggered:
                        log(f"触发: {condition} -> {side} @ {price*100:.1f}%", "TRADE")

                    if SIMULATION_MODE:
                        sim_shares = _shares_from_usdc_buy(TRADE_AMOUNT, price)
                        if sim_shares <= 0:
                            log(f"[模拟] 买入跳过：无效价格 {price}", "WARN")
                        else:
                            sim_oid = f"SIM-{int(time.time() * 1000)}"
                            state.pop("pending_order", None)
                            current_retry = retry_count if same_key_retry else 0
                            state["last_order"] = {
                                "key": order_key,
                                "time": datetime.now().isoformat(),
                                "retry_count": current_retry + 1,
                                "last_price": price,
                            }
                            state["position"] = {
                                "slug": slug,
                                "side": side,
                                "entry_price": price,
                                "entry_diff": diff_abs,
                                "size": sim_shares,
                            }
                            cum = _to_float(state.get("cumulative_realized_pnl"), 0.0)
                            state = _append_trade_history(state, {
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "slug": slug,
                                "action": "BUY",
                                "side": side,
                                "price": price,
                                "amount": TRADE_AMOUNT,
                                "shares": sim_shares,
                                "order_size_usdc": TRADE_AMOUNT,
                                "order_id": sim_oid,
                                "status": "filled",
                                "reason": condition,
                                "diff": diff,
                                "btc": btc if btc > 0 else None,
                                "ptb": ptb if ptb > 0 else None,
                                "btc_minus_ptb": _btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                "remaining_sec": remaining,
                                "cumulative_realized_pnl_usd": cum,
                            })
                            _emit_trading_analysis(
                                "BUY_FILL",
                                action="BUY",
                                slug=slug,
                                status="buy",
                                shares_type=side,
                                share_price=price,
                                share_amount=sim_shares,
                                order_size_usdc=TRADE_AMOUNT,
                                order_id=sim_oid,
                                reason=condition,
                                btc_price=btc if btc > 0 else None,
                                chainlink_btc=btc if btc > 0 else None,
                                ptb=ptb if ptb > 0 else None,
                                btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                diff_rule=diff,
                                remaining_sec=remaining,
                                pnl_trade_usd=0.0,
                                pnl_total_usd=cum,
                            )
                            insert_trade(
                                polymarket_slug=slug,
                                order_id=sim_oid,
                                side=side,
                                action="BUY",
                                open_reason=condition,
                                entry_price=price,
                                amount_usdc=TRADE_AMOUNT,
                                shares=sim_shares,
                                diff_at_trade=diff if (btc > 0 and ptb > 0) else None,
                                btc_price=btc if btc > 0 else None,
                                ptb_price=ptb if ptb > 0 else None,
                                remaining_sec=remaining,
                                status="filled",
                                result="pending",
                                btc_market_minutes=_btc_market_minutes,
                                simulation=SIMULATION_MODE,
                            )
                            save_state(state)
                            _dashboard_set(
                                position=dict(state.get("position") or {}),
                                pending_order=_dashboard_pending_order_from_state(state),
                                last_order=dict(state.get("last_order") or {}),
                                trade_history=list(state.get("trade_history") or []),
                                cumulative_realized_pnl=cum,
                            )
                            log(
                                f"[SIM] BUY {side} @ {price*100:.2f}% | USDC {TRADE_AMOUNT} | shares\u2248{sim_shares:.4f} | id {sim_oid}",
                                "TRADE",
                            )
                    elif AUTO_TRADE and trader.connected:
                        order_id = trader.place_order(token, "BUY", price, TRADE_AMOUNT)

                        if order_id:
                            state["pending_order"] = {
                                "order_id": order_id,
                                "time": datetime.now().isoformat(),
                                "slug": slug,
                                "side": side,
                                "price": price
                            }
                            current_retry = retry_count if same_key_retry else 0
                            state["last_order"] = {
                                "key": order_key,
                                "time": datetime.now().isoformat(),
                                "retry_count": current_retry + 1,
                                "last_price": price,
                            }
                            cum = _to_float(state.get("cumulative_realized_pnl"), 0.0)
                            state = _append_trade_history(state, {
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "slug": slug,
                                "action": "BUY",
                                "side": side,
                                "price": price,
                                "amount": TRADE_AMOUNT,
                                "order_size_usdc": TRADE_AMOUNT,
                                "order_id": order_id,
                                "status": "submitted",
                                "reason": condition,
                                "diff": diff,
                                "btc": btc if btc > 0 else None,
                                "ptb": ptb if ptb > 0 else None,
                                "btc_minus_ptb": _btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                "remaining_sec": remaining,
                                "cumulative_realized_pnl_usd": cum,
                            })
                            _emit_trading_analysis(
                                "BUY_SUBMIT",
                                action="BUY",
                                slug=slug,
                                status="buy",
                                shares_type=side,
                                share_price=price,
                                order_size_usdc=TRADE_AMOUNT,
                                order_id=order_id,
                                reason=condition,
                                btc_price=btc if btc > 0 else None,
                                chainlink_btc=btc if btc > 0 else None,
                                ptb=ptb if ptb > 0 else None,
                                btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                diff_rule=diff,
                                remaining_sec=remaining,
                                pnl_trade_usd=0.0,
                                pnl_total_usd=cum,
                            )
                            save_state(state)
                            _dashboard_set(
                                pending_order=_dashboard_pending_order_from_state(state),
                                last_order=dict(state.get("last_order") or {}),
                                trade_history=list(state.get("trade_history") or []),
                                cumulative_realized_pnl=cum,
                            )
                            _sync_dashboard_account_snapshot(dashboard_user)
                            log(f"订单已提交，监控中 ID {order_id}", "TRADE")
                        else:
                            log(f"订单失败：{side} @ {price*100:.1f}%", "ERR")
                            current_retry = retry_count if same_key_retry else 0
                            state["last_order"] = {
                                "key": order_key,
                                "time": datetime.now().isoformat(),
                                "retry_count": current_retry + 1,
                                "last_price": price,
                            }
                            cum = _to_float(state.get("cumulative_realized_pnl"), 0.0)
                            state = _append_trade_history(state, {
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "slug": slug,
                                "action": "BUY",
                                "side": side,
                                "price": price,
                                "amount": TRADE_AMOUNT,
                                "order_id": "",
                                "status": "failed",
                                "reason": condition,
                                "diff": diff,
                                "btc": btc if btc > 0 else None,
                                "ptb": ptb if ptb > 0 else None,
                                "btc_minus_ptb": _btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                "remaining_sec": remaining,
                                "cumulative_realized_pnl_usd": cum,
                            })
                            _emit_trading_analysis(
                                "BUY_FAILED",
                                action="BUY",
                                slug=slug,
                                status="buy",
                                shares_type=side,
                                share_price=price,
                                order_size_usdc=TRADE_AMOUNT,
                                reason=condition,
                                btc_price=btc if btc > 0 else None,
                                chainlink_btc=btc if btc > 0 else None,
                                ptb=ptb if ptb > 0 else None,
                                btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                diff_rule=diff,
                                remaining_sec=remaining,
                                pnl_trade_usd=None,
                                pnl_total_usd=cum,
                            )
                            save_state(state)
                            _dashboard_set(
                                last_order=dict(state.get("last_order") or {}),
                                trade_history=list(state.get("trade_history") or []),
                                cumulative_realized_pnl=cum,
                            )
                            _sync_dashboard_account_snapshot(dashboard_user)
                    elif not SIMULATION_MODE:
                        log(f"提醒：考虑买入 {side} @ {price*100:.1f}%", "TRADE")
                        current_retry = retry_count if same_key_retry else 0
                        state["last_order"] = {
                            "key": order_key,
                            "time": datetime.now().isoformat(),
                            "retry_count": current_retry + 1,
                            "last_price": price,
                        }
                        _emit_trading_analysis(
                            "BUY_ALERT",
                            action="BUY",
                            slug=slug,
                            status="buy",
                            shares_type=side,
                            share_price=price,
                            order_size_usdc=TRADE_AMOUNT,
                            reason=condition,
                            btc_price=btc if btc > 0 else None,
                            chainlink_btc=btc if btc > 0 else None,
                            ptb=ptb if ptb > 0 else None,
                            btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                            diff_rule=diff,
                            remaining_sec=remaining,
                            pnl_trade_usd=None,
                            pnl_total_usd=_to_float(state.get("cumulative_realized_pnl"), 0.0),
                        )
                        save_state(state)
                        _dashboard_set(last_order=dict(state.get("last_order") or {}))

            state = load_state()
            pos = state.get("position")
            tp_order = state.get("take_profit_order") or {}
            if pos and pos.get("slug") == slug:
                pos_side = pos.get("side")
                current_prob = up_price if pos_side == "UP" else down_price
                position_size = max(0.001, _to_float(pos.get("size"), TRADE_AMOUNT))
                entry_prob = _maybe_float(pos.get("entry_price"))
                stop_loss_triggered = False
                stop_prob = None
                tp_trigger_prob = None
                tp_sell_price = None
                if entry_prob is not None and entry_prob > 0:
                    stop_prob = max(0.0, entry_prob * (1.0 - STOP_LOSS_PROB_PCT))
                    risk_abs = max(0.0, entry_prob - stop_prob)
                    tp_trigger_prob = min(TAKE_PROFIT_CAP, entry_prob + risk_abs * TAKE_PROFIT_RR)
                    if tp_trigger_prob <= entry_prob:
                        tp_trigger_prob = None
                    else:
                        balanced_risk = (tp_trigger_prob - entry_prob) / TAKE_PROFIT_RR
                        balanced_stop_prob = max(0.0, entry_prob - balanced_risk)
                        if balanced_stop_prob > stop_prob:
                            stop_prob = balanced_stop_prob
                        tp_sell_price = tp_trigger_prob
                    stop_loss_triggered = (current_prob > 0) and (current_prob <= stop_prob)

                if SIMULATION_MODE and pos and pos.get("slug") == slug and entry_prob is not None and entry_prob > 0:
                    sim_exit = False
                    if stop_loss_triggered:
                        sell_x = (up_bid if pos_side == "UP" else down_bid) or (up_price if pos_side == "UP" else down_price)
                        ep = float(entry_prob)
                        xp = float(sell_x)
                        realized = position_size * (xp - ep)
                        cum = _to_float(state.get("cumulative_realized_pnl"), 0.0) + realized
                        state["cumulative_realized_pnl"] = cum
                        state = _append_trade_history(state, {
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "slug": slug,
                            "action": "SELL",
                            "side": pos_side,
                            "price": xp,
                            "entry_price": ep,
                            "shares": position_size,
                            "amount": position_size * xp,
                            "order_id": f"SIM-SL-{int(time.time() * 1000)}",
                            "status": "filled",
                            "reason": "stop_loss",
                            "diff": diff,
                            "btc": btc if btc > 0 else None,
                            "ptb": ptb if ptb > 0 else None,
                            "btc_minus_ptb": _btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                            "remaining_sec": remaining,
                            "realized_pnl_usd": realized,
                            "cumulative_realized_pnl_usd": cum,
                        })
                        _emit_trading_analysis(
                            "SELL_CLOSE",
                            reason="stop_loss",
                            slug=slug,
                            action="SELL",
                            status="sell",
                            shares_type=pos_side,
                            share_price=xp,
                            share_amount=position_size,
                            entry_share_price=ep,
                            exit_share_price=xp,
                            notional_exit_usd=position_size * xp,
                            take_profit=tp_trigger_prob,
                            stop_loss=stop_prob,
                            btc_price=btc if btc > 0 else None,
                            chainlink_btc=btc if btc > 0 else None,
                            ptb=ptb if ptb > 0 else None,
                            btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                            diff_rule=diff,
                            remaining_sec=remaining,
                            order_id=f"SIM-SL-{int(time.time() * 1000)}",
                            pnl_trade_usd=realized,
                            pnl_total_usd=cum,
                        )
                        insert_trade_close(
                            polymarket_slug=slug,
                            order_id=f"SIM-SL-{int(time.time() * 1000)}",
                            side=pos_side,
                            action="SELL",
                            open_reason="stop_loss",
                            entry_price=ep,
                            exit_price=xp,
                            amount_usdc=position_size * xp,
                            shares=position_size,
                            pnl_usd=realized,
                            cumulative_pnl_usd=cum,
                            diff_at_trade=diff if (btc > 0 and ptb > 0) else None,
                            btc_price=btc if btc > 0 else None,
                            ptb_price=ptb if ptb > 0 else None,
                            remaining_sec=remaining,
                            status="filled",
                            result="loss" if realized < 0 else "win",
                            btc_market_minutes=_btc_market_minutes,
                            simulation=SIMULATION_MODE,
                        )
                        state.pop("position", None)
                        state.pop("take_profit_order", None)
                        save_state(state)
                        _dashboard_set(
                            position={},
                            pending_order=_dashboard_pending_order_from_state(state),
                            trade_history=list(state.get("trade_history") or []),
                            cumulative_realized_pnl=cum,
                        )
                        log(
                            f"[模拟] 止损 {pos_side} @ {xp*100:.2f}% | 盈亏 ${realized:+.4f} | 累计 ${cum:+.4f}",
                            "TRADE",
                        )
                        sim_exit = True
                    elif (
                        tp_trigger_prob is not None
                        and tp_sell_price
                        and current_prob > 0
                        and current_prob >= tp_trigger_prob
                    ):
                        ep = float(entry_prob)
                        xp = float(tp_sell_price)
                        realized = position_size * (xp - ep)
                        cum = _to_float(state.get("cumulative_realized_pnl"), 0.0) + realized
                        state["cumulative_realized_pnl"] = cum
                        state = _append_trade_history(state, {
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "slug": slug,
                            "action": "SELL",
                            "side": pos_side,
                            "price": xp,
                            "entry_price": ep,
                            "shares": position_size,
                            "amount": position_size * xp,
                            "order_id": f"SIM-TP-{int(time.time() * 1000)}",
                            "status": "filled",
                            "reason": "take_profit",
                            "diff": diff,
                            "btc": btc if btc > 0 else None,
                            "ptb": ptb if ptb > 0 else None,
                            "btc_minus_ptb": _btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                            "remaining_sec": remaining,
                            "realized_pnl_usd": realized,
                            "cumulative_realized_pnl_usd": cum,
                        })
                        _emit_trading_analysis(
                            "SELL_CLOSE",
                            reason="take_profit",
                            slug=slug,
                            action="SELL",
                            status="sell",
                            shares_type=pos_side,
                            share_price=xp,
                            share_amount=position_size,
                            entry_share_price=ep,
                            exit_share_price=xp,
                            notional_exit_usd=position_size * xp,
                            take_profit=tp_trigger_prob,
                            stop_loss=stop_prob,
                            btc_price=btc if btc > 0 else None,
                            chainlink_btc=btc if btc > 0 else None,
                            ptb=ptb if ptb > 0 else None,
                            btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                            diff_rule=diff,
                            remaining_sec=remaining,
                            order_id=f"SIM-TP-{int(time.time() * 1000)}",
                            pnl_trade_usd=realized,
                            pnl_total_usd=cum,
                        )
                        insert_trade_close(
                            polymarket_slug=slug,
                            order_id=f"SIM-TP-{int(time.time() * 1000)}",
                            side=pos_side,
                            action="SELL",
                            open_reason="take_profit",
                            entry_price=ep,
                            exit_price=xp,
                            amount_usdc=position_size * xp,
                            shares=position_size,
                            pnl_usd=realized,
                            cumulative_pnl_usd=cum,
                            diff_at_trade=diff if (btc > 0 and ptb > 0) else None,
                            btc_price=btc if btc > 0 else None,
                            ptb_price=ptb if ptb > 0 else None,
                            remaining_sec=remaining,
                            status="filled",
                            result=("win" if realized > 0 else "loss"),
                            btc_market_minutes=_btc_market_minutes,
                            simulation=SIMULATION_MODE,
                        )
                        state.pop("position", None)
                        state.pop("take_profit_order", None)
                        save_state(state)
                        _dashboard_set(
                            position={},
                            pending_order=_dashboard_pending_order_from_state(state),
                            trade_history=list(state.get("trade_history") or []),
                            cumulative_realized_pnl=cum,
                        )
                        log(
                            f"[模拟] 止盈 {pos_side} @ {xp*100:.2f}% | 盈亏 ${realized:+.4f} | 累计 ${cum:+.4f}",
                            "TRADE",
                        )
                        sim_exit = True
                    if sim_exit:
                        state = load_state()
                        pos = state.get("position")
                        tp_order = state.get("take_profit_order") or {}

                if (
                    (not SIMULATION_MODE)
                    and tp_order
                    and tp_order.get("slug") == slug
                    and tp_order.get("side") == pos_side
                    and AUTO_TRADE
                    and trader.connected
                ):
                    tp_order_id = tp_order.get("order_id")
                    tp_status = trader.get_order_status(tp_order_id)
                    if tp_status and tp_status.get("filled"):
                        tp_price = float(tp_order.get("price") or tp_sell_price or 0.0)
                        tp_amount = max(0.001, _to_float(tp_order.get("amount"), position_size))
                        ep = _maybe_float(pos.get("entry_price")) or 0.0
                        realized = tp_amount * (tp_price - ep) if ep else 0.0
                        cum = _to_float(state.get("cumulative_realized_pnl"), 0.0) + realized
                        state["cumulative_realized_pnl"] = cum
                        state = _append_trade_history(state, {
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "slug": slug,
                            "action": "SELL",
                            "side": pos_side,
                            "price": tp_price,
                            "entry_price": ep,
                            "shares": tp_amount,
                            "amount": tp_amount * tp_price,
                            "order_id": tp_order_id or "",
                            "status": "filled",
                            "reason": "take_profit",
                            "diff": diff,
                            "btc": btc if btc > 0 else None,
                            "ptb": ptb if ptb > 0 else None,
                            "btc_minus_ptb": _btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                            "remaining_sec": remaining,
                            "realized_pnl_usd": realized,
                            "cumulative_realized_pnl_usd": cum,
                        })
                        _emit_trading_analysis(
                            "SELL_CLOSE",
                            reason="take_profit",
                            slug=slug,
                            action="SELL",
                            status="sell",
                            shares_type=pos_side,
                            share_price=tp_price,
                            share_amount=tp_amount,
                            entry_share_price=ep,
                            exit_share_price=tp_price,
                            notional_exit_usd=tp_amount * tp_price,
                            take_profit=tp_trigger_prob,
                            stop_loss=stop_prob,
                            btc_price=btc if btc > 0 else None,
                            chainlink_btc=btc if btc > 0 else None,
                            ptb=ptb if ptb > 0 else None,
                            btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                            diff_rule=diff,
                            remaining_sec=remaining,
                            order_id=tp_order_id or "",
                            pnl_trade_usd=realized,
                            pnl_total_usd=cum,
                        )
                        insert_trade_close(
                            polymarket_slug=slug,
                            order_id=tp_order_id or "",
                            side=pos_side,
                            action="SELL",
                            open_reason="take_profit",
                            entry_price=ep,
                            exit_price=tp_price,
                            amount_usdc=tp_amount * tp_price,
                            shares=tp_amount,
                            pnl_usd=realized,
                            cumulative_pnl_usd=cum,
                            diff_at_trade=diff if (btc > 0 and ptb > 0) else None,
                            btc_price=btc if btc > 0 else None,
                            ptb_price=ptb if ptb > 0 else None,
                            remaining_sec=remaining,
                            status="filled",
                            result=("win" if realized > 0 else "loss"),
                            btc_market_minutes=_btc_market_minutes,
                            simulation=SIMULATION_MODE,
                        )
                        state.pop("take_profit_order", None)
                        state.pop("position", None)
                        save_state(state)
                        _dashboard_set(
                            position={},
                            pending_order=_dashboard_pending_order_from_state(state),
                            trade_history=list(state.get("trade_history") or []),
                            cumulative_realized_pnl=cum,
                        )
                        _sync_dashboard_account_snapshot(dashboard_user)
                        log(f"止盈单已成交：{pos_side} @ {tp_price*100:.2f}%", "TRADE")
                        pos = None
                        tp_order = {}
                    elif tp_status and tp_status.get("status") in ["CANCELED", "CANCELLED", "REJECTED", "EXPIRED"]:
                        log("止盈单失效，价格波动后将重新挂单", "WARN")
                        state.pop("take_profit_order", None)
                        save_state(state)
                        _dashboard_set(pending_order=_dashboard_pending_order_from_state(state))
                        tp_order = {}

                if (
                    (not SIMULATION_MODE)
                    and pos
                    and (not tp_order)
                    and tp_trigger_prob is not None
                    and current_prob > 0
                    and current_prob >= tp_trigger_prob
                ):
                    log(
                        f"止盈触发：入场 {entry_prob*100:.1f}%，当前 {current_prob*100:.1f}%，目标 {tp_trigger_prob*100:.1f}%（RR≈{TAKE_PROFIT_RR:.2f}）",
                        "TRADE",
                    )
                    if AUTO_TRADE and trader.connected:
                        sell_token = market["up_token"] if pos_side == "UP" else market["down_token"]
                        tp_order_id = None
                        tp_submit_price = tp_sell_price
                        attempt_price = tp_sell_price
                        for attempt_idx in range(TAKE_PROFIT_RETRY_MAX):
                            tp_order_id = trader.place_order(sell_token, "SELL", attempt_price, position_size)
                            if tp_order_id:
                                tp_submit_price = attempt_price
                                break
                            if attempt_idx + 1 >= TAKE_PROFIT_RETRY_MAX:
                                break
                            next_price = min(TAKE_PROFIT_CAP, attempt_price + TAKE_PROFIT_RETRY_STEP)
                            if next_price <= attempt_price + 1e-9:
                                break
                            log(
                                f"止盈重试 {attempt_idx+2}/{TAKE_PROFIT_RETRY_MAX}：提价至 {next_price*100:.1f}%",
                                "WARN",
                            )
                            attempt_price = next_price
                        if tp_order_id:
                            state["take_profit_order"] = {
                                "order_id": tp_order_id,
                                "time": datetime.now().isoformat(),
                                "slug": slug,
                                "side": pos_side,
                                "price": tp_submit_price,
                                "amount": position_size,
                                "action": "SELL",
                                "reason": "take_profit",
                            }
                            cum = _to_float(state.get("cumulative_realized_pnl"), 0.0)
                            state = _append_trade_history(state, {
                                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "slug": slug,
                                "action": "SELL",
                                "side": pos_side,
                                "price": tp_submit_price,
                                "amount": position_size,
                                "order_id": tp_order_id,
                                "status": "submitted",
                                "reason": "take_profit",
                                "diff": diff,
                                "btc": btc if btc > 0 else None,
                                "ptb": ptb if ptb > 0 else None,
                                "btc_minus_ptb": _btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                "remaining_sec": remaining,
                                "cumulative_realized_pnl_usd": cum,
                            })
                            _emit_trading_analysis(
                                "SELL_SUBMIT",
                                reason="take_profit",
                                slug=slug,
                                action="SELL",
                                status="sell",
                                shares_type=pos_side,
                                share_price=tp_submit_price,
                                share_amount=position_size,
                                take_profit=tp_trigger_prob,
                                stop_loss=stop_prob,
                                order_id=tp_order_id,
                                btc_price=btc if btc > 0 else None,
                                chainlink_btc=btc if btc > 0 else None,
                                ptb=ptb if ptb > 0 else None,
                                btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                                diff_rule=diff,
                                remaining_sec=remaining,
                                pnl_trade_usd=None,
                                pnl_total_usd=cum,
                            )
                            save_state(state)
                            _dashboard_set(
                                pending_order=_dashboard_pending_order_from_state(state),
                                trade_history=list(state.get("trade_history") or []),
                                cumulative_realized_pnl=cum,
                            )
                            _sync_dashboard_account_snapshot(dashboard_user)
                            tp_order = dict(state.get("take_profit_order") or {})
                            log(f"止盈单已挂单 ID {tp_order_id}", "TRADE")
                        else:
                            log(f"止盈单失败：{pos_side} @ {attempt_price*100:.1f}%", "ERR")
                    elif not SIMULATION_MODE:
                        log(f"提醒：考虑卖出 {pos_side} @ {tp_sell_price*100:.1f}%（数量 {position_size:.4f}）", "TRADE")
                        _emit_trading_analysis(
                            "SELL_ALERT",
                            reason="take_profit",
                            slug=slug,
                            action="SELL",
                            status="sell",
                            shares_type=pos_side,
                            share_price=tp_sell_price,
                            share_amount=position_size,
                            take_profit=tp_trigger_prob,
                            stop_loss=stop_prob,
                            btc_price=btc if btc > 0 else None,
                            chainlink_btc=btc if btc > 0 else None,
                            ptb=ptb if ptb > 0 else None,
                            btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                            remaining_sec=remaining,
                            pnl_trade_usd=None,
                            pnl_total_usd=_to_float(state.get("cumulative_realized_pnl"), 0.0),
                        )

                if (not SIMULATION_MODE) and pos and stop_loss_triggered:
                    log(
                        f"止损触发：{pos_side} 概率 {current_prob*100:.1f}% <= 止损 {stop_prob*100:.1f}%（入场 {entry_prob*100:.1f}%）",
                        "TRADE",
                    )

                    if AUTO_TRADE and trader.connected:
                        if tp_order and tp_order.get("order_id"):
                            trader.cancel_order(tp_order.get("order_id"))
                            state.pop("take_profit_order", None)

                        sell_price = (up_bid if pos_side == "UP" else down_bid) or (up_price if pos_side == "UP" else down_price)
                        sell_token = market["up_token"] if pos_side == "UP" else market["down_token"]
                        sell_order_id = trader.place_order(sell_token, "SELL", sell_price, position_size)
                        ep = _maybe_float(pos.get("entry_price")) or 0.0
                        xp = float(sell_price)
                        realized = position_size * (xp - ep) if sell_order_id and ep else 0.0
                        cum = _to_float(state.get("cumulative_realized_pnl"), 0.0) + (realized if sell_order_id else 0.0)
                        if sell_order_id:
                            state["cumulative_realized_pnl"] = cum
                        state = _append_trade_history(state, {
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                            "slug": slug,
                            "action": "SELL",
                            "side": pos_side,
                            "price": sell_price,
                            "entry_price": ep,
                            "shares": position_size,
                            "amount": position_size * xp,
                            "order_id": sell_order_id or "",
                            "status": "submitted" if sell_order_id else "failed",
                            "reason": "stop_loss",
                            "diff": diff,
                            "btc": btc if btc > 0 else None,
                            "ptb": ptb if ptb > 0 else None,
                            "btc_minus_ptb": _btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                            "remaining_sec": remaining,
                            "realized_pnl_usd": realized if sell_order_id else None,
                            "cumulative_realized_pnl_usd": cum if sell_order_id else _to_float(state.get("cumulative_realized_pnl"), 0.0),
                        })
                        _emit_trading_analysis(
                            "SELL_SUBMIT" if sell_order_id else "SELL_FAILED",
                            reason="stop_loss",
                            slug=slug,
                            action="SELL",
                            status="sell",
                            shares_type=pos_side,
                            share_price=xp,
                            share_amount=position_size,
                            entry_share_price=ep,
                            exit_share_price=xp,
                            take_profit=tp_trigger_prob,
                            stop_loss=stop_prob,
                            order_id=sell_order_id or "",
                            btc_price=btc if btc > 0 else None,
                            chainlink_btc=btc if btc > 0 else None,
                            ptb=ptb if ptb > 0 else None,
                            btc_minus_ptb=_btc_ptb_snapshot(btc if btc > 0 else None, ptb if ptb > 0 else None),
                            diff_rule=diff,
                            remaining_sec=remaining,
                            pnl_trade_usd=realized if sell_order_id else None,
                            pnl_total_usd=cum if sell_order_id else _to_float(state.get("cumulative_realized_pnl"), 0.0),
                        )
                        insert_trade_close(
                            polymarket_slug=slug,
                            order_id=sell_order_id or "",
                            side=pos_side,
                            action="SELL",
                            open_reason="stop_loss",
                            entry_price=ep,
                            exit_price=xp,
                            amount_usdc=position_size * xp,
                            shares=position_size,
                            pnl_usd=realized if sell_order_id else None,
                            cumulative_pnl_usd=cum if sell_order_id else _to_float(state.get("cumulative_realized_pnl"), 0.0),
                            diff_at_trade=diff if (btc > 0 and ptb > 0) else None,
                            btc_price=btc if btc > 0 else None,
                            ptb_price=ptb if ptb > 0 else None,
                            remaining_sec=remaining,
                            status="submitted" if sell_order_id else "failed",
                            result="pending",
                            btc_market_minutes=_btc_market_minutes,
                            simulation=SIMULATION_MODE,
                        )
                        state.pop("position", None)
                        state.pop("take_profit_order", None)
                        save_state(state)
                        _dashboard_set(
                            position={},
                            pending_order=_dashboard_pending_order_from_state(state),
                            trade_history=list(state.get("trade_history") or []),
                            cumulative_realized_pnl=cum if sell_order_id else _to_float(state.get("cumulative_realized_pnl"), 0.0),
                        )
                        _sync_dashboard_account_snapshot(dashboard_user)
                        log(f"止损卖出完成：{pos_side} @ {sell_price*100:.2f}%", "TRADE")

            time.sleep(LOOP_INTERVAL_SEC)

    except KeyboardInterrupt:
        print("\n\nStopped.")
        if market_listener:
            market_listener.stop()
        redeemer.stop()


if __name__ == "__main__":
    main()
