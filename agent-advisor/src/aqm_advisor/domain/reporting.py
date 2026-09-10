"""Reporting the retrieved forecast, confidence, dose and pollen (Requirements 13-17).

**This module reads and relays. It computes nothing.** Four requirements forbid deriving
something — Req 13.5 (compute no forecast), Req 16 (explain the dose without recomputing it),
Req 12.1 (compute no weighting) and Req 15.6 (derive no second weakness signal from the nowcast
hour counts) — and they share one enforceable shape: there is no arithmetic operator anywhere in
this file. A test asserts that, which is worth more than four separate behavioural checks
because it fails on the FIRST multiplication someone adds rather than after a derived number has
reached a user.

**Req 15.6 in particular.** The confidence Service 2 returned is the single authority on how
weak a measurement is, because Service 2 has already capped it for an incomplete nowcast window.
A second derivation here could disagree with Service 2 about the same reading, and deciding
weakness from the hour counts is re-deriving part of the basis, which Req 9.4 forbids. A test
asserts no comparison puts the two hour counts on opposite sides, and carries a self-check plus
a near-miss guard — reading either count is fine, and reporting the window is Req 9.3's
traceability; only comparing them derives a verdict.

Every view fails toward disclosure. An absent confidence is not evidence of a good one, so a
missing measurement discloses rather than assuming the best.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

PARTICULATE_LAG_TEXT = (
    "Particulate effects can lag by about three days, so how you feel today may relate to "
    "exposure earlier in the week, and acting now can help before symptoms appear."
)
"""Req 13.2/13.4: a general pattern from the evidence base, not a prediction about
this user.

Written in words rather than digits on purpose. Every numeral in guidance must be a
Retrieved_Value (Req 7.1), and "about three days" needs no structural constant — where "about 3
days" would be an ungrounded numeral unless the constant happened to be configured.
"""

GASEOUS_SAME_DAY_TEXT = (
    "Gaseous pollutants such as nitrogen dioxide and ozone tend to affect people the same day, "
    "which is why they are worth treating differently from particulates."
)
"""Req 13.3: told apart from the lag. Conflating them would have a user relate
today's symptoms to the wrong day's exposure."""

TIMING_TEMPLATES: tuple[str, ...] = (
    "Levels are often lower earlier in the day than in the afternoon, so shifting outdoor time "
    "earlier can reduce what you take in.",
    "If the trend is rising, doing the outdoor part of your day sooner rather than "
    "later usually means less exposure for the same activity.",
    "A route away from traffic reduces exposure without changing what you had planned to do.",
)
"""Req 14.2/14.4: comparisons between periods the data supports, never a named clock
hour, and never an instruction to start or stop exercising. The framing is reducing
exposure while doing what you intend."""

_HIGHEST_CONFIDENCE = "high"
_QUALIFIED_FLAGS = frozenset(
    {"suspect_fault", "suspect_conflict", "uncalibrated", "calibrated_extrapolated"}
)
"""Req 15.4's qualified readings, plus the extrapolated calibration.

