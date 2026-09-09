"""Unit tests for unit conversion (task 8.1).

- 9.1/9.2: the ideal-gas relation both ways, with R = 8.314462618 J/(mol·K) and NO2 at
  46.0055 g/mol, temperature and pressure as EXPLICIT parameters.
- 9.3: conditions resolved from channel, then provider, then the configured defaults of
  15 °C and 740 hPa — the latter reflecting the ~2,560 m elevation of the default
  deployment geography, which is the whole reason this requirement exists.
- 9.7: the values used and their source are recorded.
- 9.8: a defaulted conversion caps Confidence at medium.
- 9.9: a non-positive temperature or pressure makes the conversion UNAVAILABLE — no NO2
  sub-index, one error logged naming the offending value.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from typing import Any, cast

import pytest

from aqm_ingestion.adapters.memory import InMemoryMeteorologyProvider
from aqm_ingestion.domain.conversion import (
    DEFAULT_SITE_CONDITIONS,
    MOLAR_GAS_CONSTANT,
    NO2_MOLAR_MASS_G_MOL,
    ConversionUnavailableError,
    SiteConditions,
    conversion_factor,
    ppb_from_ug_m3,
    ug_m3_from_ppb,
)
from aqm_ingestion.domain.models import Confidence
from aqm_ingestion.ingest.calibrate import ChannelObservation
from aqm_ingestion.ingest.conditions import (
    cap_confidence_for_source,
    resolve_site_conditions,
)
from aqm_ingestion.observability.logging import configure_logging

_T0 = dt.datetime(2026, 7, 1, 11, tzinfo=dt.UTC)


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


# --- Req 9.2 the constants ------------------------------------------------

def test_constants_are_the_documented_values() -> None:
    assert MOLAR_GAS_CONSTANT == 8.314462618
    assert NO2_MOLAR_MASS_G_MOL == 46.0055


def test_default_conditions_reflect_the_deployment_altitude() -> None:
    # Req 9.3: 15 °C and 740 hPa, for ~2,560 m — NOT standard temperature and pressure,
    # which is exactly the error this requirement exists to prevent
    assert DEFAULT_SITE_CONDITIONS.temperature_k == pytest.approx(288.15)
    assert DEFAULT_SITE_CONDITIONS.pressure_pa == pytest.approx(74_000.0)


def test_defaults_are_not_sea_level() -> None:
    # a guard against someone "tidying" the defaults to STP later
    assert DEFAULT_SITE_CONDITIONS.pressure_pa < 101_325.0


# --- Req 9.6 the pinned reference factors --------------------------------

def test_factor_is_pinned_at_sea_level_reference() -> None:
    factor = conversion_factor(temperature_k=298.15, pressure_pa=101_325.0)
    assert factor == pytest.approx(1.8804, rel=1e-4)


def test_factor_is_pinned_at_altitude_reference() -> None:
    factor = conversion_factor(temperature_k=288.15, pressure_pa=74_000.0)
    assert factor == pytest.approx(1.4210, rel=1e-4)


def test_altitude_materially_changes_the_factor() -> None:
    # the two pinned points differ by ~24 percent; a conversion that ignored pressure
    # would overstate an NO2 sub-index at altitude by about that much
    sea_level = conversion_factor(temperature_k=298.15, pressure_pa=101_325.0)
    altitude = conversion_factor(temperature_k=288.15, pressure_pa=74_000.0)
    assert (sea_level - altitude) / sea_level > 0.2


# --- Req 9.1 / 9.2 both directions ---------------------------------------

def test_ug_m3_from_ppb_applies_the_factor() -> None:
    expected = 40.0 * conversion_factor(temperature_k=288.15, pressure_pa=74_000.0)
    assert ug_m3_from_ppb(
        40.0, temperature_k=288.15, pressure_pa=74_000.0
    ) == pytest.approx(expected)


def test_ppb_from_ug_m3_is_the_inverse() -> None:
    conditions = {"temperature_k": 288.15, "pressure_pa": 74_000.0}
    mass = ug_m3_from_ppb(40.0, **conditions)
    assert ppb_from_ug_m3(mass, **conditions) == pytest.approx(40.0, rel=1e-9)


def test_zero_converts_to_zero_in_both_directions() -> None:
    conditions = {"temperature_k": 288.15, "pressure_pa": 74_000.0}
    assert ug_m3_from_ppb(0.0, **conditions) == 0.0
    assert ppb_from_ug_m3(0.0, **conditions) == 0.0


# --- Req 9.5 the response to temperature and pressure --------------------

def test_factor_increases_with_pressure() -> None:
    lower = conversion_factor(temperature_k=288.15, pressure_pa=70_000.0)
    higher = conversion_factor(temperature_k=288.15, pressure_pa=101_325.0)
    assert higher > lower


def test_factor_decreases_with_temperature() -> None:
    cooler = conversion_factor(temperature_k=273.15, pressure_pa=101_325.0)
    warmer = conversion_factor(temperature_k=313.15, pressure_pa=101_325.0)
    assert cooler > warmer


# --- Req 9.9 unavailable conversion --------------------------------------

@pytest.mark.parametrize("temperature", [0.0, -1.0, -273.15])
def test_non_positive_temperature_is_refused(temperature: float) -> None:
    with pytest.raises(ConversionUnavailableError, match="temperature"):
        conversion_factor(temperature_k=temperature, pressure_pa=74_000.0)


@pytest.mark.parametrize("pressure", [0.0, -1.0])
def test_non_positive_pressure_is_refused(pressure: float) -> None:
    with pytest.raises(ConversionUnavailableError, match="pressure"):
        conversion_factor(temperature_k=288.15, pressure_pa=pressure)


def test_unavailable_error_names_the_offending_value() -> None:
    with pytest.raises(ConversionUnavailableError) as caught:
        conversion_factor(temperature_k=-5.0, pressure_pa=74_000.0)
    assert "-5.0" in str(caught.value)
    assert caught.value.field == "temperature_k"
    assert caught.value.value == -5.0


@pytest.mark.parametrize("bad", [math.inf, -math.inf, math.nan])
def test_non_finite_conditions_are_refused(bad: float) -> None:
    with pytest.raises(ConversionUnavailableError):
        conversion_factor(temperature_k=bad, pressure_pa=74_000.0)


def test_site_conditions_refuses_a_non_positive_value() -> None:
    with pytest.raises(ConversionUnavailableError):
        SiteConditions(temperature_k=0.0, pressure_pa=74_000.0)


# --- Req 9.3 resolution order --------------------------------------------

def test_channel_conditions_win() -> None:
    observations = (
        ChannelObservation(
            site_code="CB0001", interval_start=_T0, channel="temperature", value=290.0
        ),
        ChannelObservation(
            site_code="CB0001", interval_start=_T0, channel="pressure", value=75_000.0
        ),
    )
    resolved = resolve_site_conditions(
        site_code="CB0001",
        interval_start=_T0,
        observations=observations,
        provider=None,
    )
    assert resolved.conditions is not None
    assert resolved.conditions.temperature_k == 290.0
    assert resolved.conditions.pressure_pa == 75_000.0
    assert resolved.source == "channel"


def test_provider_conditions_are_used_next() -> None:
    provider = InMemoryMeteorologyProvider()
    provider.set(site_code="CB0001", at=_T0, temperature_k=291.0, pressure_pa=76_000.0)
    resolved = resolve_site_conditions(
        site_code="CB0001", interval_start=_T0, observations=(), provider=provider
    )
    assert resolved.conditions is not None
    assert resolved.conditions.temperature_k == 291.0
    assert resolved.source == "provider"


def test_defaults_are_used_last() -> None:
    resolved = resolve_site_conditions(
        site_code="CB0001", interval_start=_T0, observations=(), provider=None
    )
    assert resolved.conditions == DEFAULT_SITE_CONDITIONS
    assert resolved.source == "default"


def test_a_partial_channel_pair_does_not_half_apply() -> None:
    # temperature from a channel and pressure from nowhere would silently mix a
    # measured value with a default while reporting one source; Req 9.7 records ONE
    # source, so the pair must be resolved together
    observations = (
        ChannelObservation(
            site_code="CB0001", interval_start=_T0, channel="temperature", value=290.0
        ),
    )
    resolved = resolve_site_conditions(
        site_code="CB0001",
        interval_start=_T0,
        observations=observations,
        provider=None,
    )
    assert resolved.source == "default"
    assert resolved.conditions == DEFAULT_SITE_CONDITIONS


def test_another_sites_channel_conditions_do_not_apply() -> None:
    observations = (
        ChannelObservation(
            site_code="OTHER", interval_start=_T0, channel="temperature", value=290.0
        ),
        ChannelObservation(
            site_code="OTHER", interval_start=_T0, channel="pressure", value=75_000.0
        ),
    )
    resolved = resolve_site_conditions(
        site_code="CB0001",
        interval_start=_T0,
        observations=observations,
        provider=None,
    )
    assert resolved.source == "default"


def test_a_provider_knowing_only_temperature_falls_through_to_defaults() -> None:
    provider = InMemoryMeteorologyProvider()
    provider.set(site_code="CB0001", at=_T0, temperature_k=291.0)
    resolved = resolve_site_conditions(
        site_code="CB0001", interval_start=_T0, observations=(), provider=provider
    )
    assert resolved.source == "default"


def test_a_non_positive_provider_value_is_reported_not_used(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 9.9: an implausible measurement must not silently become a conversion
    configure_logging("info")
    provider = InMemoryMeteorologyProvider()
    provider.set(site_code="CB0001", at=_T0, temperature_k=-3.0, pressure_pa=75_000.0)
    resolved = resolve_site_conditions(
        site_code="CB0001", interval_start=_T0, observations=(), provider=provider
    )
    errors = [e for e in _events(capsys.readouterr().out) if e["level"] == "error"]
    assert len(errors) == 1
    assert "-3" in json.dumps(errors[0])
    assert resolved.available is False
    assert resolved.conditions is None


def test_available_resolution_carries_conditions() -> None:
    resolved = resolve_site_conditions(
        site_code="CB0001", interval_start=_T0, observations=(), provider=None
    )
    assert resolved.available is True
    assert resolved.conditions is not None


# --- Req 9.8 the confidence cap ------------------------------------------

def test_defaulted_conditions_cap_confidence_at_medium() -> None:
    assert cap_confidence_for_source(Confidence.HIGH, "default") is Confidence.MEDIUM


def test_measured_conditions_do_not_cap_confidence() -> None:
    assert cap_confidence_for_source(Confidence.HIGH, "channel") is Confidence.HIGH
    assert cap_confidence_for_source(Confidence.HIGH, "provider") is Confidence.HIGH


def test_the_cap_never_raises_confidence() -> None:
    # a cap is a ceiling, not an assignment: a low-confidence reading stays low
    assert cap_confidence_for_source(Confidence.LOW, "default") is Confidence.LOW
    assert cap_confidence_for_source(Confidence.LOW, "channel") is Confidence.LOW
