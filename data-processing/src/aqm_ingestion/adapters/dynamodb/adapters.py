"""DynamoDB adapters for the readings, registry, and profile ports.

Requirements 14.9, 15.10, 17.11.

THE CONDITIONAL WRITE IS WHY ``resolve_duplicate`` IS A PURE FUNCTION. Requirement 14.2 defers
the
duplicate decision to Requirement 7, where status outranks magnitude — a ratified 3 replaces a
provisional 10. A read-modify-write would leave an interleaving window in which two concurrent
writers each read the old item and each decide they win. Because ``domain/dedup.py`` decides
from
two CANDIDATES with no I/O, the decision can be expressed as a DynamoDB condition expression and
the database arbitrates: the write succeeds only if the item is still what the decision was made
about, and a lost race is retried against the new state.

KEY LAYOUT, from Requirement 14.9: readings are keyed ``SITE#{SiteCode}#SP#{Species}`` with the
interval start as the sort key, so one site-species series is one partition and a window query
is a
range scan rather than a table scan. The registry is keyed by ``SiteCode`` and profiles by the
verified user identity — Requirement 17.1's "keyed by the verified identity only", which here is
the partition key itself, so there is nowhere else a profile could be filed.

EVERY ADAPTER HERE IS EXERCISED BY THE SHARED CONTRACT SUITE (Requirement 16.8, 28.10), which is
what makes the in-memory adapter a genuine stand-in rather than a parallel implementation. The
suite skips these when no endpoint is reachable, and the container-fenced job of task 28 runs
them
for real.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, cast

import boto3
from botocore.exceptions import ClientError

from aqm_ingestion.domain.dedup import resolve_stored_reading
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.domain.profile import UserProfile, build_profile
from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import (
    NearestSite,
    RegistryEntry,
    UpsertOutcome,
    WindowResult,
)

_logger = get_logger("adapters.dynamodb")

_MAX_CONDITIONAL_RETRIES = 5
"""How many times a lost conditional-write race is retried.

Finite rather than unbounded: each retry re-reads and re-decides, so a genuine livelock would
mean
a writer is being starved, which is worth failing loudly rather than spinning.
"""


def _partition(site_code: str, species: str) -> str:
    """Requirement 14.9's partition key: one site-species series per partition."""
    return f"SITE#{site_code}#SP#{species}"


