"""The domain values that cross the port boundary.

Transcribed from the design's model section rather than invented, so the ports and
the later pipeline stages agree on shape from the start. Fields belonging to a
stage that does not exist yet are declared with a default of ``None`` so an early
stage can build a reading and a later one can fill the rest in — a
``CalibratedReading`` is assembled progressively by the pipeline, not in one place.

A ``CalibratedReading`` is deliberately NOT a contract record and is never
serialized as one: conflating this service's computed values with the emitting
network's contract is exactly what Requirement 1.7 exists to prevent.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

TransportName = Literal["mqtt", "feed"]
IndexMethod = Literal["nowcast", "hourly"]
HumiditySource = Literal["channel", "provider", "none"]
ConversionSource = Literal["channel", "provider", "default"]


class QualityFlag(StrEnum):
    """How much to trust a stored Reading."""

    CALIBRATED = "calibrated"
    CALIBRATED_EXTRAPOLATED = "calibrated_extrapolated"
    UNCALIBRATED = "uncalibrated"
    SUSPECT_FAULT = "suspect_fault"
    SUSPECT_CONFLICT = "suspect_conflict"


class Confidence(StrEnum):
    """Confidence in a served value."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Confidence ordered WORST to BEST. Declared here rather than in any one caller because
# several rules compare confidences — Requirement 9.8's cap, Requirement 11.5's cap, and
# Requirement 12.8's floor — and two orderings that disagreed would be a silent defect.
CONFIDENCE_ORDER: tuple[Confidence, ...] = (
    Confidence.LOW,
    Confidence.MEDIUM,
    Confidence.HIGH,
)


def confidence_rank(confidence: Confidence) -> int:
    """Rank a Confidence, higher meaning more trustworthy."""
    return CONFIDENCE_ORDER.index(confidence)


def lowest_confidence(confidences: Iterable[Confidence]) -> Confidence:
    """The least trustworthy of several confidences (Requirement 12.8).

    Raises:
        ValueError: if given none. An overall Confidence with no contributors would be
            an invented claim about data that does not exist.
    """
    ranked = sorted(confidences, key=confidence_rank)
    if not ranked:
        raise ValueError("cannot take the lowest of no confidences")
    return ranked[0]


def cap_confidence(confidence: Confidence, ceiling: Confidence) -> Confidence:
    """Cap a Confidence at a ceiling, never raising it.

    A CAP, not an assignment: a value already below the ceiling keeps its own, because a
    ceiling says "no better than this", not "exactly this".
    """
    return min((confidence, ceiling), key=confidence_rank)


@dataclass(frozen=True, slots=True)
class DedupKey:
    """The identity of one interval measurement (Requirement 7.1).

    Deduplication and idempotent storage both hinge on this being the WHOLE
    identity: a second record with the same key is the same measurement, whatever
    else it carries.
    """

    site_code: str
    species: str
    interval_start: dt.datetime
    duration: str


@dataclass(frozen=True, slots=True)
class RawReading:
    """A parsed, validated reading before calibration."""

    key: DedupKey
    reported_value: float
    units: str
    ratification_status: str
    sensor_contract: str
    ingested_at: dt.datetime
    transport: TransportName
    # The archive is written BEFORE the reading exists downstream (Requirement
    # 16.2), so every reading can name the payload it came from.
    archive_id: str


@dataclass(frozen=True, slots=True)
class CalibratedReading:
    """A reading after calibration, conversion, and index derivation.

    ``reported_value`` is retained alongside ``corrected_value`` for audit
    (Requirement 8.10): a consumer must be able to see what arrived as well as what
    this service computed from it.
    """

    key: DedupKey
    reported_value: float
    corrected_value: float
    units: str
    quality_flag: QualityFlag
    confidence: Confidence
    calibration_strategy: str
    breakpoint_table: str
    ratification_status: str
    ingested_at: dt.datetime
    archive_id: str
    # Populated by later stages; None until the stage that owns each one runs.
    mixing_ratio_ppb: float | None = None
    sub_index: int | None = None
    band: str | None = None
    method: IndexMethod | None = None
    humidity_source: HumiditySource = "none"
    conversion_source: ConversionSource | None = None
    conversion_temperature_k: float | None = None
    conversion_pressure_pa: float | None = None
    nowcast_window_hours: int | None = None
    nowcast_hours_available: int | None = None
    nowcast_weight_factor: float | None = None
