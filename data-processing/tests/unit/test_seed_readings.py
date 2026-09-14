"""Unit tests for the readings + registry seeding loader (Task 10).

Feature: personal-diary-memory, Requirements 10.3, 10.4.

The loader writes a bounded, representative exposure history for a fixed catalogue of demo
sites: the sites go into the sensor registry and a per-site, per-species readings history goes
into the readings store, both through the store PORTS (in-memory here, dynamodb in a deployment)
and with NO ingest pipeline. Two properties matter above the rest and are pinned here:

- IDEMPOTENCE (Req 10.4). A second run over the same stores writes no duplicate: every site
  comes back UNCHANGED and the stored reading count is identical after run two.
- DETERMINISM (§2). The series is a pure function of an injected seed + clock, seeded per site,
  so two loaders built the same way produce byte-identical readings and a different seed
  produces a different series.

These are about the SEEDING — what the catalogue is, what gets written, that a rerun is a no-op,
and that the seed reaches the association path through the same GeoSelector the serving side
uses. The arithmetic of the association itself is covered by test_association_job.py.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from aqm_ingestion.adapters.memory import (
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
)
from aqm_ingestion.domain.profile import (
    RECOGNIZED_CONSENT_VERSIONS,
    Condition,
    SensitivityLevel,
    UserProfile,
    build_profile,
)
from aqm_ingestion.jobs.seed_readings import (
    DEMO_SITES,
    READINGS_PER_SITE_PER_SPECIES,
    RECENT_HOURLY_READINGS,
    SeedSummary,
    seed_exposure_history,
)
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.ports.clock import FixedClock
from aqm_ingestion.serving.geo import GeoSelector, SelectionSettings

# A fixed instant so the whole seed is reproducible; the window reaches back from here.
_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_SEED = 20260701


def _stores(
    *, clock: FixedClock | None = None
) -> tuple[InMemorySensorRegistryStore, InMemoryReadingsStore]:
    fixed = clock or FixedClock(_NOW)
    return (
        InMemorySensorRegistryStore(),
        # A generous cap so the loader's own bound, not the store's, is what limits the history.
        InMemoryReadingsStore(max_window_readings=1_000_000, clock=fixed),
    )


def _demo_profile() -> UserProfile:
    """A profile whose home sits among the seeded Cochabamba sites.

    The home coordinate matches the canonical demo profile the serving/association tests use, so
    a seeded site near it is selectable through the GeoSelector (guards Task 12).
    """
    return build_profile(
        {
            "user_id": "demo-user",
            "condition": Condition.ASTHMA,
            "sensitivity_level": SensitivityLevel.STANDARD,
            "locations": [{"name": "home", "latitude": -17.394, "longitude": -66.157}],
            "consent": {
                "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
                "given_at": _NOW,
            },
            "created_at": _NOW,
            "updated_at": _NOW,
        }
    )


# --- 1. the sites are registered ----------------------------------------


def test_the_loader_registers_the_demo_sites() -> None:
    registry, readings = _stores()
    seed_exposure_history(
        registry=registry, readings=readings, clock=FixedClock(_NOW), seed=_SEED
    )
    active = registry.list_active()
    got = {entry.record.SiteCode for entry in active}
    expected = {site.site_code for site in DEMO_SITES}
    assert got == expected
    # list_active is ordered by SiteCode (§2); assert the catalogue's own order matches.
    assert [entry.record.SiteCode for entry in active] == sorted(expected)
    assert "CB0001" in got, "the canonical demo site must be present"


# --- 2. a bounded readings history per site -----------------------------


def test_the_loader_writes_a_bounded_readings_history_per_site() -> None:
    registry, readings = _stores()
    seed_exposure_history(
        registry=registry, readings=readings, clock=FixedClock(_NOW), seed=_SEED
    )
    start = _NOW - dt.timedelta(days=365)
    end = _NOW + dt.timedelta(days=1)
    for site in DEMO_SITES:
        window = readings.query_window(site.site_code, None, start, end)
        assert window.readings, f"no history seeded for {site.site_code}"
        assert not window.truncated, "the bound must be below the store cap"
        per_species = (
            READINGS_PER_SITE_PER_SPECIES + RECENT_HOURLY_READINGS
        ) * len(site.species)
        assert len(window.readings) == per_species, (
            f"{site.site_code} should hold exactly {per_species} readings"
        )
        for reading in window.readings:
            assert reading.sub_index is not None, (
                "the association reads reading.sub_index; a None value is skipped"
            )
            assert isinstance(reading.sub_index, int)


def test_the_newest_reading_is_within_the_serving_freshness_window() -> None:
    # Req 20.8's "current" reading is one whose interval start is within freshness_hours of now.
    # A demo that only stamped a daily reading at a fixed hour would read as "unavailable" for
    # most of the day; the recent hourly tail guarantees a current reading at any record time.
    registry, readings = _stores()
    seed_exposure_history(
        registry=registry, readings=readings, clock=FixedClock(_NOW), seed=_SEED
    )
    window_hours = SelectionSettings().freshness_hours
    not_before = _NOW - dt.timedelta(hours=window_hours)
    for site in DEMO_SITES:
        w = readings.query_window(
            site.site_code, None, _NOW - dt.timedelta(days=365), _NOW + dt.timedelta(days=1)
        )
        newest = max(r.key.interval_start for r in w.readings)
        assert newest >= not_before, (
            f"{site.site_code}: newest reading {newest.isoformat()} is older than the "
            f"{window_hours}h freshness window from {_NOW.isoformat()}"
        )
        assert newest <= _NOW, "a seeded reading must not be in the future"


# --- 3. a second run writes no duplicates (Req 10.4) --------------------


def test_a_second_run_writes_no_duplicates() -> None:
    registry, readings = _stores()
    clock = FixedClock(_NOW)
    first = seed_exposure_history(registry=registry, readings=readings, clock=clock, seed=_SEED)
    count_after_first = _total_readings(readings)

    second = seed_exposure_history(
        registry=registry, readings=readings, clock=clock, seed=_SEED
    )
    count_after_second = _total_readings(readings)

    assert count_after_second == count_after_first, "a rerun must add no rows"
    assert second.sites_created == 0
    assert second.sites_updated == 0
    assert second.sites_unchanged == len(DEMO_SITES), "every site UNCHANGED on rerun"
    # The first run created them all.
    assert first.sites_created == len(DEMO_SITES)
    assert first.sites_unchanged == 0


# --- 4. the series is deterministic from the seed -----------------------


def test_the_series_is_deterministic_from_the_seed() -> None:
    reg_a, read_a = _stores()
    reg_b, read_b = _stores()
    seed_exposure_history(registry=reg_a, readings=read_a, clock=FixedClock(_NOW), seed=_SEED)
    seed_exposure_history(registry=reg_b, readings=read_b, clock=FixedClock(_NOW), seed=_SEED)
    assert _fingerprint(read_a) == _fingerprint(read_b), (
        "same seed + clock must produce byte-identical readings (§2)"
    )

    reg_c, read_c = _stores()
    seed_exposure_history(
        registry=reg_c, readings=read_c, clock=FixedClock(_NOW), seed=_SEED + 1
    )
    assert _fingerprint(read_c) != _fingerprint(read_a), (
        "a different seed must produce a different series"
    )


# --- 5. the loader logs a summary without health detail -----------------


def test_the_loader_logs_a_summary_without_health_detail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    registry, readings = _stores()
    summary = seed_exposure_history(
        registry=registry, readings=readings, clock=FixedClock(_NOW), seed=_SEED
    )
    output = capsys.readouterr().out
    lines = [json.loads(line) for line in output.splitlines() if line.strip()]
    summaries = [line for line in lines if line.get("event") == "seed_complete"]
    assert summaries, "a summary INFO line must be emitted"
    entry = summaries[-1]
    assert entry["sites_created"] == summary.sites_created
    assert entry["readings_written"] == summary.readings_written
    # There is no health data here, but assert nothing secret/marker-like leaks.
    assert "[redacted]" not in output, "no field should have tripped the redactor"
    for forbidden in ("BEGIN", "PRIVATE KEY", "password", "token"):
        assert forbidden not in output


# --- 6. the seed is reachable by the association path -------------------


def test_the_loaded_sites_are_selectable_for_the_demo_profile() -> None:
    registry, readings = _stores()
    clock = FixedClock(_NOW)
    seed_exposure_history(registry=registry, readings=readings, clock=clock, seed=_SEED)

    selector = GeoSelector(
        registry=registry,
        readings=readings,
        clock=clock,
        settings=SelectionSettings(),
    )
    selection = selector.select(_demo_profile())
    selected = {site.site_code for site in selection.sites}
    seeded = {site.site_code for site in DEMO_SITES}
    assert selected & seeded, (
        "at least one seeded site must be selectable for the demo profile — "
        "otherwise the association could never reach the seeded history (Task 12)"
    )


# --- helpers ------------------------------------------------------------


def _total_readings(store: InMemoryReadingsStore) -> int:
    """Count every stored reading across all demo sites, spanning a wide window."""
    start = _NOW - dt.timedelta(days=365)
    end = _NOW + dt.timedelta(days=1)
    return sum(
        len(store.query_window(site.site_code, None, start, end).readings)
        for site in DEMO_SITES
    )


def _fingerprint(store: InMemoryReadingsStore) -> tuple[tuple[object, ...], ...]:
    """A stable, comparable projection of every stored reading."""
    start = _NOW - dt.timedelta(days=365)
    end = _NOW + dt.timedelta(days=1)
    rows: list[tuple[object, ...]] = []
    for site in DEMO_SITES:
        for reading in store.query_window(site.site_code, None, start, end).readings:
            rows.append(
                (
                    reading.key.site_code,
                    reading.key.species,
                    reading.key.interval_start.isoformat(),
                    reading.sub_index,
                    reading.corrected_value,
                    reading.ratification_status,
                )
            )
    return tuple(rows)


def test_the_summary_is_a_small_dataclass() -> None:
    """SeedSummary carries only counts — no readings, no health, no secrets."""
    registry, readings = _stores()
    summary = seed_exposure_history(
        registry=registry, readings=readings, clock=FixedClock(_NOW), seed=_SEED
    )
    assert isinstance(summary, SeedSummary)
    assert summary.readings_written == _total_readings(readings)