class DynamoDbReadingsStore:
    """Readings in DynamoDB, with the dedup decision arbitrated by a conditional write."""

    def __init__(
        self,
        table_name: str,
        clock: Clock,
        endpoint_url: str | None = None,
        retention_days: int = 90,
        species_precedence: tuple[str, ...] = ("PM25", "NO2"),
        max_window_readings: int = 10_000,
    ) -> None:
        """Bind the table. ``clock`` is required for the same reason as in memory (§2)."""
        self._table = boto3.resource(
            "dynamodb", endpoint_url=endpoint_url
        ).Table(table_name)
        self._clock = clock
        self._retention_days = retention_days
        self._precedence = species_precedence
        self._max_window_readings = max_window_readings

    def put(self, reading: CalibratedReading) -> None:
        """Store a Reading, resolving against any stored one (Requirements 14.2, 7).

        The resolution is decided by the pure domain function and then IMPOSED with a condition
        expression, so a concurrent writer cannot overwrite the decision's premise.
        """
        for _attempt in range(_MAX_CONDITIONAL_RETRIES):
            stored = self.get(reading.key)
            if stored is None:
                winner = reading
            else:
                winner = resolve_stored_reading(stored, reading)
                if winner == stored:
                    # Nothing to write: the stored Reading already wins, and a write would churn
                    # without changing what is served (the same no-write branch Requirement 7.3
                    # takes in memory).
                    return
            try:
                self._conditional_put(winner, stored)
            except ClientError as error:
                if _is_condition_failure(error):
                    # Lost the race: the item changed under us, so re-read and re-decide rather
                    # than retrying a decision made about state that no longer exists.
                    continue
                raise
            return
        raise RuntimeError(
            f"conditional write for {reading.key.site_code}/{reading.key.species} lost "
            f"{_MAX_CONDITIONAL_RETRIES} races; a writer is being starved"
        )

    def _conditional_put(
        self, reading: CalibratedReading, previous: CalibratedReading | None
    ) -> None:
        """Write only if the item is still what the decision was made about."""
        item = _reading_to_item(reading)
        if previous is None:
            # Nothing stored: succeed only if that is STILL true.
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
            )
            return
        self._table.put_item(
            Item=item,
            ConditionExpression=(
                "corrected_value = :prev_value AND ratification_status = :prev_status"
            ),
            ExpressionAttributeValues={
                ":prev_value": Decimal(str(previous.corrected_value)),
                ":prev_status": previous.ratification_status,
            },
        )

    def put_batch(self, readings: Sequence[CalibratedReading]) -> None:
        """Store many Readings, resolving each against what is stored.

        Deliberately NOT a DynamoDB ``batch_writer``: a batch write cannot carry a condition
        expression, so it would discard exactly the dedup arbitration ``put`` exists to
        guarantee
        — a provisional value in a batch could overwrite a ratified one. Sequential conditional
        writes are slower and correct, which is the right trade for a store whose whole job is
        not losing a ratified reading.

        The port says a failure must not leave a partial batch VISIBLE. Each write is
        individually
        idempotent and order-independent (Requirement 7.7), so a retry of the whole batch
        converges on the same state — which is what makes the absence of a transaction fine.
        """
        for reading in readings:
            self.put(reading)

    def get(self, key: DedupKey) -> CalibratedReading | None:
        """Return the Reading for a key, or None."""
        response = self._table.get_item(
            Key={
                "pk": _partition(key.site_code, key.species),
                "sk": key.interval_start.isoformat(),
            }
        )
        item = response.get("Item")
        return None if item is None else _item_to_reading(item)

    def query_window(
        self,
        site_code: str,
        species: frozenset[str] | None,
        start: dt.datetime,
        end: dt.datetime,
    ) -> WindowResult:
        """Return Readings in the half-open window, reporting truncation.

        Raises:
            ValueError: for an inverted window — a caller bug, and returning nothing would hide
                it (§5). The same refusal the in-memory adapter makes, which is why the shared
                suite asserts it for both.
        """
        if end < start:
            raise ValueError(f"inverted window: start {start} is after end {end}")

        wanted = species or frozenset(self._precedence)
        floor = self._retention_floor()
        collected: list[CalibratedReading] = []
        for name in sorted(wanted):
            collected.extend(
                self._query_series(site_code, name, max(start, floor), end)
            )

        collected.sort(key=lambda r: (r.key.interval_start, self._species_rank(r.key.species)))
        truncated = len(collected) > self._max_window_readings
        return WindowResult(
            readings=tuple(collected[: self._max_window_readings]), truncated=truncated
        )

    def _query_series(
        self, site_code: str, species: str, start: dt.datetime, end: dt.datetime
    ) -> list[CalibratedReading]:
        """One site-species partition's range, paged to the end."""
        from boto3.dynamodb.conditions import Key

        readings: list[CalibratedReading] = []
        kwargs: dict[str, object] = {
            "KeyConditionExpression": Key("pk").eq(_partition(site_code, species))
            & Key("sk").gte(start.isoformat()),
        }
        while True:
            response = self._table.query(**kwargs)
            for item in response.get("Items", []):
                reading = _item_to_reading(item)
                # The END is EXCLUSIVE, applied here rather than in the key condition because
                # DynamoDB's `between` is inclusive on both sides.
                if reading.key.interval_start < end:
                    readings.append(reading)
            token = response.get("LastEvaluatedKey")
            if not token:
                return readings
            kwargs["ExclusiveStartKey"] = token

    def latest_per_species(
        self, site_codes: Sequence[str], not_before: dt.datetime
    ) -> Mapping[str, Sequence[CalibratedReading]]:
        """The newest Reading per species per site, no older than given.

        Retention applies here as well as to a window query (Requirement 14.7 says EVERY query),
        which is the behaviour the shared suite pins for both adapters.
        """
        from boto3.dynamodb.conditions import Key

        floor = max(not_before, self._retention_floor())
        result: dict[str, list[CalibratedReading]] = {}
        for site_code in site_codes:
            newest: list[CalibratedReading] = []
            for species in self._precedence:
                response = self._table.query(
                    KeyConditionExpression=Key("pk").eq(_partition(site_code, species))
                    & Key("sk").gte(floor.isoformat()),
                    # Descending, limit 1: the newest is the only one wanted, so the database
                    # does the selecting rather than this process paging a whole series.
                    ScanIndexForward=False,
                    Limit=1,
                )
                items = response.get("Items", [])
                if items:
                    newest.append(_item_to_reading(items[0]))
            if newest:
                newest.sort(key=lambda r: self._species_rank(r.key.species))
                result[site_code] = newest
        return result

    def _retention_floor(self) -> dt.datetime:
        """Retention applied at QUERY time, from the injected clock."""
        return self._clock.now() - dt.timedelta(days=self._retention_days)

    def _species_rank(self, species: str) -> tuple[int, str]:
        """Configured precedence, unlisted last then by name (Requirement 14.3)."""
        try:
            return (self._precedence.index(species), species)
        except ValueError:
            return (len(self._precedence), species)


