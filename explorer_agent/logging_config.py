"""
Centralized logging. Use get_logger(__name__) everywhere instead of print().
Logs go to console (human-readable) AND a rotating file (for later audit
of full runs - including LLM call counts, cache hits/misses, timings).
"""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config

_LOG_DIR = Path(Config.LOG_DIR)
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / "explorer_agent.log"

_configured = False


def _configure_root_logger():
    global _configured
    if _configured:
        return

    root = logging.getLogger("explorer_agent")
    root.setLevel(getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO))

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)-24s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    file_handler = RotatingFileHandler(_LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    root.propagate = False
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_root_logger()
    return logging.getLogger(f"explorer_agent.{name}")