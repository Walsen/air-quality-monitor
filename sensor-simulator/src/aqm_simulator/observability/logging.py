"""Structured JSON logging, configured once at startup.

Every log event is rendered as exactly one single-line JSON object written to
stdout, carrying an ISO-8601 UTC timestamp ending in ``Z``, a ``level``, and an
``event`` name, plus any structured context passed as keyword arguments
(Requirement 17.1). Nothing non-JSON is written to stdout, which the simulator
contract requires. Format and output are configured centrally here rather than
ad hoc per module (engineering-practices §6).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Any

# The four permitted levels (Requirement 17.5) mapped to stdlib levels.
_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}
# Reverse map so a stdlib record's level renders back to the contract name.
_LEVEL_NAMES: dict[int, str] = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}

_ROOT_NAME = "aqm_simulator"

# stdlib LogRecord attribute names, so structured extras can be separated out.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__
) | {"message", "asctime", "event", "taskName"}


def _utc_timestamp(created: float) -> str:
    """ISO-8601 UTC at whole-second precision ending in ``Z`` (Requirement 17.1)."""
    dt = _dt.datetime.fromtimestamp(created, tz=_dt.UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class _JsonFormatter(logging.Formatter):
    """Render a LogRecord as one single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _utc_timestamp(record.created),
            "level": _LEVEL_NAMES.get(record.levelno, "info"),
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["error_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
        # separators without spaces keep it compact and single-line
        return json.dumps(payload, separators=(",", ":"), default=str)


class _EventLogger:
    """Thin adapter so call sites pass an event name plus structured context."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, event: str, **context: Any) -> None:
        self._logger.debug(event, extra=context)

    def info(self, event: str, **context: Any) -> None:
        self._logger.info(event, extra=context)

    def warning(self, event: str, **context: Any) -> None:
        self._logger.warning(event, extra=context)

    def error(self, event: str, **context: Any) -> None:
        self._logger.error(event, extra=context)

    def exception(self, event: str, **context: Any) -> None:
        self._logger.exception(event, extra=context)


def configure_logging(level: str = "info") -> None:
    """Configure the JSON logger once at startup.

    Raises ``ValueError`` naming the supplied value and the four permitted
    values when ``level`` is not one of debug, info, warn, error
    (Requirement 17.8).
    """
    if level not in _LEVELS:
        permitted = ", ".join(_LEVELS)
        raise ValueError(
            f"invalid log level {level!r}; permitted values are: {permitted}"
        )

    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(_LEVELS[level])
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)


def get_logger(name: str) -> _EventLogger:
    """Return an event logger under the configured ``aqm_simulator`` root."""
    return _EventLogger(logging.getLogger(f"{_ROOT_NAME}.{name}"))