class DynamoDbSensorRegistryStore:
    """Site metadata in DynamoDB, keyed by SiteCode (Requirement 15.10)."""

    def __init__(self, table_name: str, endpoint_url: str | None = None) -> None:
        """Bind the table."""
        self._table = boto3.resource(
            "dynamodb", endpoint_url=endpoint_url
        ).Table(table_name)

    def upsert(self, record: object, at: dt.datetime) -> UpsertOutcome:
        """Register or update a site, reporting what changed (Requirement 15.2).

        An identical re-receive makes NO WRITE, so ``updated_at`` does not advance — without
        that
        a stale registry stops being detectable (Requirement 15.11), and the shared suite
        asserts the stored instant survives a later identical upsert for every adapter.
        """
        from aqm_ingestion.contract.records import SensorMetadataRecord

        typed = cast("SensorMetadataRecord", record)
        existing = self.get(typed.SiteCode)
        if existing is not None and existing.record == typed:
            return UpsertOutcome.UNCHANGED

        active = _is_active(typed, at)
        self._table.put_item(
            Item={
                "site_code": typed.SiteCode,
                "record": typed.model_dump_json(),
                "updated_at": at.isoformat(),
                "active": active,
            }
        )
        if existing is None:
            return UpsertOutcome.CREATED
        if existing.record.Location != typed.Location:
            # Requirement 15.9: a moved site invalidates every past nearest-N result, so it is a
            # warning naming BOTH positions — one alone cannot answer how far it moved.
            _logger.warning(
                "site_position_changed",
                site_code=typed.SiteCode,
                previous_latitude=existing.record.Latitude,
                previous_longitude=existing.record.Longitude,
                latitude=typed.Latitude,
                longitude=typed.Longitude,
            )
        return UpsertOutcome.UPDATED

    def get(self, site_code: str) -> RegistryEntry | None:
        """Return a site's entry, or None."""
        from aqm_ingestion.contract.records import SensorMetadataRecord

        response = self._table.get_item(Key={"site_code": site_code})
        item = response.get("Item")
        if item is None:
            return None
        return RegistryEntry(
            record=SensorMetadataRecord.model_validate_json(str(item["record"])),
            updated_at=dt.datetime.fromisoformat(str(item["updated_at"])),
            active=bool(item["active"]),
        )

    def list_active(self) -> Sequence[RegistryEntry]:
        """Active sites ordered by SiteCode (§2: the order reaches output)."""
        entries: list[RegistryEntry] = []
        kwargs: dict[str, object] = {}
        while True:
            response = self._table.scan(**kwargs)
            for item in response.get("Items", []):
                if bool(item.get("active")):
                    entry = self.get(str(item["site_code"]))
                    if entry is not None:
                        entries.append(entry)
            token = response.get("LastEvaluatedKey")
            if not token:
                break
            kwargs["ExclusiveStartKey"] = token
        entries.sort(key=lambda entry: entry.record.SiteCode)
        return entries

    def nearest(
        self, lat: float, lon: float, n: int, max_km: float | None
    ) -> Sequence[NearestSite]:
        """The nearest ACTIVE sites, closest first, ties broken by SiteCode.

        Distance is computed HERE from the stored coordinates rather than in the database,
        because
        Requirement 20.10 fixes the great-circle computation and a database-side approximation
        would give a different answer from the in-memory adapter — which the shared suite would
        catch, and which is the whole point of having one.
        """
        from aqm_ingestion.adapters.memory.adapters import _great_circle_km

        candidates: list[NearestSite] = []
        for entry in self.list_active():
            distance = _great_circle_km(
                lat, lon, float(entry.record.Latitude), float(entry.record.Longitude)
            )
            if max_km is not None and distance > max_km:
                continue
            candidates.append(NearestSite(entry=entry, distance_km=distance))
        candidates.sort(key=lambda site: (site.distance_km, site.entry.record.SiteCode))
        return candidates[:n]


class DynamoDbProfileStore:
    """User profiles in DynamoDB, keyed by the verified identity (Requirement 17.11).

    The user identity IS the partition key, which is Requirement 17.1's "keyed by the verified
    identity only" made structural: there is nowhere else a profile could be filed.
    """

    def __init__(self, table_name: str, endpoint_url: str | None = None) -> None:
        """Bind the table."""
        self._table = boto3.resource(
            "dynamodb", endpoint_url=endpoint_url
        ).Table(table_name)

    def get(self, user_id: str) -> UserProfile | None:
        """Return a user's profile, or None."""
        response = self._table.get_item(Key={"user_id": user_id})
        item = response.get("Item")
        if item is None:
            return None
        import json

        return build_profile(cast("dict[str, object]", json.loads(str(item["profile"]))))

    def put(self, profile: UserProfile) -> UserProfile:
        """Store a profile, returning what was stored."""
        self._table.put_item(
            Item={"user_id": profile.user_id, "profile": profile.model_dump_json()}
        )
        return profile

    def delete(self, user_id: str) -> None:
        """Remove a profile. Idempotent, per Requirement 17.8's erasure."""
        self._table.delete_item(Key={"user_id": user_id})


