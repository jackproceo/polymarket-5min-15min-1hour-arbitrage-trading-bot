"""
结构化日志配置 - 统一处理全项目的 print() → logging 迁移。

用法：
    from utils.logging_setup import get_logger
    log = get_logger("module_name")
    log.info("message")
    log.error("error")
    log.debug("debug detail")
"""
import logging
import sys
from pathlib import Path
from typing import Optional


# 模块级缓存，避免重复创建
_loggers: dict[str, logging.Logger] = {}
_initialized = False


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[str] = "logs",
    max_bytes: int = 10 * 1024 * 1024,  # 10 MB
    backup_count: int = 5,
) -> None:
    """
    全局日志配置：同时输出到文件（轮转）和控制台。

    Args:
        level: 日志级别 (DEBUG/INFO/WARNING/ERROR)
        log_dir: 日志目录，None/空字符串则只输出到控制台
        max_bytes: 单个日志文件最大字节数
        backup_count: 保留的旧日志文件数
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    # 清除默认 handler
    root.handlers.clear()

    fmt = logging.Formatter(
        "[%(asctime)s.%(msecs)03d] [%(levelname)-5s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 控制台 handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    # 文件 handler（轮转）
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        try:
            from logging.handlers import RotatingFileHandler

            file_handler = RotatingFileHandler(
                str(log_path / "meridian.log"),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError:
            pass  # 文件日志不可用时降级到控制台

    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def get_logger(name: str) -> logging.Logger:
    """获取模块级 Logger（自动加前缀 'meridian.'）。"""
    full_name = f"meridian.{name}"
    if full_name not in _loggers:
        _loggers[full_name] = logging.getLogger(full_name)
    return _loggers[full_name]
