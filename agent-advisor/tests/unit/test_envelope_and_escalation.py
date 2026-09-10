"""Tests for envelope resolution and the escalating response (task 4.4).

This is the path where somebody describes a severe attack while Service 2 is unreachable. Four
clauses collide there — Req 10.1 sources the emergency text from Service 2, Req 10.4 requires
escalating anyway, Req 21.3 requires the envelope anyway, and A8 forbade a local copy — so until
A8a it could not be built at all. `GuardrailEnvelope` deliberately had no default, which is why
the gap failed loudly instead of being papered over.

A8a's exception is narrow and its guard is what earns it: the configured fallback covers
`emergencyGuidance` ONLY, and the drift A8 feared is made VISIBLE by comparing the configured
text against the first one actually retrieved. Several tests below exist to keep that narrowness
honest — a fallback that quietly grew to cover the disclaimer would satisfy every test about
escalation while breaking the assumption that permitted it.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import SecretStr

from aqm_advisor.domain.envelope import (
    EnvelopeSource,
    emergency_guidance_drifted,
    resolve_envelope,
)
from aqm_advisor.domain.models import AdvisoryResponse, GuardrailEnvelope, PriorTurn
from aqm_advisor.domain.redflag import DEFAULT_RED_FLAG_RULES
from aqm_advisor.domain.turn import determine_escalation, escalating_response

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_CONFIGURED = "If you are severely breathless, seek emergency care now."
_SERVED_TEXT = "If your lips or face look blue, seek emergency care now."


def _served() -> GuardrailEnvelope:
    return GuardrailEnvelope(
        advisory_scope="exposure-reduction",
        emergency_guidance=_SERVED_TEXT,
        disclaimer="Not medical advice.",
    )


# --- Req 21.8: the resolution order ------------------------------------

def test_the_served_envelope_wins() -> None:
    resolved = resolve_envelope(
        served=_served(), cached=None, configured_emergency_guidance=_CONFIGURED
    )
    assert resolved.source is EnvelopeSource.SERVED
    assert resolved.envelope.emergency_guidance == _SERVED_TEXT


def test_the_cache_is_used_when_nothing_was_served() -> None:
    resolved = resolve_envelope(
        served=None, cached=_served(), configured_emergency_guidance=_CONFIGURED
    )
    assert resolved.source is EnvelopeSource.CACHED
    assert resolved.envelope.emergency_guidance == _SERVED_TEXT
    assert resolved.envelope.disclaimer == "Not medical advice."


def test_the_configured_fallback_is_used_when_the_cache_is_cold() -> None:
    # The cold-start case, which is exactly the first turn of a run — and a first turn is when a
    # new
    # user may be in trouble, so it cannot be the one case that has no emergency text.
    resolved = resolve_envelope(
        served=None, cached=None, configured_emergency_guidance=_CONFIGURED
    )
    assert resolved.source is EnvelopeSource.CONFIGURED
    assert resolved.envelope.emergency_guidance == _CONFIGURED


def test_the_served_envelope_wins_even_when_a_cache_exists() -> None:
    # Precedence tested in both directions: a stale cache must never shadow a live answer.
    stale = GuardrailEnvelope(
        advisory_scope="old", emergency_guidance="old text", disclaimer="old disclaimer"
    )
    resolved = resolve_envelope(
        served=_served(), cached=stale, configured_emergency_guidance=_CONFIGURED
    )
    assert resolved.envelope.emergency_guidance == _SERVED_TEXT


def test_the_cache_wins_over_the_configured_fallback() -> None:
    resolved = resolve_envelope(
        served=None, cached=_served(), configured_emergency_guidance=_CONFIGURED
    )
    assert resolved.envelope.emergency_guidance == _SERVED_TEXT


# --- Req 21.9: the exception is `emergencyGuidance` ALONE ---------------

def test_the_fallback_supplies_no_scope_and_no_disclaimer() -> None:
    # THE test that keeps A8a narrow. Req 21.9 omits rather than invents: a locally-authored
    # disclaimer
    # would be this service making a compliance statement that is Service 2's to word.
    resolved = resolve_envelope(
        served=None, cached=None, configured_emergency_guidance=_CONFIGURED
    )
    assert resolved.envelope.advisory_scope is None
    assert resolved.envelope.disclaimer is None


def test_no_configured_scope_or_disclaimer_can_be_supplied_at_all() -> None:
    # Structural, not behavioural: the resolver takes ONE configured string. There is no
    # parameter
    # through which a local scope or disclaimer could be introduced, so A8a cannot widen by
    # accident.
    import inspect

    parameters = set(inspect.signature(resolve_envelope).parameters)
    assert parameters == {"served", "cached", "configured_emergency_guidance"}


def test_a_partial_envelope_still_carries_the_emergency_text() -> None:
    resolved = resolve_envelope(
        served=None, cached=None, configured_emergency_guidance=_CONFIGURED
    )
    assert resolved.envelope.emergency_guidance


def test_the_emergency_guidance_is_never_optional_on_the_model() -> None:
    # The asymmetry made structural: scope and disclaimer may be absent, the emergency direction
    # may not.
    fields = GuardrailEnvelope.model_fields
    assert fields["emergency_guidance"].is_required()
    assert not fields["advisory_scope"].is_required()
    assert not fields["disclaimer"].is_required()


# --- Req 21.8: the drift detector A8a is bought with ---------------------

def test_drift_is_detected_when_the_configured_text_differs() -> None:
    assert emergency_guidance_drifted(served=_SERVED_TEXT, configured=_CONFIGURED) is True


def test_no_drift_is_reported_when_the_texts_agree() -> None:
    # Non-vacuity: a detector that always reported drift would make the warning meaningless and
    # train
    # an operator to ignore it — the same cost as a red-flag matcher that fires on everything.
    assert emergency_guidance_drifted(served=_CONFIGURED, configured=_CONFIGURED) is False


def test_drift_comparison_ignores_incidental_whitespace() -> None:
    # A reflowed string is not drift. Reporting it as drift would produce a warning nobody can
    # act on.
    assert (
        emergency_guidance_drifted(
            served="Seek emergency  care\nnow.", configured="Seek emergency care now."
        )
        is False
    )


def test_drift_is_reported_for_a_real_wording_change() -> None:
    assert (
        emergency_guidance_drifted(
            served="Seek emergency care now.", configured="Call your clinician today."
        )
        is True
    )


# --- Req 10.4: the escalation survives a failed retrieval ---------------

def test_an_escalating_turn_returns_an_escalation_with_no_retrieval() -> None:
    # Req 10.4. The whole point: no served envelope, no basis, nothing retrieved — and the user
    # is still
    # told to get help.
    resolved = resolve_envelope(
        served=None, cached=None, configured_emergency_guidance=_CONFIGURED
    )
    response = escalating_response(
        markers=("severe_breathlessness",),
        envelope=resolved.envelope,
        answered_at=_AT,
        degraded=True,
    )
    assert response.escalation is not None
    assert response.escalation.kind == "emergency"
    assert response.escalation.markers == ("severe_breathlessness",)
    assert response.escalation.guidance == _CONFIGURED


def test_an_escalating_degraded_turn_still_carries_the_envelope() -> None:
    # Req 21.3: the envelope is returned even in a degraded response.
    resolved = resolve_envelope(
        served=None, cached=_served(), configured_emergency_guidance=_CONFIGURED
    )
    response = escalating_response(
        markers=("blue_lips_or_face",),
        envelope=resolved.envelope,
        answered_at=_AT,
        degraded=True,
    )
    assert response.envelope.emergency_guidance == _SERVED_TEXT
    assert response.degraded is True


def test_an_escalating_turn_states_no_condition_value_when_nothing_was_retrieved() -> None:
    # Req 21.1 forbids stating a condition value on a failed retrieval, so the escalating
    # degraded
    # response carries no basis and no exposure prose to state one in.
    response = escalating_response(
        markers=("severe_breathlessness",),
        envelope=_served(),
        answered_at=_AT,
        degraded=True,
    )
    assert response.basis is None
    assert response.guidance is None


def test_the_escalation_requires_at_least_one_marker() -> None:
    # An escalation with no marker could not be audited or explained to a clinician.
    with pytest.raises(ValueError, match="marker"):
        escalating_response(markers=(), envelope=_served(), answered_at=_AT, degraded=True)


# --- Req 10.2: the emergency direction comes first -----------------------

def test_the_escalation_precedes_the_guidance_in_the_serialised_response() -> None:
    # Req 10.2 places the emergency direction FIRST in the response, and design Property 4
    # asserts the
    # emergency guidance appears before any exposure guidance. With a structured response that
    # is field
    # ORDER: a consumer rendering top to bottom must meet the emergency direction before the
    # advice.
    #
    # Req 1.2's enumeration is a field SET ("carrying exactly these fields"), not a
    # serialisation
    # contract, so ordering escalation ahead of guidance satisfies 10.2 without violating 1.2.
    keys = list(AdvisoryResponse.model_fields)
    assert keys.index("escalation") < keys.index("guidance")


def test_the_serialised_body_puts_the_escalation_before_the_guidance() -> None:
    # The property above as it actually reaches a caller, so a future model_config change that
    # altered
    # dump order would fail here rather than silently reordering the safety-critical field.
    response = escalating_response(
        markers=("severe_breathlessness",), envelope=_served(), answered_at=_AT, degraded=True
    )
    dumped = list(response.model_dump().keys())
    assert dumped.index("escalation") < dumped.index("guidance")


def test_a_non_escalating_response_is_unaffected_by_the_ordering() -> None:
    # Non-vacuity for the reorder: the field set is unchanged, so Req 1.2 still holds.
    assert set(AdvisoryResponse.model_fields) == {
        "guidance",
        "basis",
        "envelope",
        "escalation",
        "degraded",
        "answered_at",
    }


# --- step 1 of the turn: the determination itself -----------------------

def test_a_red_flag_yields_an_emergency_escalation() -> None:
    escalation = determine_escalation(
        utterance="I can't breathe",
        prior_turns=(),
        rules=DEFAULT_RED_FLAG_RULES,
        emergency_guidance=_CONFIGURED,
    )
    assert escalation is not None
    assert escalation.kind == "emergency"
    assert escalation.markers == ("severe_breathlessness",)
    assert escalation.guidance == _CONFIGURED


def test_an_ordinary_utterance_yields_no_escalation() -> None:
    # Non-vacuity for every escalation test: a determination that always escalated would direct
    # every user to emergency care, which costs exactly what missing a red flag costs.
    assert (
        determine_escalation(
            utterance="Is it safe to run this evening?",
            prior_turns=(),
            rules=DEFAULT_RED_FLAG_RULES,
            emergency_guidance=_CONFIGURED,
        )
        is None
    )


def test_a_red_flag_in_a_prior_user_utterance_still_escalates() -> None:
    prior = (
        PriorTurn(utterance=SecretStr("I can't breathe"), guidance=SecretStr("Seek care.")),
    )
    assert (
        determine_escalation(
            utterance="what should I do?",
            prior_turns=prior,
            rules=DEFAULT_RED_FLAG_RULES,
            emergency_guidance=_CONFIGURED,
        )
        is not None
    )


def test_the_determination_cannot_observe_the_model_or_the_client() -> None:
    # This is what discharges Property 3's "for every combination of model success or failure".
    # The step takes an utterance, prior turns, a rule set and the emergency text — there is no
    # parameter through which a model, a client or a clock could reach it, so the model's
    # outcome is
    # not merely untested here, it is unobservable. A signature survives refactors that an
    # expectation does not.
    import inspect

    assert set(inspect.signature(determine_escalation).parameters) == {
        "utterance",
        "prior_turns",
        "rules",
        "emergency_guidance",
    }


def test_the_turn_module_depends_on_no_adapter_port_or_model() -> None:
    # The other half of the same argument, at module level: even a transitive reference would
    # mean
    # the escalation path could be affected by something that can fail.
    import ast
    import pathlib

    from aqm_advisor.domain import turn

    tree = ast.parse(pathlib.Path(turn.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in (
        "aqm_advisor.ports.protocols",
        "aqm_advisor.ports.clock",
        "aqm_advisor.adapters.model.scripted",
        "strands",
    ):
        assert forbidden not in imported, f"the escalation path can reach {forbidden}"
