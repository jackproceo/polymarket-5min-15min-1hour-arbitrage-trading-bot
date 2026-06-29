"""
Flask Web 仪表盘：API + 静态 UI。
在机器人进程内运行（--web）或独立运行（读取 logs/bot_state.json）。
"""
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, request, make_response, redirect, render_template

from market_config import apply_market_window_settings

# 加载 .env（读取 DASHBOARD_PASSWORD）
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

# 项目根目录：仓库根目录（/config、/src 的父目录）
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "config.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"
TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip()


def _password_hash(password: str) -> str:
    """对密码做 SHA-256 哈希，用于 cookie 校验。"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _is_auth_required() -> bool:
    """是否开启了密码保护。"""
    return bool(DASHBOARD_PASSWORD)


def _check_auth(request) -> bool:
    """检查请求中的 cookie 是否匹配密码哈希。"""
    if not _is_auth_required():
        return True
    token = request.cookies.get("dashboard_token", "")
    return token == _password_hash(DASHBOARD_PASSWORD)


def create_app(project_root: Path | None = None) -> Flask:
    root = project_root or PROJECT_ROOT

    app = Flask(
        __name__,
        static_folder=str(STATIC_DIR),
        template_folder=str(TEMPLATE_DIR),
    )

    # ── 认证中间件 ──────────────────────────────────────────────
    AUTH_EXEMPT_PATHS = {"/login", "/api/login", "/api/logout", "/api/health"}

    @app.before_request
    def require_auth():
        """认证中间件：未认证用户重定向到 /login，API 请求返回 401。"""
        # 静态文件、已豁免的 API、已认证用户——放行
        if request.path.startswith("/static/"):
            return None
        if request.path in AUTH_EXEMPT_PATHS:
            return None
        if _check_auth(request):
            return None
        # 未认证 → 重定向到登录页（API 请求返回 401）
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect("/login")

    # ── 登录 / 登出 ──────────────────────────────────────────────
    @app.route("/login")
    def login_page():
        """显示登录页面（已认证则跳转到仪表盘）。"""
        if _check_auth(request):
            return redirect("/")
        return render_template("login.html")

    @app.route("/api/login", methods=["POST"])
    def api_login():
        """密码登录 API：密码正确则设置 cookie（30 天有效期）。"""
        body = request.get_json(silent=True) or {}
        pwd = body.get("password", "")
        if not _is_auth_required():
            return jsonify({"ok": True, "message": "无需密码"})
        if pwd == DASHBOARD_PASSWORD:
            resp = make_response(jsonify({"ok": True}))
            # 30 天有效期
            resp.set_cookie("dashboard_token", _password_hash(pwd), max_age=30 * 86400, httponly=True, samesite="Lax")
            return resp
        return jsonify({"ok": False, "message": "密码错误"}), 403

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        """登出 API：清除 cookie。"""
        resp = make_response(jsonify({"ok": True}))
        resp.delete_cookie("dashboard_token")
        return resp

    # ── 路由 ────────────────────────────────────────────────────
    @app.route("/")
    def index():
        """仪表盘首页（渲染 index.html）。"""
        return render_template("index.html")

    @app.route("/api/health")
    def health():
        """健康检查 API：返回机器人是否存活及快照延迟。"""
        import web_dashboard_state as wds

        snap = wds.get_snapshot()
        ts = snap.get("updated_at", 0)
        age = time.time() - ts if ts else 9999
        file_snap = wds.read_state_file(root)
        file_ts = file_snap.get("updated_at", 0) if file_snap else 0
        file_age = time.time() - file_ts if file_ts else 9999
        bot_live = age < 15.0 or file_age < 15.0
        return jsonify(
            {
                "ok": True,
                "bot_live": bot_live,
                "snapshot_age_sec": round(min(age, file_age), 2),
            }
        )

    @app.route("/api/status")
    def api_status():
        """获取完整机器人状态快照（以 SQLite 为交易数据源，内存数据用于实时盘口）。"""
        import web_dashboard_state as wds

        snap = wds.get_snapshot()
        if snap.get("status") == "initializing" or not snap.get("coins"):
            file_snap = wds.read_state_file(root)
            if file_snap:
                snap = file_snap

        # 以 SQLite 为交易统计数据源（覆盖 snapshot 中的内存数据）
        try:
            import db_manager
            db = db_manager.get_db()
            if db is not None:
                stats = db.get_trade_stats()
                by_coin = db.get_trade_stats_by_coin()
                snap["portfolio"] = {
                    "total_capital": snap.get("portfolio", {}).get("total_capital", 0),
                    "total_pnl": round(stats.get("total_pnl", 0), 2),
                    "portfolio_roi": round(stats.get("avg_roi", 0), 2),
                    "total_trades": stats.get("count", 0),
                }
                # 按币种盈亏覆盖（来自 SQLite）
                coins_block = snap.get("coins", {})
                for row in by_coin:
                    c = row.get("coin", "").lower()
                    if c in coins_block:
                        if coins_block[c].get("stats"):
                            coins_block[c]["stats"]["pnl"] = row.get("total_pnl", 0)
                            coins_block[c]["stats"]["total_trades"] = row.get("count", 0)
                            coins_block[c]["stats"]["wins"] = row.get("wins", 0)
                            coins_block[c]["stats"]["losses"] = row.get("losses", 0)
                            coins_block[c]["stats"]["win_rate"] = row.get("win_rate", 0)

                # 最近已平仓交易（来自 SQLite）
                trades = db.get_trades(limit=12)
                def _t2r(t):
                    slug = t.get("market_slug", "")
                    return {
                        "close_time": t.get("close_time", ""),
                        "market_slug": slug,
                        "coin": t.get("coin", ""),
                        "side": t.get("side", "—"),
                        "entry_price": t.get("entry_price"),
                        "exit_price": t.get("exit_price"),
                        "pnl": round(float(t.get("pnl", 0)), 2),
                        "roi_display": round(t.get("roi_pct", 0), 2) if t.get("roi_pct") is not None else 0,
                        "exit_type": t.get("exit_type", "—"),
                        "winner": t.get("winner", ""),
                        "polymarket_url": f"https://polymarket.com/zh/event/{slug}" if slug else "",
                    }
                snap["recent_trades"] = [_t2r(t) for t in trades]
        except Exception:
            pass  # SQLite 不可用时回退到内存数据

        return jsonify(snap)

    @app.route("/api/config", methods=["GET"])
    def get_config():
        """获取当前 config.json 配置（含 market_window 设置）。"""
        if not CONFIG_PATH.exists():
            return jsonify({"error": "未找到 config.json"}), 404
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            apply_market_window_settings(data)
            return jsonify(data)
        except (OSError, json.JSONDecodeError) as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/config", methods=["POST"])
    def post_config():
        """更新 config.json（原子写入 + 自动备份旧配置）。"""
        if not request.is_json:
            return jsonify({"error": "需要 JSON 请求体"}), 400
        body = request.get_json()
        if not isinstance(body, dict):
            return jsonify({"error": "无效的 JSON"}), 400
        apply_market_window_settings(body)
        if not CONFIG_PATH.parent.is_dir():
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        backup = CONFIG_PATH.with_suffix(".json.bak")
        try:
            if CONFIG_PATH.exists():
                shutil.copy2(CONFIG_PATH, backup)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(body, f, indent=2)
            return jsonify({"ok": True, "message": "已保存。重新启动机器人以生效。"})
        except OSError as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/bot/stop", methods=["POST"])
    def bot_stop():
        """请求机器人优雅关闭（设置停止标记）。"""
        import web_dashboard_state as wds

        wds.request_stop()
        return jsonify({"ok": True, "message": "已请求停止——机器人将优雅关闭。"})

    @app.route("/api/reset", methods=["POST"])
    def api_reset():
        """删除所有历史交易、余额、余额变动记录，重新开始。"""
        try:
            import db_manager
            db = db_manager.get_db()
            if db is None:
                return jsonify({"ok": False, "message": "数据库未初始化"}), 500
            deleted = db.clear_all_trades()
            return jsonify({
                "ok": True,
                "message": f"已清除所有历史数据（共 {deleted} 行），可以重新开始了。"
            })
        except Exception as e:
            return jsonify({"ok": False, "message": str(e)}), 500

    # ==================================================================
    # 数据页面路由
    # ==================================================================

    @app.route("/trades")
    def trades_page():
        """交易记录页面。"""
        return render_template("trades.html")

    @app.route("/balance")
    def balance_page():
        """余额变动页面。"""
        return render_template("balance.html")

    @app.route("/stats")
    def stats_page():
        """交易统计页面。"""
        return render_template("stats.html")

    # ==================================================================
    # 数据 API
    # ==================================================================

    @app.route("/api/trades")
    def api_trades():
        """获取交易记录（支持翻页和币种筛选，数据全部来自 SQLite）。"""
        try:
            import db_manager
            db = db_manager.get_db()
            if db is None:
                return jsonify({"trades": [], "total": 0, "page": 1})
            limit = request.args.get("limit", 100, type=int)
            offset = request.args.get("offset", 0, type=int)
            coin = request.args.get("coin", None) or None
            total = db.count_trades(coin=coin)
            trades = db.get_trades(limit=limit, offset=offset, coin=coin)
            for t in trades:
                slug = t.get("market_slug", "")
                if slug:
                    t["polymarket_url"] = f"https://polymarket.com/zh/event/{slug}"
                t["pnl_display"] = round(t["pnl"], 2) if t.get("pnl") is not None else 0
                t["roi_display"] = round(t["roi_pct"], 2) if t.get("roi_pct") is not None else 0
            return jsonify({"trades": trades, "total": total})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/balance-changes")
    def api_balance_changes():
        """获取余额变动记录（支持翻页和操作类型筛选）。"""
        try:
            import db_manager
            db = db_manager.get_db()
            if db is None:
                return jsonify({"changes": [], "total": 0})
            limit = request.args.get("limit", 100, type=int)
            op_type = request.args.get("operation_type", None)
            changes = db.get_balance_changes(limit=limit, operation_type=op_type)
            for c in changes:
                c["amount_display"] = round(c["amount"], 2) if c.get("amount") is not None else 0
            return jsonify({"changes": changes, "total": len(changes)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/trade-stats")
    def api_trade_stats():
        """获取交易统计数据（全部来自 SQLite GROUP BY）。"""
        try:
            import db_manager
            db = db_manager.get_db()
            if db is None:
                return jsonify({"stats": {}, "by_coin": []})
            stats = db.get_trade_stats()
            by_coin = db.get_trade_stats_by_coin()
            return jsonify({
                "stats": {
                    "count": stats.get("count", 0),
                    "total_pnl": round(stats.get("total_pnl", 0), 2),
                    "avg_roi": round(stats.get("avg_roi", 0), 2),
                    "wins": stats.get("wins", 0),
                    "losses": stats.get("count", 0) - stats.get("wins", 0),
                    "win_rate": round(stats.get("wins", 0) / max(stats.get("count", 1), 1) * 100, 1),
                },
                "by_coin": by_coin,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return app


def run_server_thread(
    host: str, port: int, project_root: Path | None = None
) -> None:
    """在守护线程中启动 Flask Web 服务（由 main.py --web 调用）。"""
    app = create_app(project_root or PROJECT_ROOT)

    def run():
        # 本地仪表盘抑制 Werkzeug 生产环境警告
        import logging

        log = logging.getLogger("werkzeug")
        log.setLevel(logging.ERROR)
        app.run(host=host, port=port, threaded=True, use_reloader=False)

    t = threading.Thread(target=run, name="WebDashboard", daemon=True)
    t.start()


if __name__ == "__main__":
    # 独立模式：纯 UI（当机器人以 --web 运行时，从 bot_state.json 读取状态）
    import logging

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app = create_app()
    logging.info("打开 http://127.0.0.1:5050（仪表盘）")
    app.run(host="127.0.0.1", port=5050, threaded=True)
