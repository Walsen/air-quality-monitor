"""The readings + sensor-registry seeding loader (Task 10, Requirements 10.3, 10.4).

Feature: personal-diary-memory.

The association job (``jobs/association.py``) derives a Learned_Threshold by correlating a
user's diary against a PERSISTED exposure history — the readings store, with sites resolved
through the sensor registry. In a fresh deployment both are empty, so the derivation finds
nothing to correlate against and write no threshold (Requirement 4.5's fallback). This loader
gives it a bounded, representative history to work with: it registers a small fixed catalogue
of demo sites and writes a per-site, per-species readings history, WRITING THROUGH THE STORE
PORTS with no ingest/push pipeline (Requirement 10.4).

It is not a pipeline and it is not a schedule. It runs once, on demand, and is IDEMPOTENT by
construction (Requirement 10.4): a site re-upserted with identical metadata resolves to
``UNCHANGED`` and a reading re-put with identical content collides on its Dedup_Key and resolves
to itself (see ``resolve_stored_reading``), so a second run adds no rows.

THE MODULE IS IN THREE PARTS, kept separate so each is testable on its own (§1 single
responsibility):

1. **The catalogue** — ``DEMO_SITES``, an explicit ORDERED list of ``DemoSite`` records, and
   ``_site_metadata_record``, the ONE place a ``SiteCode`` becomes a ``SensorMetadataRecord``
   (Factory-ish centralisation — no scattered SiteCode formatting).
2. **The series generator** — ``build_reading_series``, a PURE function of
   (site, species, generator, clock, count) that returns a deterministic sequence of
   ``CalibratedReading``. It takes the random stream and the clock as parameters and never
   reaches for ``datetime.now`` or a module-level RNG (§2).
3. **The loader** — ``seed_exposure_history``, which writes through the two injected store
   Protocols (``SensorRegistryStore``, ``ReadingsStore``) and returns a small ``SeedSummary`` of
   counts. It depends on the Protocols, not on any concrete adapter (Dependency Inversion), so
   the unit test injects in-memory stores and a deployment injects the DynamoDB ones.

DETERMINISM (§2). The whole seed replays byte-identically from ``seed`` + ``clock``. Each site
gets its OWN generator, seeded from the run seed and the ``SiteCode``, so the sites are
independent streams and adding a site does not perturb another's series.

THE ENTRY POINT (``main``) mirrors ``jobs/entrypoint.py``: it resolves configuration and builds
the runtime through the shared ``load_runtime``, picks the readings + registry ports off it —
the SAME dynamodb adapters a deployment selects by config — runs the loader, and catches a
startup fault to return non-zero with one logged message (§5). A ``just`` recipe calls it (Task
14 wires that recipe; this task only exposes the entry point).
"""

from __future__ import annotations

import datetime as dt
import random
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from aqm_ingestion.composition import (
    Runtime,
    StartupError,
    load_runtime,
    report_startup_failure,
)
from aqm_ingestion.config.loader import ConfigError
from aqm_ingestion.contract.records import SensorMetadataRecord
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.observability.logging import configure_logging, get_logger
from aqm_ingestion.ports.clock import Clock, SystemClock
from aqm_ingestion.ports.protocols import (
    ReadingsStore,
    SensorRegistryStore,
    UpsertOutcome,
)

_logger = get_logger(__name__)

# The readings history depth, in DAYS, one reading per day per species.
#
# Why bounded, and why this number. The association reduces readings to one Sub_Index per DAY
# per species before correlating (see AssociationJob._daily_exposure), so a finer-than-daily
# history would be collapsed away — one reading per day is the natural granularity and needs no
# more. Sixty days is comfortably inside the readings retention window (90 days,
# DEFAULT_READINGS_RETENTION_DAYS) so none of it ages out at query time, and comfortably above
# the association's default minimum-observations bar so the demo derivation clears it. It is
# also deliberately SMALL: 60 days x 2 species = 120 readings per site, a demo history, not a
# backfill. The store cap (Requirement 14.8) is orders of magnitude above this, so a truncated
# window — which would make the association write nothing — cannot happen from the seed alone.
READINGS_PER_SITE_PER_SPECIES = 60

# A short tail of HOURLY readings ending at the current hour, on top of the daily history.
# Req 20.8's "current" reading is one whose interval start is within the serving freshness
# window (default 3h). A history stamped only at a fixed daily hour reads as "unavailable" for
# most of the day; this tail guarantees a current reading whenever the demo is run or recorded.
# Six hours covers the default window with margin, and stays a demo tail, not a backfill.
RECENT_HOURLY_READINGS = 6

# The species the association / AQI path expects. PM25 is the primary Mass_Concentration species
# (DEFAULT_SPECIES_PRECEDENCE leads with it) and is the one the association tests correlate on;
# NO2 is included so the seed is representative of both indexable species.
_DEMO_SPECIES: tuple[str, ...] = ("PM25", "NO2")

