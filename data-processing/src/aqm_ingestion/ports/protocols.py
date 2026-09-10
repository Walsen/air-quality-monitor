"""The ten ports: protocols the domain depends on.

Design decision DD1: every port is a ``Protocol`` with no implementation and NO
cloud type in any signature. That is not a stylistic preference — it is what lets
the entire suite run against in-memory adapters with no AWS credentials and no
network beyond localhost (Requirement 28.5), and what lets a cloud adapter be
swapped in without the domain noticing.

They live in ONE module because they are one boundary: a reader deciding whether
the domain has grown a hidden dependency should be able to see the whole surface
at once, and each port is only a handful of lines.

``WindowResult`` carries its ``truncated`` flag in the type, so Requirement 14.8's
"report truncation rather than silently returning a partial set" is enforced by the
signature rather than by a convention a caller can forget.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from aqm_ingestion.contract.records import SensorMetadataRecord
from aqm_ingestion.domain.association import LearnedThreshold
from aqm_ingestion.domain.models import CalibratedReading, DedupKey
from aqm_ingestion.domain.profile import UserProfile
from aqm_ingestion.domain.symptoms import SymptomEntry

__all__ = ["SymptomEntry", "UserProfile"]
"""Re-exported: the ProfileStore and SymptomLogStore ports are typed on them."""

# --- boundary value types -------------------------------------------------


@dataclass(frozen=True, slots=True)
class WindowResult:
    """Readings for a window, plus whether the set was cut short.

    ``truncated`` is part of the type because Requirement 14.8 forbids silently
    returning a partial set: a caller must be able to say "there was more" without
    guessing from a length it would have to know the limit to interpret.
    """

    readings: tuple[CalibratedReading, ...]
    truncated: bool = False


class UpsertOutcome(StrEnum):
    """What an upsert did, so a caller can count changes without re-reading."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """A registered site and whether it is currently active.

    ``active`` is stored rather than derived on read so the decision is made once,
    against the Clock, at upsert time (Requirement 2.8).
    """

    record: SensorMetadataRecord
    active: bool
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class NearestSite:
    """A site and its great-circle distance from a queried point."""

    entry: RegistryEntry
    distance_km: float


@dataclass(frozen=True, slots=True)
class ArchiveMeta:
    """What the archive records alongside a payload (Requirement 16.2).

    Every field is knowable BEFORE the payload is parsed, which Requirement 16.1
    demands: archiving precedes parsing, so a payload that fails to parse must
    still archive with complete metadata. That rules out anything only the
    contents could supply — a ``SiteCode`` above all.

    The archive identifier is deliberately absent: it is DERIVED from these fields
    plus the payload, so carrying it here would make the derivation circular. It is
    the write's return value instead.

    There is no field for a credential or a profile value, which is how
    Requirement 16.6 is honoured structurally rather than by convention.
    """

    ingested_at: dt.datetime
    transport: str
    source: str


@dataclass(frozen=True, slots=True)
class MetObservation:
    """Meteorology accompanying a site at an instant.

    Both fields are optional because a provider may know one and not the other,
    and the conversion stage must be able to tell "absent" from "zero".
    """

    temperature_k: float | None
    pressure_pa: float | None
    relative_humidity_pct: float | None = None


class PollenCategory(StrEnum):
    """Requirement 24.7's closed set of per-taxon pollen categories.

    The PROVIDER supplies the category, not a raw count this service classifies. That is not a
    stylistic choice: the spec defines Breakpoint_Tables for PM2.5 and NO2 in full detail and
    gives NO pollen thresholds anywhere, and Requirement 24.2 forbids this service deriving or
    modelling either value. Classifying a count would mean inventing bands the spec withholds.
    """

    NONE = "none"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    VERY_HIGH = "very_high"


@dataclass(frozen=True, slots=True)
class ForecastResult:
    """An external forecast, with a flag for a degraded answer.

    ``degraded`` lets the serving layer disclose that enrichment failed instead of
    presenting a gap as a fact.

    ``provider`` is required by Requirement 24.3, which asks for the provider identifier with
    EVERY forecast. Without it on the port the requirement could not be expressed at all — the
    serving layer would have to name a provider it had not been told about.
    """

    values: Mapping[str, float]
    provider: str = ""
    issued_at: dt.datetime | None = None
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class PollenResult:
    """External pollen data, with the same degradation disclosure.

    ``values`` maps a taxon to a CATEGORY rather than a count — see PollenCategory for why the
    provider owns that classification.
    """

    values: Mapping[str, PollenCategory]
    provider: str = ""
    issued_at: dt.datetime | None = None
    degraded: bool = False


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """The caller's identity, and NOTHING else.

    Requirement 29.4 forbids logging a claim beyond the user identity, and the
    cheapest way to honour that is for the boundary never to carry one: the
    authenticator returns the subject, not the token's claim set.
    """

    user_id: str


class RejectionCategory(StrEnum):
    """Why a credential was refused, for the auth-rejection counters.

    ``WRONG_AUDIENCE`` is one of the three conditions Requirement 18.3 names explicitly
    (unverifiable, expired, issued for a different audience), so the set has to be able to say
    it — while Requirement 18.3 equally forbids disclosing WHICH applied beyond the category, so
    the category is all that ever travels.
    """

    MISSING = "missing"
    MALFORMED = "malformed"
    EXPIRED = "expired"
    UNTRUSTED_ISSUER = "untrusted_issuer"
    WRONG_AUDIENCE = "wrong_audience"
    INVALID_SIGNATURE = "invalid_signature"


class AuthRejectedError(Exception):
    """A credential was refused, carrying only its category.

    The category is safe to log and to count; the credential itself never travels
    with the error (§7).
    """

    def __init__(self, category: RejectionCategory) -> None:
        """Record why the credential was refused."""
        super().__init__(f"credential rejected: {category}")
        self.category = category


@dataclass(frozen=True, slots=True)
class AuditIdentifiers:
    """The fields an Audit_Record holds (Requirement 25.7).

    GREW DELIBERATELY at task 24.3. Task 17.2 introduced this with three fields because
    Requirement 17.8's erasure was all that was needed then, and a test pinned the set so a
    silent addition would fail. Requirement 25.7 names the rest — the Breakpoint_Table, the
    Calibration_Strategy SET, whether a Threshold_Crossing was reported, and the contributing
    Readings' identifying fields — so the pin moved with the requirement rather than being
    quietly widened.

    Requirement 25.8 is what keeps it safe to grow: no Condition, no Sensitivity_Level, no
    Personal_Threshold VALUE, no User_Location coordinate, and no forecast or pollen value, so
    the
    audit trail never becomes a second copy of the health-adjacent data. Note the asymmetry that
    makes ``threshold_crossed`` a bool: 25.7 wants WHETHER a crossing was reported while 25.8
    forbids the threshold value, so a field holding the number would satisfy one and violate the
    other.

    Everything here except ``user_id`` is non-identifying, which is why Requirement 17.8's
    erasure can clear the identity and retain the record.
    """

    user_id: str
    served_at: dt.datetime
    route: str
    breakpoint_table: str | None = None
    calibration_strategies: tuple[str, ...] = ()
    threshold_crossed: bool = False
    record_references: tuple[str, ...] = ()


@runtime_checkable
class AuditStore(Protocol):
    """Records what was served, and supports erasure of a user's identity.

    Deliberately minimal here: task 24 owns the full audit trail, and this port carries only
    what Requirement 17.8's deletion needs. Introduced NOW rather than deferred because a
    deletion that removed the profile and left the audit identity behind would satisfy no
    reading of Requirement 17.8, and the gap would be invisible.
    """

    def append(self, identifiers: AuditIdentifiers) -> None:
        """Append one Audit_Record."""
        ...

    def forget_user(self, user_id: str) -> int:
        """Remove every identifying field for a user, keeping de-identified counts.

        Returns the number of records de-identified, so a deletion can be CONFIRMED
        (Requirement 17.8) rather than merely attempted.
        """
        ...

    def de_identified_count(self) -> int:
        """The retained count of served responses (Requirement 17.8)."""
        ...


# --- the nine ports -------------------------------------------------------


@runtime_checkable
class ReadingsStore(Protocol):
    """Stores and queries calibrated Readings."""

    def put(self, reading: CalibratedReading) -> None:
        """Store one Reading idempotently under its DedupKey."""
        ...

    def put_batch(self, readings: Sequence[CalibratedReading]) -> None:
        """Store many Readings; a failure must not leave a partial batch visible."""
        ...

    def get(self, key: DedupKey) -> CalibratedReading | None:
        """Return the Reading for a key, or None."""
        ...

    def query_window(
        self,
        site_code: str,
        species: frozenset[str] | None,
        start: dt.datetime,
        end: dt.datetime,
    ) -> WindowResult:
        """Return Readings whose interval start is in the half-open [start, end)."""
        ...

    def latest_per_species(
        self, site_codes: Sequence[str], not_before: dt.datetime
    ) -> Mapping[str, Sequence[CalibratedReading]]:
        """Return the most recent Reading per species per site, no older than given."""
        ...


@runtime_checkable
class SensorRegistryStore(Protocol):
    """Stores site metadata and answers geographic queries."""

    def upsert(
        self, record: SensorMetadataRecord, at: dt.datetime
    ) -> UpsertOutcome:
        """Register or update a site as of ``at``, reporting what changed."""
        ...

    def get(self, site_code: str) -> RegistryEntry | None:
        """Return a site's entry, or None."""
        ...

    def list_active(self) -> Sequence[RegistryEntry]:
        """Return the currently active sites in a defined order."""
        ...

    def nearest(
        self, lat: float, lon: float, n: int, max_km: float | None
    ) -> Sequence[NearestSite]:
        """Return the nearest active sites, closest first."""
        ...


