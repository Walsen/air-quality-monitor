"""Unit tests for ReadingsStore semantics (task 13.1).

- 14.2: a write for an existing Dedup_Key RESOLVES per Requirement 7 rather than
  overwriting, so a provisional value cannot displace a ratified one.
- 14.3: half-open window, start inclusive to end exclusive, ordered by ascending interval
  start THEN the configured species precedence — which is PM25 before NO2, the reverse of
  alphabetical order, so sorting by name would look right and be wrong.
- 14.4: most recent Reading per species across a set of sites.
- 14.7: readings older than the Retention_Window (default 90 days) are excluded from every
  query, measured from the CLOCK.
- 14.8: a result is bounded at the configured maximum and truncation is REPORTED.
- 14.11: a stored Reading always carries a Quality_Flag and a Confidence.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_ingestion.adapters.memory import InMemoryReadingsStore
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.ports.clock import FixedClock

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _reading(
    *,
    site: str = "CB0001",
    species: str = "PM25",
    interval_start: dt.datetime | None = None,
    reported: float = 10.0,
    status: str = "P",
    flag: QualityFlag = QualityFlag.CALIBRATED,
) -> CalibratedReading:
    return CalibratedReading(
        key=DedupKey(
            site_code=site,
            species=species,
            interval_start=interval_start or _NOW - dt.timedelta(hours=1),
            duration="PT1H",
        ),
        reported_value=reported,
        corrected_value=reported,
        units="ug.m-3",
        quality_flag=flag,
        confidence=Confidence.HIGH,
        calibration_strategy="rh_linear",
        breakpoint_table="epa-2024-05-06",
        ratification_status=status,
        ingested_at=_NOW,
        archive_id="a1",
    )


def _store(**kwargs: object) -> InMemoryReadingsStore:
    return InMemoryReadingsStore(clock=FixedClock(_NOW), **kwargs)  # type: ignore[arg-type]


# --- Req 14.3 the species precedence ordering ---------------------------

def test_species_are_ordered_by_precedence_not_alphabetically() -> None:
    # THE trap: the configured default is PM25 before NO2, but alphabetically NO2 sorts
    # first. A name sort would pass a careless test and violate Req 14.3.
    store = _store()
    moment = _NOW - dt.timedelta(hours=1)
    store.put(_reading(species="NO2", interval_start=moment))
    store.put(_reading(species="PM25", interval_start=moment))
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=moment,
        end=_NOW,
    )
    assert [r.key.species for r in result.readings] == ["PM25", "NO2"]


def test_precedence_ordering_is_independent_of_insertion_order() -> None:
    moment = _NOW - dt.timedelta(hours=1)
    for order in (("PM25", "NO2"), ("NO2", "PM25")):
        store = _store()
        for species in order:
            store.put(_reading(species=species, interval_start=moment))
        result = store.query_window(
            site_code="CB0001", species=None, start=moment, end=_NOW
        )
        assert [r.key.species for r in result.readings] == ["PM25", "NO2"]


def test_interval_start_dominates_species_precedence() -> None:
    # Req 14.3 orders by interval start FIRST, so an older NO2 precedes a newer PM25
    store = _store()
    older = _NOW - dt.timedelta(hours=3)
    newer = _NOW - dt.timedelta(hours=1)
    store.put(_reading(species="PM25", interval_start=newer))
    store.put(_reading(species="NO2", interval_start=older))
    result = store.query_window(
        site_code="CB0001", species=None, start=older, end=_NOW
    )
    assert [r.key.species for r in result.readings] == ["NO2", "PM25"]


def test_the_precedence_is_configurable() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW), species_precedence=("NO2", "PM25"))
    moment = _NOW - dt.timedelta(hours=1)
    store.put(_reading(species="PM25", interval_start=moment))
    store.put(_reading(species="NO2", interval_start=moment))
    result = store.query_window(
        site_code="CB0001", species=None, start=moment, end=_NOW
    )
    assert [r.key.species for r in result.readings] == ["NO2", "PM25"]


# --- Req 14.3 half-open window ------------------------------------------

def test_the_window_start_is_inclusive() -> None:
    store = _store()
    moment = _NOW - dt.timedelta(hours=2)
    store.put(_reading(interval_start=moment))
    result = store.query_window(
        site_code="CB0001", species=None, start=moment, end=_NOW
    )
    assert len(result.readings) == 1


def test_the_window_end_is_exclusive() -> None:
    store = _store()
    moment = _NOW - dt.timedelta(hours=2)
    store.put(_reading(interval_start=moment))
    result = store.query_window(
        site_code="CB0001", species=None, start=moment, end=moment
    )
    assert result.readings == ()


def test_a_species_filter_restricts_the_result() -> None:
    store = _store()
    moment = _NOW - dt.timedelta(hours=1)
    store.put(_reading(species="PM25", interval_start=moment))
    store.put(_reading(species="NO2", interval_start=moment))
    result = store.query_window(
        site_code="CB0001", species=frozenset({"NO2"}), start=moment, end=_NOW
    )
    assert [r.key.species for r in result.readings] == ["NO2"]


def test_another_site_is_not_returned() -> None:
    store = _store()
    moment = _NOW - dt.timedelta(hours=1)
    store.put(_reading(site="OTHER", interval_start=moment))
    result = store.query_window(
        site_code="CB0001", species=None, start=moment, end=_NOW
    )
    assert result.readings == ()


# --- Req 14.2 resolution on write ---------------------------------------

def test_a_ratified_write_replaces_a_provisional_one() -> None:
    store = _store()
    store.put(_reading(reported=10.0, status="P"))
    store.put(_reading(reported=3.0, status="R"))
    stored = store.get(_reading().key)
    assert stored is not None
    assert stored.ratification_status == "R"
    assert stored.reported_value == 3.0  # status outranks magnitude


def test_a_provisional_write_does_not_displace_a_ratified_one() -> None:
    # the defect this test exists for: a blind replace would lose the ratified value
    store = _store()
    store.put(_reading(reported=3.0, status="R"))
    store.put(_reading(reported=99.0, status="P"))
    stored = store.get(_reading().key)
    assert stored is not None
    assert stored.ratification_status == "R"
    assert stored.reported_value == 3.0


def test_an_equal_status_conflict_keeps_the_greater_and_flags_it() -> None:
    store = _store()
    store.put(_reading(reported=10.0, status="P"))
    store.put(_reading(reported=25.0, status="P"))
    stored = store.get(_reading().key)
    assert stored is not None
    assert stored.reported_value == 25.0
    assert stored.quality_flag is QualityFlag.SUSPECT_CONFLICT


def test_a_conflict_flags_even_when_the_greater_value_was_already_stored() -> None:
    store = _store()
    store.put(_reading(reported=25.0, status="P"))
    store.put(_reading(reported=10.0, status="P"))
    stored = store.get(_reading().key)
    assert stored is not None
    assert stored.reported_value == 25.0
    assert stored.quality_flag is QualityFlag.SUSPECT_CONFLICT


def test_an_identical_rewrite_changes_nothing() -> None:
    store = _store()
    store.put(_reading())
    store.put(_reading())
    stored = store.get(_reading().key)
    assert stored is not None
    assert stored.quality_flag is QualityFlag.CALIBRATED  # not a conflict


def test_resolution_is_order_independent() -> None:
    forward = _store()
    forward.put(_reading(reported=10.0, status="P"))
    forward.put(_reading(reported=3.0, status="R"))
    backward = _store()
    backward.put(_reading(reported=3.0, status="R"))
    backward.put(_reading(reported=10.0, status="P"))
    assert forward.get(_reading().key) == backward.get(_reading().key)


def test_a_batch_write_resolves_the_same_way() -> None:
    store = _store()
    store.put_batch(
        [_reading(reported=10.0, status="P"), _reading(reported=3.0, status="R")]
    )
    stored = store.get(_reading().key)
    assert stored is not None
    assert stored.ratification_status == "R"


# --- Req 14.7 retention -------------------------------------------------

def test_a_reading_older_than_retention_is_excluded_from_a_window() -> None:
    store = _store()
    aged = _NOW - dt.timedelta(days=91)
    store.put(_reading(interval_start=aged))
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=aged - dt.timedelta(days=1),
        end=_NOW,
    )
    assert result.readings == ()


def test_a_reading_inside_retention_is_returned() -> None:
    store = _store()
    recent = _NOW - dt.timedelta(days=89)
    store.put(_reading(interval_start=recent))
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=recent - dt.timedelta(days=1),
        end=_NOW,
    )
    assert len(result.readings) == 1


def test_retention_is_measured_from_the_clock_not_the_window() -> None:
    # §2: the exclusion moves with the injected clock, so a later clock ages a reading out
    # without the query changing at all
    aged = _NOW - dt.timedelta(days=89)
    early = InMemoryReadingsStore(clock=FixedClock(_NOW))
    early.put(_reading(interval_start=aged))
    late = InMemoryReadingsStore(clock=FixedClock(_NOW + dt.timedelta(days=5)))
    late.put(_reading(interval_start=aged))

    window = {
        "site_code": "CB0001",
        "species": None,
        "start": aged - dt.timedelta(days=1),
        "end": _NOW + dt.timedelta(days=10),
    }
    assert len(early.query_window(**window).readings) == 1  # type: ignore[arg-type]
    assert late.query_window(**window).readings == ()  # type: ignore[arg-type]


def test_the_retention_window_is_configurable() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW), retention_days=1)
    aged = _NOW - dt.timedelta(days=2)
    store.put(_reading(interval_start=aged))
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=aged - dt.timedelta(days=1),
        end=_NOW,
    )
    assert result.readings == ()


def test_default_retention_is_ninety_days() -> None:
    from aqm_ingestion.adapters.memory.adapters import DEFAULT_RETENTION_DAYS

    assert DEFAULT_RETENTION_DAYS == 90


def test_retention_also_excludes_from_latest_per_species() -> None:
    # Req 14.7 says EVERY query result, so the latest-per-species path must honour it too
    store = _store()
    store.put(_reading(interval_start=_NOW - dt.timedelta(days=91)))
    latest = store.latest_per_species(["CB0001"], _NOW - dt.timedelta(days=365))
    assert not latest


# --- Req 14.4 latest per species ----------------------------------------

def test_latest_per_species_returns_the_newest() -> None:
    store = _store()
    store.put(_reading(interval_start=_NOW - dt.timedelta(hours=3), reported=1.0))
    store.put(_reading(interval_start=_NOW - dt.timedelta(hours=1), reported=2.0))
    latest = store.latest_per_species(["CB0001"], _NOW - dt.timedelta(days=1))
    assert [r.reported_value for r in latest["CB0001"]] == [2.0]


def test_latest_per_species_spans_several_sites() -> None:
    store = _store()
    moment = _NOW - dt.timedelta(hours=1)
    store.put(_reading(site="CB0001", interval_start=moment))
    store.put(_reading(site="CB0002", interval_start=moment))
    latest = store.latest_per_species(
        ["CB0001", "CB0002"], _NOW - dt.timedelta(days=1)
    )
    assert set(latest) == {"CB0001", "CB0002"}


def test_latest_per_species_orders_species_by_precedence() -> None:
    store = _store()
    moment = _NOW - dt.timedelta(hours=1)
    store.put(_reading(species="NO2", interval_start=moment))
    store.put(_reading(species="PM25", interval_start=moment))
    latest = store.latest_per_species(["CB0001"], _NOW - dt.timedelta(days=1))
    assert [r.key.species for r in latest["CB0001"]] == ["PM25", "NO2"]


# --- Req 14.8 truncation ------------------------------------------------

def test_a_result_is_bounded_and_truncation_is_reported() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW), max_window_readings=3)
    for hour in range(6):
        store.put(_reading(interval_start=_NOW - dt.timedelta(hours=hour + 1)))
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=_NOW - dt.timedelta(days=1),
        end=_NOW,
    )
    assert len(result.readings) == 3
    assert result.truncated is True


def test_an_untruncated_result_says_so() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_NOW), max_window_readings=10)
    store.put(_reading())
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=_NOW - dt.timedelta(days=1),
        end=_NOW,
    )
    assert result.truncated is False


def test_truncation_keeps_the_earliest_readings() -> None:
    # deterministic truncation: the cap applies AFTER ordering, so which readings are
    # dropped does not depend on insertion order (§2)
    store = InMemoryReadingsStore(clock=FixedClock(_NOW), max_window_readings=2)
    for hour in (3, 1, 2):
        store.put(_reading(interval_start=_NOW - dt.timedelta(hours=hour)))
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=_NOW - dt.timedelta(days=1),
        end=_NOW,
    )
    starts = [r.key.interval_start for r in result.readings]
    assert starts == sorted(starts)
    assert len(result.readings) == 2


def test_default_cap_is_ten_thousand() -> None:
    from aqm_ingestion.adapters.memory.adapters import _DEFAULT_WINDOW_CAP

    assert _DEFAULT_WINDOW_CAP == 10_000


# --- Req 14.11 quality always travels -----------------------------------

def test_a_stored_reading_always_carries_quality_and_confidence() -> None:
    # structural: CalibratedReading requires both, so a value without them cannot be
    # stored and therefore cannot be served (Req 14.11)
    store = _store()
    store.put(_reading())
    stored = store.get(_reading().key)
    assert stored is not None
    assert stored.quality_flag in set(QualityFlag)
    assert stored.confidence in set(Confidence)


def test_the_reading_type_has_no_default_for_quality() -> None:
    fields = CalibratedReading.__dataclass_fields__
    from dataclasses import MISSING

    assert fields["quality_flag"].default is MISSING
    assert fields["confidence"].default is MISSING


# --- error cases --------------------------------------------------------

def test_an_inverted_window_is_refused() -> None:
    store = _store()
    with pytest.raises(ValueError, match="start"):
        store.query_window(
            site_code="CB0001",
            species=None,
            start=_NOW,
            end=_NOW - dt.timedelta(hours=1),
        )
