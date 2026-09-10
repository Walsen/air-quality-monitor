"""Structured JSON logging with central redaction.

One single-line JSON object per event goes to stdout, configured once at startup
(Requirement 29.1). Nothing here uses ``print``, and nothing non-JSON reaches
stdout.

Redaction is deliberately implemented in exactly ONE place — the formatter — so
no call site can leak a secret by forgetting to scrub it (Requirement 29.4). A
call site is free to pass whatever context it has; the formatter decides what may
be rendered. That is the opposite of asking every caller to remember the rule, and
it is why the sensitive-key list lives here rather than in the serving layer.

What is never rendered: a resolved secret, a bearer credential, any
Authenticator claim beyond the user identity, and any User_Profile field. The user
identity itself IS permitted, because operational events need to be attributable.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
from typing import Any

_ROOT_NAME = "aqm_ingestion"
_REDACTED = "[redacted]"

# The five documented levels (Requirement 29.3).
_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_LEVEL_NAMES: dict[int, str] = {value: name for name, value in _LEVELS.items()}

# Context keys that may be rendered as-is. Everything sensitive is matched by the
# rules below; this set exists so the ONE identity field that is permitted
# (Requirement 29.4 allows the user identity) is not caught by them.
_PERMITTED_IDENTITY_KEYS = frozenset({"user_id", "sub", "subject"})

# Substrings marking a key whose value must never be rendered. Matched
# case-insensitively against the key name, so casing cannot defeat the rule.
_SENSITIVE_KEY_MARKERS = (
    "authorization", "bearer", "token", "credential", "secret", "password",
    "api_key", "apikey", "key",
    # Authenticator claims beyond the identity
    "email", "phone", "groups", "scope", "claims",
    # User_Profile fields (health-adjacent; Requirement 18 criterion 7)
    "asthma", "copd", "pregnan", "condition", "age_band", "age-band",
    "home_location", "work_location", "profile", "sensitivit", "medication",
    # The remaining Requirement 17.2 fields. NOTE the deliberate absence of a bare
    # "latitude"/"longitude" marker: Requirement 15.9 REQUIRES logging a SITE's position
    # while Requirement 17.9 forbids logging a USER's, and a key-name redactor cannot tell
    # the two subjects apart. A user's coordinates are covered by "location" here and, more
    # robustly, by UserProfile refusing to render its own contents at all.
    "location", "personal_threshold", "consent", "activity_level",
)

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {
    "message", "asctime", "event", "taskName",
}


def _is_sensitive(key: str) -> bool:
    """True when a context key must have its value redacted."""
    lowered = key.lower()
    if lowered in _PERMITTED_IDENTITY_KEYS:
        return False
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _redact(value: object) -> object:
    """Recursively redact a context value.

    Containers are walked so a sensitive field nested inside a mapping is caught
    too — a profile passed as one object must not slip through because only the
    outer key was checked.
    """
    if isinstance(value, dict):
        return {
            key: (_REDACTED if _is_sensitive(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _instant(created: float) -> str:
    """ISO-8601 UTC at whole-second precision ending in Z (Requirement 29.2)."""
    return _dt.datetime.fromtimestamp(created, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class InvalidLogLevelError(ValueError):
    """The configured log level is outside the recognized set (Requirement 29.9)."""


class _JsonFormatter(logging.Formatter):
    """Renders a record as one single-line JSON object, redacting as it goes."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "instant": _instant(record.created),
            "level": _LEVEL_NAMES.get(record.levelno, "info"),
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = _REDACTED if _is_sensitive(key) else _redact(value)
        return json.dumps(payload, separators=(",", ":"), default=str)


class _EventLogger:
    """Adapter so call sites pass an event name plus structured context."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, event: str, **context: object) -> None:
        """Developer detail (Requirement 29.3)."""
        self._logger.debug(event, extra=context)

    def info(self, event: str, **context: object) -> None:
        """Operational events, including ingestion summaries."""
        self._logger.info(event, extra=context)

    def warning(self, event: str, **context: object) -> None:
        """Recoverable conditions: quarantine, extrapolated calibration, faults."""
        self._logger.warning(event, extra=context)

    def error(self, event: str, **context: object) -> None:
        """Handled failures: archive write failure, poll failure."""
        self._logger.error(event, extra=context)

    def critical(self, event: str, **context: object) -> None:
        """Unrecoverable failures."""
        self._logger.critical(event, extra=context)


def configure_logging(level: str = "info") -> None:
    """Configure the JSON logger once at startup.

    Raises:
        InvalidLogLevelError: naming the supplied value and the permitted values
            when ``level`` is unrecognized (Requirement 29.9).
    """
    if level not in _LEVELS:
        permitted = ", ".join(_LEVELS)
        raise InvalidLogLevelError(
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
    """Return an event logger under the configured root."""
    return _EventLogger(logging.getLogger(f"{_ROOT_NAME}.{name}"))


def log_handled_error(
    logger: _EventLogger, event: str, error: BaseException, **context: object
) -> None:
    """Log a handled error with its type and stack information.

    Requirement 29.8: no failure is silent, and neither the exception nor its
    stack is ever returned to a client — the caller converts the failure into a
    documented response separately. Passing the stack as a STRING keeps the
    formatter's redaction applicable to it.
    """
    import traceback

    stack = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    logger.error(event, error_type=type(error).__name__, stack=stack, **context)
