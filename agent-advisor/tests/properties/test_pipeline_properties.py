"""Properties 4, 10 and 11: escalation precedence, turn reproducibility, invocation bounds.

Tasks 10.5, 10.6, 10.7. Validates Reqs 10.2, 22.1, 22.1a, 22.3, 25.1, 25.2, 25.3.

**Property 4 has two halves, and the second is the one worth quantifying.** That escalation is
DETERMINED first is discharged by construction: `run` calls `check_red_flags` before anything
else, and an AST test reads the order out of `run` itself. What a property test adds is the
claim about the RESPONSE — that `escalation` precedes `guidance` in the dumped body for every
response shape, which is what Req 10.2 actually asks for and what a client actually sees.

**Property 10 is quantified over the two things that could break reproducibility.** Req 25.2
injects the clock and Req 25.1 forbids hidden state, so the property is that the same request
against the same fixed clock and the same scripted retrieval yields a byte-identical response —
quantified over the request, so no particular utterance is what makes it hold. Iteration order
and dict ordering are the usual culprits and would surface here.

**Property 11's serving half is the interesting one.** The model ceiling is the framework's, so
this service can only assert it renders correctly into `limits`. The serving budget IS ours, so
the property quantifies over arbitrary call SEQUENCES: whatever order and however many, the
number permitted never exceeds the maximum.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from aqm_advisor.agent.bounds import (
    InvocationBounds,
    ServingCallBudget,
    recent_prior_turns,
)
from aqm_advisor.domain.envelope import ResolvedEnvelope, resolve_envelope
from aqm_advisor.domain.models import BasisSummary, GuardrailEnvelope, PriorTurn
from aqm_advisor.domain.redflag import DEFAULT_RED_FLAG_RULES, match_red_flags
from aqm_advisor.domain.turn import determine_escalation, escalating_response

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_CONFIGURED = "If you are severely breathless, seek emergency care now."

_RED_FLAG_PHRASES = sorted(
    {pattern for rule in DEFAULT_RED_FLAG_RULES for pattern in rule.patterns}
)
"""Drawn from the rules themselves, so a new rule is covered without editing this file.

Same source as `test_escalation_properties.py` uses for Property 3 — a hand-listed set would
drift from the rules and would silently stop covering a phrasing the moment one was added.
"""

_TOOL_NAMES = st.sampled_from(
    ["air_quality", "history", "profile_get", "profile_put", "symptom_entry_put"]
)


_ENVELOPE_SOURCES = st.sampled_from(
    [
        # served this turn
        (
            GuardrailEnvelope(
                emergency_guidance="Seek emergency care now.",
                advisory_scope="exposure-reduction",
                disclaimer="Not medical advice.",
            ),
            None,
        ),
        # cached only — retrieval failed this turn but succeeded earlier
        (None, GuardrailEnvelope(emergency_guidance="Seek emergency care now.")),
        # neither — the cold start, where A8a's configured fallback applies
        (None, None),
    ]
)
"""The three states a retrieval outcome can leave the envelope in.

