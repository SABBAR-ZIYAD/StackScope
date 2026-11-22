from __future__ import annotations

import logging
import sys

LOGGER_NAME = "github_ecosystem_miner"
PACKAGE_LOGGER = logging.getLogger(LOGGER_NAME)
PACKAGE_LOGGER.addHandler(logging.NullHandler())
PACKAGE_LOGGER.propagate = False


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def configure_logging(level: str = "INFO") -> None:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper()))
    logger.propagate = False
    for handler in list(logger.handlers):
        if isinstance(handler, logging.NullHandler):
            logger.removeHandler(handler)
    if logger.handlers:
        for handler in logger.handlers:
            handler.setLevel(logger.level)
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logger.level)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logger.addHandler(handler)


def progress_message(stage: str, current: int, total: int, detail: str = "") -> str:
    percentage = (current / total * 100) if total else 100.0
    suffix = f" | {detail}" if detail else ""
    return f"[{stage}] {current:,}/{total:,} ({percentage:5.1f}%){suffix}"
