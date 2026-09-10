"""Unit tests for the per-routine-window Inhaled_Dose (Requirements 23.1a, 23.1b).

The criterion exists to remove a specific error. A single whole-day activity figure attributes a
morning run's breathing rate to all twenty-four hours, so a user who runs for one vigorous
hour and rests for the rest of the day gets a dose computed as though they ran all day.
Req 23.1a computes
each routine window against its OWN activity level and duration, and against the concentration
measured IN that window.

Req 23.1b then requires saying WHICH basis produced the number, because a per-window sum and a
whole-day figure are both "the dose" and reporting either without its basis makes two different
numbers indistinguishable.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_ingestion.domain.dose import (
    DEFAULT_BREATHING_RATES,
    DOSE_UNIT,
    DoseBasis,
    compute_routine_doses,
    resolve_dose_basis,
)
from aqm_ingestion.domain.profile import (
    RECOGNIZED_CONSENT_VERSIONS,
    ActivityLevel,
    Condition,
    SensitivityLevel,
    Weekday,
    build_profile,
)

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _profile_fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "user_id": "user-1",
        "condition": Condition.ASTHMA,
        "sensitivity_level": SensitivityLevel.STANDARD,
        "consent": {
            "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
            "given_at": _NOW,
        },
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return base | overrides


def _routine(**overrides: object) -> dict[str, object]:
    return {
        "days": ["monday"],
        "start_time": dt.time(7, 0),
        "duration_hours": 1.0,
        "activity_level": "vigorous",
    } | overrides


# --- Req 23.1b: which basis produced the number -------------------------

def test_the_three_bases_are_distinguishable() -> None:
    assert {basis.value for basis in DoseBasis} == {
        "routine_windows",
        "whole_day_activity",
        "none",
    }


def test_routines_win_over_the_whole_day_inputs() -> None:
    # Req 23.1b: WHERE both are present, the routine records are used.
    profile = build_profile(
        _profile_fields(
            routines=[_routine()],
            activity_level=ActivityLevel.REST,
            activity_duration_hours=24.0,
        )
    )
    assert resolve_dose_basis(profile) is DoseBasis.ROUTINE_WINDOWS


def test_the_whole_day_inputs_are_used_when_there_are_no_routines() -> None:
    profile = build_profile(
        _profile_fields(activity_level=ActivityLevel.MODERATE, activity_duration_hours=2.0)
    )
    assert resolve_dose_basis(profile) is DoseBasis.WHOLE_DAY_ACTIVITY


def test_no_basis_when_neither_is_present() -> None:
    assert resolve_dose_basis(build_profile(_profile_fields())) is DoseBasis.NONE


def test_no_basis_without_a_profile() -> None:
    assert resolve_dose_basis(None) is DoseBasis.NONE


def test_half_the_whole_day_inputs_is_not_a_basis() -> None:
    # Req 23.3 forbids assuming either input, so a level without a duration is no basis at all
    # rather than a basis with a defaulted duration.
    profile = build_profile(_profile_fields(activity_level=ActivityLevel.MODERATE))
    assert resolve_dose_basis(profile) is DoseBasis.NONE


# --- Req 23.1a: a dose per window, against that window's concentration ---

def test_each_window_uses_its_own_level_and_duration() -> None:
    profile = build_profile(
        _profile_fields(
            routines=[
                _routine(
                    start_time=dt.time(7, 0), duration_hours=1.0, activity_level="vigorous"
                ),
                _routine(start_time=dt.time(12, 0), duration_hours=2.0, activity_level="rest"),
            ]
        )
    )
    report = compute_routine_doses(
        profile.routines,
        concentration_for_window=lambda _entry: (20.0, "high"),
        rates=DEFAULT_BREATHING_RATES,
        day=Weekday.MONDAY,
    )
    assert [d.duration_hours for d in report.per_window] == [1.0, 2.0]
    assert [d.breathing_rate_m3_per_h for d in report.per_window] == [3.2, 0.5]
    # 20 * 3.2 * 1 = 64; 20 * 0.5 * 2 = 20
    assert [d.micrograms for d in report.per_window] == [64.0, 20.0]
    assert report.unit == DOSE_UNIT


def test_the_report_sums_the_windows() -> None:
    profile = build_profile(
        _profile_fields(
            routines=[
                _routine(duration_hours=1.0, activity_level="vigorous"),
                _routine(start_time=dt.time(18, 0), duration_hours=1.0, activity_level="light"),
            ]
        )
    )
    report = compute_routine_doses(
        profile.routines,
        concentration_for_window=lambda _entry: (10.0, "high"),
        rates=DEFAULT_BREATHING_RATES,
        day=Weekday.MONDAY,
    )
    assert report.total_micrograms == pytest.approx(10.0 * 3.2 + 10.0 * 1.0)
    windows_sum = sum(d.micrograms for d in report.per_window)
    assert report.total_micrograms == pytest.approx(windows_sum)


def test_each_window_is_priced_at_its_own_concentration() -> None:
    # The other half of Req 23.1a: "against the concentration measured in that window". A single
    # daily average would defeat the point as thoroughly as a single activity level does.
    by_hour = {7: 100.0, 18: 10.0}
    profile = build_profile(
        _profile_fields(
            routines=[
                _routine(start_time=dt.time(7, 0), duration_hours=1.0, activity_level="rest"),
                _routine(start_time=dt.time(18, 0), duration_hours=1.0, activity_level="rest"),
            ]
        )
    )
    report = compute_routine_doses(
        profile.routines,
        concentration_for_window=lambda entry: (by_hour[entry.start_time.hour], "high"),
        rates=DEFAULT_BREATHING_RATES,
        day=Weekday.MONDAY,
    )
    assert [d.concentration_ug_m3 for d in report.per_window] == [100.0, 10.0]
    assert [d.micrograms for d in report.per_window] == [50.0, 5.0]


def test_only_windows_falling_on_the_reported_day_are_counted() -> None:
    profile = build_profile(
        _profile_fields(
            routines=[
                _routine(days=["monday"], start_time=dt.time(7, 0)),
                _routine(days=["saturday"], start_time=dt.time(9, 0)),
            ]
        )
    )
    report = compute_routine_doses(
        profile.routines,
        concentration_for_window=lambda _entry: (20.0, "high"),
        rates=DEFAULT_BREATHING_RATES,
        day=Weekday.MONDAY,
    )
    assert len(report.per_window) == 1
    assert report.per_window[0].duration_hours == 1.0


def test_a_day_with_no_routine_yields_an_empty_report_not_a_zero_dose() -> None:
    # Zero would claim the user breathed nothing, which is a different statement from "this day
    # has no recorded activity window".
    profile = build_profile(_profile_fields(routines=[_routine(days=["monday"])]))
    report = compute_routine_doses(
        profile.routines,
        concentration_for_window=lambda _entry: (20.0, "high"),
        rates=DEFAULT_BREATHING_RATES,
        day=Weekday.SUNDAY,
    )
    assert report.per_window == ()
    assert report.total_micrograms is None


def test_a_window_whose_concentration_is_unavailable_is_reported_not_guessed() -> None:
    # Req 23.3's reasoning carried into the per-window case: an assumed concentration is not a
    # measured one, so the window is reported as unavailable rather than priced at a guess.
    profile = build_profile(
        _profile_fields(
            routines=[
                _routine(start_time=dt.time(7, 0)),
                _routine(start_time=dt.time(18, 0)),
            ]
        )
    )
    report = compute_routine_doses(
        profile.routines,
        concentration_for_window=lambda entry: (
            (None, None) if entry.start_time.hour == 18 else (20.0, "high")
        ),
        rates=DEFAULT_BREATHING_RATES,
        day=Weekday.MONDAY,
    )
    assert len(report.per_window) == 1
    assert report.unavailable_windows == 1


def test_the_total_is_none_when_every_window_is_unavailable() -> None:
    profile = build_profile(_profile_fields(routines=[_routine()]))
    report = compute_routine_doses(
        profile.routines,
        concentration_for_window=lambda _entry: (None, None),
        rates=DEFAULT_BREATHING_RATES,
        day=Weekday.MONDAY,
    )
    assert report.total_micrograms is None
    assert report.unavailable_windows == 1


def test_the_windows_are_reported_in_the_stored_order() -> None:
    # Req 30.8 already ordered the stored routines by weekday then start time, so the per-window
    # report inherits a defined order rather than establishing a second one.
    profile = build_profile(
        _profile_fields(
            routines=[
                _routine(start_time=dt.time(19, 0)),
                _routine(start_time=dt.time(6, 0)),
                _routine(start_time=dt.time(12, 0)),
            ]
        )
    )
    report = compute_routine_doses(
        profile.routines,
        concentration_for_window=lambda _entry: (20.0, "high"),
        rates=DEFAULT_BREATHING_RATES,
        day=Weekday.MONDAY,
    )
    assert [d.window_start.hour for d in report.per_window] == [6, 12, 19]


def test_a_per_window_dose_carries_no_clinical_interpretation() -> None:
    # Req 23.8, inherited: there must be nowhere to put a band, severity, risk or advice.
    from aqm_ingestion.domain.dose import RoutineWindowDose

    fields = set(RoutineWindowDose.__dataclass_fields__)
    for forbidden in ("band", "severity", "risk", "advice", "interpretation"):
        assert forbidden not in fields