@runtime_checkable
class RawArchive(Protocol):
    """Stores payloads byte-for-byte before anything derives from them."""

    def write(self, payload: bytes, meta: ArchiveMeta) -> str:
        """Archive a payload verbatim and return its archive id."""
        ...

    def read(self, archive_id: str) -> bytes:
        """Return the archived payload exactly as written."""
        ...


@runtime_checkable
class ProfileStore(Protocol):
    """Stores user profiles."""

    def get(self, user_id: str) -> UserProfile | None:
        """Return a profile, or None."""
        ...

    def put(self, profile: UserProfile) -> UserProfile:
        """Store a profile and return what was stored."""
        ...

    def delete(self, user_id: str) -> None:
        """Remove a profile; absent is not an error."""
        ...


@runtime_checkable
class SymptomLogStore(Protocol):
    """Stores the user's Symptom_Log (Requirement 31.1), keyed by verified identity.

    ``put`` REPLACES any entry for the same user and date rather than accumulating
    (Requirement 31.7): two entries for one day would let that day contribute twice to the
    Requirement 32 association. ``forget_user`` DELETES rather than de-identifying, because
    unlike an Audit_Record a Symptom_Entry carries real clinical content (Requirement 31.9).

    Retention is the adapter's responsibility and is applied at QUERY time against the
    injected Clock (Requirement 31.8), mirroring the ReadingsStore — so the exclusion moves
    with the clock and nothing has to run on a timer.

    THE LEARNED THRESHOLDS LIVE HERE RATHER THAN IN A PORT OF THEIR OWN, and the reason is
    erasure. They are DERIVED from the diary and share its whole lifecycle: recomputed from it,
    meaningless without it, and — the load-bearing part — erased with it. A separate store would
    need its own erasure path, which could fall out of step with Requirement 31.9's and leave an
    inference standing about a user whose underlying data was deleted. One store owning both
    makes ``forget_user`` a single call that cannot be half-done.
    """

    def put(self, entry: SymptomEntry) -> SymptomEntry:
        """Store one entry, replacing any existing entry for the same date."""
        ...

    def query_window(
        self, user_id: str, start: dt.date, end: dt.date
    ) -> Sequence[SymptomEntry]:
        """Return entries in the INCLUSIVE [start, end] range, retention already applied.

        Inclusive on both ends because the range is calendar dates rather than instants: a
        user asking for "the last week" means the day at each end, and a half-open range over
        dates would silently drop today.
        """
        ...

    def forget_user(self, user_id: str) -> int:
        """Delete every entry for a user and return how many were removed."""
        ...

    def learned_thresholds(self, user_id: str) -> Mapping[str, LearnedThreshold]:
        """Return this user's Learned_Thresholds by species, or an empty mapping.

        Requirement 32.12 forbids computing an association on the serving path: the derivation
        runs on its own schedule and STORES its result for the serving path to read, so this is
        the handoff point.
        """
        ...

    def put_learned_thresholds(
        self, user_id: str, thresholds: Sequence[LearnedThreshold]
    ) -> None:
        """Replace this user's Learned_Thresholds with a fresh derivation.

        REPLACES rather than merges: a re-derivation over a longer diary may legitimately stop
        supporting a species it previously supported, and merging would leave that stale
        threshold alerting forever with no data behind it.
        """
        ...

    def count_all(self) -> int:
        """Total entries held, for the erasure assertions and operational reporting."""
        ...


