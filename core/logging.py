import logging
import os
from logging import LoggerAdapter
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from clients.super_ninja_client import get_thread_id
from pythonjsonlogger.json import JsonFormatter

LOG_DIR = Path("/workspace/logs")
SHARED_HANDLER = None


def get_logger(service: str, **context) -> LoggerAdapter:
    logger = logging.getLogger(f"ninja.{service}")
    if not logger.handlers:
        logger.setLevel(os.environ.get("NINJA_LOG_LEVEL", "INFO").upper())
        logger.propagate = False
        logger.addHandler(get_handler())

    return LoggerAdapter(
        logger, {"service": service, "thread_id": get_thread_id(), **context}
    )


def get_handler() -> TimedRotatingFileHandler:
    """One shared, rotating ninja.log handler for every service — no baked-in context."""
    global SHARED_HANDLER
    if SHARED_HANDLER is None:
        if os.environ.get("NINJA_TEST_MODE") != "1":
            LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = TimedRotatingFileHandler(
            LOG_DIR / "ninja.log",
            when="midnight",
            backupCount=14,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(
            JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
                rename_fields={
                    "asctime": "timestamp",
                    "levelname": "level",
                    "name": "logger",
                },
            )
        )
        SHARED_HANDLER = handler
    return SHARED_HANDLER
