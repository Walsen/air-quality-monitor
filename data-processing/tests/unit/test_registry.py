"""Unit tests for the sensor registry and geographic query (tasks 14.1, 14.2).

- 15.2: upsert by `SiteCode`, replacing when any field differs and making NO WRITE when
  every field is equal.
- 15.4: great-circle distance with an Earth radius of 6,371.0088 km.
- 15.5: nearest-N by ascending distance, ties by ascending `SiteCode`, fewer than N when
  fewer exist.
- 15.6/15.7: an inclusive maximum radius.
- 15.8 / 2.8: a site with a past non-null `EndDate` is inactive — excluded from nearest-N
  but still resolvable by `SiteCode`.
- 15.9: a changed `Latitude` or `Longitude` logs one warning naming both positions.
- 15.11: every entry records the instant it was last upserted.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.adapters.memory import InMemorySensorRegistryStore
from aqm_ingestion.adapters.memory.adapters import _great_circle_km
from aqm_ingestion.contract.records import SensorMetadataRecord
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.ports.protocols import UpsertOutcome
from tests.unit.test_records import GOLDEN_METADATA_PAYLOAD

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _record(**overrides: object) -> SensorMetadataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_METADATA_PAYLOAD)) | overrides
    return SensorMetadataRecord(**fields)


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


# --- Req 15.2 upsert on difference only ---------------------------------

def test_a_new_site_is_created() -> None:
    registry = InMemorySensorRegistryStore()
    assert registry.upsert(_record(), _NOW) is UpsertOutcome.CREATED


def test_an_identical_record_is_unchanged() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(), _NOW)
    assert registry.upsert(_record(), _NOW) is UpsertOutcome.UNCHANGED


def test_a_differing_record_is_updated() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(), _NOW)
    outcome = registry.upsert(_record(SiteName="Renamed Site"), _NOW)
    assert outcome is UpsertOutcome.UPDATED


def test_an_unchanged_upsert_makes_no_write() -> None:
    # Req 15.2 says "make no write when every field is equal", and Req 15.11 is why that
    # matters: if a no-op bumped the last-upsert instant, "we re-received identical
    # metadata" would be indistinguishable from "the record actually changed", and a
    # stale registry would stop being detectable.
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(), _NOW)
    later = _NOW + dt.timedelta(hours=6)
    registry.upsert(_record(), later)
    entry = registry.get("CB0001")
    assert entry is not None
    assert entry.updated_at == _NOW  # untouched by the no-op


def test_a_changed_record_does_advance_the_instant() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(), _NOW)
    later = _NOW + dt.timedelta(hours=6)
    registry.upsert(_record(SiteName="Renamed"), later)
    entry = registry.get("CB0001")
    assert entry is not None
    assert entry.updated_at == later


def test_an_unchanged_upsert_keeps_the_stored_record() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(), _NOW)
    before = registry.get("CB0001")
    registry.upsert(_record(), _NOW + dt.timedelta(days=1))
    assert registry.get("CB0001") == before


# --- Req 15.11 last-upserted tracking -----------------------------------

def test_every_entry_records_its_upsert_instant() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(), _NOW)
    entry = registry.get("CB0001")
    assert entry is not None
    assert entry.updated_at == _NOW


def test_the_instant_is_supplied_not_read() -> None:
    # §2: the registry never reads a clock, so two registries given different instants
    # disagree by exactly that much
    early = InMemorySensorRegistryStore()
    early.upsert(_record(), _NOW)
    late = InMemorySensorRegistryStore()
    late.upsert(_record(), _NOW + dt.timedelta(days=30))
    early_entry = early.get("CB0001")
    late_entry = late.get("CB0001")
    assert early_entry is not None
    assert late_entry is not None
    assert late_entry.updated_at - early_entry.updated_at == dt.timedelta(days=30)


# --- Req 2.8 activity marking -------------------------------------------

def test_a_null_end_date_is_active() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(EndDate=None), _NOW)
    entry = registry.get("CB0001")
    assert entry is not None
    assert entry.active is True


def test_a_future_end_date_is_still_active() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(EndDate="2027-01-01T00:00:00Z"), _NOW)
    entry = registry.get("CB0001")
    assert entry is not None
    assert entry.active is True


def test_a_past_end_date_is_inactive() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(EndDate="2026-01-01T00:00:00Z"), _NOW)
    entry = registry.get("CB0001")
    assert entry is not None
    assert entry.active is False


def test_an_end_date_exactly_now_is_inactive() -> None:
    # Req 2.8 says "no later than the current instant", so the boundary is INACTIVE
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(EndDate="2026-07-01T12:00:00Z"), _NOW)
    entry = registry.get("CB0001")
    assert entry is not None
    assert entry.active is False


def test_an_inactive_site_is_still_resolvable_by_site_code() -> None:
    # Req 15.8: excluded from nearest-N, but its historical readings still need serving
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(EndDate="2026-01-01T00:00:00Z"), _NOW)
    assert registry.get("CB0001") is not None


def test_an_inactive_site_is_absent_from_list_active() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(EndDate="2026-01-01T00:00:00Z"), _NOW)
    assert not registry.list_active()


# --- Req 15.9 the changed-position warning ------------------------------

def test_a_changed_latitude_logs_one_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(), _NOW)
    configure_logging("info")
    registry.upsert(_record(Latitude="51.6000000"), _NOW)
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["event"] == "site_position_changed"
    assert warnings[0]["SiteCode"] == "CB0001"


def test_the_warning_names_both_positions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 15.9 wants BOTH, because the useful question is how far the site moved — one
    # position alone cannot answer it
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(Latitude="51.5074000"), _NOW)
    configure_logging("info")
    registry.upsert(_record(Latitude="51.6000000"), _NOW)
    warning = next(
        e for e in _events(capsys.readouterr().out) if e["level"] == "warning"
    )
    assert warning["previous_latitude"] == "51.5074000"
    assert warning["latitude"] == "51.6000000"
    assert "previous_longitude" in warning
    assert "longitude" in warning


def test_a_changed_longitude_also_warns(capsys: pytest.CaptureFixture[str]) -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(), _NOW)
    configure_logging("info")
    registry.upsert(_record(Longitude="-0.2000000"), _NOW)
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert len(warnings) == 1


def test_a_changed_name_does_not_warn(capsys: pytest.CaptureFixture[str]) -> None:
    # only a MOVED site invalidates historical spatial assumptions
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(), _NOW)
    configure_logging("info")
    registry.upsert(_record(SiteName="Renamed"), _NOW)
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert warnings == []


def test_a_first_upsert_does_not_warn(capsys: pytest.CaptureFixture[str]) -> None:
    # there is no previous position to have changed from
    configure_logging("info")
    InMemorySensorRegistryStore().upsert(_record(), _NOW)
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert warnings == []


def test_position_equality_is_compared_as_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 2.3 keeps coordinates as STRINGS for character-identical comparison. Comparing
    # as parsed floats would treat "51.5074000" and "51.50740" as equal, silently missing
    # a re-published precision change — so the check is on the stored text.
    registry = InMemorySensorRegistryStore()
    registry.upsert(_record(Latitude="51.5074000"), _NOW)
    configure_logging("info")
    registry.upsert(_record(Latitude="51.5074001"), _NOW)
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert len(warnings) == 1


# --- Req 15.4 the distance ----------------------------------------------

def test_distance_is_zero_on_identity() -> None:
    assert _great_circle_km(51.5, -0.12, 51.5, -0.12) == pytest.approx(0.0, abs=1e-9)


def test_distance_is_symmetric() -> None:
    there = _great_circle_km(51.5, -0.12, 48.85, 2.35)
    back = _great_circle_km(48.85, 2.35, 51.5, -0.12)
    assert there == pytest.approx(back)


def test_distance_matches_a_known_separation() -> None:
    # London to Paris is about 344 km great-circle; a wrong Earth radius or a degrees /
    # radians slip would be far outside this band
    london_to_paris = _great_circle_km(51.5074, -0.1278, 48.8566, 2.3522)
    assert 330.0 < london_to_paris < 350.0


def test_one_degree_of_latitude_is_about_111_km() -> None:
    # an independent check on the radius: a meridian degree is 2*pi*R/360
    assert _great_circle_km(0.0, 0.0, 1.0, 0.0) == pytest.approx(111.195, abs=0.1)


def test_the_earth_radius_is_the_documented_value() -> None:
    from aqm_ingestion.adapters.memory.adapters import _EARTH_RADIUS_KM

    assert _EARTH_RADIUS_KM == 6371.0088


# --- Req 15.5 / 15.6 nearest-N ------------------------------------------

def _site(code: str, lat: str, lon: str, end: str | None = None) -> SensorMetadataRecord:
    return _record(SiteCode=code, Latitude=lat, Longitude=lon, EndDate=end)


def _populated() -> InMemorySensorRegistryStore:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_site("NEAR", "51.5100000", "-0.1200000"), _NOW)
    registry.upsert(_site("MID", "51.6000000", "-0.1200000"), _NOW)
    registry.upsert(_site("FAR", "52.5000000", "-0.1200000"), _NOW)
    return registry


def test_nearest_orders_by_ascending_distance() -> None:
    result = _populated().nearest(lat=51.51, lon=-0.12, n=3, max_km=None)
    assert [site.entry.record.SiteCode for site in result] == ["NEAR", "MID", "FAR"]


def test_nearest_returns_fewer_than_n_when_fewer_exist() -> None:
    result = _populated().nearest(lat=51.51, lon=-0.12, n=10, max_km=None)
    assert len(result) == 3


def test_nearest_honours_n() -> None:
    result = _populated().nearest(lat=51.51, lon=-0.12, n=2, max_km=None)
    assert len(result) == 2


def test_the_radius_boundary_is_inclusive() -> None:
    registry = _populated()
    result = registry.nearest(lat=51.51, lon=-0.12, n=3, max_km=None)
    boundary = result[1].distance_km
    within = registry.nearest(lat=51.51, lon=-0.12, n=3, max_km=boundary)
    assert [site.entry.record.SiteCode for site in within] == ["NEAR", "MID"]


def test_sites_beyond_the_radius_are_excluded() -> None:
    result = _populated().nearest(lat=51.51, lon=-0.12, n=3, max_km=5.0)
    assert [site.entry.record.SiteCode for site in result] == ["NEAR"]


def test_a_distance_tie_is_broken_by_site_code() -> None:
    registry = InMemorySensorRegistryStore()
    # same position, so identical distances
    registry.upsert(_site("ZZZZ", "51.5000000", "-0.1000000"), _NOW)
    registry.upsert(_site("AAAA", "51.5000000", "-0.1000000"), _NOW)
    result = registry.nearest(lat=51.5, lon=-0.1, n=2, max_km=None)
    assert [site.entry.record.SiteCode for site in result] == ["AAAA", "ZZZZ"]


def test_the_tie_break_is_independent_of_insertion_order() -> None:
    for order in (("AAAA", "ZZZZ"), ("ZZZZ", "AAAA")):
        registry = InMemorySensorRegistryStore()
        for code in order:
            registry.upsert(_site(code, "51.5000000", "-0.1000000"), _NOW)
        result = registry.nearest(lat=51.5, lon=-0.1, n=2, max_km=None)
        assert [site.entry.record.SiteCode for site in result] == ["AAAA", "ZZZZ"]


def test_an_inactive_site_is_excluded_from_nearest() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_site("GONE", "51.5100000", "-0.1200000", end="2026-01-01T00:00:00Z"), _NOW)
    registry.upsert(_site("LIVE", "51.6000000", "-0.1200000"), _NOW)
    result = registry.nearest(lat=51.51, lon=-0.12, n=3, max_km=None)
    assert [site.entry.record.SiteCode for site in result] == ["LIVE"]


def test_an_empty_registry_returns_nothing() -> None:
    assert not InMemorySensorRegistryStore().nearest(
        lat=51.5, lon=-0.1, n=5, max_km=None
    )


def test_distances_are_reported_alongside_the_sites() -> None:
    result = _populated().nearest(lat=51.51, lon=-0.12, n=1, max_km=None)
    assert result[0].distance_km == pytest.approx(0.0, abs=0.2)
