"""Unit tests for the Quality_Assessor (task 12.1).

- 13.1: exactly ONE Quality_Flag from the five permitted values.
- 13.2: Confidence derived deterministically — high for `calibrated` with a complete
  NowCast window where NowCast applies, medium for `calibrated` with an incomplete window
  or for `calibrated_extrapolated`, low for `uncalibrated`, `suspect_fault` or
  `suspect_conflict` — plus Requirement 9.8's medium cap and Requirement 11.5's low cap.
- 13.3: a faulted reading is FLAGGED, not quarantined.
- 13.12: same inputs, same outputs, every evaluation.

"One total function" is the design constraint, so the last test enumerates the entire
input space (384 combinations) and asserts every one produces a permitted flag and a
permitted confidence. A gap in a nested conditional would show up there rather than
waiting for a real reading to find it.
"""

from __future__ import annotations

import itertools

import pytest

from aqm_ingestion.domain.models import Confidence, QualityFlag
from aqm_ingestion.domain.quality import (
    CalibrationOutcome,
    NowCastCoverage,
    QualityInputs,
    assess_quality,
)

_PERMITTED_FLAGS = {
    QualityFlag.CALIBRATED,
    QualityFlag.CALIBRATED_EXTRAPOLATED,
    QualityFlag.UNCALIBRATED,
    QualityFlag.SUSPECT_FAULT,
    QualityFlag.SUSPECT_CONFLICT,
}


def _inputs(**overrides: object) -> QualityInputs:
    base: dict[str, object] = {
        "calibration": CalibrationOutcome.CALIBRATED,
        "nowcast": NowCastCoverage.COMPLETE,
        "conversion_source": None,
        "dedup_conflict": False,
        "fault_flagged": False,
    }
    return QualityInputs(**(base | overrides))  # type: ignore[arg-type]


# --- Req 13.1 the five permitted flags -----------------------------------

def test_the_permitted_flags_are_exactly_the_five_documented() -> None:
    assert set(QualityFlag) == _PERMITTED_FLAGS


def test_a_clean_calibrated_reading_is_calibrated() -> None:
    assert assess_quality(_inputs()).flag is QualityFlag.CALIBRATED


def test_an_extrapolated_calibration_reports_extrapolated() -> None:
    result = assess_quality(_inputs(calibration=CalibrationOutcome.EXTRAPOLATED))
    assert result.flag is QualityFlag.CALIBRATED_EXTRAPOLATED


def test_an_uncalibrated_reading_reports_uncalibrated() -> None:
    result = assess_quality(_inputs(calibration=CalibrationOutcome.UNCALIBRATED))
    assert result.flag is QualityFlag.UNCALIBRATED


def test_a_dedup_conflict_reports_suspect_conflict() -> None:
    assert assess_quality(_inputs(dedup_conflict=True)).flag is QualityFlag.SUSPECT_CONFLICT


def test_a_flagged_fault_reports_suspect_fault() -> None:
    assert assess_quality(_inputs(fault_flagged=True)).flag is QualityFlag.SUSPECT_FAULT


# --- Req 13.1 the precedence when several conditions hold ----------------

def test_a_fault_outranks_a_conflict() -> None:
    # Req 13.1 permits exactly one flag but does not order these, so the choice is
    # documented in the module: a fault is a statement about the SENSOR, which subsumes
    # a disagreement between two of its readings
    result = assess_quality(_inputs(fault_flagged=True, dedup_conflict=True))
    assert result.flag is QualityFlag.SUSPECT_FAULT


def test_a_fault_outranks_an_uncalibrated_reading() -> None:
    result = assess_quality(
        _inputs(fault_flagged=True, calibration=CalibrationOutcome.UNCALIBRATED)
    )
    assert result.flag is QualityFlag.SUSPECT_FAULT


