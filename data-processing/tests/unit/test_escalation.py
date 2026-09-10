"""Unit tests for Personal_Thresholds and escalation (tasks 19.1 and 19.3).

Requirements 22.1 through 22.10.

THE DEFECT THIS TASK EXISTS TO FIX, found by reading Requirement 22.4 against the task-17 model:
`personal_thresholds` was `Mapping[str, float]`, a bare number per species. Requirement 22.4
accepts a threshold as EITHER a Sub_Index from 1 to 500 OR a concentration with its species and
unit — and a bare float cannot tell those apart. The two readings of the same number are not
close: PM2.5 at 35 µg/m³ is a Sub_Index around 100, the bottom of the Orange band, while
Sub_Index 35 is comfortably Good. A user who meant 35 µg/m³ and read as Sub_Index 35 would
warned almost continuously; read the other way round, they would never be warned at all. So the
threshold is now a model that says which it is, and the old shape could not have been validated
into correctness — it had to change type.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from aqm_ingestion.domain.aqi.breakpoints import BreakpointTableRegistry
from aqm_ingestion.domain.association import LearnedThreshold
from aqm_ingestion.domain.escalation import (
    DEFAULT_ORANGE_BAND_LOWER,
    SENSITIVITY_ESCALATION,
    CrossingReport,
    EffectiveEscalation,
    MeasuredSubIndex,
    ThresholdCrossing,
    ThresholdSource,
    evaluate_crossings,
    resolve_escalation,
)
from aqm_ingestion.domain.models import Confidence
from aqm_ingestion.domain.profile import (
    RECOGNIZED_CONSENT_VERSIONS,
    Condition,
    PersonalThreshold,
    ProfileLimits,
    SensitivityLevel,
    ThresholdKind,
    UserProfile,
    build_profile,
)

_REGISTRY = BreakpointTableRegistry.with_defaults()
_TABLE_ID = "epa-2024-05-06"
_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _profile_fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "user_id": "user-1",
        "condition": Condition.ASTHMA,
        "sensitivity_level": SensitivityLevel.STANDARD,
        "consent": {
            "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
            "given_at": _NOW,
        },
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return base | overrides


def _limits() -> ProfileLimits:
    # The species a threshold may name come from the Breakpoint_Table registry, passed in as a
    # narrow bound (§1) rather than the profile model reaching for a registry itself.
    return ProfileLimits(threshold_species=frozenset(_REGISTRY.species_for(_TABLE_ID)))


# --- Req 22.2 and 22.1: the escalation points, transcribed ---------------

def test_the_sensitivity_mapping_is_101_76_51() -> None:
    # Transcribed FROM Requirement 22.2, not read off the implementation.
    assert SENSITIVITY_ESCALATION[SensitivityLevel.STANDARD] == 101
    assert SENSITIVITY_ESCALATION[SensitivityLevel.ELEVATED] == 76
    assert SENSITIVITY_ESCALATION[SensitivityLevel.HIGH] == 51


def test_the_default_escalation_is_the_orange_band_lower_bound() -> None:
    assert DEFAULT_ORANGE_BAND_LOWER == 101


def test_the_mapping_never_reaches_the_public_unhealthy_threshold() -> None:
    # Req 22.2's stated purpose: a respiratory user escalates at the Orange_Band or earlier,
    # never at the public Unhealthy threshold of 151. This fails if a value is ever raised.
    assert all(value <= 101 for value in SENSITIVITY_ESCALATION.values())
    assert 151 not in SENSITIVITY_ESCALATION.values()


def test_every_sensitivity_level_has_an_escalation_point() -> None:
    assert set(SENSITIVITY_ESCALATION) == set(SensitivityLevel)


def test_a_more_sensitive_level_escalates_no_later() -> None:
    # The ordering is the point of the mapping, and pins the DIRECTION so a transposed table
    # (standard 51, high 101) fails even though it holds the same three numbers.
    assert (
        SENSITIVITY_ESCALATION[SensitivityLevel.HIGH]
        < SENSITIVITY_ESCALATION[SensitivityLevel.ELEVATED]
        < SENSITIVITY_ESCALATION[SensitivityLevel.STANDARD]
    )


# --- Req 22.1: precedence ------------------------------------------------

def test_a_personal_threshold_wins_over_the_sensitivity_level() -> None:
    profile = build_profile(
        _profile_fields(
            sensitivity_level=SensitivityLevel.HIGH,
            personal_thresholds={"PM25": {"kind": "sub_index", "value": 200}},
        ),
        limits=_limits(),
    )
    effective = resolve_escalation(
        profile, species="PM25", registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert effective.sub_index == 200
    assert effective.source is ThresholdSource.PERSONAL_THRESHOLD


def test_the_sensitivity_level_applies_where_no_personal_threshold_exists() -> None:
    profile = build_profile(
        _profile_fields(sensitivity_level=SensitivityLevel.ELEVATED), limits=_limits()
    )
    effective = resolve_escalation(
        profile, species="PM25", registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert effective.sub_index == 76
    assert effective.source is ThresholdSource.SENSITIVITY_LEVEL


def test_a_personal_threshold_for_another_species_does_not_apply() -> None:
    # Req 22.1 says a Personal_Threshold FOR THE SPECIES: a PM25 threshold must not govern NO2,
    # which a per-profile (rather than per-species) lookup would get wrong.
    profile = build_profile(
        _profile_fields(
            sensitivity_level=SensitivityLevel.HIGH,
            personal_thresholds={"PM25": {"kind": "sub_index", "value": 200}},
        ),
        limits=_limits(),
    )
    effective = resolve_escalation(
        profile, species="NO2", registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert effective.sub_index == 51
    assert effective.source is ThresholdSource.SENSITIVITY_LEVEL


def test_the_default_orange_band_applies_when_the_level_is_absent() -> None:
    # The third tier of Req 22.1. Reached via the explicit no-level path rather than by
    # constructing an unlawful profile.
    effective = resolve_escalation(
        None, species="PM25", registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert effective.sub_index == DEFAULT_ORANGE_BAND_LOWER
    assert effective.source is ThresholdSource.DEFAULT_ORANGE_BAND


def test_all_four_sources_are_reachable() -> None:
    # A precedence chain whose lowest tier is unreachable is as wrong as a missing tier.
    sources = set()
    for profile in (
        build_profile(
            _profile_fields(
                personal_thresholds={"PM25": {"kind": "sub_index", "value": 90}}
            ),
            limits=_limits(),
        ),
        build_profile(_profile_fields(), limits=_limits()),
    ):
        sources.add(
            resolve_escalation(
                profile, species="PM25", registry=_REGISTRY, table_id=_TABLE_ID
            ).source
        )
    sources.add(
        resolve_escalation(
            None, species="PM25", registry=_REGISTRY, table_id=_TABLE_ID
        ).source
    )
    # Requirement 32.7's tier, added by Requirement 32.
    sources.add(
        resolve_escalation(
            build_profile(_profile_fields(), limits=_limits()),
            species="PM25",
            registry=_REGISTRY,
            table_id=_TABLE_ID,
            learned=_learned(88),
        ).source
    )
    assert sources == set(ThresholdSource)


def _learned(sub_index: int) -> dict[str, LearnedThreshold]:
    return {
        "PM25": LearnedThreshold(
            species="PM25", sub_index=sub_index, lag_days=3, observations=20
        )
    }


# --- Req 22.1 / 32.7: a learned threshold never displaces a stated one ---

def test_a_stated_threshold_wins_over_a_learned_one() -> None:
    # Requirement 22.1: a Learned_Threshold "SHALL NOT displace" a Personal_Threshold, because a
    # threshold the user stated is their explicit instruction and inference does not overrule an
    # instruction. Tested in both directions — the pair below is the other order.
    profile = build_profile(
        _profile_fields(personal_thresholds={"PM25": {"kind": "sub_index", "value": 90}}),
        limits=_limits(),
    )
    effective = resolve_escalation(
        profile,
        species="PM25",
        registry=_REGISTRY,
        table_id=_TABLE_ID,
        learned=_learned(60),
    )
    assert effective.sub_index == 90
    assert effective.source is ThresholdSource.PERSONAL_THRESHOLD


def test_a_learned_threshold_wins_over_the_sensitivity_mapping() -> None:
    # The other order: with no stated threshold, the learned tier must take precedence over the
    # Sensitivity_Level default. Testing only the pair above would leave this tier dead.
    profile = build_profile(_profile_fields(), limits=_limits())
    effective = resolve_escalation(
        profile,
        species="PM25",
        registry=_REGISTRY,
        table_id=_TABLE_ID,
        learned=_learned(88),
    )
    assert effective.sub_index == 88
    assert effective.source is ThresholdSource.LEARNED


def test_a_learned_threshold_for_another_species_does_not_leak_across() -> None:
    # The escalation point is per SPECIES, as the module docstring insists; a learned NO2
    # threshold must not govern PM25.
    profile = build_profile(_profile_fields(), limits=_limits())
    learned = {
        "NO2": LearnedThreshold(
            species="NO2", sub_index=70, lag_days=0, observations=20
        )
    }
    effective = resolve_escalation(
        profile, species="PM25", registry=_REGISTRY, table_id=_TABLE_ID, learned=learned
    )
    assert effective.source is ThresholdSource.SENSITIVITY_LEVEL


# --- Req 22.5: a concentration threshold converts through the same table -

def test_a_concentration_threshold_converts_through_the_breakpoint_table() -> None:
    profile = build_profile(
        _profile_fields(
            personal_thresholds={
                "PM25": {"kind": "concentration", "value": 35.0, "unit": "ug.m-3"}
            }
        ),
        limits=_limits(),
    )
    effective = resolve_escalation(
        profile, species="PM25", registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert effective.source is ThresholdSource.PERSONAL_THRESHOLD
    # 35 µg/m³ sits near the bottom of the Orange band, NOT at Sub_Index 35 — which is the whole
    # reason the threshold has to say which unit it is expressed in.
    assert 95 <= effective.sub_index <= 105
    assert effective.sub_index != 35


def test_the_conversion_uses_the_same_table_the_response_uses() -> None:
    # Req 22.5's purpose is comparability, so the threshold's index must be exactly what the
    # response would report for that same concentration.
    from aqm_ingestion.domain.aqi.subindex import compute_sub_index

    table = _REGISTRY.resolve(_TABLE_ID, "PM25")
    expected = compute_sub_index(35.0, table).sub_index
    profile = build_profile(
        _profile_fields(
            personal_thresholds={
                "PM25": {"kind": "concentration", "value": 35.0, "unit": "ug.m-3"}
            }
        ),
        limits=_limits(),
    )
    effective = resolve_escalation(
        profile, species="PM25", registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert effective.sub_index == expected


def test_a_sub_index_threshold_is_not_converted() -> None:
    profile = build_profile(
        _profile_fields(
            personal_thresholds={"PM25": {"kind": "sub_index", "value": 35}}
        ),
        limits=_limits(),
    )
    effective = resolve_escalation(
        profile, species="PM25", registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert effective.sub_index == 35


# --- Req 22.3, 22.7: crossings on greater-or-equal ----------------------

def _measured(sub_index: int, *, confidence: Confidence = Confidence.HIGH) -> MeasuredSubIndex:
    return MeasuredSubIndex(
        site_code="SITE1", species="PM25", sub_index=sub_index, confidence=confidence
    )


def _standard_profile() -> UserProfile:
    return build_profile(
        _profile_fields(sensitivity_level=SensitivityLevel.STANDARD), limits=_limits()
    )


def test_a_sub_index_equal_to_the_threshold_is_a_crossing() -> None:
    # Req 22.3 says equality IS a crossing, and a strict > is the natural mistake.
    report = evaluate_crossings(
        [_measured(101)], _standard_profile(), registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert report.crossed is True
    assert len(report.crossings) == 1


def test_a_sub_index_one_below_the_threshold_is_not_a_crossing() -> None:
    report = evaluate_crossings(
        [_measured(100)], _standard_profile(), registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert report.crossed is False
    assert report.crossings == ()


def test_a_sub_index_above_the_threshold_is_a_crossing() -> None:
    report = evaluate_crossings(
        [_measured(150)], _standard_profile(), registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert report.crossed is True


# --- Req 22.6: what a crossing reports ---------------------------------

def test_a_crossing_names_its_site_species_sub_index_and_threshold() -> None:
    report = evaluate_crossings(
        [_measured(120)], _standard_profile(), registry=_REGISTRY, table_id=_TABLE_ID
    )
    crossing = report.crossings[0]
    assert crossing.site_code == "SITE1"
    assert crossing.species == "PM25"
    assert crossing.sub_index == 120
    assert crossing.threshold == 101


def test_the_report_names_the_threshold_source() -> None:
    report = evaluate_crossings(
        [_measured(120)], _standard_profile(), registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert report.crossings[0].source is ThresholdSource.SENSITIVITY_LEVEL


def test_every_crossing_is_listed_not_just_the_first() -> None:
    measurements = [
        MeasuredSubIndex(
            site_code=site, species="PM25", sub_index=150, confidence=Confidence.HIGH
        )
        for site in ("SITE1", "SITE2", "SITE3")
    ]
    report = evaluate_crossings(
        measurements, _standard_profile(), registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert len(report.crossings) == 3


def test_crossings_are_ordered_deterministically() -> None:
    # §2: the order reaches the response, so it must not depend on input order.
    forwards = evaluate_crossings(
        [
            MeasuredSubIndex("SITE2", "PM25", 150, Confidence.HIGH),
            MeasuredSubIndex("SITE1", "NO2", 150, Confidence.HIGH),
        ],
        _standard_profile(),
        registry=_REGISTRY,
        table_id=_TABLE_ID,
    )
    backwards = evaluate_crossings(
        [
            MeasuredSubIndex("SITE1", "NO2", 150, Confidence.HIGH),
            MeasuredSubIndex("SITE2", "PM25", 150, Confidence.HIGH),
        ],
        _standard_profile(),
        registry=_REGISTRY,
        table_id=_TABLE_ID,
    )
    assert forwards.crossings == backwards.crossings


# --- Req 22.9: the effective escalation is reviewable -------------------

def test_the_report_carries_the_effective_escalation_per_species() -> None:
    profile = build_profile(
        _profile_fields(
            sensitivity_level=SensitivityLevel.STANDARD,
            personal_thresholds={"NO2": {"kind": "sub_index", "value": 60}},
        ),
        limits=_limits(),
    )
    report = evaluate_crossings(
        [
            MeasuredSubIndex("SITE1", "PM25", 10, Confidence.HIGH),
            MeasuredSubIndex("SITE1", "NO2", 10, Confidence.HIGH),
        ],
        profile,
        registry=_REGISTRY,
        table_id=_TABLE_ID,
    )
    assert report.effective["PM25"].sub_index == 101
    assert report.effective["NO2"].sub_index == 60


def test_the_effective_escalation_is_reported_even_with_no_crossing() -> None:
    # Req 22.9 makes the BASIS reviewable, which matters most when nothing crossed: the user
    # needs to know what they were measured against.
    report = evaluate_crossings(
        [_measured(10)], _standard_profile(), registry=_REGISTRY, table_id=_TABLE_ID
    )
    assert report.crossed is False
    assert report.effective["PM25"].sub_index == 101


# --- Req 22.8: low confidence is reported alongside --------------------

def test_a_crossing_carries_the_confidence_of_the_value_that_drove_it() -> None:
    report = evaluate_crossings(
        [_measured(150, confidence=Confidence.LOW)],
        _standard_profile(),
        registry=_REGISTRY,
        table_id=_TABLE_ID,
    )
    assert report.crossings[0].confidence is Confidence.LOW


def test_a_low_confidence_crossing_is_still_reported() -> None:
    # Req 22.8 requires the Confidence be REPORTED, not that the crossing be suppressed —
    # suppressing it would hide a real exceedance behind a data-quality caveat.
    report = evaluate_crossings(
        [_measured(150, confidence=Confidence.LOW)],
        _standard_profile(),
        registry=_REGISTRY,
        table_id=_TABLE_ID,
    )
    assert report.crossed is True


def test_a_crossing_cannot_be_built_without_a_confidence() -> None:
    # Structural reading of Req 22.8: there is no way to report a crossing that omits it.
    assert "confidence" in ThresholdCrossing.__dataclass_fields__
    with pytest.raises(TypeError):
        ThresholdCrossing(  # type: ignore[call-arg]
            site_code="SITE1",
            species="PM25",
            sub_index=150,
            threshold=101,
            source=ThresholdSource.SENSITIVITY_LEVEL,
        )


# --- Req 22.10: determinism -------------------------------------------

def test_the_same_inputs_yield_the_same_crossings() -> None:
    # Req 22.10. Compares EQUALITY across evaluations rather than collapsing into a set: the
    # report carries a read-only mapping, and what the requirement claims is that repeated
    # evaluation agrees, not that the result is hashable.
    profile = _standard_profile()
    measurements = [_measured(150), _measured(50)]
    first = evaluate_crossings(
        measurements, profile, registry=_REGISTRY, table_id=_TABLE_ID
    )
    for _ in range(4):
        again = evaluate_crossings(
            measurements, profile, registry=_REGISTRY, table_id=_TABLE_ID
        )
        assert again.crossings == first.crossings
        assert again.crossed == first.crossed
        assert dict(again.effective) == dict(first.effective)


def test_the_reported_basis_cannot_be_mutated_by_a_caller() -> None:
    # A plain dict inside a frozen dataclass would let a caller rewrite what a crossing claims
    # to have been judged against, which Req 22.9 exists to make reviewable.
    report = evaluate_crossings(
        [_measured(150)], _standard_profile(), registry=_REGISTRY, table_id=_TABLE_ID
    )
    with pytest.raises(TypeError):
        report.effective["PM25"] = EffectiveEscalation(  # type: ignore[index]
            sub_index=1, source=ThresholdSource.DEFAULT_ORANGE_BAND
        )


# --- Req 22.4: rejection at the profile write --------------------------

def test_a_sub_index_threshold_below_one_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(
            _profile_fields(
                personal_thresholds={"PM25": {"kind": "sub_index", "value": 0}}
            ),
            limits=_limits(),
        )


def test_a_sub_index_threshold_above_five_hundred_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(
            _profile_fields(
                personal_thresholds={"PM25": {"kind": "sub_index", "value": 501}}
            ),
            limits=_limits(),
        )


def test_the_sub_index_range_boundaries_are_accepted() -> None:
    # Req 22.4 says "from 1 to 500", so both ends are lawful; an exclusive bound is the
    # natural off-by-one.
    for value in (1, 500):
        profile = build_profile(
            _profile_fields(
                personal_thresholds={"PM25": {"kind": "sub_index", "value": value}}
            ),
            limits=_limits(),
        )
        assert profile.personal_thresholds["PM25"].value == value


def test_a_species_the_breakpoint_table_does_not_define_is_rejected() -> None:
    # Req 22.4. Note the contrast with Requirement 21.2, which deliberately DOES name O3 in a
    # weighting: a weighting states clinical relevance, while a threshold must be comparable to
    # a Sub_Index and so needs a table.
    with pytest.raises(ValidationError):
        build_profile(
            _profile_fields(
                personal_thresholds={"O3": {"kind": "sub_index", "value": 100}}
            ),
            limits=_limits(),
        )


def test_a_concentration_threshold_requires_a_unit() -> None:
    with pytest.raises(ValidationError):
        build_profile(
            _profile_fields(
                personal_thresholds={"PM25": {"kind": "concentration", "value": 35.0}}
            ),
            limits=_limits(),
        )


def test_a_concentration_threshold_with_the_wrong_unit_is_rejected() -> None:
    # ppb for PM2.5 is not a unit mistake to paper over: converting it as µg/m³ would silently
    # move the user's trigger point.
    with pytest.raises(ValidationError):
        build_profile(
            _profile_fields(
                personal_thresholds={
                    "PM25": {"kind": "concentration", "value": 35.0, "unit": "ppb"}
                }
            ),
            limits=_limits(),
        )


def test_a_sub_index_threshold_must_not_carry_a_unit() -> None:
    # A unit on a Sub_Index means the caller has confused the two forms, which is exactly the
    # confusion this model exists to prevent — so it is a rejection, not an ignored extra.
    with pytest.raises(ValidationError):
        build_profile(
            _profile_fields(
                personal_thresholds={
                    "PM25": {"kind": "sub_index", "value": 100, "unit": "ug.m-3"}
                }
            ),
            limits=_limits(),
        )


def test_a_negative_concentration_threshold_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(
            _profile_fields(
                personal_thresholds={
                    "PM25": {"kind": "concentration", "value": -1.0, "unit": "ug.m-3"}
                }
            ),
            limits=_limits(),
        )


def test_a_rejected_threshold_does_not_echo_the_value() -> None:
    # The §7 guarantee task 17.1 established still holds on this new path: a Personal_Threshold
    # is health-adjacent, so a rejection must not repeat it.
    with pytest.raises(ValidationError) as caught:
        build_profile(
            _profile_fields(
                personal_thresholds={"PM25": {"kind": "sub_index", "value": 9999}}
            ),
            limits=_limits(),
        )
    assert "9999" not in str(caught.value)


def test_an_unknown_threshold_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(
            _profile_fields(
                personal_thresholds={"PM25": {"kind": "percentile", "value": 90}}
            ),
            limits=_limits(),
        )


def test_a_threshold_is_immutable_once_built() -> None:
    profile = build_profile(
        _profile_fields(
            personal_thresholds={"PM25": {"kind": "sub_index", "value": 100}}
        ),
        limits=_limits(),
    )
    with pytest.raises(ValidationError):
        profile.personal_thresholds["PM25"].value = 200


# --- shape -------------------------------------------------------------

def test_the_default_threshold_species_match_the_shipped_tables() -> None:
    # DEFAULT_THRESHOLD_SPECIES is declared in the profile module so it keeps no dependency on
    # the AQI layer (§1). That independence is only safe if the two cannot drift, so this is the
    # guard: adding a Breakpoint_Table without extending the default fails here.
    from aqm_ingestion.domain.profile import DEFAULT_THRESHOLD_SPECIES

    assert set(DEFAULT_THRESHOLD_SPECIES) == set(_REGISTRY.species_for(_TABLE_ID))


def test_each_threshold_unit_matches_its_tables_own_unit() -> None:
    # Same reasoning for the unit map: a mismatch here would reject a lawful threshold or accept
    # one in the wrong unit, and both move the user's trigger point.
    from aqm_ingestion.domain.profile import SPECIES_THRESHOLD_UNITS

    for species, unit in SPECIES_THRESHOLD_UNITS.items():
        assert _REGISTRY.resolve(_TABLE_ID, species).unit == unit


def test_the_threshold_model_says_which_form_it_is() -> None:
    # The defect this task fixed: a bare float could not distinguish these.
    assert set(ThresholdKind) == {ThresholdKind.SUB_INDEX, ThresholdKind.CONCENTRATION}
    assert set(PersonalThreshold.model_fields) == {"kind", "value", "unit"}


def test_the_report_shape_is_minimal() -> None:
    assert set(CrossingReport.__dataclass_fields__) == {
        "crossed",
        "crossings",
        "effective",
    }
    assert set(EffectiveEscalation.__dataclass_fields__) == {"sub_index", "source"}
