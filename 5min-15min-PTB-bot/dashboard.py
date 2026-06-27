#!/usr/bin/env python3
"""
Polymarket BTC 5分钟/15分钟 涨跌自动交易 - 仪表盘模块
Flask路由、SSE流、Web服务器启动。
"""
import json
import threading

from flask import Response, jsonify, request, send_from_directory, stream_with_context

from config import WEB_ENABLED, WEB_HOST, WEB_PORT, STATIC_DIR, DASHBOARD_PASSWORD
from state import app, dashboard_lock, dashboard_cond, dashboard_state, dashboard_version
from utils import set_btc_market_minutes, get_btc_market_minutes
from database import get_trade_stats, get_recent_trades, get_latest_account_snapshot, get_account_history


def _check_pwd():
    """检查请求中的密码是否匹配 DASHBOARD_PASSWORD。空密码表示不设防。"""
    if not DASHBOARD_PASSWORD:
        return True
    pwd = request.args.get("pwd", "") or request.headers.get("X-Pwd", "")
    return pwd == DASHBOARD_PASSWORD


def _require_pwd():
    """如果密码不匹配则返回 403 响应，否则返回 None。"""
    if not _check_pwd():
        return jsonify({"ok": False, "error": "unauthorized"}), 403
    return None


@app.route("/")
def dashboard_index():
    return send_from_directory(STATIC_DIR, "dashboard.html")


@app.route("/api/auth")
def api_auth():
    """验证密码的专用端点，返回 200 或 403。"""
    err = _require_pwd()
    if err:
        return err
    return jsonify({"ok": True})


@app.route("/api/status")
def dashboard_status():
    err = _require_pwd()
    if err:
        return err
    with dashboard_lock:
        return jsonify(dict(dashboard_state))


@app.route("/api/logs")
def dashboard_logs():
    err = _require_pwd()
    if err:
        return err
    with dashboard_lock:
        return jsonify({"items": list(dashboard_state.get("activity") or [])[-300:]})


@app.route("/api/stream")
def dashboard_stream():
    err = _require_pwd()
    if err:
        return err
    def _event(name, payload):
        return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

    def generate():
        last_seen = -1
        last_log_sig = ""
        while True:
            with dashboard_cond:
                if dashboard_version == last_seen:
                    dashboard_cond.wait(timeout=15)
                version_now = dashboard_version
                state_now = dict(dashboard_state)

            if version_now != last_seen:
                logs = list(state_now.get("activity") or [])[-300:]
                state_now.pop("activity", None)
                yield _event("status", {"data": state_now})

                if logs:
                    tail = logs[-1]
                    sig = f"{len(logs)}|{tail.get('time','')}|{tail.get('message','')}"
                else:
                    sig = "0"
                if sig != last_log_sig:
                    yield _event("logs", {"items": logs})
                    last_log_sig = sig

                last_seen = version_now
            else:
                yield ": ping\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.route("/api/history")
def dashboard_history():
    err = _require_pwd()
    if err:
        return err
    with dashboard_lock:
        live_items = list(dashboard_state.get("live_trades") or [])
        if live_items:
            return jsonify({"items": live_items[-300:]})
        local_items = list(dashboard_state.get("trade_history") or [])
        wallet_items = list(dashboard_state.get("wallet_history") or [])
        return jsonify({"items": (local_items + wallet_items)[-300:]})


@app.route("/api/btc-market-minutes", methods=["POST"])
def api_btc_market_minutes():
    err = _require_pwd()
    if err:
        return err
    if not WEB_ENABLED:
        return jsonify({"ok": False, "error": "web disabled"}), 404
    try:
        body = request.get_json(silent=True) or {}
        m = body.get("minutes", body.get("interval", 15))
        m = int(m)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid body"}), 400
    if m not in (5, 15):
        return jsonify({"ok": False, "error": "minutes must be 5 or 15"}), 400
    set_btc_market_minutes(m)
    return jsonify({"ok": True, "minutes": get_btc_market_minutes()})


@app.route("/api/db/stats")
def api_db_stats():
    err = _require_pwd()
    if err:
        return err
    return jsonify(get_trade_stats())


@app.route("/api/db/trades")
def api_db_trades():
    err = _require_pwd()
    if err:
        return err
    limit = request.args.get("limit", 100, type=int)
    rows = get_recent_trades(limit)
    return jsonify({"items": [dict(r) for r in rows]})


@app.route("/api/db/account")
def api_db_account():
    err = _require_pwd()
    if err:
        return err
    row = get_latest_account_snapshot()
    if row is None:
        return jsonify({})
    return jsonify(dict(row))


@app.route("/api/db/account-history")
def api_db_account_history():
    err = _require_pwd()
    if err:
        return err
    limit = request.args.get("limit", 50, type=int)
    rows = get_account_history(limit)
    return jsonify({"items": [dict(r) for r in rows]})


def start_web_server():
    if not WEB_ENABLED:
        return

    def run():
        app.run(host=WEB_HOST, port=WEB_PORT, threaded=True, use_reloader=False)

    t = threading.Thread(target=run, daemon=True)
    t.start()
