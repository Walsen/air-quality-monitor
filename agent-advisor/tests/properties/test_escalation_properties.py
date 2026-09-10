"""Property 3: red-flag escalation is unconditional (task 4.3).

The design quantifies Property 3 over "every combination of retrieval success or failure, model
success or failure, and air-quality band". All three are discharged, but not in the same way,
and the difference matters:

* **Retrieval success or failure** — quantified over here as the envelope source:
  served, the last envelope retrieved in this process, or A8a's configured fallback.
  Those are the three states a retrieval outcome leaves behind.
* **Air-quality band** — discharged BY CONSTRUCTION. `determine_escalation` has no
  parameter for a reading, so no band can reach it.
* **Model success or failure** — discharged BY CONSTRUCTION for the same reason: the
  determination takes an utterance, prior turns, a rule set and the emergency text, so
  a model cannot reach it either. Asserted as a signature AND as a module-level
  dependency check in `test_envelope_and_escalation.py`, because a signature survives
  a refactor an expectation does not.

Quantifying over a dimension the code cannot observe would be an assertion that cannot fail. The
structural form is both stronger and honest about which it is.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st
from pydantic import SecretStr

from aqm_advisor.domain.envelope import EnvelopeSource, resolve_envelope
from aqm_advisor.domain.models import GuardrailEnvelope, PriorTurn
from aqm_advisor.domain.redflag import DEFAULT_RED_FLAG_RULES, match_red_flags
from aqm_advisor.domain.turn import determine_escalation, escalating_response

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_CONFIGURED = "If you are severely breathless, seek emergency care now."

# Every phrasing the default rules recognise. Drawn from the rules themselves rather than
# hand-listed, so
# a pattern added to the recognition set is covered by this property without anyone remembering
# to add it.
_RED_FLAG_PHRASES = sorted(
    {pattern for rule in DEFAULT_RED_FLAG_RULES for pattern in rule.patterns}
)

_ENVELOPES = st.sampled_from(
    [
        # served
        (
            GuardrailEnvelope(
                emergency_guidance="Seek emergency care now.",
                advisory_scope="exposure-reduction",
                disclaimer="Not medical advice.",
            ),
            None,
        ),
        # cached only (retrieval failed this turn, succeeded earlier)
        (None, GuardrailEnvelope(emergency_guidance="Seek emergency care now.")),
        # neither (cold start, retrieval never succeeded)
        (None, None),
    ]
)


@given(
    phrase=st.sampled_from(_RED_FLAG_PHRASES),
    prefix=st.text(max_size=40),
    suffix=st.text(max_size=40),
    sources=_ENVELOPES,
    degraded=st.booleans(),
)
def test_a_red_flag_always_escalates_whatever_the_retrieval_did(
    phrase: str,
    prefix: str,
    suffix: str,
    sources: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    degraded: bool,
) -> None:
    """An utterance containing a recognised phrase escalates, from any envelope source."""
    served, cached = sources
    utterance = f"{prefix} {phrase} {suffix}"

    markers = match_red_flags(utterance, DEFAULT_RED_FLAG_RULES)
    assert markers != (), f"the recognition set did not match its own pattern: {phrase!r}"

    resolved = resolve_envelope(
        served=served, cached=cached, configured_emergency_guidance=_CONFIGURED
    )
    response = escalating_response(
        markers=markers,
        envelope=resolved.envelope,
        answered_at=_AT,
        degraded=degraded,
    )

    assert response.escalation is not None
    assert response.escalation.kind == "emergency"
    # Req 10.1: the direction uses the envelope's wording, whichever source supplied it.
    assert response.escalation.guidance == resolved.envelope.emergency_guidance
    assert response.escalation.guidance != ""


@given(sources=_ENVELOPES)
def test_an_emergency_direction_is_available_from_every_source(
    sources: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
) -> None:
    """There is no resolution path that yields an empty emergency direction (Req 10.4, A8a)."""
    served, cached = sources
    resolved = resolve_envelope(
        served=served, cached=cached, configured_emergency_guidance=_CONFIGURED
    )
    assert resolved.envelope.emergency_guidance.strip() != ""
    assert resolved.source in set(EnvelopeSource)


@given(
    benign=st.text(alphabet=st.characters(whitelist_categories=("Ll", "Zs")), max_size=60),
)
def test_ordinary_text_does_not_escalate(benign: str) -> None:
    """The other half: escalation is unconditional GIVEN a red flag, not in general.

    Without this, a matcher that returned every marker for every input would satisfy the
    property above perfectly while making the service useless — it would direct every user to
    emergency care.
    """
    assume_no_pattern = all(
        pattern not in benign.casefold()
        for rule in DEFAULT_RED_FLAG_RULES
        for pattern in rule.patterns
    )
    if assume_no_pattern:
        assert match_red_flags(benign, DEFAULT_RED_FLAG_RULES) == ()


@given(
    phrase=st.sampled_from(_RED_FLAG_PHRASES),
    prefix=st.text(max_size=30),
    sources=_ENVELOPES,
    in_prior_turn=st.booleans(),
)
def test_the_determination_escalates_from_every_retrieval_state(
    phrase: str,
    prefix: str,
    sources: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    in_prior_turn: bool,
) -> None:
    """Step 1 escalates whatever retrieval did, and whichever message carried the red flag."""
    served, cached = sources
    resolved = resolve_envelope(
        served=served, cached=cached, configured_emergency_guidance=_CONFIGURED
    )

    # The red flag arrives either in this message or in an earlier one (Req 10.7).
    prior: tuple[PriorTurn, ...]
    if in_prior_turn:
        utterance = "what should I do?"
        prior = (
            PriorTurn(
                utterance=SecretStr(f"{prefix} {phrase}"), guidance=SecretStr("Seek care.")
            ),
        )
    else:
        utterance = f"{prefix} {phrase}"
        prior = ()

    escalation = determine_escalation(
        utterance=utterance,
        prior_turns=prior,
        rules=DEFAULT_RED_FLAG_RULES,
        emergency_guidance=resolved.envelope.emergency_guidance,
    )
    assert escalation is not None
    assert escalation.kind == "emergency"
    assert escalation.markers != ()
    assert escalation.guidance == resolved.envelope.emergency_guidance
    assert escalation.guidance.strip() != ""


@given(
    phrase=st.sampled_from(_RED_FLAG_PHRASES),
    sources=_ENVELOPES,
    degraded=st.booleans(),
)
def test_the_assembled_response_always_leads_with_the_escalation(
    phrase: str,
    sources: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    degraded: bool,
) -> None:
    """Req 10.2: however the turn was resolved, the emergency direction precedes the advice."""
    served, cached = sources
    resolved = resolve_envelope(
        served=served, cached=cached, configured_emergency_guidance=_CONFIGURED
    )
    escalation = determine_escalation(
        utterance=phrase,
        prior_turns=(),
        rules=DEFAULT_RED_FLAG_RULES,
        emergency_guidance=resolved.envelope.emergency_guidance,
    )
    assert escalation is not None
    response = escalating_response(
        markers=escalation.markers,
        envelope=resolved.envelope,
        answered_at=_AT,
        degraded=degraded,
    )
    dumped = list(response.model_dump().keys())
    assert dumped.index("escalation") < dumped.index("guidance")
    assert response.escalation is not None