@runtime_checkable
class MeteorologyProvider(Protocol):
    """Supplies meteorology for unit conversion and humidity correction."""

    def observation(self, site_code: str, at: dt.datetime) -> MetObservation | None:
        """Return the observation for a site at an instant, or None."""
        ...


@runtime_checkable
class ForecastClient(Protocol):
    """Fetches external forecast and pollen data."""

    def forecast(self, lat: float, lon: float) -> ForecastResult:
        """Return a forecast for ROUNDED coordinates only (Requirement 24.8)."""
        ...

    def pollen(self, lat: float, lon: float) -> PollenResult:
        """Return pollen data for rounded coordinates only."""
        ...


@runtime_checkable
class MqttTransport(Protocol):
    """Delivers pushed records from a broker.

    ``messages`` yields a DELIVERY TAG alongside each payload, and acknowledgement is a
    separate call, because Requirement 4.6 requires acknowledging only AFTER the archive
    write succeeds. A port that merely yielded (topic, payload) could not express that at
    all: there would be nothing to acknowledge and no way to leave a message unacknowledged
    so the broker redelivers it.
    """

    def subscribe(self, topic_filter: str) -> None:
        """Subscribe to a topic filter."""
        ...

    def messages(self) -> Iterator[tuple[str, bytes, int]]:
        """Yield (topic, payload, delivery_tag) triples in arrival order."""
        ...

    def acknowledge(self, delivery_tag: int) -> None:
        """Acknowledge one delivered message (Requirement 4.6)."""
        ...

    def close(self) -> None:
        """Release the connection."""
        ...


@runtime_checkable
class FeedClient(Protocol):
    """Polls an external reference-contract feed.

    THE CREDENTIAL IS DELIBERATELY ABSENT from this interface. Requirement 5.2 puts it in an
    `X-API-KEY` header and forbids it reaching any log, response body, or the archive — so it
    belongs to the ADAPTER that makes the request, never to the poller that drives this port.
    A poller that cannot obtain the credential cannot log it, which turns §7's rule from a
    discipline into a structural guarantee.
    """

    def fetch_data(
        self, since: dt.datetime, until: dt.datetime, species: frozenset[str]
    ) -> bytes:
        """Return the `/SensorData` payload for the half-open [since, until)."""
        ...

    def fetch_sensors(self) -> bytes:
        """Return the `/ListSensors` payload for a registry refresh (Req 5.7)."""
        ...


@runtime_checkable
class Authenticator(Protocol):
    """Verifies a caller's credential."""

    def verify(self, credential: str) -> VerifiedIdentity:
        """Return the caller's identity.

        Raises:
            AuthRejectedError: carrying only the rejection category.
        """
        ...
