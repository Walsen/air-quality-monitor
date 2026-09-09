"""Unit tests for quarantine retention and reporting (task 5.2).

- 6.8: retain the record AS RECEIVED with its reasons, the ingestion instant, the
  transport, and the archive identifier, for the configured retention (30 days).
- 6.9: ONE structured warning per record naming SiteCode, Species, DateTime, and the
  reason CATEGORIES.
- 6.10: never written to the ReadingsStore, never in a Serving_Response.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.domain.validation import QuarantineReason, ValidationLimits
from aqm_ingestion.ingest.quarantine import (
    QuarantinedRecord,
    quarantine_expires_at,
    screen_reading,
)
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.observability.metrics import MetricsRegistry
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_LIMITS = ValidationLimits()
_ARCHIVE_ID = "a1b2c3"


def _record(**overrides: object) -> SensorDataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | overrides
    return SensorDataRecord(**fields)


def _valid() -> SensorDataRecord:
    return _record(DateTime="2026-07-01T11:00:00Z")


def _bad() -> SensorDataRecord:
    return _record(Species="PM25", ScaledValue=-1.0, DateTime="2026-07-01T11:00:00Z")


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


# --- Req 6.10 the accept / quarantine split ------------------------------

def test_a_valid_record_is_accepted() -> None:
    outcome = screen_reading(
        _valid(), now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
    )
    assert outcome.accepted is not None
    assert outcome.quarantined is None


def test_an_invalid_record_yields_no_accepted_record() -> None:
    # Req 6.10 is structural here: there is simply nothing storable to pass on, so
    # a caller CANNOT write a quarantined record to the ReadingsStore by mistake
    outcome = screen_reading(
        _bad(), now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
    )
    assert outcome.accepted is None
    assert outcome.quarantined is not None


def test_outcome_is_never_both() -> None:
    for record in (_valid(), _bad()):
        outcome = screen_reading(
            record, now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
        )
        assert (outcome.accepted is None) != (outcome.quarantined is None)


# --- Req 6.8 what is retained --------------------------------------------

def test_quarantined_record_retains_the_record_as_received() -> None:
    record = _bad()
    outcome = screen_reading(
        record, now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
    )
    assert outcome.quarantined is not None
    # as received: every contract field, unmodified
    assert outcome.quarantined.raw_record == record.model_dump()


def test_quarantined_record_retains_provenance() -> None:
    outcome = screen_reading(
        _bad(), now=_NOW, limits=_LIMITS, transport="feed", archive_id=_ARCHIVE_ID
    )
    assert outcome.quarantined is not None
    quarantined = outcome.quarantined
    assert quarantined.ingested_at == _NOW
    assert quarantined.transport == "feed"
    assert quarantined.archive_id == _ARCHIVE_ID


def test_quarantined_record_retains_every_reason() -> None:
    record = _record(
        Species="PM25",
        Units="ppb",
        ScaledValue=-1.0,
        Duration="PT1H",
        DateTime="2026-07-01T13:30:00Z",
    )
    outcome = screen_reading(
        record, now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
    )
    assert outcome.quarantined is not None
    assert len(outcome.quarantined.reasons) >= 4  # Req 6.1: all of them


def test_quarantined_record_is_frozen() -> None:
    outcome = screen_reading(
        _bad(), now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
    )
    assert outcome.quarantined is not None
    with pytest.raises((AttributeError, TypeError)):
        outcome.quarantined.transport = "changed"  # type: ignore[misc]


def test_retention_default_is_thirty_days() -> None:
    assert quarantine_expires_at(_NOW) == _NOW + dt.timedelta(days=30)


def test_retention_period_is_configurable() -> None:
    assert quarantine_expires_at(_NOW, retention_days=7) == _NOW + dt.timedelta(days=7)


# --- Req 6.9 one warning per record --------------------------------------

def test_exactly_one_warning_is_logged_per_quarantined_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    screen_reading(
        _bad(), now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
    )
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert len(warnings) == 1


def test_one_warning_even_when_many_rules_are_broken(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 6.9 says one warning PER RECORD, not one per reason — four faults must
    # not become four log lines
    configure_logging("info")
    screen_reading(
        _record(
            Species="PM25", Units="ppb", ScaledValue=-1.0, Duration="PT1H",
            DateTime="2026-07-01T13:30:00Z",
        ),
        now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID,
    )
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert len(warnings) == 1


def test_warning_names_the_documented_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    screen_reading(
        _bad(), now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
    )
    warning = next(
        e for e in _events(capsys.readouterr().out) if e["level"] == "warning"
    )
    assert warning["event"] == "reading_quarantined"
    assert warning["SiteCode"] == json.loads(GOLDEN_DATA_PAYLOAD)["SiteCode"]
    assert warning["Species"] == "PM25"
    assert warning["DateTime"] == "2026-07-01T11:00:00Z"
    assert QuarantineReason.VALUE_OUT_OF_RANGE.value in warning["reasons"]


def test_warning_carries_categories_not_free_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 6.9 asks for reason CATEGORIES: a stable enumerable set an operator can
    # aggregate on, rather than the human-readable detail strings
    configure_logging("info")
    screen_reading(
        _bad(), now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
    )
    warning = next(
        e for e in _events(capsys.readouterr().out) if e["level"] == "warning"
    )
    permitted = {reason.value for reason in QuarantineReason}
    assert set(warning["reasons"]) <= permitted


def test_no_warning_for_an_accepted_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    screen_reading(
        _valid(), now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
    )
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert warnings == []


# --- Req 6.9 the per-reason counter --------------------------------------

def test_each_reason_increments_its_own_counter() -> None:
    metrics = MetricsRegistry()
    screen_reading(
        _record(
            Species="PM25", Units="ppb", ScaledValue=-1.0, Duration="PT1H",
            DateTime="2026-07-01T13:30:00Z",
        ),
        now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID,
        metrics=metrics,
    )
    quarantine_counts = metrics.snapshot().counters["records_quarantined"]
    assert QuarantineReason.VALUE_OUT_OF_RANGE.value in quarantine_counts
    assert QuarantineReason.UNEXPECTED_UNIT.value in quarantine_counts
    assert QuarantineReason.FUTURE_DATED.value in quarantine_counts


def test_counters_accumulate_across_records() -> None:
    metrics = MetricsRegistry()
    for _ in range(3):
        screen_reading(
            _bad(), now=_NOW, limits=_LIMITS, transport="mqtt",
            archive_id=_ARCHIVE_ID, metrics=metrics,
        )
    counters = metrics.snapshot().counters["records_quarantined"]
    assert counters[QuarantineReason.VALUE_OUT_OF_RANGE.value] == 3


def test_metrics_are_optional() -> None:
    # §1: screening must not require a registry just to validate — the counter is
    # an observability concern, not a precondition
    outcome = screen_reading(
        _bad(), now=_NOW, limits=_LIMITS, transport="mqtt", archive_id=_ARCHIVE_ID
    )
    assert outcome.quarantined is not None


# --- Req 7 / §7 the retained record holds nothing sensitive --------------

def test_quarantined_record_has_no_profile_or_credential_field() -> None:
    assert set(QuarantinedRecord.__dataclass_fields__) == {
        "raw_record",
        "reasons",
        "ingested_at",
        "transport",
        "archive_id",
    }
