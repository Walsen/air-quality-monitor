"""The four ports: Protocols the application depends on (Requirements 2, 3, 4, 20, 33, 34).

Design decision DD1: every I/O boundary is a ``Protocol`` with no implementation and NO Bedrock,
AgentCore or httpx type in any signature. That is not a stylistic preference — it is what lets
the
whole suite run with no AWS credentials and no network beyond localhost (Requirement 26.5), and
it
is asserted by a test that renders every signature and scans for an SDK name.

THE MODEL PORT IS ABSENT FROM THIS MODULE, and that is DD2 rather than an omission: the Strands
``Model`` abstract class IS the Model_Port. A wrapper would have to be kept in step with the
framework's own abstraction for no gain, and a ``Model`` subclass whose ``stream`` yields
scripted
events performs no network call — which is what makes the advisory path runnable offline. Note
the
real surface is FOUR abstract methods (``stream``, ``structured_output``, ``get_config``,
``update_config``), verified against ``strands-agents==1.55.1``: a subclass missing any of them
cannot be instantiated at all.

THE CREDENTIAL IS OPAQUE (assumption A4a). AgentCore validates the inbound token and Service 2
validates it on receipt, so a third parse here would add a place for three things to disagree —
and
a component that parses a token is a component that can log a claim. It travels as a ``str`` and
is
never inspected.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# --- boundary value types ------------------------------------------------


class ServingFailureKind(StrEnum):
    """Why a Serving_Client call did not produce a usable body (Requirement 21.1).

    A CATEGORY, never the underlying message: Requirement 21.4 forbids returning a raw error
    body,
    and a kind is what a degraded response can honestly name.
    """

    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    UNAUTHORIZED = "unauthorized"
    BAD_REQUEST = "bad_request"
    UNUSABLE_BODY = "unusable_body"
    SERVER_ERROR = "server_error"


class ServingClientError(Exception):
    """A Serving_Client call failed, carrying only its KIND.

    Carries no response body, no URL and no credential, so there is nothing here that a degraded
    response or a log could disclose even by accident (Requirements 5.4, 21.4).
    """

    def __init__(self, kind: ServingFailureKind) -> None:
        """Record the failure kind."""
        super().__init__(kind.value)
        self.kind = kind


class GuardrailVerdict(StrEnum):
    """What an independent output check concluded (Requirement 34.4)."""

    PASSED = "passed"
    INTERVENED = "intervened"
    UNAVAILABLE = "unavailable"
    """The check could not run. Requirement 34.6 FAILS CLOSED on this, so it is a distinct value
    rather than being folded into ``INTERVENED``: an operator needs to tell "the guardrail
    stopped
    this" from "the guardrail could not look", even though both withhold the text."""


@dataclass(frozen=True, slots=True)
class GuardrailResult:
    """One verdict, with the topic categories that fired.

    ``categories`` names what kind of rule matched — never the offending text. Requirement 8.6
    logs
    the category precisely so a rejected generation is diagnosable without storing it.
    """

    verdict: GuardrailVerdict
    categories: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdviceRecord:
    """The audit trail for one turn (Requirement 20.2).

    There is NOWHERE here to put an utterance, the guidance text, a condition, a threshold value
    or
    a coordinate (Requirement 20.3). That is structural minimisation, the same technique Service
    2
    applied to its own Audit_Record: it keeps erasure down to removing an identity, and a test
    asserts the field set so a later addition fails loudly.
    """

    user_id: str
    turn_at: dt.datetime
    route: str
    escalated: bool
    threshold_crossed: bool
    driving_pollutant: str | None
    record_references: tuple[str, ...]
    guardrail_rejected: bool
    rejection_category: str | None
    idempotency_key: str
    """Requirement 32.4c: a re-invoked entrypoint delivers the same turn twice, so a record
    needs
    to be recognisable as a duplicate rather than appended again."""


# --- the ports -----------------------------------------------------------


@runtime_checkable
class ServingClient(Protocol):
    """Service 2's API (Requirements 2, 3, 4).

    The ``credential`` is OPAQUE — forwarded, never parsed. See the module docstring.

    Every method raises :class:`ServingClientError` rather than returning a sentinel, because
    Requirement 21.1 requires a DEGRADED response naming the failure kind, and a sentinel return
    would let a caller treat "unavailable" as "nothing found".
    """

    def air_quality(self, credential: str) -> Mapping[str, object]:
        """Return the per-user air-quality view (Requirement 2.1)."""
        ...

    def history(
        self,
        credential: str,
        start: dt.datetime,
        end: dt.datetime,
        species: frozenset[str] | None = None,
    ) -> Mapping[str, object]:
        """Return a readings history over a window (Requirement 3.1)."""
        ...

    def profile_get(self, credential: str) -> Mapping[str, object]:
        """Return the user's stored profile (Requirement 4.1)."""
        ...

    def profile_put(
        self, credential: str, patch: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Replace the user's profile after explicit confirmation (Requirement 27.2)."""
        ...

    def profile_delete(self, credential: str) -> Mapping[str, object]:
        """Erase the user's profile and report the receipt."""
        ...

    def symptom_entry_put(
        self, credential: str, entry: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Record one diary entry (Requirement 28.3).

        Idempotent at Service 2, which replaces per date rather than accumulating — a property
        Requirement 32.4c depends on when a re-invoked entrypoint delivers the same turn twice.
        """
        ...


@runtime_checkable
class GuardrailChecker(Protocol):
    """Independent verification of EMITTED text (Requirement 34.2).

    ``source`` is always OUTPUT at this boundary. Requirement 34.3's denied topics are the
    load-bearing control for "never emit medication or diagnostic advice", and they can only act
    on
    text that already exists — which is why this is a check on a held generation rather than a
    hope
    about how one is produced.
    """

    def check(self, text: str) -> GuardrailResult:
        """Return the verdict for one generation. Never raises for an intervention."""
        ...


@runtime_checkable
class AdviceAuditStore(Protocol):
    """Where an Advice_Record is appended (Requirement 20.1)."""

    def append(self, record: AdviceRecord) -> None:
        """Record one turn. A failure here must not prevent the response (Requirement 20.5)."""
        ...

    def forget_user(self, user_id: str) -> int:
        """Erase this user's records and return the count."""
        ...


@runtime_checkable
class AssociationTrigger(Protocol):
    """Requests the Requirement 33 association derivation. FIRE-AND-FORGET.

    Never awaited inside a turn (Requirement 33.1): a queue between a person and their answer
    makes
    the product worse, which is why Requirement 32.13 refuses asynchrony for the conversational
    turn while Requirement 33 adopts it for the learning path.
    """

    def request(self, user_id: str, correlation_id: str) -> None:
        """Ask for a derivation. Returns immediately; a failure is logged, not raised."""
        ...


def port_protocols() -> Sequence[type]:
    """Every port declared here, so a test can iterate them rather than listing them.

    Derived rather than written twice: a port added later is scanned for an SDK type in its
    signature BY DEFAULT, which is the same "covered by default" shape the leak sweeps use.
    """
    return (ServingClient, GuardrailChecker, AdviceAuditStore, AssociationTrigger)


__all__ = [
    "AdviceAuditStore",
    "AdviceRecord",
    "AssociationTrigger",
    "GuardrailChecker",
    "GuardrailResult",
    "GuardrailVerdict",
    "ServingClient",
    "ServingClientError",
    "ServingFailureKind",
    "port_protocols",
]
