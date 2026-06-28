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
        """获取完整机器人状态快照（优先用内存快照，回退到文件快照）。"""
        import web_dashboard_state as wds

        snap = wds.get_snapshot()
        if snap.get("status") == "initializing" or not snap.get("coins"):
            file_snap = wds.read_state_file(root)
            if file_snap:
                return jsonify(file_snap)
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
    print(f"[WEB] 打开 http://127.0.0.1:5050（仪表盘）")
    app.run(host="127.0.0.1", port=5050, threaded=True)