# A ratified status, so a re-put of identical content resolves to itself and the second run adds
# nothing (resolve_stored_reading: a ratified reading re-put with equal reported_value keeps the
# stored one). Provisional would behave the same for identical content, but ratified is what a
# settled historical reading is, and it removes any doubt about the idempotence path.
_RATIFIED = "R"

# Sub_Index bounds for the seeded series. Kept inside Requirement 32.8's permitted Learned
# Threshold range (1..500) and high enough that the elevated days clear the association's
# threshold floor, so the demo actually produces a threshold.
_SUB_INDEX_MIN = 20
_SUB_INDEX_MAX = 180

_UNITS_BY_SPECIES: Mapping[str, str] = {"PM25": "ug.m-3", "NO2": "ppb"}
_CALIBRATION_BY_SPECIES: Mapping[str, str] = {"PM25": "rh_linear", "NO2": "identity"}
_BREAKPOINT_TABLE = "epa-2024-05-06"
_INTERVAL_DURATION = "PT1H"

# The hour of day each daily reading is stamped at. Fixed, so the series is reproducible and the
# per-day reduction in the association has one unambiguous interval to pick up.
_READING_HOUR = 9


@dataclass(frozen=True, slots=True)
class DemoSite:
    """One demo site in the seeding catalogue.

    Coordinates are 7-decimal strings, matching the metadata contract (Requirement 2.2) and the
    ``SensorMetadataRecord`` fields. They sit near the demo profile's Cochabamba home so the
    ``GeoSelector`` selects them for that user (guards the association reaching the seed).
    """

    site_code: str
    site_name: str
    latitude: str
    longitude: str
    species: tuple[str, ...] = _DEMO_SPECIES


# The catalogue: an explicit, ORDERED list. CB0001 is the canonical demo site (its metadata is
# the golden payload in test_records.py); CB0002 and CB0003 are two more within a few hundred
# metres, so the user's nearest-N selection resolves to real seeded sites rather than one.
# Adding a scenario/site here is one new entry (§1 open/closed), not another branch anywhere.
DEMO_SITES: tuple[DemoSite, ...] = (
    DemoSite(
        site_code="CB0001",
        site_name="Cochabamba 0001",
        latitude="-17.3912345",
        longitude="-66.1523456",
    ),
    DemoSite(
        site_code="CB0002",
        site_name="Cochabamba 0002",
        latitude="-17.3945678",
        longitude="-66.1567890",
    ),
    DemoSite(
        site_code="CB0003",
        site_name="Cochabamba 0003",
        latitude="-17.3898765",
        longitude="-66.1489012",
    ),
)


def _site_metadata_record(site: DemoSite) -> SensorMetadataRecord:
    """Build a demo site's ``SensorMetadataRecord`` — the ONE place a SiteCode is formatted.

    Centralised (Factory-ish) so SiteCode formatting and the fixed metadata fields do not
    scatter across call sites (§1). The values mirror the golden Cochabamba metadata:
    ``EndDate`` is None so every site is active, and the coordinates carry through verbatim.
    """
    return SensorMetadataRecord(
        SiteCode=site.site_code,
        SiteName=site.site_name,
        DeviceCode=f"AQM-{site.site_code}",
        InstallationCode=f"INST-{site.site_code}",
        Facility="Cochabamba",
        Location={  # type: ignore[arg-type]
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": (site.latitude, site.longitude),
            },
        },
        Latitude=site.latitude,
        Longitude=site.longitude,
        Borough="Cochabamba",
        SiteClassification="Urban Background",
        SensorHeightAboveGround=3.0,
        DistanceToKerb=12.5,
        SponsorName="Kanata Air Quality Network",
        SiteLocationType="Urban Background",
        StartDate="2024-01-01T00:00:00Z",
        EndDate=None,
        PowerTag="Mains",
        SiteDescription=None,
        SitePhotoURL=None,
        SensorContract="Cellular-BO",
    )


