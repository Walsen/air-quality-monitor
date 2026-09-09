"""Unit tests for the in-memory adapters (task 3.3).

These fakes carry the whole offline suite (Req 28.5), so they must be
DETERMINISTIC: defined iteration order, derived identifiers, and no clock of their
own — an instant always arrives as a parameter (§2).

Requirements 14.9, 15.10, 16.8, 17.11, 18.8, 24.9 each ask for a local adapter
sufficient to exercise its port without a cloud account.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.adapters.memory import (
    InMemoryForecastClient,
    InMemoryMeteorologyProvider,
    InMemoryProfileStore,
    InMemoryRawArchive,
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
    LocalAuthenticator,
    ScriptedFeedClient,
    ScriptedMqttTransport,
)
from aqm_ingestion.contract.records import SensorMetadataRecord
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.ports.clock import FixedClock
from aqm_ingestion.ports.protocols import (
    ArchiveMeta,
    Authenticator,
    AuthRejectedError,
    FeedClient,
    ForecastClient,
    MeteorologyProvider,
    MqttTransport,
    ProfileStore,
    RawArchive,
    ReadingsStore,
    RejectionCategory,
    SensorRegistryStore,
    UpsertOutcome,
    UserProfile,
)
from tests.unit.test_records import GOLDEN_METADATA_PAYLOAD

_T0 = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _key(site: str = "CB0001", species: str = "PM25", hour: int = 11) -> DedupKey:
    return DedupKey(
        site_code=site,
        species=species,
        interval_start=dt.datetime(2026, 7, 1, hour, tzinfo=dt.UTC),
        duration="PT1H",
    )


def _reading(key: DedupKey | None = None, corrected: float = 12.0) -> CalibratedReading:
    return CalibratedReading(
        key=key or _key(),
        reported_value=11.5,
        corrected_value=corrected,
        units="ug.m-3",
        quality_flag=QualityFlag.CALIBRATED,
        confidence=Confidence.HIGH,
        calibration_strategy="identity",
        breakpoint_table="epa",
        ratification_status="R",
        ingested_at=_T0,
        archive_id="archive-1",
    )


def _metadata(**overrides: object) -> SensorMetadataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_METADATA_PAYLOAD)) | overrides
    return SensorMetadataRecord(**fields)


# --- every adapter satisfies its port ------------------------------------

def test_adapters_satisfy_their_ports() -> None:
    assert isinstance(InMemoryReadingsStore(clock=FixedClock(_T0)), ReadingsStore)
    assert isinstance(InMemorySensorRegistryStore(), SensorRegistryStore)
    assert isinstance(InMemoryRawArchive(), RawArchive)
    assert isinstance(InMemoryProfileStore(), ProfileStore)
    assert isinstance(InMemoryMeteorologyProvider(), MeteorologyProvider)
    assert isinstance(InMemoryForecastClient(), ForecastClient)
    assert isinstance(ScriptedMqttTransport(), MqttTransport)
    assert isinstance(ScriptedFeedClient(), FeedClient)
    assert isinstance(LocalAuthenticator({}), Authenticator)


# --- ReadingsStore -------------------------------------------------------

def test_put_then_get_returns_the_reading() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_T0))
    reading = _reading()
    store.put(reading)
    assert store.get(reading.key) == reading


def test_put_is_idempotent_per_dedup_key() -> None:
    # One key holds one reading. This test originally asserted "last write wins", which
    # Requirement 14.2 forbids: a write for an existing Dedup_Key RESOLVES per
    # Requirement 7, so with equal ratification status the GREATER reported value prevails
    # and the reading is marked disputed — a blind overwrite would have lost a ratified
    # value in the same situation.
    store = InMemoryReadingsStore(clock=FixedClock(_T0))
    store.put(_reading(corrected=10.0))
    store.put(_reading(corrected=20.0))  # same key
    result = store.query_window("CB0001", None, _T0 - dt.timedelta(days=1), _T0)
    assert len(result.readings) == 1
    written = (_reading(corrected=10.0), _reading(corrected=20.0))
    assert result.readings[0].reported_value == max(
        reading.reported_value for reading in written
    )


def test_get_missing_key_returns_none() -> None:
    assert InMemoryReadingsStore(clock=FixedClock(_T0)).get(_key()) is None


def test_query_window_is_half_open() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_T0))
    for hour in (9, 10, 11):
        store.put(_reading(_key(hour=hour)))
    start = dt.datetime(2026, 7, 1, 9, tzinfo=dt.UTC)
    end = dt.datetime(2026, 7, 1, 11, tzinfo=dt.UTC)
    hours = [
        r.key.interval_start.hour
        for r in store.query_window("CB0001", None, start, end).readings
    ]
    assert hours == [9, 10]  # 11 excluded


def test_query_window_filters_by_species() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_T0))
    store.put(_reading(_key(species="PM25")))
    store.put(_reading(_key(species="NO2")))
    result = store.query_window(
        "CB0001", frozenset({"NO2"}), _T0 - dt.timedelta(days=1), _T0
    )
    assert [r.key.species for r in result.readings] == ["NO2"]


def test_query_window_orders_deterministically() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_T0))
    for hour in (11, 9, 10):
        store.put(_reading(_key(hour=hour)))
    result = store.query_window("CB0001", None, _T0 - dt.timedelta(days=1), _T0)
    hours = [r.key.interval_start.hour for r in result.readings]
    assert hours == sorted(hours)  # defined order regardless of insertion (§2)


def test_query_window_reports_truncation() -> None:
    store = InMemoryReadingsStore(max_window_readings=2, clock=FixedClock(_T0))
    for hour in (8, 9, 10):
        store.put(_reading(_key(hour=hour)))
    result = store.query_window("CB0001", None, _T0 - dt.timedelta(days=1), _T0)
    assert len(result.readings) == 2
    assert result.truncated is True  # never a silent partial set (Req 14.8)


def test_query_window_not_truncated_when_within_cap() -> None:
    store = InMemoryReadingsStore(max_window_readings=10, clock=FixedClock(_T0))
    store.put(_reading())
    result = store.query_window("CB0001", None, _T0 - dt.timedelta(days=1), _T0)
    assert result.truncated is False


def test_put_batch_stores_every_reading() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_T0))
    store.put_batch([_reading(_key(hour=9)), _reading(_key(hour=10))])
    result = store.query_window("CB0001", None, _T0 - dt.timedelta(days=1), _T0)
    assert len(result.readings) == 2


def test_latest_per_species_returns_the_newest() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_T0))
    store.put(_reading(_key(species="PM25", hour=9), corrected=1.0))
    store.put(_reading(_key(species="PM25", hour=11), corrected=2.0))
    latest = store.latest_per_species(["CB0001"], _T0 - dt.timedelta(days=1))
    assert [r.corrected_value for r in latest["CB0001"]] == [2.0]


def test_latest_per_species_honours_not_before() -> None:
    store = InMemoryReadingsStore(clock=FixedClock(_T0))
    store.put(_reading(_key(hour=9)))
    latest = store.latest_per_species(["CB0001"], dt.datetime(2026, 7, 1, 10, tzinfo=dt.UTC))
    assert latest.get("CB0001", []) == []  # too old to count as fresh


# --- RawArchive ----------------------------------------------------------

def test_archive_round_trips_bytes_exactly() -> None:
    archive = InMemoryRawArchive()
    payload = b'{"Species":"PM25"}'
    archive_id = archive.write(payload, ArchiveMeta(_T0, "mqtt", "aqm/london/data"))
    assert archive.read(archive_id) == payload  # byte-for-byte (Req 1.10)


def test_archive_id_is_derived_not_random() -> None:
    # determinism (§2): the same payload and meta must archive to the same id, so a
    # replay does not invent a new identifier
    meta = ArchiveMeta(_T0, "mqtt", "aqm/london/data")
    first = InMemoryRawArchive().write(b"payload", meta)
    second = InMemoryRawArchive().write(b"payload", meta)
    assert first == second


def test_different_payloads_get_different_ids() -> None:
    meta = ArchiveMeta(_T0, "mqtt", "aqm/london/data")
    archive = InMemoryRawArchive()
    assert archive.write(b"a", meta) != archive.write(b"b", meta)


def test_reading_an_unknown_archive_id_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        InMemoryRawArchive().read("nope")


# --- SensorRegistryStore -------------------------------------------------

def test_upsert_reports_created_then_unchanged() -> None:
    registry = InMemorySensorRegistryStore()
    record = _metadata()
    assert registry.upsert(record, _T0) is UpsertOutcome.CREATED
    assert registry.upsert(record, _T0) is UpsertOutcome.UNCHANGED


def test_upsert_reports_updated_on_a_change() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_metadata(), _T0)
    assert registry.upsert(_metadata(SiteName="Renamed"), _T0) is UpsertOutcome.UPDATED


def test_site_with_past_enddate_is_inactive() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(
        _metadata(StartDate="2024-01-01T00:00:00Z", EndDate="2025-01-01T00:00:00Z"), _T0
    )
    entry = registry.get("CB0001")
    assert entry is not None
    assert entry.active is False  # Req 2.8


def test_site_with_null_enddate_is_active() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_metadata(EndDate=None), _T0)
    entry = registry.get("CB0001")
    assert entry is not None and entry.active is True


def test_list_active_excludes_inactive_and_is_ordered() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_metadata(SiteCode="CB0002"), _T0)
    registry.upsert(_metadata(SiteCode="CB0001"), _T0)
    registry.upsert(
        _metadata(SiteCode="CB0003", EndDate="2025-01-01T00:00:00Z"), _T0
    )
    codes = [entry.record.SiteCode for entry in registry.list_active()]
    assert codes == ["CB0001", "CB0002"]  # sorted, inactive omitted


def test_nearest_returns_closest_first() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_metadata(SiteCode="FAR", Latitude="-18.0000000",
                              Location={"type": "Feature", "geometry": {
                                  "type": "Point",
                                  "coordinates": ["-18.0000000", "-66.1523456"]}}), _T0)
    registry.upsert(_metadata(SiteCode="NEAR"), _T0)
    nearest = registry.nearest(-17.3912345, -66.1523456, n=2, max_km=None)
    assert [site.entry.record.SiteCode for site in nearest] == ["NEAR", "FAR"]


def test_nearest_respects_max_km() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_metadata(), _T0)
    assert registry.nearest(0.0, 0.0, n=5, max_km=1.0) == []


def test_nearest_excludes_inactive_sites() -> None:
    registry = InMemorySensorRegistryStore()
    registry.upsert(_metadata(EndDate="2025-01-01T00:00:00Z"), _T0)
    assert registry.nearest(-17.3912345, -66.1523456, n=5, max_km=None) == []


# --- ProfileStore --------------------------------------------------------

def test_profile_put_get_delete() -> None:
    store = InMemoryProfileStore()
    profile = UserProfile(user_id="u-1", updated_at=_T0)
    assert store.put(profile) == profile
    assert store.get("u-1") == profile
    store.delete("u-1")
    assert store.get("u-1") is None


def test_deleting_an_absent_profile_is_not_an_error() -> None:
    InMemoryProfileStore().delete("nobody")


# --- MeteorologyProvider / ForecastClient -------------------------------

def test_meteorology_returns_a_configured_observation() -> None:
    provider = InMemoryMeteorologyProvider()
    provider.set("CB0001", _T0, temperature_k=290.0, pressure_pa=101325.0)
    observation = provider.observation("CB0001", _T0)
    assert observation is not None
    assert observation.temperature_k == 290.0


def test_meteorology_returns_none_when_unknown() -> None:
    assert InMemoryMeteorologyProvider().observation("CB0001", _T0) is None


def test_forecast_defaults_to_degraded_when_unconfigured() -> None:
    # a missing forecast must be disclosed, never presented as fact
    result = InMemoryForecastClient().forecast(-17.39, -66.15)
    assert result.degraded is True


def test_configured_forecast_is_not_degraded() -> None:
    client = InMemoryForecastClient()
    client.set_forecast(-17.39, -66.15, {"pm25": 12.0})
    assert client.forecast(-17.39, -66.15).degraded is False


# --- MqttTransport / FeedClient -----------------------------------------

def test_scripted_mqtt_replays_in_order() -> None:
    transport = ScriptedMqttTransport(
        [("aqm/sensors/CB0001/data", b"1", 1), ("aqm/sensors/CB0002/data", b"2", 2)]
    )
    transport.subscribe("aqm/sensors/+/data")
    assert [payload for _topic, payload, _tag in transport.messages()] == [b"1", b"2"]


def test_scripted_mqtt_records_acknowledgements() -> None:
    # Requirement 4.6 needs acknowledgement to be observable, which is why the port carries
    # a delivery tag and a separate acknowledge call
    transport = ScriptedMqttTransport([("aqm/sensors/CB0001/data", b"1", 9)])
    for _topic, _payload, tag in transport.messages():
        transport.acknowledge(tag)
    assert transport.acknowledged == [9]


def test_scripted_mqtt_records_the_subscription() -> None:
    transport = ScriptedMqttTransport()
    transport.subscribe("aqm/sensors/+/data")
    assert transport.subscriptions == ["aqm/sensors/+/data"]


def test_scripted_feed_returns_the_configured_payload() -> None:
    feed = ScriptedFeedClient({(_T0, _T0 + dt.timedelta(hours=1)): b"[]"})
    window = feed.fetch_data(_T0, _T0 + dt.timedelta(hours=1), frozenset({"PM25"}))
    assert window == b"[]"


def test_scripted_feed_returns_empty_array_when_unscripted() -> None:
    assert ScriptedFeedClient().fetch_data(_T0, _T0, frozenset()) == b"[]"


def test_scripted_feed_serves_a_sensor_list() -> None:
    # Requirement 5.7's /ListSensors, which the original port could not express
    feed = ScriptedFeedClient(sensors=b'[{"SiteCode": "CB0001"}]')
    assert feed.fetch_sensors() == b'[{"SiteCode": "CB0001"}]'
    assert feed.sensor_calls == 1


def test_scripted_feed_holds_no_credential() -> None:
    # Req 5.2 / §7: the port carries none, so the offline suite needs none either
    assert not [name for name in vars(ScriptedFeedClient()) if "key" in name.lower()]


# --- LocalAuthenticator -------------------------------------------------

def test_configured_credential_resolves_to_its_identity() -> None:
    auth = LocalAuthenticator({"dev-token": "u-1"})
    assert auth.verify("dev-token").user_id == "u-1"


def test_missing_credential_is_rejected_as_missing() -> None:
    auth = LocalAuthenticator({"dev-token": "u-1"})
    with pytest.raises(AuthRejectedError) as caught:
        auth.verify("")
    assert caught.value.category is RejectionCategory.MISSING


def test_unknown_credential_is_rejected_as_invalid_signature() -> None:
    auth = LocalAuthenticator({"dev-token": "u-1"})
    with pytest.raises(AuthRejectedError) as caught:
        auth.verify("wrong")
    assert caught.value.category is RejectionCategory.INVALID_SIGNATURE


def test_rejection_never_echoes_the_credential() -> None:
    auth = LocalAuthenticator({"dev-token": "u-1"})
    with pytest.raises(AuthRejectedError) as caught:
        auth.verify("super-secret-value")
    assert "super-secret-value" not in str(caught.value)  # §7
