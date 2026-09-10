"""The feed poller: a pull-path entry point over the same Ingest_Pipeline.

Symmetric with :mod:`aqm_ingestion.ingest.mqtt_entry` and equally thin. It owns three
policies and nothing else: which window to request, when to refresh the registry, and how far
to advance ingestion state. Requirement 5.4 requires the pull path to produce the SAME
Calibrated_Reading as the push path for byte-identical input, and the only way to guarantee
that rather than test for it is to share one pipeline — which is why neither entry point
contains a stage.

**The credential never passes through this module.** Requirement 5.2 forbids it reaching any
log, response body, or the archive, and the FeedClient port deliberately does not carry one:
the adapter that makes the HTTP request holds it. A poller that cannot obtain the credential
cannot leak it, which turns §7's rule from a discipline into a structural property. The
resolver here exists only so an entry-point script can hand the value to the ADAPTER.

**Why the high-water mark advances only on success** (Requirement 5.6). It is derived from
the store rather than tracked separately, so a failed poll leaves it where it was for free —
there is no separate cursor that could advance while ingestion did not. An empty window and a
broken feed must NOT look alike, which is why an unparseable body is a failure rather than a
poll that found nothing: the latter would silently advance past data the feed never delivered.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

from aqm_ingestion.contract.parser import ParseError, parse_metadata_records
from aqm_ingestion.ingest.archive import ArchiveWriteFailedError
from aqm_ingestion.ingest.pipeline import IngestPipeline
from aqm_ingestion.observability.logging import get_logger, log_handled_error
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import (
    ArchiveMeta,
    FeedClient,
    ReadingsStore,
    SensorRegistryStore,
)

_logger = get_logger("ingest.feed")

DEFAULT_OVERLAP_HOURS = 2
"""Requirement 5.3's default overlap, re-requesting recent data so a late arrival is caught."""

DEFAULT_BACKFILL_HOURS = 24
"""Requirement 5.3's default initial backfill span."""

DEFAULT_MAX_RECORDS = 500_000
"""Requirement 5.8's default per-invocation record bound."""

DEFAULT_REGISTRY_REFRESH_HOURS = 24
"""Requirement 5.7's default registry-refresh cadence."""

DEFAULT_SPECIES: frozenset[str] = frozenset({"PM25", "NO2"})


@dataclass(frozen=True, slots=True)
class FeedSettings:
    """The configured values the poller needs — narrow, not a whole config (§1)."""

    enabled: bool = True
    species: frozenset[str] = DEFAULT_SPECIES
    overlap_hours: int = DEFAULT_OVERLAP_HOURS
    backfill_hours: int = DEFAULT_BACKFILL_HOURS
    max_records: int = DEFAULT_MAX_RECORDS
    registry_refresh_hours: int = DEFAULT_REGISTRY_REFRESH_HOURS


@dataclass(frozen=True, slots=True)
class PollReport:
    """What one invocation did, including the exit status of Requirement 5.6."""

    received: int = 0
    accepted: int = 0
    deduplicated: int = 0
    quarantined: int = 0
    exit_code: int = 0
    window: tuple[dt.datetime, dt.datetime] | None = None
    remaining_window: tuple[dt.datetime, dt.datetime] | None = None


def resolve_credential(
    *, env_var: str = "AQM_FEED_API_KEY", path: str | None = None
) -> str:
    """Resolve the feed credential from the environment or a supplied path (Req 5.2).

    Those two sources ONLY — never a committed file, never a default (§7). Trailing
    whitespace is stripped because a key file written by an operator usually ends in a
    newline, and sending that produces a puzzling 401 rather than an obvious error.

    Raises:
        ValueError: naming the SOURCE that was empty and never the value, so a failure path
            cannot echo credential material either.
    """
    if path is not None:
        content = Path(path).read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError(f"feed credential file {path!r} is empty")
        return content

    value = os.environ.get(env_var, "").strip()
    if not value:
        raise ValueError(
            f"feed credential is not set: expected environment variable {env_var} or a "
            "runtime-supplied path"
        )
    return value


