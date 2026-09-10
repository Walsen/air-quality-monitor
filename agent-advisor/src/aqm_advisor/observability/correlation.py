"""The session correlation identifier, and its propagation through OpenTelemetry baggage.

Req 32.11 wants the spans of one advisory session attributable to that session, and a
`runtimeSessionId` of at least 33 characters wherever this service originates one. That length
is AgentCore's constraint, not a preference: a shorter id is rejected by the platform, so a
generator emitting 32 characters would fail only once deployed. It is pinned here, and the
generator is asserted against the validator so the pair cannot drift into a state where the
service originates an id it would itself refuse.

**Nothing here configures a collector, an exporter or a provider** (Req 32.10). It reads and
writes the AMBIENT OpenTelemetry context only. With no provider installed the SDK returns proxy
instruments and a non-recording span, which cost nothing; when AgentCore installs its own
provider the same proxies bind to it. Installing a provider here would risk displacing the
platform's automatic instrumentation and losing every metric silently, which is the failure this
module is arranged to avoid.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import baggage, context

SESSION_ID_MIN_LENGTH = 33
"""AgentCore's minimum for `runtimeSessionId` (Req 32.11). A shorter one is rejected."""

SESSION_ID_BAGGAGE_KEY = "session.id"
"""The baggage key the session identifier travels under."""


class InvalidSessionIdError(ValueError):
    """A session identifier AgentCore would reject.

    The message names the CONSTRAINT and never the offending value. An id is not secret, but one
    supplied by a caller is untrusted input and refusal messages are logged; naming the rule is
    enough to fix the fault.
    """

    def __init__(self) -> None:
        """Build the refusal, naming only the constraint."""
        super().__init__(
            f"a session identifier must be at least {SESSION_ID_MIN_LENGTH} characters "
            "and not blank"
        )


def new_session_id() -> str:
    """Originate a session identifier that satisfies Req 32.11.

    Two hex UUID4s concatenated: 64 characters, comfortably above the 33-character floor, and
    with enough entropy that a repeat — which would attribute two sessions' spans to one — is
    not a practical concern.

    Deliberately not in `domain/`: this is a source of randomness, which the determinism rule
    bans there and an architecture check enforces.
    """
    return f"{uuid.uuid4().hex}{uuid.uuid4().hex}"


def validate_session_id(session_id: str) -> None:
    """Refuse a session identifier the platform would reject.

    Raises:
        InvalidSessionIdError: if the identifier is blank or shorter than the platform minimum.
    """
    if not session_id.strip() or len(session_id) < SESSION_ID_MIN_LENGTH:
        raise InvalidSessionIdError


def current_session_id() -> str | None:
    """The session identifier in the ambient context, or `None` outside a scope."""
    value = baggage.get_baggage(SESSION_ID_BAGGAGE_KEY)
    return value if isinstance(value, str) else None


@contextmanager
def session_scope(session_id: str) -> Iterator[str]:
    """Publish `session_id` to baggage for the duration of the block.

    Validates BEFORE attaching, so a scope can never publish an id AgentCore would reject — that
    failure would otherwise surface far from the call site that caused it.

    Detaches in a `finally`, so a raising turn cannot leak the context. A leaked correlation id
    is worse than none at all: the next session's spans would be attributed to this one and the
    telemetry would look correct while being wrong.
    """
    validate_session_id(session_id)
    token = context.attach(baggage.set_baggage(SESSION_ID_BAGGAGE_KEY, session_id))
    try:
        yield session_id
    finally:
        context.detach(token)


__all__ = [
    "SESSION_ID_BAGGAGE_KEY",
    "SESSION_ID_MIN_LENGTH",
    "InvalidSessionIdError",
    "current_session_id",
    "new_session_id",
    "session_scope",
    "validate_session_id",
]
