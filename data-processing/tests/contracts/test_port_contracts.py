"""One shared behavioural suite per port, run against EVERY adapter of that port.

Requirements 14.10, 15.10, 16.8, 17.11, 28.10.

WHY PARAMETRISATION RATHER THAN TWO TEST FILES. The requirement is that an adapter swap cannot
change domain behaviour, and two separate suites cannot establish that: they would drift, and
the
in-memory adapter would quietly become a PARALLEL IMPLEMENTATION that the offline suite proves
things about while the cloud adapter does something else. Parametrising one suite over the
adapters
means every assertion below runs against each, so a divergence is a failure rather than a
discrepancy nobody looks for.

THE OFFLINE GUARANTEE IS WHAT SHAPES THE FIXTURE. The dev-environment steering requires the
whole
suite to pass with no credentials and no network beyond localhost, so a cloud adapter is only
included when its backing service is genuinely reachable — otherwise the parameter is SKIPPED,
not
failed. A skip is honest: it says "this adapter was not exercised here", which is exactly true
in
the offline suite, and the container-fenced job of task 28 is where it runs for real.

``archive_key`` and ``derive_archive_id`` live in ``ports/`` precisely so the memory and S3
archives cannot disagree about where a replay lands, and the RawArchive suite below asserts that
agreement directly rather than trusting it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator
from typing import Any, cast

import httpx
import pytest

from aqm_ingestion.adapters.memory import (
    InMemoryProfileStore,
    InMemoryRawArchive,
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
)
from aqm_ingestion.contract.records import SensorMetadataRecord
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.domain.profile import (
    RECOGNIZED_CONSENT_VERSIONS,
    Condition,
    SensitivityLevel,
    build_profile,
)
from aqm_ingestion.ports.archive_key import archive_key, derive_archive_id
from aqm_ingestion.ports.clock import FixedClock
from aqm_ingestion.ports.protocols import (
    ArchiveMeta,
    ForecastClient,
    MeteorologyProvider,
    ProfileStore,
    RawArchive,
    ReadingsStore,
    SensorRegistryStore,
    UpsertOutcome,
)

_T0 = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_CONSENT = next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS)))


def _reading(
    site_code: str = "SITE1",
    species: str = "PM25",
    *,
    instant: dt.datetime | None = None,
    corrected: float = 18.2,
    status: str = "R",
) -> CalibratedReading:
    return CalibratedReading(
        key=DedupKey(
            site_code=site_code,
            species=species,
            interval_start=instant or _T0,
            duration="PT1H",
        ),
        reported_value=24.1,
        corrected_value=corrected,
        units="ug.m-3" if species == "PM25" else "ppb",
        quality_flag=QualityFlag.CALIBRATED,
        confidence=Confidence.HIGH,
        calibration_strategy="rh_linear" if species == "PM25" else "identity",
        breakpoint_table="epa-2024-05-06",
        ratification_status=status,
        ingested_at=_T0,
        archive_id=f"archive-{site_code}-{species}",
        sub_index=68,
        band="Moderate",
    )


def _metadata(
    site_code: str = "SITE1", lat: float = 51.507, lon: float = -0.128
) -> SensorMetadataRecord:
    """A metadata record built from the GOLDEN payload.

    Built from the golden Service 1 payload so the suite cannot drift from the real contract.
    """
    import json

    from tests.unit.test_records import GOLDEN_METADATA_PAYLOAD

    fields = cast("dict[str, Any]", json.loads(GOLDEN_METADATA_PAYLOAD))
    location = cast("dict[str, Any]", fields["Location"])
    cast("dict[str, Any]", location["geometry"])["coordinates"] = [
        f"{lat:.7f}",
        f"{lon:.7f}",
    ]
    fields |= {
        "SiteCode": site_code,
        "Latitude": f"{lat:.7f}",
        "Longitude": f"{lon:.7f}",
    }
    return SensorMetadataRecord(**fields)


def _profile(user_id: str = "user-1", condition: Condition = Condition.ASTHMA) -> object:
    return build_profile(
        {
            "user_id": user_id,
            "condition": condition,
            "sensitivity_level": SensitivityLevel.STANDARD,
            "consent": {"version": _CONSENT, "given_at": _T0},
            "created_at": _T0,
            "updated_at": _T0,
        }
    )


# --- the adapter factories each suite is parametrised over ---------------
#
# A cloud adapter is included as a factory that SKIPS when its backing service is unreachable,
# so
# the offline suite reports it as not exercised rather than failing. The container-fenced job of
# task 28 provides the service and the same assertions then run for real.


def _require_endpoint() -> str:
    """The localhost endpoint the container-fenced job provides, or SKIP.

    Probing matters: boto3 builds a client LAZILY, so constructing an adapter succeeds with no
    credentials and no service, and the failure only surfaces on the first real call. A factory
    that merely constructed would therefore FAIL the offline suite rather than skipping it,
    which
    is the opposite of the offline guarantee. So the endpoint must be configured AND answer.
    """
    import os

    endpoint = os.environ.get("AQM_AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip(
            "no AQM_AWS_ENDPOINT_URL: cloud adapters run in the container-fenced job "
            "(task 28), not in the offline suite"
        )
    return endpoint


def _dynamodb_readings() -> ReadingsStore:
    pytest.importorskip("boto3")
    endpoint = _require_endpoint()
    from aqm_ingestion.adapters.dynamodb import DynamoDbReadingsStore

    store = DynamoDbReadingsStore(
        table_name="aqm-readings", clock=FixedClock(_T0), endpoint_url=endpoint
    )
    _probe(lambda: store.get(_reading("PROBE").key))
    return store


def _dynamodb_registry() -> SensorRegistryStore:
    pytest.importorskip("boto3")
    endpoint = _require_endpoint()
    from aqm_ingestion.adapters.dynamodb import DynamoDbSensorRegistryStore

    store = DynamoDbSensorRegistryStore(
        table_name="aqm-registry", endpoint_url=endpoint
    )
    _probe(lambda: store.get("PROBE"))
    return store


def _dynamodb_profiles() -> ProfileStore:
    pytest.importorskip("boto3")
    endpoint = _require_endpoint()
    from aqm_ingestion.adapters.dynamodb import DynamoDbProfileStore

    store = DynamoDbProfileStore(table_name="aqm-profiles", endpoint_url=endpoint)
    _probe(lambda: store.get("PROBE"))
    return store


def _s3_archive() -> RawArchive:
    pytest.importorskip("boto3")
    endpoint = _require_endpoint()
    from aqm_ingestion.adapters.s3 import S3RawArchive

    store = S3RawArchive(bucket="aqm-raw", endpoint_url=endpoint)
    _probe(lambda: store.read("probe"))
    return store


def _probe(call: Callable[[], object]) -> None:
    """Make one real call, skipping when the service is not there.

    A read, never a write: probing must not leave anything behind in a store the suite is about
    to make assertions over.
    """
    try:
        call()
    except Exception as unreachable:
        pytest.skip(f"cloud adapter not reachable: {type(unreachable).__name__}")


_READINGS_ADAPTERS: tuple[tuple[str, Callable[[], ReadingsStore]], ...] = (
    ("memory", lambda: InMemoryReadingsStore(clock=FixedClock(_T0))),
    ("dynamodb", _dynamodb_readings),
)
_REGISTRY_ADAPTERS: tuple[tuple[str, Callable[[], SensorRegistryStore]], ...] = (
    ("memory", InMemorySensorRegistryStore),
    ("dynamodb", _dynamodb_registry),
)
_PROFILE_ADAPTERS: tuple[tuple[str, Callable[[], ProfileStore]], ...] = (
    ("memory", InMemoryProfileStore),
    ("dynamodb", _dynamodb_profiles),
)
_ARCHIVE_ADAPTERS: tuple[tuple[str, Callable[[], RawArchive]], ...] = (
    ("memory", InMemoryRawArchive),
    ("s3", _s3_archive),
)


@pytest.fixture(params=_READINGS_ADAPTERS, ids=lambda entry: entry[0])
def readings(request: pytest.FixtureRequest) -> Iterator[ReadingsStore]:
    """Every ReadingsStore adapter (Requirement 14.10)."""
    yield request.param[1]()


@pytest.fixture(params=_REGISTRY_ADAPTERS, ids=lambda entry: entry[0])
def registry(request: pytest.FixtureRequest) -> Iterator[SensorRegistryStore]:
    """Every SensorRegistryStore adapter (Requirement 15.10)."""
    yield request.param[1]()


@pytest.fixture(params=_PROFILE_ADAPTERS, ids=lambda entry: entry[0])
def profiles(request: pytest.FixtureRequest) -> Iterator[ProfileStore]:
    """Every ProfileStore adapter (Requirement 17.11)."""
    yield request.param[1]()


@pytest.fixture(params=_ARCHIVE_ADAPTERS, ids=lambda entry: entry[0])
def archive(request: pytest.FixtureRequest) -> Iterator[RawArchive]:
    """Every RawArchive adapter (Requirement 16.8)."""
    yield request.param[1]()


# --- ReadingsStore: Requirement 14 ------------------------------------

def test_a_stored_reading_is_returned_by_key(readings: ReadingsStore) -> None:
    reading = _reading()
    readings.put(reading)
    assert readings.get(reading.key) == reading


def test_an_absent_key_returns_none(readings: ReadingsStore) -> None:
    assert readings.get(_reading("NOWHERE").key) is None


def test_storing_the_same_reading_twice_is_idempotent(readings: ReadingsStore) -> None:
    # Req 14.2: a second identical write leaves one Reading, whatever the adapter.
    reading = _reading()
    readings.put(reading)
    readings.put(reading)
    result = readings.query_window(
        site_code="SITE1",
        species=None,
        start=_T0 - dt.timedelta(hours=1),
        end=_T0 + dt.timedelta(hours=1),
    )
    assert len(result.readings) == 1


def test_a_ratified_reading_supersedes_a_provisional_one(readings: ReadingsStore) -> None:
    # Req 14.2 defers to Req 7: status OUTRANKS magnitude, so a ratified 3 replaces a
    # provisional 10. This is the behaviour a conditional write must reproduce exactly.
    readings.put(_reading(corrected=10.0, status="P"))
    readings.put(_reading(corrected=3.0, status="R"))
    stored = readings.get(_reading().key)
    assert stored is not None
    assert stored.corrected_value == 3.0
    assert stored.ratification_status == "R"


def test_a_provisional_reading_never_displaces_a_ratified_one(
    readings: ReadingsStore,
) -> None:
    # The other direction, which a last-write-wins adapter would fail.
    readings.put(_reading(corrected=3.0, status="R"))
    readings.put(_reading(corrected=10.0, status="P"))
    stored = readings.get(_reading().key)
    assert stored is not None
    assert stored.corrected_value == 3.0


def test_a_window_query_is_half_open(readings: ReadingsStore) -> None:
    readings.put(_reading(instant=_T0))
    inside = readings.query_window(
        site_code="SITE1", species=None, start=_T0, end=_T0 + dt.timedelta(hours=1)
    )
    outside = readings.query_window(
        site_code="SITE1",
        species=None,
        start=_T0 + dt.timedelta(hours=1),
        end=_T0 + dt.timedelta(hours=2),
    )
    assert len(inside.readings) == 1
    # The END is exclusive, so a reading exactly at the end bound is not returned.
    assert readings.query_window(
        site_code="SITE1", species=None, start=_T0 - dt.timedelta(hours=1), end=_T0
    ).readings == ()
    assert outside.readings == ()


def test_a_window_query_filters_by_species(readings: ReadingsStore) -> None:
    readings.put(_reading(species="PM25"))
    readings.put(_reading(species="NO2"))
    result = readings.query_window(
        site_code="SITE1",
        species=frozenset({"NO2"}),
        start=_T0 - dt.timedelta(hours=1),
        end=_T0 + dt.timedelta(hours=1),
    )
    assert {r.key.species for r in result.readings} == {"NO2"}


def test_a_window_query_is_scoped_to_its_site(readings: ReadingsStore) -> None:
    readings.put(_reading(site_code="SITE1"))
    readings.put(_reading(site_code="SITE2"))
    result = readings.query_window(
        site_code="SITE1",
        species=None,
        start=_T0 - dt.timedelta(hours=1),
        end=_T0 + dt.timedelta(hours=1),
    )
    assert {r.key.site_code for r in result.readings} == {"SITE1"}


def test_the_latest_per_species_is_the_newest(readings: ReadingsStore) -> None:
    readings.put(_reading(instant=_T0 - dt.timedelta(hours=2)))
    readings.put(_reading(instant=_T0))
    latest = readings.latest_per_species(
        ["SITE1"], not_before=_T0 - dt.timedelta(hours=6)
    )
    assert [r.key.interval_start for r in latest["SITE1"]] == [_T0]


def test_the_latest_per_species_excludes_what_is_too_old(
    readings: ReadingsStore,
) -> None:
    readings.put(_reading(instant=_T0 - dt.timedelta(days=10)))
    latest = readings.latest_per_species(
        ["SITE1"], not_before=_T0 - dt.timedelta(hours=1)
    )
    assert latest.get("SITE1", ()) == ()


def test_an_inverted_window_is_refused(readings: ReadingsStore) -> None:
    # §5: an inverted window is a caller bug, and returning nothing would hide it.
    with pytest.raises(ValueError, match=r"window|start|end"):
        readings.query_window(
            site_code="SITE1", species=None, start=_T0, end=_T0 - dt.timedelta(hours=1)
        )


# --- SensorRegistryStore: Requirement 15 -----------------------------

def test_an_upserted_site_is_returned(registry: SensorRegistryStore) -> None:
    registry.upsert(_metadata(), at=_T0)
    entry = registry.get("SITE1")
    assert entry is not None
    assert entry.record.SiteCode == "SITE1"


def test_an_absent_site_returns_none(registry: SensorRegistryStore) -> None:
    assert registry.get("NOWHERE") is None


def test_a_first_upsert_reports_created(registry: SensorRegistryStore) -> None:
    assert registry.upsert(_metadata(), at=_T0) is UpsertOutcome.CREATED


def test_an_identical_upsert_reports_unchanged(registry: SensorRegistryStore) -> None:
    registry.upsert(_metadata(), at=_T0)
    assert registry.upsert(_metadata(), at=_T0) is UpsertOutcome.UNCHANGED


def test_an_identical_upsert_does_not_advance_the_instant(
    registry: SensorRegistryStore,
) -> None:
    # Req 15.2 and 15.11 together: a no-op write must not bump updated_at, or a stale registry
    # stops being detectable. The behaviour every adapter has to reproduce.
    registry.upsert(_metadata(), at=_T0)
    first = registry.get("SITE1")
    registry.upsert(_metadata(), at=_T0 + dt.timedelta(days=1))
    second = registry.get("SITE1")
    assert first is not None
    assert second is not None
    assert second.updated_at == first.updated_at


def test_a_changed_upsert_reports_updated(registry: SensorRegistryStore) -> None:
    registry.upsert(_metadata(), at=_T0)
    changed = _metadata(lat=51.600)
    assert registry.upsert(changed, at=_T0 + dt.timedelta(days=1)) is UpsertOutcome.UPDATED


def test_the_nearest_site_is_returned_closest_first(
    registry: SensorRegistryStore,
) -> None:
    registry.upsert(_metadata("FAR", lat=51.560), at=_T0)
    registry.upsert(_metadata("NEAR", lat=51.510), at=_T0)
    nearest = registry.nearest(51.507, -0.128, 2, 50.0)
    assert [site.entry.record.SiteCode for site in nearest] == ["NEAR", "FAR"]


def test_the_nearest_site_respects_the_radius(registry: SensorRegistryStore) -> None:
    registry.upsert(_metadata("PARIS", lat=48.857, lon=2.352), at=_T0)
    assert registry.nearest(51.507, -0.128, 3, 10.0) == []


def test_the_nearest_site_respects_n(registry: SensorRegistryStore) -> None:
    for index in range(4):
        registry.upsert(_metadata(f"S{index}", lat=51.507 + index * 0.002), at=_T0)
    assert len(registry.nearest(51.507, -0.128, 2, 50.0)) == 2


def test_the_active_list_is_ordered_by_site_code(registry: SensorRegistryStore) -> None:
    # §2: the order reaches output, so it must be defined for every adapter.
    registry.upsert(_metadata("ZZZ"), at=_T0)
    registry.upsert(_metadata("AAA"), at=_T0)
    codes = [entry.record.SiteCode for entry in registry.list_active()]
    assert codes == sorted(codes)


# --- ProfileStore: Requirement 17.11 --------------------------------

def test_a_stored_profile_is_returned(profiles: ProfileStore) -> None:
    stored = profiles.put(_profile())  # type: ignore[arg-type]
    assert profiles.get("user-1") == stored


def test_an_absent_profile_returns_none(profiles: ProfileStore) -> None:
    assert profiles.get("nobody") is None


def test_a_profile_write_replaces_the_previous_one(profiles: ProfileStore) -> None:
    profiles.put(_profile(condition=Condition.ASTHMA))  # type: ignore[arg-type]
    profiles.put(_profile(condition=Condition.COPD))  # type: ignore[arg-type]
    stored = profiles.get("user-1")
    assert stored is not None
    assert stored.condition is Condition.COPD


def test_a_deleted_profile_is_gone(profiles: ProfileStore) -> None:
    profiles.put(_profile())  # type: ignore[arg-type]
    profiles.delete("user-1")
    assert profiles.get("user-1") is None


def test_deleting_an_absent_profile_is_not_an_error(profiles: ProfileStore) -> None:
    # Req 17.8's erasure is idempotent, so a second request must confirm rather than raise.
    profiles.delete("nobody")


def test_one_users_profile_is_invisible_to_another(profiles: ProfileStore) -> None:
    # Req 17.10 at the STORE, for every adapter: a mis-keyed cloud table would fail here.
    profiles.put(_profile("user-a"))  # type: ignore[arg-type]
    assert profiles.get("user-b") is None


# --- RawArchive: Requirement 16 -------------------------------------

def test_archived_bytes_read_back_byte_identically(archive: RawArchive) -> None:
    payload = b'{"deliberately": "not normalised", "spacing":   1}'
    meta = ArchiveMeta(ingested_at=_T0, transport="mqtt", source="aqm/sensors/X/data")
    identifier = archive.write(payload, meta)
    assert archive.read(identifier) == payload


def test_unparseable_bytes_are_archived_unchanged(archive: RawArchive) -> None:
    # Req 16.1 archives BEFORE parsing, so the bytes worth keeping most are the ones that do not
    # parse — an adapter that validated on write would lose exactly those.
    payload = b"\x00\x01 not json at all \xff"
    meta = ArchiveMeta(ingested_at=_T0, transport="feed", source="window")
    identifier = archive.write(payload, meta)
    assert archive.read(identifier) == payload


def test_re_archiving_the_same_payload_is_idempotent(archive: RawArchive) -> None:
    # Req 27.6: the identifier is DERIVED from the payload and meta, so replay reproduces it.
    payload = b'{"a": 1}'
    meta = ArchiveMeta(ingested_at=_T0, transport="mqtt", source="topic")
    assert archive.write(payload, meta) == archive.write(payload, meta)


def test_a_different_transport_yields_a_different_identifier(
    archive: RawArchive,
) -> None:
    # Req 4.7 keeps the ingestion path recoverable, which Property 39 relies on: the push and
    # pull paths MUST differ here, and an adapter that ignored the meta would fail.
    payload = b'{"a": 1}'
    push = archive.write(
        payload, ArchiveMeta(ingested_at=_T0, transport="mqtt", source="topic")
    )
    pull = archive.write(
        payload, ArchiveMeta(ingested_at=_T0, transport="feed", source="window")
    )
    assert push != pull


def test_every_adapter_derives_the_same_identifier(archive: RawArchive) -> None:
    # THE AGREEMENT THAT MATTERS MOST. derive_archive_id lives in ports/ so the memory and S3
    # archives cannot disagree about where a replay lands; this asserts each adapter uses it
    # rather than inventing its own scheme, which is what Req 16.8's shared suite is for.
    payload = b'{"a": 1}'
    meta = ArchiveMeta(ingested_at=_T0, transport="mqtt", source="topic")
    assert archive.write(payload, meta) == derive_archive_id(payload, meta)


def test_the_archive_key_orders_lexically_by_time(archive: RawArchive) -> None:
    # Req 16.5: zero-padded so LEXICAL order follows CHRONOLOGICAL order, which is what makes a
    # time range enumerable without a full scan. A property of the shared derivation, asserted
    # here so an adapter cannot substitute its own layout.
    earlier = archive_key(_T0, "id-a")
    later = archive_key(_T0 + dt.timedelta(days=1), "id-a")
    assert earlier < later


def test_reading_an_unknown_identifier_raises(archive: RawArchive) -> None:
    # The PORT declares read(...) -> bytes, so an unknown identifier is a broken invariant
    # rather
    # than a normal branch: this archive is append-only, so a caller holding an id it cannot
    # read
    # has hit a fault. This assertion is what caught my S3 draft returning None instead — the
    # first real divergence the shared suite found, and exactly what it is for.
    with pytest.raises(KeyError):
        archive.read("no-such-archive-id")


def test_the_archive_exposes_only_write_and_read(archive: RawArchive) -> None:
    # Req 16.3's append-only guarantee is enforced by the PORT SHAPE: there is no delete or
    # overwrite to call, for any adapter.
    public = {name for name in dir(archive) if not name.startswith("_")}
    assert "delete" not in public
    assert "remove" not in public
    assert {"write", "read"} <= public


# ======================================================================================
# ForecastClient — the shared contract (Requirements 24.3, 24.4, 24.7, 24.9)
# ======================================================================================
#
# THIS IS THE MOST VALUABLE PARAMETRISATION IN THE FILE, because the two adapters here differ
# more than any other pair: the in-memory one answers from a dict, the HTTP one from a network
# provider. Requirement 24.4's degradation contract is what the enricher above them leans on,
# and
# it is the behaviour an HTTP adapter is most likely to get wrong by raising instead. The HTTP
# adapter runs OFFLINE against a canned transport, so unlike the cloud stores these parameters
# do
# not skip — both are exercised on every run.


def _memory_forecast_unconfigured() -> ForecastClient:
    from aqm_ingestion.adapters.memory.adapters import InMemoryForecastClient

    return InMemoryForecastClient()


def _http_forecast_unreachable() -> ForecastClient:
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    return HttpForecastClient(
        base_url="https://forecast.invalid",
        credential="unused-in-this-path",
        transport=httpx.MockTransport(refuse),
    )


_UNAVAILABLE_FORECASTS = (
    ("memory", _memory_forecast_unconfigured),
    ("http", _http_forecast_unreachable),
)


@pytest.fixture(params=_UNAVAILABLE_FORECASTS, ids=lambda entry: entry[0])
def unavailable_forecast(request: pytest.FixtureRequest) -> Iterator[ForecastClient]:
    """A ForecastClient that cannot answer, however each adapter fails to."""
    yield request.param[1]()


def test_an_unavailable_forecast_degrades_instead_of_raising(
    unavailable_forecast: ForecastClient,
) -> None:
    # Req 24.4: serve the response WITHOUT forecast values and do NOT fail the request. An
    # adapter that raised would hand the enricher an exception it has no way to answer, since
    # the
    # requirement says the request still succeeds.
    result = unavailable_forecast.forecast(51.507, -0.128)

    assert result.degraded is True
    assert result.values == {}


def test_an_unavailable_pollen_lookup_degrades_instead_of_raising(
    unavailable_forecast: ForecastClient,
) -> None:
    result = unavailable_forecast.pollen(51.507, -0.128)

    assert result.degraded is True
    assert result.values == {}


def test_a_degraded_forecast_is_repeatable(unavailable_forecast: ForecastClient) -> None:
    # §2: two calls in the same state give the same answer. A retry counter or a cached failure
    # inside an adapter would break this, and Req 24.4 gives no licence for either.
    first = unavailable_forecast.forecast(51.507, -0.128)
    second = unavailable_forecast.forecast(51.507, -0.128)

    assert first == second


def test_every_forecast_adapter_reports_a_provider_when_it_answers() -> None:
    # Req 24.3: the provider identifier travels with EVERY forecast. Asserted over the ANSWERING
    # path of both adapters, which needs each configured its own way — so this test builds them
    # rather than taking the unavailable fixture.
    from aqm_ingestion.adapters.forecast import HttpForecastClient
    from aqm_ingestion.adapters.memory.adapters import InMemoryForecastClient

    memory = InMemoryForecastClient()
    memory.set_forecast(51.507, -0.128, {"PM25": 42.0})

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"values": {"PM25": 42.0}})

    http = HttpForecastClient(
        base_url="https://forecast.example",
        credential="unused-in-this-path",
        transport=httpx.MockTransport(answer),
    )

    for client in (memory, http):
        result = client.forecast(51.507, -0.128)
        assert result.degraded is False
        assert result.provider, f"{type(client).__name__} answered without naming a provider"
        assert result.values["PM25"] == 42.0


# ======================================================================================
# MeteorologyProvider — the shared contract (Requirement 8.4)
# ======================================================================================


def _memory_meteorology_empty() -> MeteorologyProvider:
    from aqm_ingestion.adapters.memory.adapters import InMemoryMeteorologyProvider

    return InMemoryMeteorologyProvider()


def _http_meteorology_unreachable() -> MeteorologyProvider:
    from aqm_ingestion.adapters.forecast import HttpMeteorologyProvider

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    return HttpMeteorologyProvider(
        base_url="https://met.invalid",
        credential="unused-in-this-path",
        transport=httpx.MockTransport(refuse),
    )


_UNAVAILABLE_METEOROLOGY = (
    ("memory", _memory_meteorology_empty),
    ("http", _http_meteorology_unreachable),
)


@pytest.fixture(params=_UNAVAILABLE_METEOROLOGY, ids=lambda entry: entry[0])
def unavailable_meteorology(request: pytest.FixtureRequest) -> Iterator[MeteorologyProvider]:
    """A MeteorologyProvider that cannot answer."""
    yield request.param[1]()


def test_an_unavailable_observation_returns_none_rather_than_raising(
    unavailable_meteorology: MeteorologyProvider,
) -> None:
    # Req 8.4's precedence ENDS in "no RH at all", and Req 8.6 continues uncalibrated. A raise
    # would fail an ingestion the requirements say proceeds — so None is the contract, for every
    # adapter, however it came to have no answer.
    assert unavailable_meteorology.observation("AQM1", _T0) is None
