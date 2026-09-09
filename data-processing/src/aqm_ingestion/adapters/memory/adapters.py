"""In-memory adapters: the offline suite runs entirely against these.

Requirement 28.5 requires the whole suite to pass with no AWS credentials and no
network beyond localhost, which means every port needs a local implementation good
enough to exercise the behaviour its users depend on — not a stub that returns
empty.

Three properties matter more here than in a production adapter, because these
fakes are what the correctness tests observe:

- DETERMINISM. Every query returns a defined order (§2), so a test cannot pass by
  accident of dict ordering, and the archive id is DERIVED from the payload rather
  than randomly generated, so replaying the same payload yields the same id.
- NO HIDDEN CLOCK. An instant always arrives as a parameter. ``upsert`` decides
  whether a site is active from the ``at`` it is handed (Requirement 2.8), never
  from a clock of its own.
- HONEST DEGRADATION. An unconfigured forecast comes back marked ``degraded``
  rather than as a confident zero, so the serving layer discloses the gap instead
  of presenting it as a fact.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections.abc import Iterator, Mapping, Sequence

from aqm_ingestion.contract.records import SensorMetadataRecord
from aqm_ingestion.domain.models import CalibratedReading, DedupKey
from aqm_ingestion.ports.protocols import (
    ArchiveMeta,
    AuthRejectedError,
    ForecastResult,
    MetObservation,
    NearestSite,
    PollenResult,
    RegistryEntry,
    RejectionCategory,
    UpsertOutcome,
    UserProfile,
    VerifiedIdentity,
    WindowResult,
)

_EARTH_RADIUS_KM = 6371.0088
_DEFAULT_WINDOW_CAP = 10_000


def _great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres (haversine)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


class InMemoryReadingsStore:
    """Readings held in a dict keyed by DedupKey, so a re-put is idempotent."""

    def __init__(self, max_window_readings: int = _DEFAULT_WINDOW_CAP) -> None:
        """Hold readings, capping a window at ``max_window_readings``."""
        self._readings: dict[DedupKey, CalibratedReading] = {}
        self._cap = max_window_readings

    def put(self, reading: CalibratedReading) -> None:
        """Store one Reading; the same key replaces rather than duplicates."""
        self._readings[reading.key] = reading

    def put_batch(self, readings: Sequence[CalibratedReading]) -> None:
        """Store many Readings."""
        for reading in readings:
            self.put(reading)

    def get(self, key: DedupKey) -> CalibratedReading | None:
        """Return the Reading for a key, or None."""
        return self._readings.get(key)

    def query_window(
        self,
        site_code: str,
        species: frozenset[str] | None,
        start: dt.datetime,
        end: dt.datetime,
    ) -> WindowResult:
        """Return Readings in the half-open [start, end), reporting truncation."""
        matched = [
            reading
            for reading in self._readings.values()
            if reading.key.site_code == site_code
            and start <= reading.key.interval_start < end
            and (species is None or reading.key.species in species)
        ]
        # Sorted so the result never depends on insertion order (§2).
        matched.sort(key=lambda r: (r.key.interval_start, r.key.species))
        truncated = len(matched) > self._cap
        return WindowResult(readings=tuple(matched[: self._cap]), truncated=truncated)

    def latest_per_species(
        self, site_codes: Sequence[str], not_before: dt.datetime
    ) -> Mapping[str, Sequence[CalibratedReading]]:
        """Return the newest Reading per species per site, no older than given."""
        latest: dict[str, dict[str, CalibratedReading]] = {}
        for reading in self._readings.values():
            if reading.key.site_code not in site_codes:
                continue
            if reading.key.interval_start < not_before:
                continue
            per_species = latest.setdefault(reading.key.site_code, {})
            current = per_species.get(reading.key.species)
            if current is None or reading.key.interval_start > current.key.interval_start:
                per_species[reading.key.species] = reading
        return {
            site: [per_species[name] for name in sorted(per_species)]
            for site, per_species in sorted(latest.items())
        }


class InMemoryRawArchive:
    """Payloads held verbatim, addressed by a DERIVED id.

    The id is a digest of the payload and its metadata rather than a random value,
    so archiving the same payload twice yields the same id and a replay stays
    reproducible (§2).
    """

    def __init__(self) -> None:
        """Start with an empty archive."""
        self._payloads: dict[str, bytes] = {}

    def write(self, payload: bytes, meta: ArchiveMeta) -> str:
        """Archive a payload byte-for-byte and return its derived id."""
        digest = hashlib.sha256()
        digest.update(payload)
        digest.update(str(meta.site_code).encode("utf-8"))
        digest.update(meta.transport.encode("utf-8"))
        digest.update(meta.received_at.isoformat().encode("utf-8"))
        archive_id = digest.hexdigest()
        self._payloads[archive_id] = payload
        return archive_id

    def read(self, archive_id: str) -> bytes:
        """Return the archived payload exactly as written.

        Raises:
            KeyError: if the id is unknown.
        """
        return self._payloads[archive_id]


class InMemorySensorRegistryStore:
    """Site metadata with geographic queries over the active sites."""

    def __init__(self) -> None:
        """Start with an empty registry."""
        self._entries: dict[str, RegistryEntry] = {}

    def upsert(self, record: SensorMetadataRecord, at: dt.datetime) -> UpsertOutcome:
        """Register or update a site as of ``at``, reporting what changed."""
        # Activity is decided HERE against the supplied instant (Requirement 2.8)
        # rather than on every read, so the answer cannot drift between callers.
        active = record.EndDate is None or record.EndDate > at.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        entry = RegistryEntry(record=record, active=active, updated_at=at)
        existing = self._entries.get(record.SiteCode)
        self._entries[record.SiteCode] = entry
        if existing is None:
            return UpsertOutcome.CREATED
        if existing.record == record and existing.active == active:
            return UpsertOutcome.UNCHANGED
        return UpsertOutcome.UPDATED

    def get(self, site_code: str) -> RegistryEntry | None:
        """Return a site's entry, or None."""
        return self._entries.get(site_code)

    def list_active(self) -> Sequence[RegistryEntry]:
        """Return the active sites ordered by SiteCode."""
        return [
            entry
            for _, entry in sorted(self._entries.items())
            if entry.active
        ]

    def nearest(
        self, lat: float, lon: float, n: int, max_km: float | None
    ) -> Sequence[NearestSite]:
        """Return the nearest ACTIVE sites, closest first."""
        candidates: list[NearestSite] = []
        for entry in self.list_active():
            distance = _great_circle_km(
                lat, lon, float(entry.record.Latitude), float(entry.record.Longitude)
            )
            if max_km is not None and distance > max_km:
                continue
            candidates.append(NearestSite(entry=entry, distance_km=distance))
        # SiteCode breaks a distance tie so the order is total and reproducible.
        candidates.sort(key=lambda site: (site.distance_km, site.entry.record.SiteCode))
        return candidates[:n]


