"""Property 3, partially: red-flag escalation is unconditional (task 4.3).

**Scope, stated honestly.** The design's Property 3 quantifies over "every combination of
retrieval success or failure, model success or failure, and air-quality band". Two of those
three dimensions exist today: retrieval success or failure appears as the envelope source
(served, cached, or the A8a configured fallback), and air-quality band is irrelevant by
construction because the matcher has no parameter for it. The model dimension needs task 10's
pipeline, so this file does NOT yet discharge Property 3 in full and task 4.3 stays open until
it does.

Labelling it as complete would be worse than leaving it open: a property marked validated is a
property nobody re-reads.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from aqm_advisor.domain.envelope import EnvelopeSource, resolve_envelope
from aqm_advisor.domain.models import GuardrailEnvelope
from aqm_advisor.domain.redflag import DEFAULT_RED_FLAG_RULES, match_red_flags
from aqm_advisor.domain.turn import escalating_response

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
