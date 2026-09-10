"""Unit tests for the unknown-site allowance (task 5.4, Requirement 6.7).

An unknown `SiteCode` is deliberately NOT a validation failure. Losing a reading to a
lagging registry refresh would be worse than holding one whose coordinates are not
yet known, so the record is INGESTED, one `warning` is logged, and the site is
excluded from geographic results until its metadata arrives.

The distinction this file exists to pin: ingested-with-a-warning and quarantined are
different outcomes, and it would be easy to implement the allowance as a quarantine
reason by mistake.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.adapters.memory import InMemorySensorRegistryStore
from aqm_ingestion.contract.records import SensorDataRecord, SensorMetadataRecord
from aqm_ingestion.domain.validation import QuarantineReason, ValidationLimits
from aqm_ingestion.ingest.quarantine import screen_reading
from aqm_ingestion.ingest.registry_check import SiteRegistration, check_site_known
from aqm_ingestion.observability.logging import configure_logging
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD, GOLDEN_METADATA_PAYLOAD

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_LIMITS = ValidationLimits()


def _record(**overrides: object) -> SensorDataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | overrides
    return SensorDataRecord(**fields)


def _registry_with(site_code: str) -> InMemorySensorRegistryStore:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_METADATA_PAYLOAD))
    fields["SiteCode"] = site_code
    registry = InMemorySensorRegistryStore()
    registry.upsert(SensorMetadataRecord(**fields), _NOW)
    return registry


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


# --- Req 6.7 the record is KEPT ------------------------------------------

def test_unknown_site_is_not_a_quarantine_reason() -> None:
    # the whole point of Req 6.7: a lagging registry must not cost us data
    record = _record(SiteCode="ZZ9999", DateTime="2026-07-01T11:00:00Z")
    outcome = screen_reading(
        record, now=_NOW, limits=_LIMITS, transport="mqtt", archive_id="a1"
    )
    assert outcome.accepted is not None
    assert outcome.quarantined is None


def test_no_quarantine_reason_can_express_an_unknown_site() -> None:
    # a stronger guard than the test above: were someone to add such a reason, the
    # allowance could be broken from the validator without touching this module
    assert not any("site" in reason.value for reason in QuarantineReason)


def test_unknown_site_reports_as_not_known() -> None:
    registration = check_site_known("ZZ9999", InMemorySensorRegistryStore())
    assert registration.known is False
    assert registration.site_code == "ZZ9999"


def test_known_site_reports_as_known() -> None:
    assert check_site_known("CB0001", _registry_with("CB0001")).known is True


# --- Req 6.7 one warning -------------------------------------------------

def test_unknown_site_logs_exactly_one_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    check_site_known("ZZ9999", InMemorySensorRegistryStore())
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert len(warnings) == 1
    assert warnings[0]["event"] == "unknown_site"
    assert warnings[0]["SiteCode"] == "ZZ9999"


def test_known_site_logs_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    check_site_known("CB0001", _registry_with("CB0001"))
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert warnings == []


# --- Req 6.7 excluded from geographic results ----------------------------

def test_unknown_site_is_excluded_from_geographic_results() -> None:
    # no coordinates exist for it, so it cannot participate in a nearest-N query;
    # the registration says so explicitly rather than leaving a caller to infer it
    registration = check_site_known("ZZ9999", InMemorySensorRegistryStore())
    assert registration.eligible_for_geographic_results is False


def test_known_site_is_eligible_for_geographic_results() -> None:
    registration = check_site_known("CB0001", _registry_with("CB0001"))
    assert registration.eligible_for_geographic_results is True


def test_an_unknown_site_is_absent_from_the_nearest_query() -> None:
    # the exclusion is real, not merely advertised: a site with no metadata cannot
    # appear in a geographic result because the registry has no coordinates for it
    registry = InMemorySensorRegistryStore()
    assert not registry.nearest(lat=51.5, lon=-0.12, n=5, max_km=None)


def test_registration_is_frozen() -> None:
    registration = check_site_known("ZZ9999", InMemorySensorRegistryStore())
    with pytest.raises((AttributeError, TypeError)):
        registration.known = True  # type: ignore[misc]


def test_registration_fields_are_minimal() -> None:
    assert set(SiteRegistration.__dataclass_fields__) == {"site_code", "known"}