class FeedPoller:
    """Drives the pull path for one invocation."""

    def __init__(
        self,
        client: FeedClient,
        pipeline: IngestPipeline,
        readings: ReadingsStore,
        registry: SensorRegistryStore,
        clock: Clock,
        settings: FeedSettings,
    ) -> None:
        """Hold the ports, the clock, and the settings."""
        self._client = client
        self._pipeline = pipeline
        self._readings = readings
        self._registry = registry
        self._clock = clock
        self._settings = settings
        self._registry_refreshed_at: dt.datetime | None = None

    def poll(self) -> PollReport:
        """Run one poll invocation.

        Returns:
            The report, whose ``exit_code`` is non-zero exactly when Requirement 5.6's
            failure conditions occurred and ingestion state was therefore not advanced.
        """
        if not self._settings.enabled:
            _logger.info("feed_disabled", reason="pull_interface_disabled_by_config")
            return PollReport()

        started = self._clock.now()
        self._maybe_refresh_registry(started)

        window = self._derive_window(started)
        try:
            payload = self._client.fetch_data(window[0], window[1], self._settings.species)
        except (OSError, TimeoutError, RuntimeError) as error:
            return self._fail("feed_fetch_failed", error, window)

        bounded, remaining = self._bound(payload, window)
        if bounded is None:
            return self._fail(
                "feed_body_unparseable",
                ValueError("payload rejected wholesale by the Parser"),
                window,
            )

        meta = ArchiveMeta(
            ingested_at=started,
            transport="feed",
            source=f"{window[0].isoformat()}/{window[1].isoformat()}",
        )
        try:
            summary = self._pipeline.ingest(bounded, meta)
        except ArchiveWriteFailedError as error:
            # Requirement 16.7 and 5.6 agree here: nothing was processed, so the window must
            # be retried and the invocation must fail.
            return self._fail("feed_archive_failed", error, window)

        report = PollReport(
            received=summary.received,
            accepted=summary.accepted,
            deduplicated=summary.deduplicated,
            quarantined=summary.quarantined,
            exit_code=0,
            window=window,
            remaining_window=remaining,
        )
        self._log_summary(report, started)
        return report

    def _derive_window(
        self, now: dt.datetime
    ) -> tuple[dt.datetime, dt.datetime]:
        """Derive the poll window (Requirement 5.3).

        From the most recent ingested interval MINUS the overlap, or the initial backfill
        span when nothing has been ingested. Derived from the STORE rather than a separate
        cursor, so a failed poll cannot advance it (Requirement 5.6) without any extra care.
        """
        latest = self._latest_ingested(now)
        if latest is None:
            return (now - dt.timedelta(hours=self._settings.backfill_hours), now)
        return (latest - dt.timedelta(hours=self._settings.overlap_hours), now)

    def _latest_ingested(self, now: dt.datetime) -> dt.datetime | None:
        """The most recent ingested interval start for the requested species.

        Derived from the STORE, across the sites the registry knows, rather than from a
        separate cursor — which is what makes Requirement 5.6 hold for free: a failed poll
        cannot advance a mark that is read from stored data rather than written.

        An empty registry therefore yields no mark and the initial backfill span applies,
        which is the correct behaviour on a first run.
        """
        site_codes = [entry.record.SiteCode for entry in self._registry.list_active()]
        if not site_codes:
            return None

        latest: dt.datetime | None = None
        horizon = now - dt.timedelta(days=365)
        for readings in self._readings.latest_per_species(site_codes, horizon).values():
            for reading in readings:
                if reading.key.species not in self._settings.species:
                    continue
                if latest is None or reading.key.interval_start > latest:
                    latest = reading.key.interval_start
        return latest

    def _bound(
        self, payload: bytes, window: tuple[dt.datetime, dt.datetime]
    ) -> tuple[bytes | None, tuple[dt.datetime, dt.datetime] | None]:
        """Apply Requirement 5.8's record bound, oldest portion first.

        Returns ``(None, None)`` when the body cannot be read at all, which the caller turns
        into a Requirement 5.6 failure rather than an empty poll.
        """
        try:
            loaded = json.loads(payload.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return (None, None)

        if isinstance(loaded, dict):
            return (payload, None)
        if not isinstance(loaded, list):
            return (None, None)
        if len(loaded) <= self._settings.max_records:
            return (payload, None)

        # Oldest first (Requirement 5.8), so the next invocation resumes FORWARD from where
        # this one stopped and no interval is skipped.
        def interval_of(element: object) -> str:
            return (
                str(element.get("DateTime", ""))
                if isinstance(element, dict)
                else ""
            )

        ordered = sorted(loaded, key=interval_of)
        taken = ordered[: self._settings.max_records]
        boundary = interval_of(ordered[self._settings.max_records])
        remaining_start = (
            dt.datetime.strptime(boundary, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
            if boundary
            else window[0]
        )
        return (json.dumps(taken).encode("utf-8"), (remaining_start, window[1]))

    def _maybe_refresh_registry(self, now: dt.datetime) -> None:
        """Refresh the registry on its cadence (Requirement 5.7).

        A refresh failure does NOT fail the invocation: readings are the point of the poll,
        and a stale registry only costs geographic results, which Requirement 6.7 already
        tolerates. Losing readings would be worse.
        """
        due = self._registry_refreshed_at is None or (
            now - self._registry_refreshed_at
            >= dt.timedelta(hours=self._settings.registry_refresh_hours)
        )
        if not due:
            return
        try:
            payload = self._client.fetch_sensors()
            parsed = parse_metadata_records(payload.decode("utf-8", errors="replace"))
        except (OSError, TimeoutError, RuntimeError, ParseError) as error:
            log_handled_error(_logger, "registry_refresh_failed", error)
            return

        for record in parsed.records:
            self._registry.upsert(record, now)
        self._registry_refreshed_at = now
        _logger.info(
            "registry_refreshed",
            sites=len(parsed.records),
            rejected=len(parsed.rejections),
        )

    def _fail(
        self,
        event: str,
        error: Exception,
        window: tuple[dt.datetime, dt.datetime],
    ) -> PollReport:
        """Report a Requirement 5.6 failure: log it, advance nothing, exit non-zero."""
        log_handled_error(
            _logger,
            event,
            error,
            window_start=window[0].isoformat(),
            window_end=window[1].isoformat(),
        )
        return PollReport(exit_code=1, window=window)

    def _log_summary(self, report: PollReport, started: dt.datetime) -> None:
        """Emit Requirement 5.5's one structured summary."""
        window = report.window or (started, started)
        _logger.info(
            "feed_poll",
            window_start=window[0].isoformat(),
            window_end=window[1].isoformat(),
            received=report.received,
            accepted=report.accepted,
            deduplicated=report.deduplicated,
            quarantined=report.quarantined,
            elapsed_seconds=(self._clock.now() - started).total_seconds(),
        )