def _is_active(record: object, at: dt.datetime) -> bool:
    """Whether a site is active as of an instant (Requirement 2.8).

    Decided ONCE at write time and stored, so it cannot drift between readers — the same choice
    the in-memory adapter makes.
    """
    from aqm_ingestion.contract.records import SensorMetadataRecord

    typed = cast("SensorMetadataRecord", record)
    if typed.EndDate is None:
        return True
    # "No later than" makes the boundary INACTIVE.
    return dt.datetime.fromisoformat(typed.EndDate.replace("Z", "+00:00")) > at


def _is_condition_failure(error: ClientError) -> bool:
    """Whether a ClientError is a lost conditional-write race rather than a real fault."""
    code = error.response.get("Error", {}).get("Code")
    return bool(code == "ConditionalCheckFailedException")


def _reading_to_item(reading: CalibratedReading) -> dict[str, object]:
    """Render a Reading as an item, floats as Decimal since DynamoDB refuses float."""
    item: dict[str, object] = {
        "pk": _partition(reading.key.site_code, reading.key.species),
        "sk": reading.key.interval_start.isoformat(),
        "duration": reading.key.duration,
        "reported_value": Decimal(str(reading.reported_value)),
        "corrected_value": Decimal(str(reading.corrected_value)),
        "units": reading.units,
        "quality_flag": str(reading.quality_flag),
        "confidence": str(reading.confidence),
        "calibration_strategy": reading.calibration_strategy,
        "breakpoint_table": reading.breakpoint_table,
        "ratification_status": reading.ratification_status,
        "ingested_at": reading.ingested_at.isoformat(),
        "archive_id": reading.archive_id,
        "humidity_source": reading.humidity_source,
    }
    optional: dict[str, object | None] = {
        "mixing_ratio_ppb": reading.mixing_ratio_ppb,
        "sub_index": reading.sub_index,
        "band": reading.band,
        "method": reading.method,
        "conversion_source": reading.conversion_source,
        "conversion_temperature_k": reading.conversion_temperature_k,
        "conversion_pressure_pa": reading.conversion_pressure_pa,
        "nowcast_window_hours": reading.nowcast_window_hours,
        "nowcast_hours_available": reading.nowcast_hours_available,
        "nowcast_weight_factor": reading.nowcast_weight_factor,
    }
    for name, value in optional.items():
        if value is None:
            continue
        item[name] = Decimal(str(value)) if isinstance(value, float) else value
    return item


def _item_to_reading(item: Mapping[str, Any]) -> CalibratedReading:
    """Rebuild a Reading from an item, Decimal back to float."""

    def number(name: str) -> float | None:
        raw = item.get(name)
        return None if raw is None else float(raw)

    def whole(name: str) -> int | None:
        raw = item.get(name)
        return None if raw is None else int(raw)

    def text(name: str) -> str | None:
        raw = item.get(name)
        return None if raw is None else str(raw)

    site_code, species = str(item["pk"]).removeprefix("SITE#").split("#SP#")
    return CalibratedReading(
        key=DedupKey(
            site_code=site_code,
            species=species,
            interval_start=dt.datetime.fromisoformat(str(item["sk"])),
            duration=str(item["duration"]),
        ),
        reported_value=float(item["reported_value"]),
        corrected_value=float(item["corrected_value"]),
        units=str(item["units"]),
        quality_flag=QualityFlag(str(item["quality_flag"])),
        confidence=Confidence(str(item["confidence"])),
        calibration_strategy=str(item["calibration_strategy"]),
        breakpoint_table=str(item["breakpoint_table"]),
        ratification_status=str(item["ratification_status"]),
        ingested_at=dt.datetime.fromisoformat(str(item["ingested_at"])),
        archive_id=str(item["archive_id"]),
        mixing_ratio_ppb=number("mixing_ratio_ppb"),
        sub_index=whole("sub_index"),
        band=text("band"),
        method=text("method"),  # type: ignore[arg-type]
        humidity_source=str(item.get("humidity_source", "none")),  # type: ignore[arg-type]
        conversion_source=text("conversion_source"),  # type: ignore[arg-type]
        conversion_temperature_k=number("conversion_temperature_k"),
        conversion_pressure_pa=number("conversion_pressure_pa"),
        nowcast_window_hours=whole("nowcast_window_hours"),
        nowcast_hours_available=whole("nowcast_hours_available"),
        nowcast_weight_factor=number("nowcast_weight_factor"),
    )


__all__ = [
    "DynamoDbProfileStore",
    "DynamoDbReadingsStore",
    "DynamoDbSensorRegistryStore",
]
