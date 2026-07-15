"""Logging configuration: a rotating file handler plus an optional in-UI callback handler.

Secrets are never logged by the application; this module only wires up handlers.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Project root is two levels up from this file (src/contextbot/logging_setup.py).
DEFAULT_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"


class CallbackLogHandler(logging.Handler):
    """Forwards each formatted log record to a callback (e.g. the UI log view)."""

    def __init__(self, callback: Callable[[str], None], level: int = logging.INFO):
        super().__init__(level)
        self._callback = callback
        self.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(self.format(record))
        except Exception:  # never let logging crash the app
            self.handleError(record)


def configure_logging(
    level: str = "INFO",
    *,
    log_dir: Optional[Path] = None,
    ui_callback: Optional[Callable[[str], None]] = None,
) -> logging.Logger:
    """Configure the ``contextbot`` logger with a rotating file handler and optional UI handler.

    Returns the package logger. Idempotent: existing handlers are cleared first.
    """
    log_dir = log_dir or DEFAULT_LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("contextbot")
    logger.setLevel(level)
    logger.propagate = False

    # Clear previous handlers so repeated calls (e.g. in tests) don't stack up.
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    file_handler = RotatingFileHandler(
        log_dir / "contextbot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    logger.addHandler(file_handler)

    if ui_callback is not None:
        logger.addHandler(CallbackLogHandler(ui_callback, level=logging.getLevelName(level)))

    return logger
