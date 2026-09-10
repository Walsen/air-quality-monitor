"""Tests for the reviewable basis (task 3.2).

The identifier tests are the load-bearing ones. `RecordReference.identifier()` is what an
Advice_Record
stores for Req 20.2, and a collapse there fails in the worst way available: the trail keeps a
plausible
number of references, each one valid, while silently under-reporting which Readings a claim
actually rested
on. So two Readings differing only in species, and two differing only in instant, are each
asserted to
produce DIFFERENT identifiers — the two collapses a bare site code would cause.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from aqm_advisor.domain.models import (
    BasisSummary,
    Escalation,
    GuardrailEnvelope,
    NowcastBasis,
    RecordReference,
    SpeciesBasis,
)

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _reference(**kwargs: object) -> RecordReference:
    defaults: dict[str, object] = {
        "site_code": "AQM1",
        "species": "PM25",
        "date_time": _AT,
        "duration": "PT1H",
    }
    return RecordReference(**{**defaults, **kwargs})  # type: ignore[arg-type]


def _basis(**kwargs: object) -> BasisSummary:
    defaults: dict[str, object] = {
        "driving_pollutant": "PM25",
        "site_code": "AQM1",
        "distance_km": 1.2,
        "as_of": _AT,
        "per_species": (
            SpeciesBasis(species="PM25", sub_index=68, band="Moderate", confidence="high"),
        ),
        "threshold": None,
        "threshold_source": None,
        "breakpoint_table": "epa-2024-05-06",
        "calibration_strategies": {"PM25": "rh_linear"},
        "nowcast": NowcastBasis(window_hours=12, hours_available=12, weight_factor=0.72),
        "records": (_reference(),),
    }
    return BasisSummary(**{**defaults, **kwargs})  # type: ignore[arg-type]


# --- Req 20.2: the identifier distinguishes what it must ----------------

def test_two_readings_differing_only_in_species_get_different_identifiers() -> None:
    # ONE sensor reports several species. A site-code-only identifier would fold them into one.
    pm = _reference(species="PM25").identifier()
    no2 = _reference(species="NO2").identifier()
    assert pm != no2


def test_two_readings_differing_only_in_instant_get_different_identifiers() -> None:
    # The SAME sensor and species report at successive instants. Folding those together would
    # make an
    # hour-old reading indistinguishable from the current one in the audit trail.
    earlier = _reference(date_time=_AT).identifier()
    later = _reference(date_time=_AT + dt.timedelta(hours=1)).identifier()
    assert earlier != later


def test_two_readings_differing_only_in_duration_get_different_identifiers() -> None:
    # A 1-hour mean and a 24-hour mean at the same instant are different Readings with different
    # meanings, and Service 2 serves both.
    assert _reference(duration="PT1H").identifier() != _reference(duration="P1D").identifier()


def test_two_readings_differing_only_in_site_get_different_identifiers() -> None:
    one = _reference(site_code="AQM1").identifier()
    two = _reference(site_code="AQM2").identifier()
    assert one != two


def test_an_identifier_is_stable_across_equal_references() -> None:
    # It is an IDENTIFIER: the same Reading must always name itself the same way, or an audit
    # trail
    # could not be compared against itself.
    assert _reference().identifier() == _reference().identifier()


def test_an_identifier_is_unchanged_by_the_instants_representation() -> None:
    # The same instant expressed in another offset, and with a sub-second component, is the same
    # Reading. If the identifier disagreed, one Reading would appear twice in the trail.
    offset = dt.timezone(dt.timedelta(hours=2))
    same_instant = _AT.astimezone(offset).replace(microsecond=987654)
    assert _reference(date_time=same_instant).identifier() == _reference().identifier()


def test_an_identifier_carries_nothing_health_adjacent() -> None:
    # Req 20.3: the audit trail holds no health-adjacent content. All four parts are public
    # sensor
    # facts, which is what makes storing the composite permissible at all.
    identifier = _reference().identifier()
    assert identifier == "AQM1:PM25:2026-07-01T12:00:00Z:PT1H"


def test_the_basis_derives_its_identifiers_from_its_own_records() -> None:
    basis = _basis(records=(_reference(species="PM25"), _reference(species="NO2")))
    assert basis.record_identifiers() == (
        "AQM1:PM25:2026-07-01T12:00:00Z:PT1H",
        "AQM1:NO2:2026-07-01T12:00:00Z:PT1H",
    )


def test_the_basis_preserves_record_order() -> None:
    first, second = _reference(site_code="AQM1"), _reference(site_code="AQM2")
    assert _basis(records=(second, first)).record_identifiers()[0].startswith("AQM2")


# --- Req 9.3: the nowcast survives unchanged ----------------------------

def test_a_partial_window_survives_into_the_basis_unchanged() -> None:
    # Req 9.4 forbids re-deriving any part of the basis, so a short window must be carried as
    # served —
    # not normalised to a full window, not dropped, not rounded.
    basis = _basis(nowcast=NowcastBasis(window_hours=12, hours_available=8, weight_factor=0.5))
    assert basis.nowcast is not None
    assert basis.nowcast.window_hours == 12
    assert basis.nowcast.hours_available == 8
    assert basis.nowcast.weight_factor == 0.5


def test_a_partial_window_reports_an_incomplete_window() -> None:
    basis = _basis(nowcast=NowcastBasis(window_hours=12, hours_available=8, weight_factor=0.5))
    assert basis.nowcast_window_is_complete is False


def test_a_full_window_reports_a_complete_window() -> None:
    assert _basis().nowcast_window_is_complete is True


# --- Req 9.3a: an absent nowcast is a complete answer -------------------

def test_an_absent_nowcast_reports_neither_complete_nor_incomplete() -> None:
    # Req 9.3a: `None` means the index was NOT nowcast-derived, which is an ordinary answer.
    # Returning
    # True would claim a completeness never measured; returning False would invent a weakness.
    assert _basis(nowcast=None).nowcast_window_is_complete is None


def test_an_absent_nowcast_is_distinguishable_from_a_zero_hour_window() -> None:
    # The two states Service 2 keeps apart: not_applicable (no window species at all, e.g. NO2)
    # and
    # insufficient (a window with too few hours). Collapsing them would invent a warning for NO2
    # or
    # hide one for a starved PM2.5 window.
    absent = _basis(nowcast=None)
    starved = _basis(nowcast=NowcastBasis(window_hours=12, hours_available=0,
    weight_factor=0.0))
    assert absent.nowcast_window_is_complete is None
    assert starved.nowcast_window_is_complete is False


def test_a_basis_without_a_nowcast_is_still_a_valid_basis() -> None:
    # Req 9.3a calls it a complete answer, so construction must not require the nowcast.
    basis = _basis(nowcast=None)
    assert basis.driving_pollutant == "PM25"
    assert basis.records != ()


# --- Req 9.4: copied, never computed ------------------------------------

def test_no_basis_field_has_a_default_that_could_invent_a_value() -> None:
    # Req 9.4 forbids recomputing or re-deriving any part of the basis. A default is the
    # quietest way
    # to re-derive one: an omitted field would arrive as a plausible value nobody served.
    for name, field in BasisSummary.model_fields.items():
        assert field.is_required(), f"{name} has a default and could invent a value"


def test_every_nowcast_field_is_required() -> None:
    for name, field in NowcastBasis.model_fields.items():
        assert field.is_required(), f"{name} has a default"


def test_every_record_reference_field_is_required() -> None:
    for name, field in RecordReference.model_fields.items():
        assert field.is_required(), f"{name} has a default"


def test_the_envelope_is_carried_whole_and_has_no_defaults() -> None:
    # Req 8.5 requires the envelope on every response. A default would let this service invent a
    # disclaimer that is Service 2's responsibility to word.
    for name, field in GuardrailEnvelope.model_fields.items():
        assert field.is_required(), f"{name} has a default"


def test_a_naive_basis_instant_is_refused() -> None:
    with pytest.raises(ValidationError):
        _basis(as_of=dt.datetime(2026, 7, 1, 12))


def test_a_naive_record_instant_is_refused() -> None:
    with pytest.raises(ValidationError):
        _reference(date_time=dt.datetime(2026, 7, 1, 12))


def test_the_basis_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError):
        _basis(overallAqi=68)


# --- escalation ---------------------------------------------------------

def test_an_escalation_must_name_a_marker() -> None:
    # An escalation with no marker could not be audited or explained, and would be
    # indistinguishable
    # from a spurious one.
    with pytest.raises(ValidationError, match="marker"):
        Escalation(kind="emergency", markers=(), guidance="Seek care now.")


def test_an_escalation_records_which_markers_matched() -> None:
    escalation = Escalation(
        kind="emergency",
        markers=("blue_lips", "reliever_ineffective"),
        guidance="Seek care now.",
    )
    assert escalation.markers == ("blue_lips", "reliever_ineffective")


def test_an_escalation_kind_is_constrained() -> None:
    with pytest.raises(ValidationError):
        Escalation(kind="mild", markers=("x",), guidance="g")  # type: ignore[arg-type]
