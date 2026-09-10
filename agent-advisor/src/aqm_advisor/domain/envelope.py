"""Resolving the Guardrail_Envelope, including when Service 2 is unreachable (Req 21.8, A8a).

Four clauses collide on this path and A8a is what resolves them. Req 10.1 sources the emergency
text from Service 2; Req 10.4 requires escalating even when the Serving_Client is unavailable;
Req 21.3 requires the envelope even in a degraded response; and A8 forbade a second copy of
those strings here because "the drift would be invisible".

A8's objection is not duplication as such — it is INVISIBLE drift. So the exception is bought
with a detector: `emergency_guidance_drifted` compares the configured fallback against the first
text actually retrieved, and a mismatch is logged. That makes the drift visible instead of
tolerated, which satisfies A8's stated reason rather than overriding it.

**The exception covers `emergencyGuidance` alone.** `resolve_envelope` takes ONE configured
string, so there is no parameter through which a local `advisoryScope` or `disclaimer` could be
introduced — A8a cannot widen by accident, only by someone changing this signature and the test
that pins it. Req 21.9 omits those two rather than inventing them, because a locally-authored
disclaimer would be this service making a compliance statement that is Service 2's to word,
whereas an absent emergency direction fails a person in trouble.

Nothing here holds state. The cache is passed IN, so this module stays pure and the domain keeps
no session memory (A6).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from aqm_advisor.domain.models import GuardrailEnvelope

_WHITESPACE = re.compile(r"\s+")


class EnvelopeSource(StrEnum):
    """Which source supplied the envelope this turn (Req 21.8 records it).

    Worth recording rather than inferring: an operator seeing escalations answered from
    `CONFIGURED` is seeing that retrieval has been failing since the process started, which is a
    different and more serious signal than a single degraded turn.
    """

    SERVED = "served"
    CACHED = "cached"
    CONFIGURED = "configured"


@dataclass(frozen=True, slots=True)
class ResolvedEnvelope:
    """The envelope to return, and where it came from."""

    envelope: GuardrailEnvelope
    source: EnvelopeSource


def resolve_envelope(
    *,
    served: GuardrailEnvelope | None,
    cached: GuardrailEnvelope | None,
    configured_emergency_guidance: str,
) -> ResolvedEnvelope:
    """Resolve the envelope in Req 21.8's order: served, cached, then the configured fallback.

    A stale cache never shadows a live answer, and the configured fallback is reached only when
    nothing has been retrieved yet in this process — the cold start, which is the first turn of
    a run and therefore exactly the turn that cannot be left without an emergency direction.

    The fallback yields `emergency_guidance` and nothing else. `advisory_scope` and `disclaimer`
    come back absent (Req 21.9).
    """
    if served is not None:
        return ResolvedEnvelope(envelope=served, source=EnvelopeSource.SERVED)
    if cached is not None:
        return ResolvedEnvelope(envelope=cached, source=EnvelopeSource.CACHED)
    return ResolvedEnvelope(
        envelope=GuardrailEnvelope(emergency_guidance=configured_emergency_guidance),
        source=EnvelopeSource.CONFIGURED,
    )


def emergency_guidance_drifted(*, served: str, configured: str) -> bool:
    """Whether the configured fallback has drifted from what Service 2 returns (Req 21.8).

    Compares on collapsed whitespace, because a reflowed string is not drift and reporting it as
    drift would produce a warning nobody can act on — an alert that fires on non-events is an
    alert an operator learns to ignore, which costs exactly what having no detector costs.

    Returns a BOOLEAN and never the texts. The caller logs the field name only: these strings
    are not health data, but a warning that quoted both would put clinical wording into a log
    for no diagnostic gain, and the field name is all an operator needs to go and compare them.
    """
    return _normalise(served) != _normalise(configured)


def _normalise(text: str) -> str:
    """Collapse whitespace and trim, so formatting is not mistaken for drift."""
    return _WHITESPACE.sub(" ", text).strip()


__all__ = [
    "EnvelopeSource",
    "ResolvedEnvelope",
    "emergency_guidance_drifted",
    "resolve_envelope",
]