class InMemoryProfileStore:
    """User profiles held in a dict."""

    def __init__(self) -> None:
        """Start with no profiles."""
        self._profiles: dict[str, UserProfile] = {}

    def get(self, user_id: str) -> UserProfile | None:
        """Return a profile, or None."""
        return self._profiles.get(user_id)

    def put(self, profile: UserProfile) -> UserProfile:
        """Store a profile and return what was stored."""
        self._profiles[profile.user_id] = profile
        return profile

    def delete(self, user_id: str) -> None:
        """Remove a profile; absent is not an error (deletion is idempotent)."""
        self._profiles.pop(user_id, None)


class InMemoryMeteorologyProvider:
    """Meteorology observations configured per site and instant."""

    def __init__(self) -> None:
        """Start with no observations."""
        self._observations: dict[tuple[str, dt.datetime], MetObservation] = {}

    def set(
        self,
        site_code: str,
        at: dt.datetime,
        temperature_k: float | None = None,
        pressure_pa: float | None = None,
        relative_humidity_pct: float | None = None,
    ) -> None:
        """Configure the observation returned for a site at an instant."""
        self._observations[(site_code, at)] = MetObservation(
            temperature_k=temperature_k,
            pressure_pa=pressure_pa,
            relative_humidity_pct=relative_humidity_pct,
        )

    def observation(self, site_code: str, at: dt.datetime) -> MetObservation | None:
        """Return the configured observation, or None when unknown."""
        return self._observations.get((site_code, at))


