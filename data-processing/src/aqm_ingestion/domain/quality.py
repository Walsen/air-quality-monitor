"""Deriving the Quality_Flag and Confidence for one Reading.

Requirement 13 asks for exactly one flag and one confidence per reading, computed the same
way every time (13.12). Task 12.1 asks for it as ONE TOTAL FUNCTION, which is why this is
an ordered rule list over an explicit input record rather than conditionals scattered
through the pipeline: a total function's coverage can be checked by enumerating its input
space, and a test does exactly that over all 384 combinations.

**A precedence the requirements leave open.** Requirement 13.1 permits exactly one flag,
but a reading can be simultaneously faulted, conflicted, and uncalibrated, and no
criterion orders them. The order chosen here is fault, then conflict, then the calibration
outcome, on the reasoning that each earlier condition subsumes the later one as an
explanation: a misbehaving SENSOR accounts for a disagreement between two of its readings,
and a disagreement accounts for more than a missing humidity correction.

That choice is safe to make locally because all three map to `low` confidence
(Requirement 13.2), so the precedence affects only the label an operator reads, never what
a consumer is told it may trust. A test asserts that.

**Why the caps compose rather than override.** Requirement 9.8 caps at medium and
Requirement 11.5 caps at low, and both are CEILINGS applied through the domain's single
confidence ordering — so a reading that is already `low` is not raised to `medium` by a
defaulted conversion, and the two caps cannot disagree about which confidence is lower.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from aqm_ingestion.domain.models import (
    Confidence,
    ConversionSource,
    QualityFlag,
    cap_confidence,
)


class CalibrationOutcome(StrEnum):
    """What calibration achieved for a reading.

    Deliberately its own type rather than a QualityFlag: this is an INPUT describing what
    happened during calibration, and reusing the output enum would blur which of the two
    a value is.
    """

    CALIBRATED = "calibrated"
    EXTRAPOLATED = "extrapolated"
    UNCALIBRATED = "uncalibrated"


class NowCastCoverage(StrEnum):
    """How well the NowCast window was covered.

    Four states, not three: Requirement 13.2's `incomplete` (some hours missing, but
    enough to compute a NowCast) and Requirement 11.5's `insufficient` (too few recent
    hours, so no NowCast at all) carry different confidences, and NOT_APPLICABLE is a
    third distinct thing — NO2 has no window to be incomplete, and treating its absence as
    incomplete would penalise NO2 for a rule about PM2.5.
    """

    NOT_APPLICABLE = "not_applicable"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True, slots=True)
class QualityInputs:
    """Everything the assessment depends on, and nothing else.

    A narrow record rather than a reading or a config object (§1): the assessor needs
    these five facts, and taking a whole reading would let it depend on values that must
    not influence a quality judgement.
    """

    calibration: CalibrationOutcome
    nowcast: NowCastCoverage
    conversion_source: ConversionSource | None
    dedup_conflict: bool
    fault_flagged: bool


@dataclass(frozen=True, slots=True)
class QualityAssessment:
    """Exactly one Quality_Flag and one Confidence (Requirement 13.1)."""

    flag: QualityFlag
    confidence: Confidence


# The calibration outcome's own flag and confidence, before the overriding conditions and
# the caps. A table rather than a branch chain, so a new outcome is one entry (§1).
_CALIBRATION_RESULT: dict[CalibrationOutcome, tuple[QualityFlag, Confidence]] = {
    CalibrationOutcome.CALIBRATED: (QualityFlag.CALIBRATED, Confidence.HIGH),
    CalibrationOutcome.EXTRAPOLATED: (
        QualityFlag.CALIBRATED_EXTRAPOLATED,
        Confidence.MEDIUM,
    ),
    CalibrationOutcome.UNCALIBRATED: (QualityFlag.UNCALIBRATED, Confidence.LOW),
}

# Requirement 13.2's window rule, applied only to an otherwise-calibrated reading: an
# incomplete window makes it medium. NOT_APPLICABLE and COMPLETE both leave it alone.
_COVERAGE_CEILING: dict[NowCastCoverage, Confidence | None] = {
    NowCastCoverage.NOT_APPLICABLE: None,
    NowCastCoverage.COMPLETE: None,
    NowCastCoverage.INCOMPLETE: Confidence.MEDIUM,
    NowCastCoverage.INSUFFICIENT: Confidence.LOW,  # Requirement 11.5's cap
}


def assess_quality(inputs: QualityInputs) -> QualityAssessment:
    """Derive the one Quality_Flag and Confidence for a reading.

    Total over its input space: every combination of the five inputs yields a permitted
    flag and a permitted confidence, which a test verifies by enumeration.

    Args:
        inputs: the five facts the assessment depends on.

    Returns:
        Exactly one flag and one confidence (Requirements 13.1, 13.2, 13.12).
    """
    flag = _select_flag(inputs)
    confidence = _derive_confidence(inputs, flag)
    return QualityAssessment(flag=flag, confidence=confidence)


def _select_flag(inputs: QualityInputs) -> QualityFlag:
    """Pick the single flag, most-explanatory condition first.

    See the module docstring for why this order, and why choosing it locally is safe.
    """
    if inputs.fault_flagged:
        # Requirement 13.3: flagged, NOT quarantined — a suspected fault is an
        # assessment rather than a validation failure, so the reading is kept.
        return QualityFlag.SUSPECT_FAULT
    if inputs.dedup_conflict:
        return QualityFlag.SUSPECT_CONFLICT
    return _CALIBRATION_RESULT[inputs.calibration][0]


def _derive_confidence(inputs: QualityInputs, flag: QualityFlag) -> Confidence:
    """Derive the Confidence and apply every cap (Requirements 13.2, 9.8, 11.5)."""
    if flag in (QualityFlag.SUSPECT_FAULT, QualityFlag.SUSPECT_CONFLICT):
        # Requirement 13.2 puts both at low outright. No cap can lower it further, and
        # none may raise it, so this returns directly.
        return Confidence.LOW

    confidence = _CALIBRATION_RESULT[inputs.calibration][1]

    coverage_ceiling = _COVERAGE_CEILING[inputs.nowcast]
    if coverage_ceiling is not None:
        confidence = cap_confidence(confidence, coverage_ceiling)

    if inputs.conversion_source == "default":
        # Requirement 9.8. Note the comparison is against the literal "default" and not
        # against None: PM2.5 needs no conversion at all, and mistaking "no conversion"
        # for "a defaulted conversion" would penalise every PM2.5 reading.
        confidence = cap_confidence(confidence, Confidence.MEDIUM)

    return confidence
