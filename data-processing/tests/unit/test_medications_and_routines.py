"""Unit tests for the Medication_Entry and Routine_Entry profile fields (Requirement 30).

The governing idea is that medication safety here is STRUCTURAL, not a validation rule. Req 30.3
forbids storing a dose, frequency, route, prescriber or administration schedule — and the way it
is satisfied is that the stored shape has nowhere to put one. So the load-bearing test is not
"a dose is rejected" but "there is no field a dose could go in", which is what
``test_the_medication_shape_has_nowhere_to_put_a_dose`` asserts over the model's own field set.

- 30.1: exactly two fields, and an extra one is rejected naming the field, never echoing it.
- 30.2: the role is `reliever`, `preventer` or `other` — the only clinical property the
  guidance needs.
- 30.3: no dose, frequency, route, prescriber or schedule EXISTS in the shape.
- 30.4/30.6: configured caps, default 10 medications and 14 routines, rejected naming the limit.
- 30.5: a routine is days + start time + duration + activity level + optional location.
- 30.7: a duration must be positive and must not run past the end of its start day.
- 30.8: routines are ordered by day of week then start time, so a report is reproducible.
- 30.9: both are profile fields under Req 17.9 — nothing reaches a log or an error message.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from aqm_ingestion.domain.profile import (
    DEFAULT_MEDICATION_LIMIT,
    DEFAULT_ROUTINE_LIMIT,
    WEEKDAY_ORDER,
    ActivityLevel,
    Condition,
    LocationName,
    MedicationEntry,
    MedicationRole,
    ProfileLimits,
    RoutineEntry,
    SensitivityLevel,
    Weekday,
    build_profile,
)

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_CONSENT = {"version": "2026-01-01", "given_at": _NOW}


def _fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "user_id": "user-123",
        "condition": Condition.ASTHMA,
        "sensitivity_level": SensitivityLevel.ELEVATED,
        "consent": _CONSENT,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return base | overrides


def _medication(**overrides: object) -> dict[str, object]:
    return {"name": "salbutamol", "role": "reliever"} | overrides


def _routine(**overrides: object) -> dict[str, object]:
    return {
        "days": ["monday", "wednesday"],
        "start_time": dt.time(7, 0),
        "duration_hours": 1.0,
        "activity_level": "vigorous",
        "location": "home",
    } | overrides


# --- Req 30.1 / 30.3: the shape is the safety mechanism -----------------

def test_the_medication_shape_is_exactly_two_fields() -> None:
    assert set(MedicationEntry.model_fields) == {"name", "role"}


@pytest.mark.parametrize(
    "forbidden",
    ["dose", "dosage", "frequency", "route", "prescriber", "schedule", "strength", "notes"],
)
def test_the_medication_shape_has_nowhere_to_put_a_dose(forbidden: str) -> None:
    # Req 30.3 is a STRUCTURAL prohibition. This is the test that states it as one: the field
    # does not exist, so dosing advice has no stored input to draw on. A validation-only reading
    # of 30.3 would let the field exist and merely reject bad values.
    assert forbidden not in MedicationEntry.model_fields


def test_an_extra_medication_field_is_rejected_naming_it_without_echoing_the_value() -> None:
    with pytest.raises(ValidationError) as caught:
        build_profile(_fields(medications=[_medication(dose="two puffs twice daily")]))
    rendered = str(caught.value)
    assert "dose" in rendered
    assert "two puffs" not in rendered, "a rejected value must not travel in the rejection"


# --- Req 30.2: the role enumeration -------------------------------------

def test_the_three_roles_are_exactly_the_documented_ones() -> None:
    assert {role.value for role in MedicationRole} == {"reliever", "preventer", "other"}


def test_an_unrecognized_role_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(_fields(medications=[_medication(role="rescue")]))


# --- Req 30.5: the routine shape ----------------------------------------

def test_the_routine_shape_is_exactly_the_documented_field_set() -> None:
    assert set(RoutineEntry.model_fields) == {
        "days",
        "start_time",
        "duration_hours",
        "activity_level",
        "location",
    }


def test_a_routine_round_trips_its_values() -> None:
    profile = build_profile(_fields(routines=[_routine()]))
    (entry,) = profile.routines
    assert entry.days == (Weekday.MONDAY, Weekday.WEDNESDAY)
    assert entry.start_time == dt.time(7, 0)
    assert entry.duration_hours == 1.0
    assert entry.activity_level is ActivityLevel.VIGOROUS
    assert entry.location is LocationName.HOME


def test_the_routine_location_is_optional() -> None:
    profile = build_profile(_fields(routines=[_routine(location=None)]))
    assert profile.routines[0].location is None


def test_a_routine_location_outside_the_permitted_set_is_rejected() -> None:
    # Req 30.5 binds the location name to Req 17.5's set, not to free text.
    with pytest.raises(ValidationError):
        build_profile(_fields(routines=[_routine(location="gym")]))


def test_a_routine_with_no_days_is_rejected() -> None:
    # A routine on no day never happens, so storing one records an event that does not occur —
    # the same reasoning Req 23.6 applies to a zero duration.
    with pytest.raises(ValidationError):
        build_profile(_fields(routines=[_routine(days=[])]))


def test_a_repeated_day_collapses_rather_than_double_counting() -> None:
    # Two "monday"s in one routine would make a per-window dose report count Monday twice.
    profile = build_profile(_fields(routines=[_routine(days=["monday", "monday"])]))
    assert profile.routines[0].days == (Weekday.MONDAY,)


def test_a_naive_start_time_is_accepted_and_an_offset_one_is_not() -> None:
    # A routine happens at seven in the morning, which is a wall-clock time and not an instant.
    # So unlike every datetime in this codebase, a tz-aware value here is the WRONG shape.
    ok = build_profile(_fields(routines=[_routine(start_time=dt.time(7, 0))]))
    assert ok.routines[0].start_time.tzinfo is None
    with pytest.raises(ValidationError):
        build_profile(
            _fields(routines=[_routine(start_time=dt.time(7, 0, tzinfo=dt.UTC))])
        )


# --- Req 30.7: the duration bounds --------------------------------------

@pytest.mark.parametrize("duration", [0.0, -1.0])
def test_a_non_positive_routine_duration_is_rejected(duration: float) -> None:
    with pytest.raises(ValidationError) as caught:
        build_profile(_fields(routines=[_routine(duration_hours=duration)]))
    assert "duration_hours" in str(caught.value)


def test_a_routine_running_past_the_end_of_its_day_is_rejected() -> None:
    with pytest.raises(ValidationError) as caught:
        build_profile(
            _fields(routines=[_routine(start_time=dt.time(23, 0), duration_hours=2.0)])
        )
    rendered = str(caught.value)
    assert "duration_hours" in rendered
    assert "24" in rendered, "the message must name the permitted range (§5)"


def test_the_rejection_does_not_disclose_the_start_time() -> None:
    # Req 30.7 asks for "the permitted range", and the range REMAINING for the duration is
    # derived from the start time — so a message saying "at most 1 hour" satisfies 30.7 while
    # disclosing a profile field, which Req 30.9 forbids. A range derived from a value is still
    # the value. The first implementation of this check leaked exactly that way.
    with pytest.raises(ValidationError) as caught:
        build_profile(
            _fields(routines=[_routine(start_time=dt.time(23, 17), duration_hours=5.0)])
        )
    rendered = str(caught.value)
    for leak in ("23", "17", "0.71", "0.72"):
        assert leak not in rendered, f"the start time leaked into the rejection via {leak!r}"


def test_a_routine_ending_exactly_at_midnight_is_accepted() -> None:
    # The boundary Req 30.7 permits: "past the end of its start day" excludes landing ON it.
    profile = build_profile(
        _fields(routines=[_routine(start_time=dt.time(22, 0), duration_hours=2.0)])
    )
    assert profile.routines[0].duration_hours == 2.0


# --- Req 30.8: deterministic ordering -----------------------------------

def test_routines_are_ordered_by_day_of_week_then_start_time() -> None:
    submitted = [
        _routine(days=["friday"], start_time=dt.time(18, 0)),
        _routine(days=["monday"], start_time=dt.time(19, 0)),
        _routine(days=["monday"], start_time=dt.time(7, 0)),
    ]
    profile = build_profile(_fields(routines=submitted))
    assert [(e.days, e.start_time) for e in profile.routines] == [
        ((Weekday.MONDAY,), dt.time(7, 0)),
        ((Weekday.MONDAY,), dt.time(19, 0)),
        ((Weekday.FRIDAY,), dt.time(18, 0)),
    ]


def test_the_ordering_is_the_same_whatever_order_the_write_arrived_in() -> None:
    entries = [
        _routine(days=["sunday"], start_time=dt.time(9, 0)),
        _routine(days=["tuesday"], start_time=dt.time(6, 30)),
        _routine(days=["tuesday"], start_time=dt.time(6, 0)),
    ]
    forward = build_profile(_fields(routines=list(entries))).routines
    backward = build_profile(_fields(routines=list(reversed(entries)))).routines
    assert forward == backward


def test_the_weekday_order_is_monday_first_and_complete() -> None:
    # The sort key depends on this being a total order over every weekday; a missing member
    # would make one day's routines unsortable.
    assert len(WEEKDAY_ORDER) == 7
    assert set(WEEKDAY_ORDER) == set(Weekday)
    assert WEEKDAY_ORDER[0] is Weekday.MONDAY


def test_the_days_within_one_routine_are_ordered_too() -> None:
    profile = build_profile(_fields(routines=[_routine(days=["sunday", "monday"])]))
    assert profile.routines[0].days == (Weekday.MONDAY, Weekday.SUNDAY)


def test_medications_are_ordered_so_a_response_is_reproducible() -> None:
    # Practice §2 rather than Req 30.8, which names only routines: any iteration reaching output
    # needs a defined order, and the medication list reaches the advisor.
    entries = [_medication(name="beclometasone"), _medication(name="salbutamol")]
    forward = build_profile(_fields(medications=list(entries))).medications
    backward = build_profile(_fields(medications=list(reversed(entries)))).medications
    assert forward == backward
    assert [m.name for m in forward] == ["beclometasone", "salbutamol"]


# --- Req 30.4 / 30.6: the configured caps -------------------------------

def test_the_default_caps_are_the_documented_ones() -> None:
    assert DEFAULT_MEDICATION_LIMIT == 10
    assert DEFAULT_ROUTINE_LIMIT == 14


def test_too_many_medications_is_rejected_naming_the_limit() -> None:
    over = [_medication(name=f"drug-{n}") for n in range(DEFAULT_MEDICATION_LIMIT + 1)]
    with pytest.raises(ValidationError) as caught:
        build_profile(_fields(medications=over))
    assert str(DEFAULT_MEDICATION_LIMIT) in str(caught.value)


def test_too_many_routines_is_rejected_naming_the_limit() -> None:
    over = [_routine() for _ in range(DEFAULT_ROUTINE_LIMIT + 1)]
    with pytest.raises(ValidationError) as caught:
        build_profile(_fields(routines=over))
    assert str(DEFAULT_ROUTINE_LIMIT) in str(caught.value)


def test_the_caps_are_configuration_not_constants() -> None:
    limits = ProfileLimits(medication_limit=1, routine_limit=1)
    build_profile(_fields(medications=[_medication()]), limits)
    with pytest.raises(ValidationError):
        build_profile(
            _fields(medications=[_medication(name="a"), _medication(name="b")]), limits
        )


def test_exactly_the_limit_is_accepted() -> None:
    # Req 30.4 says "at most", so the boundary value is permitted; a strict comparison here
    # would reject the last entry a user is entitled to store.
    at_limit = [_medication(name=f"drug-{n}") for n in range(DEFAULT_MEDICATION_LIMIT)]
    profile = build_profile(_fields(medications=at_limit))
    assert len(profile.medications) == DEFAULT_MEDICATION_LIMIT


# --- Req 30.9: still opaque ---------------------------------------------

def test_the_profile_still_reveals_only_its_identity_with_medications_present() -> None:
    profile = build_profile(
        _fields(medications=[_medication(name="salbutamol")], routines=[_routine()])
    )
    rendered = f"{profile!r} {profile}"
    assert "salbutamol" not in rendered
    assert "monday" not in rendered
    assert "user-123" in rendered


def test_both_fields_default_to_empty_so_an_existing_profile_stays_valid() -> None:
    profile = build_profile(_fields())
    assert profile.medications == ()
    assert profile.routines == ()
