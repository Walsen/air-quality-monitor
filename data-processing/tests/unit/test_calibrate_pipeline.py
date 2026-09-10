"""Unit tests for RH resolution and domain flagging (task 7.3).

- 8.4: RH comes from the first available of three sources, in order — a meteorology
  CHANNEL record for the same SiteCode and interval, the MeteorologyProvider port, then
  no RH at all.
- 8.5: a configured mapping takes a `Species` value outside the contract's four
  permitted values to one of `rh`, `temperature`, `pressure`; empty by default.
- 8.7: out of domain, the strategy is STILL APPLIED, flagged `calibrated_extrapolated`,
  with one warning naming the input and its bound.
- 8.10: the reported value is retained alongside the corrected one.
- 8.11: the strategy identifier and the RH source are recorded on the reading.
- 3.6: a Species outside the permitted set that the mapping recognises is ROUTED, not
  rejected.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.adapters.memory import InMemoryMeteorologyProvider
from aqm_ingestion.contract.parser import RejectionKind, parse_data_records
from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.domain.calibration import CalibrationRegistry
from aqm_ingestion.domain.models import Confidence, QualityFlag
from aqm_ingestion.ingest.calibrate import (
    ChannelObservation,
    MeteorologyChannelMapping,
    calibrate_reading,
    channel_observations,
    resolve_humidity,
)
from aqm_ingestion.observability.logging import configure_logging
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

_T0 = dt.datetime(2026, 7, 1, 11, tzinfo=dt.UTC)
_MOMENT = "2026-07-01T11:00:00Z"
_REGISTRY = CalibrationRegistry.with_defaults()


def _record(**overrides: object) -> SensorDataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | overrides
    fields.setdefault("DateTime", _MOMENT)
    return SensorDataRecord(**fields)


def _payload(*overrides: dict[str, Any]) -> str:
    base = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD))
    return json.dumps([base | override for override in overrides])


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


# --- Req 8.5 the channel mapping -----------------------------------------

def test_mapping_is_empty_by_default() -> None:
    assert MeteorologyChannelMapping().is_empty


def test_mapping_recognises_a_configured_species() -> None:
    mapping = MeteorologyChannelMapping({"RH": "rh"})
    assert mapping.channel_for("RH") == "rh"
    assert mapping.channel_for("PM25") is None


def test_mapping_refuses_an_unknown_channel() -> None:
    # §5: only the three documented channels exist; a typo must fail loudly at
    # configuration time rather than silently dropping meteorology. The ignore is
    # deliberate — this mapping arrives from a config FILE at runtime, where the type
    # checker cannot help, so the runtime guard is the one that matters.
    with pytest.raises(ValueError, match="rh, temperature, pressure"):
        MeteorologyChannelMapping({"RH": "humidity"})  # type: ignore[dict-item]


def test_mapping_refuses_a_contract_species() -> None:
    # Req 8.5 says a value OUTSIDE the contract's four permitted values. Mapping PM25
    # to a channel would make the same value both a Reading and meteorology.
    with pytest.raises(ValueError, match="outside the contract"):
        MeteorologyChannelMapping({"PM25": "rh"})


# --- Req 3.6 routing rather than rejection -------------------------------

def test_unmapped_unknown_species_is_still_rejected() -> None:
    # unchanged behaviour: without a mapping, Req 3.3 governs
    result = parse_data_records(_payload({"Species": "RH"}))
    assert result.records == ()
    assert result.rejections[0].kind is RejectionKind.INVALID_ENUM


def test_mapped_unknown_species_is_routed_not_rejected() -> None:
    result = parse_data_records(
        _payload({"Species": "RH"}), meteorology_species=frozenset({"RH"})
    )
    assert result.rejections == ()
    assert result.records == ()  # Req 3.6: never stored as a Reading
    assert len(result.meteorology) == 1


def test_routing_keeps_the_element_uninterpreted() -> None:
    # the parser decides WHOSE an element is; what it MEANS belongs to calibration
    result = parse_data_records(
        _payload({"Species": "RH"}), meteorology_species=frozenset({"RH"})
    )
    assert result.meteorology[0]["Species"] == "RH"


def test_readings_survive_alongside_a_routed_meteorology_record() -> None:
    # Req 3.5's spirit: a sibling of another kind must not cost us the readings
    result = parse_data_records(
        _payload({}, {"Species": "RH", "ScaledValue": 55.0}),
        meteorology_species=frozenset({"RH"}),
    )
    assert len(result.records) == 1
    assert len(result.meteorology) == 1
    assert result.rejections == ()


def test_channel_observations_interprets_a_routed_element() -> None:
    element = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | {
        "Species": "RH",
        "ScaledValue": 62.5,
        "DateTime": _MOMENT,
    }
    mapping = MeteorologyChannelMapping({"RH": "rh"})
    observations = channel_observations([element], mapping)
    assert observations == (
        ChannelObservation(
            site_code="CB0001", interval_start=_T0, channel="rh", value=62.5
        ),
    )


def test_channel_observations_skips_an_unmapped_element() -> None:
    element = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD)) | {"Species": "XX"}
    assert channel_observations([element], MeteorologyChannelMapping({"RH": "rh"})) == ()


# --- Req 8.4 the resolution order ----------------------------------------

def test_channel_wins_over_the_provider() -> None:
    provider = InMemoryMeteorologyProvider()
    provider.set(site_code="CB0001", at=_T0, relative_humidity_pct=10.0)
    channel = ChannelObservation(
        site_code="CB0001", interval_start=_T0, channel="rh", value=70.0
    )
    resolved = resolve_humidity(
        site_code="CB0001",
        interval_start=_T0,
        channel_observations=(channel,),
        provider=provider,
    )
    assert resolved.value == 70.0
    assert resolved.source == "channel"


def test_provider_is_used_when_no_channel_record_matches() -> None:
    provider = InMemoryMeteorologyProvider()
    provider.set(site_code="CB0001", at=_T0, relative_humidity_pct=44.0)
    resolved = resolve_humidity(
        site_code="CB0001", interval_start=_T0, channel_observations=(), provider=provider
    )
    assert resolved.value == 44.0
    assert resolved.source == "provider"


def test_a_channel_record_for_another_site_does_not_apply() -> None:
    # Req 8.4 requires the SAME SiteCode; borrowing another site's humidity would
    # silently corrupt the correction
    channel = ChannelObservation(
        site_code="OTHER", interval_start=_T0, channel="rh", value=70.0
    )
    resolved = resolve_humidity(
        site_code="CB0001",
        interval_start=_T0,
        channel_observations=(channel,),
        provider=None,
    )
    assert resolved.value is None
    assert resolved.source == "none"


def test_a_channel_record_for_another_interval_does_not_apply() -> None:
    channel = ChannelObservation(
        site_code="CB0001",
        interval_start=_T0 + dt.timedelta(hours=1),
        channel="rh",
        value=70.0,
    )
    resolved = resolve_humidity(
        site_code="CB0001",
        interval_start=_T0,
        channel_observations=(channel,),
        provider=None,
    )
    assert resolved.source == "none"


def test_a_temperature_channel_is_not_mistaken_for_humidity() -> None:
    channel = ChannelObservation(
        site_code="CB0001", interval_start=_T0, channel="temperature", value=295.0
    )
    resolved = resolve_humidity(
        site_code="CB0001",
        interval_start=_T0,
        channel_observations=(channel,),
        provider=None,
    )
    assert resolved.source == "none"


def test_no_source_at_all_resolves_to_none() -> None:
    resolved = resolve_humidity(
        site_code="CB0001", interval_start=_T0, channel_observations=(), provider=None
    )
    assert resolved.value is None
    assert resolved.source == "none"


def test_a_provider_that_knows_the_site_but_not_the_humidity_yields_none() -> None:
    # MetObservation's RH is optional: knowing temperature is not knowing humidity
    provider = InMemoryMeteorologyProvider()
    provider.set(site_code="CB0001", at=_T0, temperature_k=295.0)
    resolved = resolve_humidity(
        site_code="CB0001", interval_start=_T0, channel_observations=(), provider=provider
    )
    assert resolved.source == "none"


# --- Req 8.10 / 8.11 the assembled reading -------------------------------

def test_reading_retains_both_values() -> None:
    reading = calibrate_reading(
        record=_record(Species="PM25", ScaledValue=100.0),
        humidity=50.0,
        humidity_source="provider",
        registry=_REGISTRY,
        strategy_name="rh_linear",
        ingested_at=_T0,
        archive_id="a1",
    )
    assert reading.reported_value == 100.0  # Req 8.10
    assert reading.corrected_value != 100.0  # a correction really was applied
    assert reading.corrected_value == pytest.approx(0.524 * 100.0 - 0.0862 * 50.0 + 5.75)


def test_reading_records_the_strategy_and_humidity_source() -> None:
    reading = calibrate_reading(
        record=_record(Species="PM25", ScaledValue=100.0),
        humidity=50.0,
        humidity_source="channel",
        registry=_REGISTRY,
        strategy_name="rh_linear",
        ingested_at=_T0,
        archive_id="a1",
    )
    assert reading.calibration_strategy == "rh_linear"  # Req 8.11
    assert reading.humidity_source == "channel"


def test_in_domain_reading_is_flagged_calibrated() -> None:
    reading = calibrate_reading(
        record=_record(Species="PM25", ScaledValue=100.0),
        humidity=50.0,
        humidity_source="provider",
        registry=_REGISTRY,
        strategy_name="rh_linear",
        ingested_at=_T0,
        archive_id="a1",
    )
    assert reading.quality_flag is QualityFlag.CALIBRATED
    assert reading.confidence is Confidence.HIGH


# --- Req 8.6 no RH -------------------------------------------------------

def test_absent_humidity_uses_the_fallback_and_is_uncalibrated() -> None:
    reading = calibrate_reading(
        record=_record(Species="PM25", ScaledValue=100.0),
        humidity=None,
        humidity_source="none",
        registry=_REGISTRY,
        strategy_name="rh_linear",
        ingested_at=_T0,
        archive_id="a1",
    )
    assert reading.corrected_value == 100.0  # identity applied
    assert reading.calibration_strategy == "identity"  # the fallback that ran
    assert reading.quality_flag is QualityFlag.UNCALIBRATED
    assert reading.confidence is Confidence.LOW


def test_absent_humidity_still_retains_the_reported_value() -> None:
    reading = calibrate_reading(
        record=_record(Species="PM25", ScaledValue=42.0),
        humidity=None,
        humidity_source="none",
        registry=_REGISTRY,
        strategy_name="rh_linear",
        ingested_at=_T0,
        archive_id="a1",
    )
    assert reading.reported_value == 42.0


def test_uncalibrated_is_not_reported_as_extrapolated() -> None:
    # two different conditions; conflating them would misdescribe the reading
    reading = calibrate_reading(
        record=_record(Species="PM25", ScaledValue=5_000.0),
        humidity=None,
        humidity_source="none",
        registry=_REGISTRY,
        strategy_name="rh_linear",
        ingested_at=_T0,
        archive_id="a1",
    )
    assert reading.quality_flag is QualityFlag.UNCALIBRATED


# --- Req 8.7 out of domain -----------------------------------------------

def test_out_of_domain_is_applied_and_flagged() -> None:
    reading = calibrate_reading(
        record=_record(Species="PM25", ScaledValue=400.0),  # domain max is 250
        humidity=50.0,
        humidity_source="provider",
        registry=_REGISTRY,
        strategy_name="rh_linear",
        ingested_at=_T0,
        archive_id="a1",
    )
    assert reading.quality_flag is QualityFlag.CALIBRATED_EXTRAPOLATED
    # still applied, not refused
    assert reading.corrected_value == pytest.approx(0.524 * 400.0 - 0.0862 * 50.0 + 5.75)


def test_out_of_domain_logs_one_warning_naming_input_and_bound(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    calibrate_reading(
        record=_record(Species="PM25", ScaledValue=400.0),
        humidity=95.0,
        humidity_source="provider",
        registry=_REGISTRY,
        strategy_name="rh_linear",
        ingested_at=_T0,
        archive_id="a1",
    )
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert len(warnings) == 1  # ONE, even though both inputs are out of domain
    breaches = " ".join(warnings[0]["breaches"])
    assert "400" in breaches
    assert "250" in breaches
    assert "95" in breaches
    assert "90" in breaches


def test_in_domain_logs_no_warning(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    calibrate_reading(
        record=_record(Species="PM25", ScaledValue=100.0),
        humidity=50.0,
        humidity_source="provider",
        registry=_REGISTRY,
        strategy_name="rh_linear",
        ingested_at=_T0,
        archive_id="a1",
    )
    warnings = [e for e in _events(capsys.readouterr().out) if e["level"] == "warning"]
    assert warnings == []


# --- Req 8.12 per-species strategy --------------------------------------

def test_no2_uses_identity_by_default() -> None:
    reading = calibrate_reading(
        record=_record(Species="NO2", ScaledValue=40.0),
        humidity=50.0,
        humidity_source="provider",
        registry=_REGISTRY,
        strategy_name=None,  # resolve from the per-species default
        ingested_at=_T0,
        archive_id="a1",
    )
    assert reading.calibration_strategy == "identity"
    assert reading.corrected_value == 40.0


def test_pm25_uses_rh_linear_by_default() -> None:
    reading = calibrate_reading(
        record=_record(Species="PM25", ScaledValue=100.0),
        humidity=50.0,
        humidity_source="provider",
        registry=_REGISTRY,
        strategy_name=None,
        ingested_at=_T0,
        archive_id="a1",
    )
    assert reading.calibration_strategy == "rh_linear"
