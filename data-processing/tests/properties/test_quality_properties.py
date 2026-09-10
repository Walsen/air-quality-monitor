"""Quality assessment property test (task 12.3).

Feature: ingestion-and-serving-service
- Property 31: confidence derivation is deterministic and respects its caps
  (Requirements 13.2, 13.12, 9.8)

The unit tests already enumerate the whole input space, so this property earns its place by
asserting the RELATIONAL claims enumeration does not: that adding a cap never raises a
confidence, that the flag and confidence stay mutually consistent, and that repeated
evaluation agrees.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.domain.models import (
    Confidence,
    ConversionSource,
    QualityFlag,
    confidence_rank,
)
from aqm_ingestion.domain.quality import (
    CalibrationOutcome,
    NowCastCoverage,
    QualityInputs,
    assess_quality,
)

_conversion: st.SearchStrategy[ConversionSource | None] = st.sampled_from(
    [None, "channel", "provider", "default"]
)
_LOW_FLAGS = {
    QualityFlag.UNCALIBRATED,
    QualityFlag.SUSPECT_FAULT,
    QualityFlag.SUSPECT_CONFLICT,
}


@st.composite
def _inputs(draw: st.DrawFn) -> QualityInputs:
    return QualityInputs(
        calibration=draw(st.sampled_from(list(CalibrationOutcome))),
        nowcast=draw(st.sampled_from(list(NowCastCoverage))),
        conversion_source=draw(_conversion),
        dedup_conflict=draw(st.booleans()),
        fault_flagged=draw(st.booleans()),
    )


@given(inputs=_inputs())
def test_property_31_confidence_derivation_is_deterministic_and_respects_its_caps(
    inputs: QualityInputs,
) -> None:
    """Feature: ingestion-and-serving-service, Property 31."""
    result = assess_quality(inputs)

    # Req 13.12: the same inputs always agree
    assert result == assess_quality(inputs)

    # Req 13.1: exactly one permitted flag, and one permitted confidence
    assert result.flag in set(QualityFlag)
    assert result.confidence in set(Confidence)

    # Req 13.2: the low-confidence flags are always low, whatever else is true
    if result.flag in _LOW_FLAGS:
        assert result.confidence is Confidence.LOW

    # Req 13.2: a value can only be high when nothing at all is degraded
    if result.confidence is Confidence.HIGH:
        assert result.flag is QualityFlag.CALIBRATED
        assert inputs.nowcast in (
            NowCastCoverage.COMPLETE,
            NowCastCoverage.NOT_APPLICABLE,
        )
        assert inputs.conversion_source != "default"


@given(inputs=_inputs())
def test_property_31_a_defaulted_conversion_never_raises_confidence(
    inputs: QualityInputs,
) -> None:
    """Feature: ingestion-and-serving-service, Property 31 (Req 9.8 is a CEILING).

    The cap must be monotone: taking the same inputs and defaulting the conversion can
    only lower the confidence or leave it, never lift it. A cap implemented as an
    assignment would raise a `low` reading to `medium` and fail here.
    """
    measured = assess_quality(
        QualityInputs(
            calibration=inputs.calibration,
            nowcast=inputs.nowcast,
            conversion_source="provider",
            dedup_conflict=inputs.dedup_conflict,
            fault_flagged=inputs.fault_flagged,
        )
    )
    defaulted = assess_quality(
        QualityInputs(
            calibration=inputs.calibration,
            nowcast=inputs.nowcast,
            conversion_source="default",
            dedup_conflict=inputs.dedup_conflict,
            fault_flagged=inputs.fault_flagged,
        )
    )
    assert confidence_rank(defaulted.confidence) <= confidence_rank(measured.confidence)
    # and the cap changes only the confidence, never the flag
    assert defaulted.flag == measured.flag


@given(inputs=_inputs())
def test_property_31_degrading_coverage_never_raises_confidence(
    inputs: QualityInputs,
) -> None:
    """Feature: ingestion-and-serving-service, Property 31 (coverage is monotone).

    Requirement 13.2 and Requirement 11.5 order the coverage states, so walking from
    complete to incomplete to insufficient must never increase the confidence.
    """
    ordered = (
        NowCastCoverage.COMPLETE,
        NowCastCoverage.INCOMPLETE,
        NowCastCoverage.INSUFFICIENT,
    )
    confidences = [
        assess_quality(
            QualityInputs(
                calibration=inputs.calibration,
                nowcast=coverage,
                conversion_source=inputs.conversion_source,
                dedup_conflict=inputs.dedup_conflict,
                fault_flagged=inputs.fault_flagged,
            )
        ).confidence
        for coverage in ordered
    ]
    ranks = [confidence_rank(confidence) for confidence in confidences]
    assert ranks == sorted(ranks, reverse=True)


@given(inputs=_inputs())
def test_property_31_a_fault_or_conflict_dominates_every_other_input(
    inputs: QualityInputs,
) -> None:
    """Feature: ingestion-and-serving-service, Property 31 (flag precedence).

    Requirement 13.3 makes a fault an assessment rather than a validation failure, so it
    must surface whatever the calibration outcome or coverage says — and it must never be
    masked by a cleaner-looking calibration.
    """
    faulted = assess_quality(
        QualityInputs(
            calibration=inputs.calibration,
            nowcast=inputs.nowcast,
            conversion_source=inputs.conversion_source,
            dedup_conflict=inputs.dedup_conflict,
            fault_flagged=True,
        )
    )
    assert faulted.flag is QualityFlag.SUSPECT_FAULT
    assert faulted.confidence is Confidence.LOW


@given(inputs=_inputs())
def test_property_31_the_flag_never_depends_on_the_conversion_source(
    inputs: QualityInputs,
) -> None:
    """Feature: ingestion-and-serving-service, Property 31 (Req 9.8's scope).

    Requirement 9.8 caps CONFIDENCE only. A defaulted conversion must not relabel a
    reading, because the calibration itself succeeded — claiming otherwise would report a
    calibration problem that never happened.
    """
    sources: tuple[ConversionSource | None, ...] = (
        None,
        "channel",
        "provider",
        "default",
    )
    flags = {
        assess_quality(
            QualityInputs(
                calibration=inputs.calibration,
                nowcast=inputs.nowcast,
                conversion_source=source,
                dedup_conflict=inputs.dedup_conflict,
                fault_flagged=inputs.fault_flagged,
            )
        ).flag
        for source in sources
    }
    assert len(flags) == 1