def build_reading_series(
    site: DemoSite,
    species: str,
    generator: random.Random,
    clock: Clock,
    count: int = READINGS_PER_SITE_PER_SPECIES,
    count_hourly: int = RECENT_HOURLY_READINGS,
) -> tuple[CalibratedReading, ...]:
    """Build one site+species readings history, deterministically (§2).

    A PURE function of its arguments: it draws every value from the injected ``generator`` and
    every instant from the injected ``clock``, never from a module-level RNG or a wall-clock
    read. Emits ``count`` daily readings at a fixed hour ending YESTERDAY, then a tail of
    ``count_hourly`` HOURLY readings ending at the clock's current hour, so the newest reading
    is within the serving freshness window at any run time. All readings are emitted in
    ASCENDING interval order so the result is defined regardless of anything downstream (§2).

    Args:
        site: the catalogue entry the readings belong to.
        species: the measured species (drives units and calibration strategy).
        generator: the per-site random stream (seeded by the loader from seed + SiteCode).
        clock: the instant the lookback window reaches back from (§2 — never read directly here
            beyond this one call).
        count: how many daily readings to emit — the bound, defaulting to the module constant.
        count_hourly: how many recent hourly readings to append, ending at the current hour.

    Returns:
        ``count`` readings, ascending by interval start, each carrying a non-None integer
        ``sub_index`` (the association skips a None one).
    """
    today = clock.now().date()
    units = _UNITS_BY_SPECIES[species]
    calibration = _CALIBRATION_BY_SPECIES[species]

    readings: list[CalibratedReading] = []
    # Walk oldest-to-newest so the emitted order is ascending; draw one value per day. The daily
    # series ends YESTERDAY, not today: the hourly tail below owns today, so the two never
    # write the same interval_start (a collision would silently drop a reading via the Dedup_Key
    # and make the count depend on the current hour). So the range is count..1, not count-1..0.
    for day_offset in range(count, 0, -1):
        on = today - dt.timedelta(days=day_offset)
        interval_start = dt.datetime.combine(on, dt.time(_READING_HOUR), tzinfo=dt.UTC)
        sub_index = generator.randint(_SUB_INDEX_MIN, _SUB_INDEX_MAX)
        value = float(sub_index)
        readings.append(
            CalibratedReading(
                key=DedupKey(
                    site_code=site.site_code,
                    species=species,
                    interval_start=interval_start,
                    duration=_INTERVAL_DURATION,
                ),
                reported_value=value,
                corrected_value=value,
                units=units,
                quality_flag=QualityFlag.CALIBRATED,
                confidence=Confidence.HIGH,
                calibration_strategy=calibration,
                breakpoint_table=_BREAKPOINT_TABLE,
                ratification_status=_RATIFIED,
                ingested_at=interval_start,
                archive_id=f"seed-{site.site_code}-{species}-{on.isoformat()}",
                sub_index=sub_index,
                band="Moderate",
            )
        )

    # Recent hourly tail: readings for the last RECENT_HOURLY_READINGS hours ending at the
    # current hour, so the newest reading is inside the serving freshness window at any run
    # time (§2 — the instant comes from the injected clock, the values from the injected
    # generator, so the whole series stays reproducible). Values are drawn from the SAME
    # distribution as the daily series, so a "current" figure is coherent with the trend the
    # history shows rather than a spike.
    now = clock.now()
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    for hour_offset in range(count_hourly - 1, -1, -1):
        interval_start = current_hour - dt.timedelta(hours=hour_offset)
        sub_index = generator.randint(_SUB_INDEX_MIN, _SUB_INDEX_MAX)
        value = float(sub_index)
        readings.append(
            CalibratedReading(
                key=DedupKey(
                    site_code=site.site_code,
                    species=species,
                    interval_start=interval_start,
                    duration=_INTERVAL_DURATION,
                ),
                reported_value=value,
                corrected_value=value,
                units=units,
                quality_flag=QualityFlag.CALIBRATED,
                confidence=Confidence.HIGH,
                calibration_strategy=calibration,
                breakpoint_table=_BREAKPOINT_TABLE,
                ratification_status=_RATIFIED,
                ingested_at=interval_start,
                archive_id=(
                    f"seed-{site.site_code}-{species}-{interval_start.isoformat()}"
                ),
                sub_index=sub_index,
                band="Moderate",
            )
        )
    return tuple(readings)


@dataclass(frozen=True, slots=True)
class SeedSummary:
    """What one seeding run wrote — counts only, no readings and no health data (§6).

    Safe to log: site outcome counts and a total readings count, nothing that names a user or
    carries a secret.
    """

    sites_created: int
    sites_updated: int
    sites_unchanged: int
    readings_written: int


# The outcome each upsert maps to, so the loader counts changes without re-reading the registry.
_OUTCOME_FIELDS: Mapping[UpsertOutcome, str] = {
    UpsertOutcome.CREATED: "created",
    UpsertOutcome.UPDATED: "updated",
    UpsertOutcome.UNCHANGED: "unchanged",
}