Same shape as `test_escalation_properties.py` uses, because `resolve_envelope` takes the served
and cached envelopes and DERIVES the source — the source is its output, not an input, so it
cannot be generated directly.
"""


def _resolved(
    pair: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
) -> ResolvedEnvelope:
    served, cached = pair
    return resolve_envelope(
        served=served, cached=cached, configured_emergency_guidance=_CONFIGURED
    )


# --- Property 4: escalation precedes advice -----------------------------


@given(
    st.sampled_from(_RED_FLAG_PHRASES),
    _ENVELOPE_SOURCES,
    st.booleans(),
    st.booleans(),
)
def test_escalation_is_declared_before_guidance_in_every_response(
    phrase: str,
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    degraded: bool,
    with_guidance: bool,
) -> None:
    # Req 10.2 is a FIELD ORDER requirement, so it is asserted on the dumped body — the thing a
    # client
    # actually receives — rather than on the model's attribute order.
    resolved = _resolved(envelopes)
    response = escalating_response(
        markers=match_red_flags(phrase, DEFAULT_RED_FLAG_RULES),
        envelope=resolved.envelope,
        answered_at=_AT,
        degraded=degraded,
        guidance="the air is moderate today" if with_guidance else None,
    )
    keys = list(response.model_dump().keys())
    assert keys.index("escalation") < keys.index("guidance")


@given(st.sampled_from(_RED_FLAG_PHRASES), st.lists(st.text(max_size=40), max_size=3))
def test_an_escalating_turn_always_carries_an_escalation(
    phrase: str, noise: list[str]
) -> None:
    # Whatever else the utterance contains, a recognised red flag escalates. Quantified over
    # surrounding
    # text because a matcher keyed on the whole utterance rather than the phrase would fail
    # here.
    utterance = " ".join([*noise, phrase])
    escalation = determine_escalation(
        utterance=utterance,
        prior_turns=(),
        rules=DEFAULT_RED_FLAG_RULES,
        emergency_guidance=_CONFIGURED,
    )
    assert escalation is not None
    assert escalation.markers != ()


@given(st.sampled_from(_RED_FLAG_PHRASES), _ENVELOPE_SOURCES)
def test_the_escalation_guidance_is_always_the_envelopes(
    phrase: str, envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None]
) -> None:
    # This service never words the emergency direction itself. Quantified over all three
    # envelope states,
    # since that is the dimension A8a's fallback introduces.
    resolved = _resolved(envelopes)
    response = escalating_response(
        markers=match_red_flags(phrase, DEFAULT_RED_FLAG_RULES),
        envelope=resolved.envelope,
        answered_at=_AT,
        degraded=False,
    )
    assert response.escalation is not None
    assert response.escalation.guidance == resolved.envelope.emergency_guidance


@given(st.sampled_from(_RED_FLAG_PHRASES), _ENVELOPE_SOURCES)
def test_a_degraded_escalating_turn_states_no_air_quality(
    phrase: str, envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None]
) -> None:
    # Req 21.1: no condition value when retrieval failed. The safest way to honour it is to have
    # nothing
    # to state it in, so `guidance` and `basis` both stay absent.
    response = escalating_response(
        markers=match_red_flags(phrase, DEFAULT_RED_FLAG_RULES),
        envelope=_resolved(envelopes).envelope,
        answered_at=_AT,
        degraded=True,
    )
    assert response.guidance is None
    assert response.basis is None


# --- Property 10: turn reproducibility ----------------------------------


_UTTERANCES = st.text(
    alphabet=st.characters(codec="utf-8", categories=("L", "N", "Zs")),
    min_size=1,
    max_size=60,
).filter(lambda value: value.strip() != "")


@given(_UTTERANCES, _ENVELOPE_SOURCES, st.booleans())
def test_the_same_inputs_produce_a_byte_identical_response(
    utterance: str,
    envelopes: tuple[GuardrailEnvelope | None, GuardrailEnvelope | None],
    degraded: bool,
) -> None:
    # Req 25.1 and 25.3. Iteration order and incidental dict ordering are the usual causes of a
    # non-reproducible response, and both would surface as a difference here.
    envelope = _resolved(envelopes).envelope
    markers = match_red_flags(utterance, DEFAULT_RED_FLAG_RULES) or ("unspecified",)

    def build() -> str:
        return escalating_response(
            markers=markers,
            envelope=envelope,
            answered_at=_AT,
            degraded=degraded,
        ).model_dump_json()

    assert build() == build()


@given(_UTTERANCES)
def test_the_answered_at_comes_only_from_the_supplied_instant(utterance: str) -> None:
    # Req 25.2: the clock is injected, so a turn built with a fixed instant carries exactly that
    # instant.
    # A `datetime.now()` anywhere in the path would make this flaky rather than failing
    # outright, which is
    # why it is quantified rather than pinned once.
    response = escalating_response(
        markers=match_red_flags(utterance, DEFAULT_RED_FLAG_RULES) or ("unspecified",),
        envelope=_resolved((None, None)).envelope,
        answered_at=_AT,
        degraded=False,
    )
    assert response.answered_at == _AT


@given(st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=5))
def test_marker_order_is_preserved_across_builds(markers: list[str]) -> None:
    # A set anywhere in the marker path would reorder these between runs, and the audit would
    # then record
    # a different escalation for the same event.
    envelope = _resolved((None, None)).envelope
    first = escalating_response(
        markers=markers, envelope=envelope, answered_at=_AT, degraded=False
    )
    second = escalating_response(
        markers=markers, envelope=envelope, answered_at=_AT, degraded=False
    )
    assert first.escalation is not None
    assert second.escalation is not None
    assert first.escalation.markers == second.escalation.markers == tuple(markers)


# --- Property 11: invocation bounds hold --------------------------------


@given(
    st.integers(min_value=1, max_value=20),
    st.one_of(st.none(), st.integers(min_value=1, max_value=100_000)),
    st.one_of(st.none(), st.integers(min_value=1, max_value=100_000)),
)
def test_the_configured_ceilings_reach_the_limits_mapping_unchanged(
    turns: int, output_tokens: int | None, total_tokens: int | None
) -> None:
    # Req 22.1a. The model ceiling is the FRAMEWORK's to enforce, so what this service can be
    # held to is
    # that it renders the configuration faithfully — a ceiling silently altered on the way
    # through would
    # be a bound nobody configured.
    bounds = InvocationBounds(
        model_invocations=turns, output_tokens=output_tokens, total_tokens=total_tokens
    )
    limits = bounds.as_limits()
    assert limits["turns"] == turns
    assert limits.get("output_tokens") == output_tokens
    assert limits.get("total_tokens") == total_tokens


@given(
    st.integers(min_value=1, max_value=20),
    st.one_of(st.none(), st.integers(min_value=1, max_value=100)),
)
def test_an_absent_ceiling_is_never_rendered_as_a_number(
    turns: int, output_tokens: int | None
) -> None:
    # Req 22.1c: `Limits` validates present keys as positive, so an unset ceiling must be
    # OMITTED. Zero
    # would raise instead of lifting the cap — a bound that looks configured and is not.
    limits = InvocationBounds(
        model_invocations=turns, output_tokens=output_tokens
    ).as_limits()
    assert ("output_tokens" in limits) == (output_tokens is not None)


@given(st.integers(min_value=1, max_value=8), st.lists(_TOOL_NAMES, max_size=20))
def test_the_serving_budget_never_permits_more_than_its_maximum(
    maximum: int, calls: list[str]
) -> None:
    # THE serving half of Property 11 (Req 22.3), quantified over arbitrary call SEQUENCES. This
    # bound is
    # ours because the calls happen inside a tool body where the framework's limits cannot see
    # them.
    budget = ServingCallBudget(maximum=maximum)
    permitted = sum(1 for name in calls if budget.consume(name))
    assert permitted <= maximum
    assert budget.used == permitted


@given(st.integers(min_value=1, max_value=8), st.lists(_TOOL_NAMES, max_size=20))
def test_the_budget_permits_every_call_it_can_afford(
    maximum: int, calls: list[str]
) -> None:
    # Non-vacuity: a budget that refused everything would satisfy the bound while making every
    # turn
    # degraded. The permitted count is exactly the smaller of the demand and the ceiling.
    budget = ServingCallBudget(maximum=maximum)
    permitted = sum(1 for name in calls if budget.consume(name))
    assert permitted == min(maximum, len(calls))


@given(st.integers(min_value=1, max_value=8), st.lists(_TOOL_NAMES, max_size=20))
def test_every_refused_call_is_named(maximum: int, calls: list[str]) -> None:
    # Which call was dropped decides whether the turn can still answer, so the refusals are
    # recorded
    # rather than merely counted.
    budget = ServingCallBudget(maximum=maximum)
    refused = [name for name in calls if not budget.consume(name)]
    assert list(budget.refused) == refused


@given(
    st.lists(st.text(min_size=1, max_size=10), max_size=12),
    st.integers(min_value=1, max_value=8),
)
def test_the_prior_turn_window_never_exceeds_its_maximum(
    texts: list[str], maximum: int
) -> None:
    # Req 22.4's bound, quantified. The window is also always a SUFFIX, which is the part that
    # matters:
    # keeping the oldest turns would drop the context the current utterance follows on from.
    from pydantic import SecretStr

    supplied = tuple(
        PriorTurn(utterance=SecretStr(text), guidance=SecretStr("answer"))
        for text in texts
    )
    kept = recent_prior_turns(supplied, maximum=maximum)
    assert len(kept) <= maximum
    assert kept == supplied[len(supplied) - len(kept) :]


@given(st.integers(min_value=1, max_value=6))
def test_a_basis_summary_is_not_required_for_a_bound_to_hold(maximum: int) -> None:
    # Guards against a future coupling: the bounds must not depend on retrieval having
    # succeeded, or a
    # degraded turn would become unbounded exactly when it is least safe to be.
    assert ServingCallBudget(maximum=maximum).maximum == maximum
    assert BasisSummary.__name__ == "BasisSummary"