`calibrated_extrapolated` is included deliberately: it is a calibration that ran out of data,
which is squarely what Req 15.3's "never present a low-cost reading as reference-grade" is
about.
"""

_ELEVATED_POLLEN = frozenset({"moderate", "high", "very high", "very_high"})


def _as_int(value: object) -> int | None:
    """Narrow a served value to an int, or None when it is absent or not numeric.

    Narrowed rather than cast: a served value of an unexpected TYPE is a Service 2 change, and
    reading it as absent is honest where forcing it would either raise on the response path or
    invent a number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: object) -> float | None:
    """Narrow a served value to a float, or None when it is absent or not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class ForecastView:
    """The retrieved forecast, as served."""

    available: bool
    trend: str | None = None
    source: str | None = None
    tomorrow_aqi: int | None = None


@dataclass(frozen=True, slots=True)
class ConfidenceView:
    """What the retrieved measurement says about its own reliability."""

    confidence: str | None
    qualified: bool
    disclose: bool


@dataclass(frozen=True, slots=True)
class DoseView:
    """The retrieved inhaled dose and the basis Service 2 used to produce it."""

    available: bool
    dose: float | None = None
    basis: str | None = None
    unavailable_windows: int = 0


@dataclass(frozen=True, slots=True)
class PollenView:
    """The retrieved pollen outlook."""

    available: bool
    synergy: bool
    categories: Mapping[str, str] = field(default_factory=dict)


def forecast_view(forecast: Mapping[str, object] | None) -> ForecastView:
    """Read the retrieved forecast (Req 13.1, 7.4, 7.5).

    A `degraded` forecast yields NO next-day value (Req 7.5) but keeps its provider: saying "the
    forecast from X was unavailable" is honest, whereas an unattributed absence tells an
    operator nothing about what failed.

    Nothing is computed. Req 13.5 is explicit that this service's contribution is fusion rather
    than prediction, and the module-level arithmetic check is what keeps that true.
    """
    if forecast is None:
        return ForecastView(available=False)

    source = forecast.get("source")
    trend = forecast.get("trend")
    if bool(forecast.get("degraded", False)):
        return ForecastView(
            available=False,
            source=None if source is None else str(source),
            trend=None if trend is None else str(trend),
        )

    raw_value = forecast.get("tomorrowAqi")
    return ForecastView(
        available=True,
        trend=None if trend is None else str(trend),
        source=None if source is None else str(source),
        tomorrow_aqi=_as_int(raw_value),
    )


def confidence_view(measurement: Mapping[str, object] | None) -> ConfidenceView:
    """Read the driving measurement's confidence and quality flag (Req 15.1-15.4).

    `disclose` is true whenever the confidence is not the highest value (Req 15.2) or the
    reading carries a qualified flag (Req 15.4). Derived from what Service 2 returned and from
    nothing else — Req 15.6 makes that value the single authority on measurement weakness.

    A MISSING measurement discloses. An absent confidence is not evidence of a good one, and the
    fail-safe direction here is to caveat rather than to reassure.
    """
    if measurement is None:
        return ConfidenceView(confidence=None, qualified=False, disclose=True)

    raw_confidence = measurement.get("confidence")
    confidence = None if raw_confidence is None else str(raw_confidence)
    raw_flag = measurement.get("qualityFlag")
    qualified = raw_flag is not None and str(raw_flag).casefold() in _QUALIFIED_FLAGS
    below_best = confidence is None or confidence.casefold() != _HIGHEST_CONFIDENCE
    return ConfidenceView(
        confidence=confidence, qualified=qualified, disclose=below_best or qualified
    )


def dose_view(personalized: Mapping[str, object] | None) -> DoseView:
    """Read the retrieved inhaled dose (Req 16).

    The dose is EXPLAINED, never recomputed: Service 2 owns concentration times an
    activity-adjusted breathing rate over a duration, and this service repeats the number and
    the basis it was produced from.

    A dose of `None` is unavailable; a dose of zero is a measured value. Collapsing the two
    would report a real number the retrieval never produced.
    """
    if personalized is None:
        return DoseView(available=False)

    raw_dose = personalized.get("inhaledDose")
    raw_basis = personalized.get("doseBasis")
    raw_windows = personalized.get("unavailableDoseWindows", 0)
    return DoseView(
        available=raw_dose is not None,
        dose=_as_float(raw_dose),
        basis=None if raw_basis is None else str(raw_basis),
        unavailable_windows=_as_int(raw_windows) or 0,
    )


def pollen_view(
    personalized: Mapping[str, object] | None, pollution_elevated: bool = False
) -> PollenView:
    """Read the retrieved pollen outlook and decide whether the synergy applies (Req 17).

    Req 17.3 states the pollen-pollution synergy when BOTH are elevated. Flagging it on either
    alone would assert an interaction whenever one factor was present, which is a stronger claim
    than the requirement makes and than the evidence supports.
    """
    block = None if personalized is None else personalized.get("pollen")
    if not isinstance(block, Mapping):
        return PollenView(available=False, synergy=False)

    categories = {str(key): str(value) for key, value in block.items()}
    pollen_elevated = any(
        value.casefold() in _ELEVATED_POLLEN for value in categories.values()
    )
    return PollenView(
        available=True,
        synergy=pollen_elevated and pollution_elevated,
        categories=categories,
    )


__all__ = [
    "GASEOUS_SAME_DAY_TEXT",
    "PARTICULATE_LAG_TEXT",
    "TIMING_TEMPLATES",
    "ConfidenceView",
    "DoseView",
    "ForecastView",
    "PollenView",
    "confidence_view",
    "dose_view",
    "forecast_view",
    "pollen_view",
]
