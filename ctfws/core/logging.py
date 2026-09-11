"""File logging configuration for normal and debug modes."""

from __future__ import annotations

import logging
from pathlib import Path


def configure_logging(log_file: Path, debug: bool = False) -> logging.Logger:
    """Configure one workspace file logger without polluting the TUI."""

    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ctfws")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    has_file_handler = any(
        isinstance(handler, logging.FileHandler) and handler.baseFilename == str(log_file)
        for handler in logger.handlers
    )
    if not has_file_handler:
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    return logger
