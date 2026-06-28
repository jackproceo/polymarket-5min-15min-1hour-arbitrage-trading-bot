"""
线程安全的快照 + 停止请求，用于 Web 仪表盘（与机器人同一进程）。
"""
import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

_lock = threading.RLock()
_snapshot: Dict[str, Any] = {"status": "initializing"}
_stop_requested = False
_session_start: float = 0.0


def set_session_start(ts: float) -> None:
    """设置机器人启动时间戳（首次启动时调用一次）。"""
    global _session_start
    with _lock:
        _session_start = ts


def set_snapshot(data: Dict[str, Any]) -> None:
    """由主交易循环调用（约每 0.1 秒一次）。"""
    global _snapshot
    with _lock:
        data = dict(data)
        data["updated_at"] = time.time()
        _snapshot = data


def get_snapshot() -> Dict[str, Any]:
    """获取当前快照的只读副本（线程安全）。"""
    with _lock:
        return dict(_snapshot)


def request_stop() -> None:
    """发送停止请求（Web 仪表盘 / 远程停止用）。"""
    global _stop_requested
    with _lock:
        _stop_requested = True


def consume_stop_request() -> bool:
    """主循环：如果为 True，设置停止标记并清除请求。"""
    global _stop_requested
    with _lock:
        if _stop_requested:
            _stop_requested = False
            return True
        return False


def write_state_file(project_root: Path, data: Dict[str, Any]) -> None:
    """可选：写入 logs/bot_state.json，用于无需共享内存的只读监控。"""
    path = project_root / "logs" / "bot_state.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        payload = dict(data)
        payload["updated_at"] = time.time()
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        tmp.replace(path)
    except OSError:
        pass


def read_state_file(project_root: Path) -> Optional[Dict[str, Any]]:
    """从 logs/bot_state.json 读取快照（独立模式读取器用）。"""
    path = project_root / "logs" / "bot_state.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