def test_a_conflict_outranks_an_uncalibrated_reading() -> None:
    result = assess_quality(
        _inputs(dedup_conflict=True, calibration=CalibrationOutcome.UNCALIBRATED)
    )
    assert result.flag is QualityFlag.SUSPECT_CONFLICT


def test_the_precedence_choice_does_not_change_the_confidence() -> None:
    # the reason the ordering is safe to choose: all three map to low, so precedence
    # affects only the LABEL an operator reads, never what a consumer may trust
    for overrides in (
        {"fault_flagged": True},
        {"dedup_conflict": True},
        {"calibration": CalibrationOutcome.UNCALIBRATED},
        {"fault_flagged": True, "dedup_conflict": True},
    ):
        assert assess_quality(_inputs(**overrides)).confidence is Confidence.LOW


# --- Req 13.2 the confidence derivation ----------------------------------

def test_calibrated_with_a_complete_window_is_high() -> None:
    result = assess_quality(_inputs(nowcast=NowCastCoverage.COMPLETE))
    assert result.confidence is Confidence.HIGH


def test_calibrated_with_no_nowcast_applicable_is_high() -> None:
    # NO2 takes its own 1-hour value (Req 11.10), so there is no window to be
    # incomplete; treating that as incomplete would penalise NO2 for a rule about PM2.5
    result = assess_quality(_inputs(nowcast=NowCastCoverage.NOT_APPLICABLE))
    assert result.confidence is Confidence.HIGH


def test_calibrated_with_an_incomplete_window_is_medium() -> None:
    result = assess_quality(_inputs(nowcast=NowCastCoverage.INCOMPLETE))
    assert result.confidence is Confidence.MEDIUM


def test_extrapolated_is_medium() -> None:
    result = assess_quality(
        _inputs(calibration=CalibrationOutcome.EXTRAPOLATED)
    )
    assert result.confidence is Confidence.MEDIUM


def test_extrapolated_with_a_complete_window_is_still_medium() -> None:
    # the extrapolation is the binding constraint, not the window
    result = assess_quality(
        _inputs(
            calibration=CalibrationOutcome.EXTRAPOLATED,
            nowcast=NowCastCoverage.COMPLETE,
        )
    )
    assert result.confidence is Confidence.MEDIUM


@pytest.mark.parametrize(
    "overrides",
    [
        {"calibration": CalibrationOutcome.UNCALIBRATED},
        {"fault_flagged": True},
        {"dedup_conflict": True},
    ],
)
def test_the_low_confidence_flags_are_low(overrides: dict[str, object]) -> None:
    assert assess_quality(_inputs(**overrides)).confidence is Confidence.LOW


# --- Req 9.8 the medium cap for a defaulted conversion -------------------

def test_a_defaulted_conversion_caps_confidence_at_medium() -> None:
    result = assess_quality(
        _inputs(nowcast=NowCastCoverage.COMPLETE, conversion_source="default")
    )
    assert result.confidence is Confidence.MEDIUM


def test_a_measured_conversion_does_not_cap() -> None:
    for source in ("channel", "provider"):
        result = assess_quality(
            _inputs(nowcast=NowCastCoverage.COMPLETE, conversion_source=source)
        )
        assert result.confidence is Confidence.HIGH


def test_no_conversion_at_all_does_not_cap() -> None:
    # PM2.5 needs no conversion, so None must not be mistaken for "defaulted"
    result = assess_quality(
        _inputs(nowcast=NowCastCoverage.COMPLETE, conversion_source=None)
    )
    assert result.confidence is Confidence.HIGH


def test_the_medium_cap_never_raises_a_low_confidence() -> None:
    result = assess_quality(
        _inputs(
            calibration=CalibrationOutcome.UNCALIBRATED, conversion_source="default"
        )
    )
    assert result.confidence is Confidence.LOW


# --- Req 11.5 the low cap for insufficient coverage ---------------------

