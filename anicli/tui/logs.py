"""Logs go to a file while the TUI runs.

anicli-api attaches a StreamHandler bound to the real stderr on import: its retry warnings
("[2] DDOSServerDetectError ...") would be written over the Textual screen and break the layout.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from anicli.common.config import get_data_dir

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_handler: RotatingFileHandler | None = None


def log_path() -> Path:
    return get_data_dir() / "anihub.log"


def setup() -> Path:
    """Route library and app logs into `log_path()` instead of the terminal. Safe to call twice."""
    global _handler  # noqa: PLW0603
    if _handler is None:
        _handler = RotatingFileHandler(log_path(), maxBytes=1_000_000, backupCount=1, encoding="utf-8")
        _handler.setFormatter(logging.Formatter(_FORMAT))
        # warnings of any library (httpx, textual-image, ...) end up here instead of stderr
        logging.getLogger().addHandler(_handler)

    api = logging.getLogger("anicli-api")
    for handler in list(api.handlers):
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            api.removeHandler(handler)
    # its records (INFO and up) propagate to the root file handler
    api.propagate = True
    logging.getLogger("anicli").setLevel(logging.INFO)
    return log_path()
