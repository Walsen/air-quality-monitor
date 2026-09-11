"""Structured JSON logging with central redaction (Requirements 19, 24).

One single-line JSON object per event on stdout, never ``print`` — the contract a log consumer
depends on, and asserted over real source by a test that walks the package's AST for a ``print``
call.

REDACTION IS CONFIGURED ONCE, IN THE FORMATTER, and that is the whole design. A per-call-site
discipline cannot deliver Requirement 19.2's promise, because the promise has to hold at call
sites nobody has written yet. So the formatter is the only place that decides, and every event
goes through it.

**Service 3 holds a category of data Service 2 never did: the user's own words.** Requirement
19.2 forbids any utterance substring reaching a log. A key-name redactor cannot promise that
alone — a caller can always put prose under an innocent key — so there are two layers, and the
second is structural: ``AdvisoryRequest`` carries the utterance in a field that refuses to
render, the same technique that made Service 2's ``UserProfile`` opaque. This module is the
first layer.

TWO CLAUSES THAT LOOK LIKE ONE. Requirement 19.4 permits logging the pseudonymous identity while
19.2 forbids everything else about the user. So the identity is an explicit exception rather
than an oversight, and a test asserts it still appears — otherwise every redaction test above it
would pass just as happily against a logger that emitted nothing.

THE COUNT ALLOWLIST CARRIES A LESSON FROM SERVICE 2, where a configured CAP on medications was
redacted as though it were a medication name. Here the collision is ``token``: Requirement 22
bounds a turn in OUTPUT TOKENS and Requirement 24.4 counts token usage, and a token COUNT is not
a credential. The exception is EXACT-MATCH, so ``output_tokens`` is permitted while
``access_token``, ``id_token`` and ``token_value`` stay redacted. Two guards keep it honest: a
near-miss must still be redacted, and an entry no marker would have caught is dead weight that
implies coverage it does not provide — a guard that caught two such entries in Service 2 on its
first run.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
import traceback
from typing import Any

REDACTED = "[redacted]"

PERMITTED_LOG_LEVELS: tuple[str, ...] = (
    "debug",
    "info",
    "warning",
    "error",
    "critical",
)

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}
_LEVEL_NAMES = {value: name for name, value in _LEVELS.items()}

PERMITTED_IDENTITY_KEYS = frozenset({"user_id", "sub", "subject"})
"""Requirement 19.4's exception: the pseudonymous identity may be logged."""

PERMITTED_COUNT_KEYS = frozenset(
    {
        # Requirement 22's budgets and Requirement 24.4's usage counters. Each is a NUMBER of
        # tokens, which the `token` marker cannot tell from a credential. Exact names only.
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "max_output_tokens",
        "max_total_tokens",
    }
)
"""Keys that look credential-shaped but carry a count. See the module docstring."""

PERMITTED_LOCATION_KEYS = frozenset({"location_name", "site_name", "site_code"})
"""Requirement 19.4's prescribed ALTERNATIVE to a coordinate.

Req 19.4 has two clauses, and the broad `location` marker below satisfied the first while making
the second impossible: "THE Service SHALL NOT include a coordinate in a log entry, and WHERE a
location must be identified THE Service SHALL use the location name Service 2 returned." With
`location_name` redacted there was no way to identify a site at all, so an operator diagnosing
one user's site had nothing to go on and the requirement's safe option was unavailable.

Exact names only, exactly as `PERMITTED_COUNT_KEYS` is. A substring exception would re-open the
marker it exists to narrow — `location` would match `location_coordinates` again. A site code
and a site name are public sensor facts, the same reasoning that lets the audit record store the
composite identifier.
"""

SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    # Credentials. The inbound credential is opaque to this service (assumption A4a) and must
    # never be rendered even so.
    "authorization",
    "bearer",
    "token",
    "credential",
    "secret",
    "password",
    "api_key",
    "apikey",
    "key",
    # The user's own words, and anything generated from them. Requirement 19.2 forbids an
    # utterance substring; Requirement 20.3 keeps guidance text out of the audit record, and a
    # log is no more entitled to it.
    "utterance",
    "prior_turn",
    "guidance",
    "generated",
    "prompt",
    "rejected_text",
    "text",
    # Health-adjacent profile values, read from Service 2 and never this service's to disclose.
    "condition",
    "sensitivit",
    "medication",
    "personal_threshold",
    "diagnos",
    # Symptom diary values (Requirement 31.10 permits at most the identity and the date).
    "severity",
    "marker",
    "reliever",
    "note",
    "symptom",
    # A user's position. Unlike Service 2 this service never logs a SITE's coordinates — it
    # reports what it retrieved rather than describing sensors — so there is no competing clause
    # here and a bare marker is safe.
    "latitude",
    "longitude",
    "coordinate",
    "location",
)

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "event",
    "taskName",
}