class InMemoryForecastClient:
    """Forecast and pollen values configured per rounded coordinate."""

    def __init__(self) -> None:
        """Start with nothing configured, so every answer is degraded."""
        self._forecasts: dict[tuple[float, float], Mapping[str, float]] = {}
        self._pollen: dict[tuple[float, float], Mapping[str, float]] = {}

    @staticmethod
    def _at(lat: float, lon: float) -> tuple[float, float]:
        # Rounded, matching Requirement 24.8's rounded-coordinate rule.
        return (round(lat, 2), round(lon, 2))

    def set_forecast(self, lat: float, lon: float, values: Mapping[str, float]) -> None:
        """Configure the forecast for a coordinate."""
        self._forecasts[self._at(lat, lon)] = values

    def set_pollen(self, lat: float, lon: float, values: Mapping[str, float]) -> None:
        """Configure the pollen values for a coordinate."""
        self._pollen[self._at(lat, lon)] = values

    def forecast(self, lat: float, lon: float) -> ForecastResult:
        """Return the configured forecast, or a DEGRADED empty result."""
        values = self._forecasts.get(self._at(lat, lon))
        if values is None:
            return ForecastResult(values={}, degraded=True)
        return ForecastResult(values=values)

    def pollen(self, lat: float, lon: float) -> PollenResult:
        """Return the configured pollen values, or a DEGRADED empty result."""
        values = self._pollen.get(self._at(lat, lon))
        if values is None:
            return PollenResult(values={}, degraded=True)
        return PollenResult(values=values)


class ScriptedMqttTransport:
    """Replays a scripted message sequence in order."""

    def __init__(self, messages: Sequence[tuple[str, bytes]] | None = None) -> None:
        """Hold the sequence to replay and record subscriptions."""
        self._messages = list(messages or [])
        self.subscriptions: list[str] = []
        self.closed = False

    def subscribe(self, topic_filter: str) -> None:
        """Record the subscription so a test can assert the resolved filter."""
        self.subscriptions.append(topic_filter)

    def messages(self) -> Iterator[tuple[str, bytes]]:
        """Yield the scripted messages in arrival order."""
        yield from self._messages

    def close(self) -> None:
        """Mark the transport closed."""
        self.closed = True


class ScriptedFeedClient:
    """Returns a payload configured per polling window."""

    def __init__(
        self, payloads: Mapping[tuple[dt.datetime, dt.datetime], bytes] | None = None
    ) -> None:
        """Hold the per-window payloads."""
        self._payloads = dict(payloads or {})
        self.calls: list[tuple[dt.datetime, dt.datetime]] = []

    def fetch(self, since: dt.datetime, until: dt.datetime) -> bytes:
        """Return the configured payload, or an empty JSON array."""
        self.calls.append((since, until))
        # An empty ARRAY rather than empty bytes: an unscripted window means "no
        # records", which the Parser must be able to accept as a valid payload.
        return self._payloads.get((since, until), b"[]")


class LocalAuthenticator:
    """Resolves configured development credentials to configured identities.

    Requirement 18.8 wants a local authenticator so the serving suite runs without
    a cloud identity provider. The credential never appears in the raised error or
    anywhere else (§7) — only its rejection category travels.
    """

    def __init__(self, credentials: Mapping[str, str]) -> None:
        """Hold the credential-to-identity mapping."""
        self._credentials = dict(credentials)

    def verify(self, credential: str) -> VerifiedIdentity:
        """Return the caller's identity.

        Raises:
            AuthRejectedError: with MISSING for an empty credential and
                INVALID_SIGNATURE for one that does not resolve.
        """
        if not credential:
            raise AuthRejectedError(RejectionCategory.MISSING)
        user_id = self._credentials.get(credential)
        if user_id is None:
            raise AuthRejectedError(RejectionCategory.INVALID_SIGNATURE)
        return VerifiedIdentity(user_id=user_id)
