"""Tests for the grounding set, the symptom draft and the audit record (task 3.3).

`test_the_audit_record_has_nowhere_to_put_health_adjacent_content` is the one that matters. Req
20.3 keeps the audit trail free of the utterance, the guidance text, a condition, a sensitivity,
a personal threshold and a coordinate — so that erasure has only an identity to remove. A test
that checked one recorded instance would pass while the FIELD still existed, waiting for a
future call site to populate it. So this asserts over the field set: there must be nowhere to
put those things.

That technique is carried from Service 2, where a medication entry was made structurally unable
to hold a dose — the test asserted no such field exists rather than that it was empty.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from aqm_advisor.domain.records import (
    AdviceRecord,
    RetrievedValues,
    SymptomEntryDraft,
    ToolCall,
)

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _as_instant(value: object) -> dt.datetime:
    """Narrow a parametrised value back to a datetime for mypy --strict."""
    assert isinstance(value, dt.datetime)
    return value


def _record(**kwargs: object) -> AdviceRecord:
    defaults: dict[str, object] = {
        "user_id": "pseudonymous-user-1",
        "turn_at": _AT,
        "route": "/invocations",
        "escalated": False,
        "threshold_crossed": False,
        "driving_pollutant": "PM25",
        "record_references": ("AQM1:PM25:2026-07-01T12:00:00Z:PT1H",),
        "guardrail_rejected": False,
        "rejection_category": None,
        "idempotency_key": "k1",
    }
    return AdviceRecord(**{**defaults, **kwargs})  # type: ignore[arg-type]


# --- Req 20.3: the audit trail is structurally minimal ------------------

_FORBIDDEN_TOKENS = frozenset(
    {
        # Matched EXACTLY against a field name's underscore-separated tokens, not as substrings.
        # A substring sweep is wrong here and this is not hypothetical: `lat` occurs inside
        # `escaLATed`, a field Req 20.2 REQUIRES, so a substring sweep fails on correct code and
        # the
        # obvious "fix" is to delete a required field. Same collision class as Service 2's
        # logger,
        # where a configured medication CAP was redacted as though it were a medication.
        "utterance",
        "guidance",
        "text",
        "prose",
        "condition",
        "sensitivity",
        "lat",
        "lon",
        "latitude",
        "longitude",
        "coordinate",
        "coordinates",
        "location",
        "credential",
        "token",
    }
)

_FORBIDDEN_PHRASES = ("threshold_value", "personal_threshold", "secret")
"""Multi-token or affix-bearing names, where a substring check is the correct shape."""


def _audit_offenders(field_names: object) -> set[str]:
    """Field names that could hold something Req 20.3 forbids."""
    assert isinstance(field_names, (set, frozenset, dict, list, tuple))
    offenders: set[str] = set()
    for name in field_names:
        tokens = set(str(name).split("_"))
        if tokens & _FORBIDDEN_TOKENS:
            offenders.add(str(name))
        if any(phrase in str(name) for phrase in _FORBIDDEN_PHRASES):
            offenders.add(str(name))
    return offenders


def test_the_audit_record_has_nowhere_to_put_health_adjacent_content() -> None:
    assert _audit_offenders(set(AdviceRecord.model_fields)) == set()


def test_the_audit_record_carries_exactly_the_contracted_fields() -> None:
    # Pinned so a future field cannot be added without this test being read and changed — which
    # is
    # where someone has to notice it against Req 20.3.
    assert set(AdviceRecord.model_fields) == {
        "user_id",
        "turn_at",
        "route",
        "escalated",
        "threshold_crossed",
        "driving_pollutant",
        "record_references",
        "guardrail_rejected",
        "rejection_category",
        "idempotency_key",
    }


def test_the_forbidden_name_sweep_would_catch_a_planted_field() -> None:
    # Non-vacuity. Without this, a sweep whose token list never matched anything would pass on a
    # record that DID carry a condition, and report safety it was not providing.
    from pydantic import BaseModel

    class Planted(BaseModel):
        user_id: str
        condition: str
        home_latitude: float
        personal_threshold: int

    assert _audit_offenders(set(Planted.model_fields)) == {
        "condition",
        "home_latitude",
        "personal_threshold",
    }


def test_the_sweep_does_not_fire_on_a_field_that_merely_contains_a_marker() -> None:
    # The near-miss guard, and the reason the sweep matches TOKENS. `escalated` contains "lat"
    # and
    # `route` contains no marker; both are required fields, and a substring sweep would condemn
    # the
    # first. Service 2 learned this the same way — a sweep that fires on correct code gets
    # "fixed" by
    # deleting the correct code.
    from pydantic import BaseModel

    class Innocent(BaseModel):
        escalated: bool
        route: str
        threshold_crossed: bool
        driving_pollutant: str

    assert _audit_offenders(set(Innocent.model_fields)) == set()


def test_the_threshold_is_recorded_as_a_boolean_not_a_value() -> None:
    # Req 20.2 records THAT a threshold was crossed; Req 20.3 forbids recording the threshold
    # itself,
    # which is a personal health parameter. The type is what enforces the difference.
    annotation = AdviceRecord.model_fields["threshold_crossed"].annotation
    assert annotation is bool


def test_the_rejection_category_is_a_category_not_the_rejected_text() -> None:
    # Req 8.6: a rejection names the category and never the offending text.
    record = _record(guardrail_rejected=True, rejection_category="dosing")
    assert record.rejection_category == "dosing"


def test_a_naive_turn_instant_is_refused() -> None:
    with pytest.raises(ValidationError):
        _record(turn_at=dt.datetime(2026, 7, 1, 12))


def test_the_audit_record_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        _record(utterance="my chest was tight")


# --- Req 32.4c: idempotency --------------------------------------------
#
# MOVED to tests/unit/test_idempotency.py, which owns the key derivation now that both keyed
# writes share one. The tests that lived here also tested a property that NO LONGER EXISTS: that
# the key is stable across the instant's representation. It is stable across it because the
# instant is not key material at all — a strictly stronger position than normalising it, since a
# re-invoked entrypoint reads a later clock and no amount of normalising fixes that.


# --- Req 28: the symptom draft -----------------------------------------

def test_a_draft_is_unconfirmed_by_default() -> None:
    # Req 28.2/28.3: inferring a severity from prose is a judgement about the user's health, and
    # the
    # user is the authority on it. The safe default is therefore "not yet agreed".
    draft = SymptomEntryDraft(
        date=dt.date(2026, 7, 1), severity=3, markers=("wheeze",), reliever_used=True
    )
    assert draft.confirmed is False


def test_a_draft_severity_is_bounded() -> None:
    for severity in (0, 6, -1):
        with pytest.raises(ValidationError):
            SymptomEntryDraft(
                date=dt.date(2026, 7, 1),
                severity=severity,
                markers=(),
                reliever_used=False,
            )


def test_a_draft_note_is_length_bounded() -> None:
    with pytest.raises(ValidationError):
        SymptomEntryDraft(
            date=dt.date(2026, 7, 1),
            severity=3,
            markers=(),
            reliever_used=False,
            note="x" * 281,
        )


def test_a_confirmed_draft_can_be_produced_only_by_confirming_it() -> None:
    draft = SymptomEntryDraft(
        date=dt.date(2026, 7, 1), severity=3, markers=("wheeze",), reliever_used=True
    )
    confirmed = draft.confirm()
    assert confirmed.confirmed is True
    assert draft.confirmed is False, "confirming must not mutate the draft the user reviewed"


def test_a_correction_replaces_the_inferred_reading_and_stays_unconfirmed() -> None:
    # Req 28.2: apply the USER'S correction rather than its own reading. A correction must also
    # not
    # arrive pre-confirmed, or the user's fix would be written without their agreement to the
    # fix.
    draft = SymptomEntryDraft(
        date=dt.date(2026, 7, 1), severity=4, markers=("wheeze",), reliever_used=True
    )
    corrected = draft.correct(severity=2, markers=("cough",))
    assert corrected.severity == 2
    assert corrected.markers == ("cough",)
    assert corrected.confirmed is False


# --- the grounding set --------------------------------------------------

def test_the_tool_call_trajectory_is_ordered() -> None:
    # Req 35.4 asserts WHICH tools were called and in what order, so the container must preserve
    # it.
    values = RetrievedValues(
        numerals=frozenset({"68"}),
        medications=frozenset({"salbutamol"}),
        pollen_categories=frozenset(),
        tool_calls=(ToolCall(name="air_quality"), ToolCall(name="profile_get")),
    )
    assert [call.name for call in values.tool_calls] == ["air_quality", "profile_get"]


def test_a_repeated_tool_call_is_preserved_rather_than_deduplicated() -> None:
    # A trajectory is a sequence of events, not a set of names. Collapsing a repeat would hide a
    # retry loop, which is exactly what a trajectory assertion is meant to reveal.
    values = RetrievedValues(
        numerals=frozenset(),
        medications=frozenset(),
        pollen_categories=frozenset(),
        tool_calls=(ToolCall(name="air_quality"), ToolCall(name="air_quality")),
    )
    assert len(values.tool_calls) == 2


def test_the_permitted_sets_are_sets_because_membership_is_the_question() -> None:
    # Grounding asks "was this numeral served?", which is membership. Using a set here is not
    # laziness:
    # it makes the order of retrieved values irrelevant to the check, so grounding cannot depend
    # on it.
    values = RetrievedValues(
        numerals=frozenset({"68", "18.2"}),
        medications=frozenset(),
        pollen_categories=frozenset(),
        tool_calls=(),
    )
    assert "68" in values.numerals
    assert isinstance(values.numerals, frozenset)