class InvalidLogLevelError(ValueError):
    """The configured log level is outside the recognized set (Requirement 23)."""


def is_sensitive(key: str) -> bool:
    """True when a context key must have its value redacted.

    Public because the tests assert over it directly: a rule this load-bearing should be
    checkable without going through a formatted record, so a near-miss can be pinned key by key.
    """
    lowered = key.lower()
    if (
        lowered in PERMITTED_IDENTITY_KEYS
        or lowered in PERMITTED_COUNT_KEYS
        or lowered in PERMITTED_LOCATION_KEYS
    ):
        return False
    return any(marker in lowered for marker in SENSITIVE_KEY_MARKERS)


def _redact(value: object) -> object:
    """Recursively redact a context value.

    Containers are walked, so a sensitive field nested inside a retrieved body is caught too — a
    Service 2 response passed as one object must not slip through because only the outer key was
    checked.
    """
    if isinstance(value, dict):
        return {
            key: (REDACTED if is_sensitive(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _instant(created: float) -> str:
    """ISO-8601 UTC at whole-second precision, ending in Z."""
    return _dt.datetime.fromtimestamp(created, tz=_dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class _JsonFormatter(logging.Formatter):
    """Renders a record as one single-line JSON object, redacting as it goes."""

    def format(self, record: logging.LogRecord) -> str:
        """Render the record, redacting every non-reserved context key by name."""
        payload: dict[str, Any] = {
            "instant": _instant(record.created),
            "level": _LEVEL_NAMES.get(record.levelno, "info"),
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = REDACTED if is_sensitive(key) else _redact(value)
        return json.dumps(payload, separators=(",", ":"), default=str)


class EventLogger:
    """Adapter so call sites pass an event NAME plus structured context.

    An event name rather than a formatted message, because a message built by interpolation is
    exactly how a redacted value re-enters a log: the formatter can only redact what arrives as
    a separate key.
    """

    def __init__(self, logger: logging.Logger) -> None:
        """Wrap a standard-library logger."""
        self._logger = logger

    def debug(self, event: str, **context: object) -> None:
        """Developer detail."""
        self._logger.debug(event, extra=context)

    def info(self, event: str, **context: object) -> None:
        """An operational event."""
        self._logger.info(event, extra=context)

    def warning(self, event: str, **context: object) -> None:
        """A recoverable issue — a degraded turn, a rejected generation, a retry."""
        self._logger.warning(event, extra=context)

    def error(self, event: str, **context: object) -> None:
        """A handled failure."""
        self._logger.error(event, extra=context)

    def critical(self, event: str, **context: object) -> None:
        """An unrecoverable failure."""
        self._logger.critical(event, extra=context)


def configure_logging(level: str = "info") -> None:
    """Configure the root logger once, emitting single-line JSON to stdout.

    Idempotent: the handler is REPLACED rather than appended. A second call that added a handler
    would double every line, which is how a duplicated log silently doubles an operator's error
    counts.

    Raises:
        InvalidLogLevelError: naming the permitted set, so an operator can correct it.
    """
    lowered = level.lower()
    if lowered not in _LEVELS:
        raise InvalidLogLevelError(
            f"log level {level!r} is not recognized; permitted: "
            f"{', '.join(PERMITTED_LOG_LEVELS)}"
        )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(_LEVELS[lowered])


def get_logger(name: str) -> EventLogger:
    """Return the event logger for a module."""
    return EventLogger(logging.getLogger(name))


def log_handled_error(
    logger: EventLogger, event: str, error: BaseException, **context: object
) -> None:
    """Log a handled error with its type and stack (Requirement 24.3).

    Errors handled at a boundary must still be logged — silent failure is not acceptable (§5) —
    while neither the exception nor its stack ever reaches a caller, which the boundary converts
    into a documented response separately.

    The stack is passed as a STRING so the formatter's redaction still applies to it. Rendered
    as a structure it would sidestep that path, and a stack frame can carry a local holding an
    utterance.
    """
    stack = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).strip()
    logger.error(event, error_type=type(error).__name__, stack=stack, **context)


__all__ = [
    "PERMITTED_COUNT_KEYS",
    "PERMITTED_IDENTITY_KEYS",
    "PERMITTED_LOCATION_KEYS",
    "PERMITTED_LOG_LEVELS",
    "REDACTED",
    "SENSITIVE_KEY_MARKERS",
    "EventLogger",
    "InvalidLogLevelError",
    "configure_logging",
    "get_logger",
    "is_sensitive",
    "log_handled_error",
]