def seed_exposure_history(
    *,
    registry: SensorRegistryStore,
    readings: ReadingsStore,
    clock: Clock,
    seed: int,
    sites: Sequence[DemoSite] = DEMO_SITES,
) -> SeedSummary:
    """Seed the registry and readings store from the demo catalogue, idempotently.

    Writes through the two injected store Protocols (Dependency Inversion — no concrete adapter
    named here), so the offline unit test injects in-memory stores and a deployment injects the
    DynamoDB ones by config. Registers each site as of ``clock.now()`` and writes its
    per-species readings history; a rerun re-upserts identical metadata (UNCHANGED) and re-puts
    identical readings (Dedup_Key collision resolving to itself), adding nothing (Req 10.4).

    Args:
        registry: the sensor-registry store the demo sites are upserted into.
        readings: the readings store the exposure history is written into.
        clock: the instant sites are registered at and the readings window reaches back from
            (§2).
        seed: the run seed; each site's stream is seeded from this plus its SiteCode, so the
            whole seed is reproducible and the sites are independent.
        sites: the catalogue to seed, defaulting to ``DEMO_SITES``.

    Returns:
        A ``SeedSummary`` of the site outcome counts and the total readings written.
    """
    at = clock.now()
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    readings_written = 0

    # Ordered by SiteCode so the run has a defined order (§2) and its log is readable.
    for site in sorted(sites, key=lambda entry: entry.site_code):
        outcome = registry.upsert(_site_metadata_record(site), at=at)
        counts[_OUTCOME_FIELDS[outcome]] += 1

        # A generator PER site, seeded from the run seed and the SiteCode, so each site is an
        # independent, reproducible stream (§2).
        generator = random.Random(f"{seed}:{site.site_code}")
        batch: list[CalibratedReading] = []
        for species in site.species:
            batch.extend(build_reading_series(site, species, generator, clock))
        readings.put_batch(batch)
        readings_written += len(batch)

    summary = SeedSummary(
        sites_created=counts["created"],
        sites_updated=counts["updated"],
        sites_unchanged=counts["unchanged"],
        readings_written=readings_written,
    )
    _logger.info(
        "seed_complete",
        sites=len(sites),
        sites_created=summary.sites_created,
        sites_updated=summary.sites_updated,
        sites_unchanged=summary.sites_unchanged,
        readings_written=summary.readings_written,
    )
    return summary


def run(runtime: Runtime, seed: int) -> int:
    """Seed through a built runtime's readings + registry ports.

    Picks the SAME ports the serving and association paths use off the runtime, so the loader
    writes through whichever adapters config selected (memory locally, dynamodb deployed).

    Returns:
        0 always — a per-site store failure would surface as an exception from the adapter,
        which the boundary in ``main`` catches; there is no partial-success code to report here.
    """
    registry = cast("SensorRegistryStore", _port(runtime, "sensor_registry_store"))
    readings = cast("ReadingsStore", _port(runtime, "readings_store"))
    seed_exposure_history(registry=registry, readings=readings, clock=runtime.clock, seed=seed)
    return 0


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    clock: Clock | None = None,
) -> int:
    """Resolve configuration, build the runtime, and run one seeding pass.

    Mirrors ``jobs/entrypoint.main``: the runtime is built through the shared ``load_runtime``
    the seeding path and the serving/association paths resolve configuration and select adapters
    identically. An optional single positional argument is the integer seed, so an operator can
    reproduce a specific history; it defaults to a fixed value so a bare invocation is
    deterministic.

    Returns:
        0 on success, 1 on any startup failure, with one logged message per fault (§5).
    """
    import os

    environment = dict(os.environ if env is None else env)
    configure_logging(environment.get("AQM_LOG_LEVEL", "info"))

    seed = _resolve_seed(argv)

    try:
        runtime = load_runtime(environment, clock or SystemClock())
    except (ConfigError, StartupError) as failure:
        return report_startup_failure(failure)

    return run(runtime, seed)


_DEFAULT_SEED = 20260701


def _resolve_seed(argv: Sequence[str] | None) -> int:
    """Read the seed from the first argument, or use the default.

    Raises:
        StartupError: never — a non-integer argument falls back to the default with a warning,
            rather than failing a demo seeding over a typo. The seed only affects which
            reproducible series is written, so a wrong one is harmless.
    """
    args = tuple(argv or ())
    if not args:
        return _DEFAULT_SEED
    try:
        return int(args[0])
    except ValueError:
        _logger.warning("seed_argument_not_an_integer", using_default=_DEFAULT_SEED)
        return _DEFAULT_SEED


def _port(runtime: Runtime, name: str) -> object:
    """Take one port off the runtime, failing loudly if the root did not build it.

    Mirrors ``jobs/entrypoint._port``: returns ``object`` so each call site casts to the port it
    wants, and the guarantee that every name resolves comes from the composition root's own
    agreement test rather than a runtime ``isinstance`` that a Protocol makes weaker than it
    looks.
    """
    port = runtime.ports.get(name)
    if port is None:  # pragma: no cover - the composition guard makes this unreachable
        raise StartupError(f"the composition root built no {name}")
    return port


__all__ = [
    "DEMO_SITES",
    "READINGS_PER_SITE_PER_SPECIES",
    "RECENT_HOURLY_READINGS",
    "DemoSite",
    "SeedSummary",
    "build_reading_series",
    "main",
    "run",
    "seed_exposure_history",
]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
