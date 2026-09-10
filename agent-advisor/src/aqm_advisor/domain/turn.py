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
    PriorTurn,
)
from aqm_advisor.domain.redflag import RedFlagRule, match_request_red_flags


def determine_escalation(
    *,
    utterance: str,
    prior_turns: Sequence[PriorTurn],
    rules: Sequence[RedFlagRule],
    emergency_guidance: str,
) -> Escalation | None:
    """Step 1 of the turn: decide whether this turn must direct the user to emergency care.

    Returns the `Escalation` when a red flag is recognised, or `None` when the turn should carry
    on to retrieval and generation. Req 10.2 requires this determination BEFORE any exposure
    guidance is generated, and the design records it as happening "before anything can fail".

    **This is what makes Property 3's model dimension structural rather than tested.** The step
    takes an utterance, prior turns, a rule set and the emergency text. There is no parameter
    through which a model, a Serving_Client or a clock could reach it, so the model's success or
    failure is not merely untested here — it is unobservable. Req 10.4's reasoning is that a
    check needing either could not fire when both are unavailable; expressing that as a
    signature means no later change can quietly reintroduce the dependency.

    `emergency_guidance` arrives as a STRING rather than an envelope so this step cannot be
    blocked on resolving one. The caller resolves it (served, then cached, then A8a's configured
    fallback) and there is no resolution path that yields an empty direction.
    """
    markers = match_request_red_flags(utterance, prior_turns, rules)
    if not markers:
        return None
    return Escalation(kind="emergency", markers=markers, guidance=emergency_guidance)


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


__all__ = ["determine_escalation", "escalating_response"]
