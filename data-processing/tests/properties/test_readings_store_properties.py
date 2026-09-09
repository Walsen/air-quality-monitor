"""ReadingsStore property tests (tasks 13.2, 13.3, 13.4).

Feature: ingestion-and-serving-service
- Property 25: readings store round-trip (Requirements 14.5, 14.2)
- Property 26: window query completeness and half-open exclusivity
  (Requirements 14.6, 14.3)
- Property 27: retention window excludes aged readings (Requirements 14.7, 14.8)

Property 26 asserts completeness in BOTH directions, because each alone is trivially
satisfiable: returning everything satisfies "every reading inside the window is returned",
and returning nothing satisfies "no reading outside it is returned".
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.adapters.memory import InMemoryReadingsStore
from aqm_ingestion.adapters.memory.adapters import DEFAULT_RETENTION_DAYS
from aqm_ingestion.domain.aqi.overall import DEFAULT_SPECIES_PRECEDENCE
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.ports.clock import FixedClock

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_SPECIES = ("PM25", "NO2")


def _reading(
    site: str, species: str, hours_ago: int, reported: float
) -> CalibratedReading:
    return CalibratedReading(
        key=DedupKey(
            site_code=site,
            species=species,
            interval_start=_NOW - dt.timedelta(hours=hours_ago),
            duration="PT1H",
        ),
        reported_value=reported,
        corrected_value=reported,
        units="ug.m-3",
        quality_flag=QualityFlag.CALIBRATED,
        confidence=Confidence.HIGH,
        calibration_strategy="rh_linear",
        breakpoint_table="epa-2024-05-06",
        ratification_status="R",
        ingested_at=_NOW,
        archive_id="a1",
    )


# One reading per (species, hour) so no two share a Dedup_Key, keeping the round-trip
# claim about storage rather than about resolution — which Property 8 already covers.
@st.composite
def _readings(draw: st.DrawFn) -> list[CalibratedReading]:
    slots = draw(
        st.lists(
            st.tuples(st.sampled_from(_SPECIES), st.integers(min_value=0, max_value=48)),
            min_size=1,
            max_size=12,
            unique=True,
        )
    )
    return [
        _reading("CB0001", species, hours_ago, draw(st.floats(0.0, 500.0)))
        for species, hours_ago in slots
    ]


def _store(**kwargs: object) -> InMemoryReadingsStore:
    return InMemoryReadingsStore(clock=FixedClock(_NOW), **kwargs)  # type: ignore[arg-type]


@given(readings=_readings())
def test_property_25_readings_store_round_trip(
    readings: list[CalibratedReading],
) -> None:
    """Feature: ingestion-and-serving-service, Property 25."""
    store = _store()
    store.put_batch(readings)

    # Req 14.5: a query whose window contains the interval start returns a Reading equal
    # in EVERY field — not merely one with the same key
    for written in readings:
        result = store.query_window(
            site_code=written.key.site_code,
            species=frozenset({written.key.species}),
            start=written.key.interval_start,
            end=written.key.interval_start + dt.timedelta(seconds=1),
        )
        assert result.readings == (written,)

    # and a get by key agrees with the window query
    for written in readings:
        assert store.get(written.key) == written


@given(readings=_readings())
def test_property_25_rewriting_the_same_readings_changes_nothing(
    readings: list[CalibratedReading],
) -> None:
    """Feature: ingestion-and-serving-service, Property 25 (Req 14.2 idempotence)."""
    once = _store()
    once.put_batch(readings)
    twice = _store()
    twice.put_batch(readings)
    twice.put_batch(readings)

    window = {
        "site_code": "CB0001",
        "species": None,
        "start": _NOW - dt.timedelta(days=30),
        "end": _NOW + dt.timedelta(hours=1),
    }
    assert once.query_window(**window) == twice.query_window(**window)  # type: ignore[arg-type]


@given(
    readings=_readings(),
    bounds=st.tuples(
        st.integers(min_value=0, max_value=48), st.integers(min_value=0, max_value=48)
    ),
)
def test_property_26_window_completeness_and_half_open_exclusivity(
    readings: list[CalibratedReading], bounds: tuple[int, int]
) -> None:
    """Feature: ingestion-and-serving-service, Property 26."""
    store = _store()
    store.put_batch(readings)

    high_hours, low_hours = sorted(bounds, reverse=True)
    start = _NOW - dt.timedelta(hours=high_hours)
    end = _NOW - dt.timedelta(hours=low_hours)

    result = store.query_window(
        site_code="CB0001", species=None, start=start, end=end
    )
    returned = set(result.readings)

    expected = {
        reading
        for reading in readings
        if start <= reading.key.interval_start < end  # half-open (Req 14.3)
    }

    # Req 14.6 asserted BOTH ways: returning everything would satisfy the first half and
    # returning nothing the second, so only together do they pin the behaviour
    assert expected <= returned  # every reading inside the window is returned
    assert returned <= expected  # and nothing outside it is

    # Req 14.3's ordering: ascending interval start, then the configured species
    # precedence — which is PM25 before NO2, the REVERSE of alphabetical
    def rank(reading: CalibratedReading) -> tuple[dt.datetime, int]:
        return (
            reading.key.interval_start,
            DEFAULT_SPECIES_PRECEDENCE.index(reading.key.species),
        )

    assert list(result.readings) == sorted(result.readings, key=rank)


@given(readings=_readings())
def test_property_26_an_empty_window_returns_nothing(
    readings: list[CalibratedReading],
) -> None:
    """Feature: ingestion-and-serving-service, Property 26 (degenerate window).

    A window whose start equals its end contains no instant at all under half-open
    semantics, so it must return nothing however much is stored.
    """
    store = _store()
    store.put_batch(readings)
    result = store.query_window(
        site_code="CB0001", species=None, start=_NOW, end=_NOW
    )
    assert result.readings == ()


@given(
    age_days=st.integers(min_value=0, max_value=400),
    species=st.sampled_from(_SPECIES),
)
def test_property_27_retention_window_excludes_aged_readings(
    age_days: int, species: str
) -> None:
    """Feature: ingestion-and-serving-service, Property 27."""
    store = _store()
    reading = _reading("CB0001", species, hours_ago=age_days * 24, reported=12.0)
    store.put(reading)

    # a window wide enough to contain the reading regardless of its age, so the ONLY
    # thing that can exclude it is retention
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=_NOW - dt.timedelta(days=500),
        end=_NOW + dt.timedelta(days=1),
    )

    inside_retention = age_days <= DEFAULT_RETENTION_DAYS
    assert bool(result.readings) == inside_retention

    # Req 14.7 says every query result, so latest-per-species agrees
    latest = store.latest_per_species(["CB0001"], _NOW - dt.timedelta(days=500))
    assert bool(latest) == inside_retention


@given(
    count=st.integers(min_value=1, max_value=40),
    cap=st.integers(min_value=1, max_value=40),
)
def test_property_27_a_result_is_bounded_and_truncation_is_reported(
    count: int, cap: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 27 (Req 14.8).

    The bound and the truncation flag must agree: reporting truncation without applying the
    bound, or applying it silently, both leave a caller with a partial set it cannot detect.
    """
    store = _store(max_window_readings=cap)
    store.put_batch(
        [_reading("CB0001", "PM25", hours_ago=hour, reported=1.0) for hour in range(count)]
    )
    result = store.query_window(
        site_code="CB0001",
        species=None,
        start=_NOW - dt.timedelta(days=30),
        end=_NOW + dt.timedelta(hours=1),
    )
    assert len(result.readings) == min(count, cap)
    assert result.truncated == (count > cap)
