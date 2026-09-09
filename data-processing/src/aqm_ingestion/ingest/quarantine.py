"""Screening a reading into acceptance or quarantine.

Requirement 6.10 says a quarantined record must never reach the ReadingsStore and
never appear in a Serving_Response. That is easy to promise and easy to break, so it
is expressed STRUCTURALLY here instead: :func:`screen_reading` returns a
:class:`ScreeningOutcome` whose ``accepted`` is ``None`` whenever the record was
quarantined. A caller cannot store a quarantined record by mistake, because there is
no storable value to hand to the store.

This lives in ``ingest/`` rather than ``domain/`` because it reports — it emits the
Requirement 6.9 warning and increments the Requirement 6.9 counter. The pure rules it
builds on stay in :mod:`aqm_ingestion.domain.validation`, which knows nothing about
logging or metrics.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass

from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.domain.validation import (
    QuarantineProblem,
    ValidationLimits,
    validate_reading,
)
from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.observability.metrics import MetricsRegistry

_logger = get_logger("ingest.quarantine")

DEFAULT_QUARANTINE_RETENTION_DAYS = 30


@dataclass(frozen=True, slots=True)
class QuarantinedRecord:
    """A record held out of storage, with everything needed to diagnose it.

    Requirement 6.8 fixes this field set. ``raw_record`` is the record AS RECEIVED
    so a fix can be verified against exactly what arrived, and the provenance
    fields tie it back to the archived payload it came from.

    There is no field for a profile value or a credential (§7): a quarantine record
    is diagnostic data an operator reads, and health-adjacent or secret material has
    no place in it.
    """

    raw_record: Mapping[str, object]
    reasons: tuple[QuarantineProblem, ...]
    ingested_at: dt.datetime
    transport: str
    archive_id: str

    @property
    def reason_categories(self) -> tuple[str, ...]:
        """The distinct reason categories, in a stable order (Requirement 6.9).

        Categories rather than details: an operator aggregates on an enumerable set,
        and the detail strings are for reading one record, not for grouping many.
        """
        seen: dict[str, None] = {}
        for problem in self.reasons:
            seen.setdefault(problem.reason.value, None)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class ScreeningOutcome:
    """Exactly one of the two: a storable reading, or a quarantined record.

    Both fields being optional looks permissive, but the invariant is enforced by
    construction — the only two constructors set one and leave the other None — and
    asserted by a test. This is what makes Requirement 6.10 structural.
    """

    accepted: SensorDataRecord | None = None
    quarantined: QuarantinedRecord | None = None


def quarantine_expires_at(
    ingested_at: dt.datetime,
    retention_days: int = DEFAULT_QUARANTINE_RETENTION_DAYS,
) -> dt.datetime:
    """When a quarantined record may be dropped (Requirement 6.8).

    Returned rather than enforced here: expiry is the store's job (a DynamoDB TTL
    attribute in the default adapter), and computing it in one place keeps the
    adapters from each picking their own window.
    """
    return ingested_at + dt.timedelta(days=retention_days)


def screen_reading(
    record: SensorDataRecord,
    *,
    now: dt.datetime,
    limits: ValidationLimits,
    transport: str,
    archive_id: str,
    metrics: MetricsRegistry | None = None,
) -> ScreeningOutcome:
    """Validate a reading, quarantining it with every reason if it fails.

    Args:
        record: a parsed record.
        now: the current instant from the caller's Clock (§2 — never read here).
        limits: the configured ceilings, skew tolerance, and retention window.
        transport: how the payload arrived, retained for diagnosis.
        archive_id: the archived payload this record came from (Requirement 16.2).
        metrics: optional registry for the per-reason counter. Optional because
            validating must not require an observability object (§1).

    Returns:
        An outcome carrying EITHER a storable record or a quarantined one, never
        both and never neither.
    """
    problems = validate_reading(record, now, limits)
    if not problems:
        return ScreeningOutcome(accepted=record)

    quarantined = QuarantinedRecord(
        raw_record=record.model_dump(),
        reasons=problems,
        ingested_at=now,
        transport=transport,
        archive_id=archive_id,
    )
    _report(quarantined, record, metrics)
    return ScreeningOutcome(quarantined=quarantined)


def _report(
    quarantined: QuarantinedRecord,
    record: SensorDataRecord,
    metrics: MetricsRegistry | None,
) -> None:
    """Emit ONE warning and count each reason (Requirement 6.9)."""
    categories = quarantined.reason_categories

    # One event per RECORD, not per reason: a record breaking four rules is one
    # operational fact, and four lines would misreport the volume of bad data.
    _logger.warning(
        "reading_quarantined",
        SiteCode=record.SiteCode,
        Species=record.Species,
        DateTime=record.DateTime,
        transport=quarantined.transport,
        archive_id=quarantined.archive_id,
        reasons=list(categories),
    )

    if metrics is not None:
        for category in categories:
            metrics.record_quarantined(reason=category)
