"""Property tests for Personal_Thresholds and escalation.

Feature: ingestion-and-serving-service, Property 33.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.domain.aqi.breakpoints import BreakpointTableRegistry
from aqm_ingestion.domain.escalation import (
    DEFAULT_ORANGE_BAND_LOWER,
    SENSITIVITY_ESCALATION,
    MeasuredSubIndex,
    ThresholdSource,
    evaluate_crossings,
    resolve_escalation,
)
from aqm_ingestion.domain.models import Confidence
from aqm_ingestion.domain.profile import (
    RECOGNIZED_CONSENT_VERSIONS,
    Condition,
    ProfileLimits,
    SensitivityLevel,
    UserProfile,
    build_profile,
)

_REGISTRY = BreakpointTableRegistry.with_defaults()
_TABLE_ID = "epa-2024-05-06"
_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_LIMITS = ProfileLimits(threshold_species=frozenset(_REGISTRY.species_for(_TABLE_ID)))
_SPECIES = ("PM25", "NO2")

_sub_indexes = st.integers(min_value=0, max_value=600)
_thresholds = st.integers(min_value=1, max_value=500)
_levels = st.sampled_from(list(SensitivityLevel))
_confidences = st.sampled_from(list(Confidence))


def _profile(
    level: SensitivityLevel, thresholds: dict[str, dict[str, object]] | None = None
) -> UserProfile:
    return build_profile(
        {
            "user_id": "user-1",
            "condition": Condition.ASTHMA,
            "sensitivity_level": level,
            "personal_thresholds": thresholds or {},
            "consent": {
                "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
                "given_at": _NOW,
            },
            "created_at": _NOW,
            "updated_at": _NOW,
        },
        limits=_LIMITS,
    )


@given(
    sub_index=_sub_indexes,
    threshold=_thresholds,
    species=st.sampled_from(_SPECIES),
    confidence=_confidences,
)
def test_property_33_crossing_holds_exactly_on_greater_or_equal(
    sub_index: int, threshold: int, species: str, confidence: Confidence
) -> None:
    """Feature: ingestion-and-serving-service, Property 33.

    Threshold crossing holds exactly on greater-or-equal.
    Validates Requirements 22.1, 22.3, 22.6 and 22.7.

    Requirement 22.7 is an IF AND ONLY IF, so this asserts BOTH directions against the same
    generated pair. One direction alone is trivially satisfiable — always reporting a crossing
    satisfies "reports when >=", and never reporting one satisfies "does not report when <".
    """
    profile = _profile(
        SensitivityLevel.STANDARD,
        {species: {"kind": "sub_index", "value": threshold}},
    )
    report = evaluate_crossings(
        [MeasuredSubIndex("SITE1", species, sub_index, confidence)],
        profile,
        registry=_REGISTRY,
        table_id=_TABLE_ID,
    )

    expected = sub_index >= threshold
    assert report.crossed is expected
    assert (len(report.crossings) == 1) is expected

    # Req 22.6: a reported crossing names the pair it was judged on, and Req 22.8: it carries
    # the Confidence of the value that drove it, whatever that Confidence is.
    if expected:
        crossing = report.crossings[0]
        assert crossing.sub_index == sub_index
        assert crossing.threshold == threshold
        assert crossing.species == species
        assert crossing.confidence is confidence
        assert crossing.source is ThresholdSource.PERSONAL_THRESHOLD

    # Req 22.9: the basis is reported either way.
    assert report.effective[species].sub_index == threshold


@given(level=_levels, sub_index=_sub_indexes)
def test_property_33_the_sensitivity_tier_is_used_when_no_personal_threshold_exists(
    level: SensitivityLevel, sub_index: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 33.

    Requirement 22.1's precedence holds for every Sensitivity_Level: with no Personal_Threshold,
    the mapped point governs, and the boundary is still greater-or-equal.

    Separate from the property above because that one always supplies a Personal_Threshold,
    so it exercises only the FIRST tier — a chain is not tested by its first link.
    """
    point = SENSITIVITY_ESCALATION[level]
    report = evaluate_crossings(
        [MeasuredSubIndex("SITE1", "PM25", sub_index, Confidence.HIGH)],
        _profile(level),
        registry=_REGISTRY,
        table_id=_TABLE_ID,
    )
    assert report.crossed is (sub_index >= point)
    assert report.effective["PM25"].source is ThresholdSource.SENSITIVITY_LEVEL

    # Req 22.2's stated purpose, asserted for every level rather than only the shipped table:
    # escalation never waits for the public Unhealthy threshold of 151.
    assert point <= DEFAULT_ORANGE_BAND_LOWER


@given(species=st.sampled_from(_SPECIES), concentration=st.floats(0.0, 500.0))
def test_property_33_a_concentration_threshold_is_judged_on_the_same_scale(
    species: str, concentration: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 33.

    Requirement 22.5: a concentration Personal_Threshold converts through the same
    Breakpoint_Table the response uses, so a reading AT the threshold concentration always
    crosses.

    This is the observable consequence of "comparable", and it is what would fail if the
    threshold were ever converted through a different table or a parallel calculation.
    """
    unit = {"PM25": "ug.m-3", "NO2": "ppb"}[species]
    profile = _profile(
        SensitivityLevel.STANDARD,
        {species: {"kind": "concentration", "value": concentration, "unit": unit}},
    )
    threshold_index = resolve_escalation(
        profile, species=species, registry=_REGISTRY, table_id=_TABLE_ID
    ).sub_index

    from aqm_ingestion.domain.aqi.subindex import compute_sub_index

    table = _REGISTRY.resolve(_TABLE_ID, species)
    measured = compute_sub_index(concentration, table).sub_index

    report = evaluate_crossings(
        [MeasuredSubIndex("SITE1", species, measured, Confidence.HIGH)],
        profile,
        registry=_REGISTRY,
        table_id=_TABLE_ID,
    )
    assert measured == threshold_index
    assert report.crossed is True
