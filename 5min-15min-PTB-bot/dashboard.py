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
from database import get_trade_stats, get_recent_trades, get_latest_account_snapshot, get_account_history, _get_conn


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


def _safe_row(row):
    """将 sqlite3.Row 安全转为可 JSON 序列化的 dict。"""
    if row is None:
        return {}
    item = {}
    for key in row.keys():
        val = row[key]
        if isinstance(val, bytes):
            item[key] = val.decode("utf-8", errors="replace")
        elif val is None:
            item[key] = None
        elif isinstance(val, (int, float, str, bool)):
            item[key] = val
        else:
            item[key] = str(val)
    return item


def _safe_rows(rows):
    return [_safe_row(r) for r in (rows or [])]


@app.route("/api/db/trades")
def api_db_trades():
    err = _require_pwd()
    if err:
        return err
    try:
        raw = request.args.get("limit", "100")
        limit = int(raw) if raw and str(raw).isdigit() else 100
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return jsonify({"items": _safe_rows(rows)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"items": [], "error": str(e)}), 500


@app.route("/api/db/account")
def api_db_account():
    err = _require_pwd()
    if err:
        return err
    try:
        row = get_latest_account_snapshot()
        return jsonify(_safe_row(row))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/db/account-history")
def api_db_account_history():
    err = _require_pwd()
    if err:
        return err
    try:
        limit = request.args.get("limit", 50, type=int)
        rows = get_account_history(limit)
        return jsonify({"items": _safe_rows(rows)})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"items": [], "error": str(e)}), 500


@app.route("/api/db/stats/detail")
def api_db_stats_detail():
    """返回详细的多维统计数据。"""
    err = _require_pwd()
    if err:
        return err
    conn = _get_conn()
    result = {}

    # 按平仓原因分组
    rows = conn.execute("""
        SELECT open_reason, COUNT(*) as cnt,
               COALESCE(SUM(pnl_usd),0) as total_pnl,
               COALESCE(AVG(pnl_usd),0) as avg_pnl,
               COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0) as wins,
               COALESCE(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),0) as losses
        FROM trades WHERE action='SELL'
        GROUP BY open_reason ORDER BY cnt DESC
    """).fetchall()
    result["by_reason"] = _safe_rows(rows)

    # 按方向分组
    rows = conn.execute("""
        SELECT side, COUNT(*) as cnt,
               COALESCE(SUM(pnl_usd),0) as total_pnl,
               COALESCE(AVG(pnl_usd),0) as avg_pnl,
               COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0) as wins,
               COALESCE(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),0) as losses
        FROM trades WHERE action='SELL'
        GROUP BY side ORDER BY cnt DESC
    """).fetchall()
    result["by_side"] = _safe_rows(rows)

    # 按市场周期分组
    rows = conn.execute("""
        SELECT btc_market_minutes, COUNT(*) as cnt,
               COALESCE(SUM(pnl_usd),0) as total_pnl,
               COALESCE(AVG(pnl_usd),0) as avg_pnl,
               COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0) as wins,
               COALESCE(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),0) as losses
        FROM trades WHERE action='SELL'
        GROUP BY btc_market_minutes ORDER BY cnt DESC
    """).fetchall()
    result["by_minutes"] = _safe_rows(rows)

    # 按入场价格区间分组
    rows = conn.execute("""
        SELECT 
            CASE 
                WHEN entry_price < 0.5 THEN '0-50%'
                WHEN entry_price < 0.6 THEN '50-60%'
                WHEN entry_price < 0.7 THEN '60-70%'
                WHEN entry_price < 0.8 THEN '70-80%'
                WHEN entry_price < 0.9 THEN '80-90%'
                ELSE '90-100%'
            END as price_range,
            COUNT(*) as cnt,
            COALESCE(SUM(pnl_usd),0) as total_pnl,
            COALESCE(AVG(pnl_usd),0) as avg_pnl,
            COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0) as wins,
            COALESCE(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),0) as losses
        FROM trades WHERE action='SELL' AND entry_price IS NOT NULL
        GROUP BY price_range ORDER BY price_range
    """).fetchall()
    result["by_entry_price"] = _safe_rows(rows)

    # 按差价区间分组
    rows = conn.execute("""
        SELECT 
            CASE 
                WHEN diff_at_trade IS NULL THEN 'N/A'
                WHEN diff_at_trade < 0 THEN 'BTC<PTB'
                WHEN diff_at_trade < 40 THEN '0-40'
                WHEN diff_at_trade < 80 THEN '40-80'
                ELSE '80+'
            END as diff_range,
            COUNT(*) as cnt,
            COALESCE(SUM(pnl_usd),0) as total_pnl,
            COALESCE(AVG(pnl_usd),0) as avg_pnl,
            COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0) as wins,
            COALESCE(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),0) as losses
        FROM trades WHERE action='SELL'
        GROUP BY diff_range ORDER BY diff_range
    """).fetchall()
    result["by_diff"] = _safe_rows(rows)

    # 按日期分组
    rows = conn.execute("""
        SELECT substr(created_at,1,10) as day,
               COUNT(*) as cnt,
               COALESCE(SUM(pnl_usd),0) as total_pnl,
               COALESCE(AVG(pnl_usd),0) as avg_pnl,
               COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0) as wins,
               COALESCE(SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),0) as losses
        FROM trades WHERE action='SELL'
        GROUP BY day ORDER BY day DESC
    """).fetchall()
    result["by_day"] = _safe_rows(rows)

    # BUY 操作记录数（开仓频率）
    buy_cnt = conn.execute("SELECT COUNT(*) as cnt FROM trades WHERE action='BUY'").fetchone()
    result["total_buys"] = buy_cnt["cnt"] if buy_cnt else 0

    return jsonify(result)


def start_web_server():
    if not WEB_ENABLED:
        return

    def run():
        app.run(host=WEB_HOST, port=WEB_PORT, threaded=True, use_reloader=False)

    t = threading.Thread(target=run, daemon=True)
    t.start()