def test_insufficient_nowcast_coverage_caps_confidence_at_low() -> None:
    result = assess_quality(_inputs(nowcast=NowCastCoverage.INSUFFICIENT))
    assert result.confidence is Confidence.LOW


def test_insufficient_coverage_does_not_change_the_flag() -> None:
    # Req 11.5 caps CONFIDENCE; the reading was still calibrated, and relabelling it
    # would claim a calibration problem that did not occur
    result = assess_quality(_inputs(nowcast=NowCastCoverage.INSUFFICIENT))
    assert result.flag is QualityFlag.CALIBRATED


def test_insufficient_coverage_is_distinct_from_incomplete() -> None:
    incomplete = assess_quality(_inputs(nowcast=NowCastCoverage.INCOMPLETE))
    insufficient = assess_quality(_inputs(nowcast=NowCastCoverage.INSUFFICIENT))
    assert incomplete.confidence is Confidence.MEDIUM
    assert insufficient.confidence is Confidence.LOW


# --- Req 13.3 flagged, not quarantined ----------------------------------

def test_a_fault_does_not_quarantine() -> None:
    # a suspected fault is an ASSESSMENT, not a validation failure, so the assessor
    # returns a flag and has no quarantine path at all
    result = assess_quality(_inputs(fault_flagged=True))
    assert result.flag is QualityFlag.SUSPECT_FAULT
    assert not hasattr(result, "quarantined")


# --- Req 13.12 determinism ----------------------------------------------

def test_the_same_inputs_give_the_same_assessment() -> None:
    inputs = _inputs(nowcast=NowCastCoverage.INCOMPLETE, conversion_source="default")
    assert assess_quality(inputs) == assess_quality(inputs)


def test_inputs_are_frozen() -> None:
    inputs = _inputs()
    with pytest.raises((AttributeError, TypeError)):
        inputs.dedup_conflict = True  # type: ignore[misc]


# --- totality -----------------------------------------------------------

def test_the_assessor_is_total_over_its_entire_input_space() -> None:
    # "one total function" is the design constraint (task 12.1), so every combination
    # must yield a permitted flag and a permitted confidence. A gap in a nested
    # conditional would surface here rather than on a real reading.
    combinations = itertools.product(
        list(CalibrationOutcome),
        list(NowCastCoverage),
        [None, "channel", "provider", "default"],
        [False, True],
        [False, True],
    )
    seen = 0
    for calibration, nowcast, conversion, conflict, fault in combinations:
        result = assess_quality(
            QualityInputs(
                calibration=calibration,
                nowcast=nowcast,
                conversion_source=conversion,  # type: ignore[arg-type]
                dedup_conflict=conflict,
                fault_flagged=fault,
            )
        )
        assert result.flag in _PERMITTED_FLAGS
        assert result.confidence in set(Confidence)
        seen += 1
    assert seen == 3 * 4 * 4 * 2 * 2  # 384: the whole space really was walked


def test_every_permitted_flag_is_reachable() -> None:
    # a total function that can never produce one of its outputs has a dead branch
    reachable = set()
    for calibration, nowcast, conversion, conflict, fault in itertools.product(
        list(CalibrationOutcome),
        list(NowCastCoverage),
        [None, "channel", "provider", "default"],
        [False, True],
        [False, True],
    ):
        reachable.add(
            assess_quality(
                QualityInputs(
                    calibration=calibration,
                    nowcast=nowcast,
                    conversion_source=conversion,  # type: ignore[arg-type]
                    dedup_conflict=conflict,
                    fault_flagged=fault,
                )
            ).flag
        )
    assert reachable == _PERMITTED_FLAGS


def test_every_confidence_is_reachable() -> None:
    reachable = {
        assess_quality(_inputs(**overrides)).confidence
        for overrides in (
            {},
            {"nowcast": NowCastCoverage.INCOMPLETE},
            {"fault_flagged": True},
        )
    }
    assert reachable == set(Confidence)
