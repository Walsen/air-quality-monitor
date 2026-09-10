"""Unit tests for deduplication (task 6.1).

Requirement 7's five resolution rules, as a pure function of two candidates:

- 7.2 absent key         -> store
- 7.3 all fields equal   -> no write, counted as a duplicate, success to the caller
- 7.4 incoming R, stored P -> replace (ratified supersedes provisional)
- 7.5 incoming P, stored R -> retain, one warning
- 7.6 equal status, differing value -> keep the GREATER, flag suspect_conflict, warn

The subtle one is 7.4 interacting with 7.6: a ratified replacement must CLEAR a prior
suspect_conflict flag. If it preserved the flag, the outcome would depend on whether
the conflicting provisional pair arrived before or after the ratified record, breaking
Requirement 7.7's order independence. There is a test for exactly that below.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.domain.dedup import (
    DedupAction,
    dedup_key_for,
    resolve_duplicate,
)
from aqm_ingestion.domain.models import QualityFlag
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

_MOMENT = "2026-07-01T11:00:00Z"


def _record(**overrides: object) -> SensorDataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | overrides
    fields.setdefault("DateTime", _MOMENT)
    return SensorDataRecord(**fields)


# --- Req 7.1 the key -----------------------------------------------------

def test_key_is_built_from_the_four_documented_fields() -> None:
    key = dedup_key_for(_record(SiteCode="CB0001", Species="PM25", Duration="PT1H"))
    assert key.site_code == "CB0001"
    assert key.species == "PM25"
    assert key.duration == "PT1H"
    assert key.interval_start == dt.datetime(2026, 7, 1, 11, tzinfo=dt.UTC)


def test_records_differing_only_in_value_share_a_key() -> None:
    # this is what makes them duplicate CANDIDATES rather than distinct measurements
    assert dedup_key_for(_record(ScaledValue=1.0)) == dedup_key_for(
        _record(ScaledValue=2.0)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SiteCode", "CB0002"),
        ("Species", "NO2"),
        ("Duration", "PT15M"),
        ("DateTime", "2026-07-01T12:00:00Z"),
    ],
)
def test_each_key_field_distinguishes_a_measurement(field: str, value: str) -> None:
    assert dedup_key_for(_record()) != dedup_key_for(_record(**{field: value}))


def test_key_is_hashable_so_it_can_index_a_store() -> None:
    assert len({dedup_key_for(_record()), dedup_key_for(_record())}) == 1


# --- Req 7.2 absent ------------------------------------------------------

def test_absent_key_is_stored() -> None:
    resolution = resolve_duplicate(stored=None, incoming=_record())
    assert resolution.action is DedupAction.STORE
    assert resolution.winner == _record()
    assert resolution.quality_flag is None  # no conflict to declare
    assert resolution.warning is None


# --- Req 7.3 identical ---------------------------------------------------

def test_identical_record_causes_no_write() -> None:
    record = _record()
    resolution = resolve_duplicate(stored=record, incoming=record)
    assert resolution.action is DedupAction.NO_WRITE
    assert resolution.is_duplicate is True
    assert resolution.warning is None  # an expected re-delivery, not a problem


def test_identical_record_reports_the_stored_value_as_the_winner() -> None:
    record = _record()
    resolution = resolve_duplicate(stored=record, incoming=record)
    assert resolution.winner == record


# --- Req 7.4 ratified supersedes provisional -----------------------------

def test_ratified_replaces_provisional() -> None:
    stored = _record(RatificationStatus="P", ScaledValue=10.0)
    incoming = _record(RatificationStatus="R", ScaledValue=3.0)
    resolution = resolve_duplicate(stored=stored, incoming=incoming)
    assert resolution.action is DedupAction.REPLACE
    assert resolution.winner == incoming
    # the ratified value wins even though it is SMALLER: status outranks magnitude
    assert resolution.winner.ScaledValue == 3.0


def test_ratified_replacement_is_not_a_conflict() -> None:
    resolution = resolve_duplicate(
        stored=_record(RatificationStatus="P", ScaledValue=10.0),
        incoming=_record(RatificationStatus="R", ScaledValue=3.0),
    )
    assert resolution.quality_flag is not QualityFlag.SUSPECT_CONFLICT
    assert resolution.warning is None


# --- Req 7.5 provisional never displaces ratified ------------------------

def test_provisional_does_not_displace_ratified() -> None:
    stored = _record(RatificationStatus="R", ScaledValue=3.0)
    incoming = _record(RatificationStatus="P", ScaledValue=99.0)
    resolution = resolve_duplicate(stored=stored, incoming=incoming)
    assert resolution.action is DedupAction.NO_WRITE
    assert resolution.winner == stored


def test_displaced_provisional_warns_naming_the_key() -> None:
    resolution = resolve_duplicate(
        stored=_record(RatificationStatus="R", ScaledValue=3.0),
        incoming=_record(RatificationStatus="P", ScaledValue=99.0),
    )
    assert resolution.warning is not None
    assert "CB0001" in resolution.warning


# --- Req 7.6 equal-status value conflict ---------------------------------

def test_equal_status_conflict_keeps_the_greater_value() -> None:
    resolution = resolve_duplicate(
        stored=_record(RatificationStatus="P", ScaledValue=10.0),
        incoming=_record(RatificationStatus="P", ScaledValue=25.0),
    )
    assert resolution.action is DedupAction.REPLACE
    assert resolution.winner.ScaledValue == 25.0
    assert resolution.quality_flag is QualityFlag.SUSPECT_CONFLICT


def test_conflict_keeps_the_greater_value_when_stored_is_greater() -> None:
    resolution = resolve_duplicate(
        stored=_record(RatificationStatus="P", ScaledValue=25.0),
        incoming=_record(RatificationStatus="P", ScaledValue=10.0),
    )
    # the greater value is already stored, so nothing needs writing — but the
    # conflict is still declared, because the reading is now known to be disputed
    assert resolution.winner.ScaledValue == 25.0
    assert resolution.quality_flag is QualityFlag.SUSPECT_CONFLICT


def test_conflict_warning_names_the_key_and_both_values() -> None:
    resolution = resolve_duplicate(
        stored=_record(RatificationStatus="P", ScaledValue=10.0),
        incoming=_record(RatificationStatus="P", ScaledValue=25.0),
    )
    assert resolution.warning is not None
    assert "CB0001" in resolution.warning
    assert "10.0" in resolution.warning
    assert "25.0" in resolution.warning


def test_conflict_applies_at_ratified_status_too() -> None:
    # Req 7.6 says "RatificationStatus equals", not "is provisional"
    resolution = resolve_duplicate(
        stored=_record(RatificationStatus="R", ScaledValue=10.0),
        incoming=_record(RatificationStatus="R", ScaledValue=25.0),
    )
    assert resolution.quality_flag is QualityFlag.SUSPECT_CONFLICT
    assert resolution.winner.ScaledValue == 25.0


# --- the 7.4 / 7.6 interaction that order-independence depends on --------

def test_ratified_replacement_clears_a_prior_conflict_flag() -> None:
    # If a ratified replacement PRESERVED an earlier suspect_conflict, the final
    # state would depend on whether the conflicting provisional pair arrived before
    # or after the ratified record — which Req 7.7 forbids.
    resolution = resolve_duplicate(
        stored=_record(RatificationStatus="P", ScaledValue=25.0),
        incoming=_record(RatificationStatus="R", ScaledValue=3.0),
        stored_flag=QualityFlag.SUSPECT_CONFLICT,
    )
    assert resolution.action is DedupAction.REPLACE
    assert resolution.quality_flag is not QualityFlag.SUSPECT_CONFLICT


def test_a_prior_conflict_flag_survives_an_equal_status_duplicate() -> None:
    # conversely, a conflict already found must not be forgotten by a later
    # identical-status arrival, or the flag would depend on arrival order too
    resolution = resolve_duplicate(
        stored=_record(RatificationStatus="P", ScaledValue=25.0),
        incoming=_record(RatificationStatus="P", ScaledValue=25.0),
        stored_flag=QualityFlag.SUSPECT_CONFLICT,
    )
    assert resolution.quality_flag is QualityFlag.SUSPECT_CONFLICT


def test_a_prior_conflict_flag_survives_a_rejected_provisional() -> None:
    resolution = resolve_duplicate(
        stored=_record(RatificationStatus="R", ScaledValue=25.0),
        incoming=_record(RatificationStatus="P", ScaledValue=1.0),
        stored_flag=QualityFlag.SUSPECT_CONFLICT,
    )
    assert resolution.quality_flag is QualityFlag.SUSPECT_CONFLICT


# --- Req 7.1 mismatched keys are a programming error ---------------------

def test_resolving_records_with_different_keys_is_refused() -> None:
    # §5: this is not bad upstream data, it is a caller bug — two different
    # measurements are not duplicate candidates and silently picking one would
    # corrupt a stored reading
    with pytest.raises(ValueError, match="same Dedup_Key"):
        resolve_duplicate(
            stored=_record(SiteCode="CB0001"), incoming=_record(SiteCode="CB0002")
        )


# --- resolution is pure --------------------------------------------------

def test_resolution_does_not_mutate_either_candidate() -> None:
    stored = _record(RatificationStatus="P", ScaledValue=10.0)
    incoming = _record(RatificationStatus="P", ScaledValue=25.0)
    resolve_duplicate(stored=stored, incoming=incoming)
    assert stored.ScaledValue == 10.0
    assert incoming.ScaledValue == 25.0


def test_resolution_is_repeatable() -> None:
    stored = _record(RatificationStatus="P", ScaledValue=10.0)
    incoming = _record(RatificationStatus="P", ScaledValue=25.0)
    first = resolve_duplicate(stored=stored, incoming=incoming)
    second = resolve_duplicate(stored=stored, incoming=incoming)
    assert first == second
