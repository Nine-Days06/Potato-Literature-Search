# utils/logger.py
"""统一日志配置，同时输出到控制台和文件。

文件按大小轮转（20MB × 5），并在每次取 logger 时清掉超过 30 天的旧日志。
"""

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime

LOG_MAX_BYTES = 20 * 1024 * 1024
LOG_BACKUPS = 5
LOG_RETENTION_DAYS = 30


def _purge_old(log_dir: Path) -> None:
    """删掉过期日志；目录不可读或删除失败都不影响主流程。"""
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for path in log_dir.glob("*.log*"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def get_logger(name: str, log_dir: Path = None) -> logging.Logger:
    """
    获取带格式的 logger。
    同时输出到控制台（INFO 级别）和日志文件（DEBUG 级别）。
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # 避免重复添加 handler

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台 handler
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding='utf-8')
        except Exception:
            pass
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 文件 handler
    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        _purge_old(log_dir)
        ts = datetime.now().strftime("%Y%m%d")
        fh = RotatingFileHandler(
            log_dir / f"{name}_{ts}.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger
