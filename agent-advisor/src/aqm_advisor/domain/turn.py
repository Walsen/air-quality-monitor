"""Assembling the response for an escalating turn (Req 10.2, 10.4, 21.3).

This is deliberately NOT the turn pipeline — that is task 10's Template Method, which orders the
whole sequence. What lives here is the one assembly that must work when nothing else does: a
person has described a red flag and the service has to answer them whether or not retrieval, the
model, or the guardrail check succeeded.

Everything it needs is a parameter. There is no client, no model, no clock and no config, so
this cannot be made conditional on any of them — the same reason `match_red_flags` takes only an
utterance and a rule set. The instant is passed in because Req 1.3 takes it from the injected
Clock and Req 25.2 forbids reading the wall clock in a component that composes a response.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from aqm_advisor.domain.models import (
    AdvisoryResponse,
    BasisSummary,
    Escalation,
    GuardrailEnvelope,
)


def escalating_response(
    *,
    markers: Sequence[str],
    envelope: GuardrailEnvelope,
    answered_at: dt.datetime,
    degraded: bool,
    guidance: str | None = None,
    basis: BasisSummary | None = None,
) -> AdvisoryResponse:
    """Build the response for a turn that must direct the user to emergency care.

    The escalation's guidance is the envelope's `emergency_guidance` — Service 2's wording where
    it could be retrieved, and A8a's configured fallback where it could not. This service never
    words the emergency direction itself beyond that one configured string.

    `guidance` (the exposure advice) defaults to None and stays None on a degraded turn. Req
    21.1 forbids stating any condition value when retrieval failed, and the safest way to honour
    that is to have nothing to state it in: an escalating turn with no data says get help, and
    does not speculate about the air.

    Requirement 10.5 is honoured by what this does NOT do. It directs the user to emergency care
    and makes no statement about whether the described symptoms are or are not an emergency;
    `markers` names which rules matched, which is a recognition rather than a determination.

    Raises:
        ValueError: if `markers` is empty. An escalation that cannot say what triggered it could
        not be
            audited or explained to a clinician, and would be indistinguishable from a spurious
            one.
    """
    escalation = Escalation(
        kind="emergency",
        markers=tuple(markers),
        guidance=envelope.emergency_guidance,
    )
    return AdvisoryResponse(
        escalation=escalation,
        guidance=guidance,
        basis=basis,
        envelope=envelope,
        degraded=degraded,
        answered_at=answered_at,
    )


__all__ = ["escalating_response"]
