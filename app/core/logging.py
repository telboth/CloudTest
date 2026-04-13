from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging() -> None:
    runtime_logs_dir = Path(__file__).resolve().parents[2] / ".runtime" / "logs"
    runtime_logs_dir.mkdir(parents=True, exist_ok=True)

    app_log = runtime_logs_dir / "api.app.log"
    error_log = runtime_logs_dir / "api.error.log"

    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app_handler = RotatingFileHandler(app_log, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    app_handler.setLevel(logging.INFO)
    app_handler.setFormatter(formatter)

    error_handler = RotatingFileHandler(error_log, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    existing_handler_keys = {
        getattr(handler, "baseFilename", None)
        for handler in root_logger.handlers
    }
    for handler in (app_handler, error_handler):
        if getattr(handler, "baseFilename", None) not in existing_handler_keys:
            root_logger.addHandler(handler)

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(logger_name)
        logger.setLevel(logging.INFO)
        logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
