from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime


def setup_logging(log_dir: Path) -> tuple[logging.Logger, Path]:
    """初始化文件日志，并返回应用 logger 与本次日志文件路径。"""
    log_dir = Path(log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / f"hongguo_{datetime.now():%Y%m%d_%H%M%S}.log"
    logger = logging.getLogger("hongguo")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    # 防止同一进程重复创建窗口时叠加 handler。
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(threadName)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=8,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    logger.info("应用启动")
    logger.info("日志文件：%s", log_path)
    return logger, log_path
