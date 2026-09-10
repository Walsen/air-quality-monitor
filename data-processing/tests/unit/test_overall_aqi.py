"""Unit tests for the Overall_AQI and Driving_Pollutant (task 11.1).

- 12.1: the Overall_AQI is the MAXIMUM Sub_Index across the available species.
- 12.2: the Driving_Pollutant is its argmax, ties broken by the configured species
  precedence, default PM25 before NO2, so the result is deterministic.
- 12.3: the overall value is >= every contributing Sub_Index and equal to at least one.
- 12.4: the Band comes from the Requirement 10.5 mapping.
- 12.5: with one species available, that species drives, and the Basis says which had none.
- 12.6: with NO species available, NO Overall_AQI and NO Driving_Pollutant — not a zero
  and not a default band, because reporting `Good` for "we do not know" would be worse
  than reporting nothing.
- 12.7: each contributor reports whether it came from `nowcast` or `hourly`.
- 12.8: the overall Confidence is the LOWEST among the contributors.
- 1.7: the index species are structurally excluded from every index path.
"""

from __future__ import annotations

import pytest

from aqm_ingestion.domain.aqi.overall import (
    DEFAULT_SPECIES_PRECEDENCE,
    SpeciesContribution,
    compute_overall_aqi,
)
from aqm_ingestion.domain.models import Confidence


def _contribution(
    species: str = "PM25",
    sub_index: int = 60,
    method: str = "hourly",
    confidence: Confidence = Confidence.HIGH,
) -> SpeciesContribution:
    return SpeciesContribution(
        species=species,
        sub_index=sub_index,
        method=method,  # type: ignore[arg-type]
        confidence=confidence,
    )


# --- Req 1.7 structural exclusion of index species ------------------------

@pytest.mark.parametrize("species", ["PM25Index", "NO2Index"])
def test_an_index_species_cannot_form_a_contribution(species: str) -> None:
    # Req 1.7 / 11.1 ask for STRUCTURAL exclusion, not a filter a later edit could drop:
    # there is no way to build a contribution for an index species at all
    with pytest.raises(ValueError, match="index species"):
        _contribution(species=species)


@pytest.mark.parametrize("species", ["PM25", "NO2"])
def test_a_mass_concentration_species_forms_a_contribution(species: str) -> None:
    assert _contribution(species=species).species == species


def test_an_unknown_species_is_refused() -> None:
    with pytest.raises(ValueError, match="species"):
        _contribution(species="SO2")


# --- Req 12.6 nothing available ------------------------------------------

def test_no_contributions_yields_no_overall_aqi() -> None:
    assert compute_overall_aqi([]) is None


def test_the_absent_result_is_none_not_a_zero() -> None:
    # Req 12.6 is explicit that a zero or a default band must not be reported: `Good`
    # for "we have no data" would actively mislead an advisory
    result = compute_overall_aqi([])
    assert result is None  # no object exists to carry a misleading 0 or "Good"


# --- Req 12.1 / 12.3 the maximum -----------------------------------------

def test_overall_is_the_maximum_sub_index() -> None:
    result = compute_overall_aqi(
        [_contribution("PM25", 40), _contribution("NO2", 120)]
    )
    assert result is not None
    assert result.value == 120


def test_overall_equals_one_of_the_contributors() -> None:
    contributions = [_contribution("PM25", 40), _contribution("NO2", 120)]
    result = compute_overall_aqi(contributions)
    assert result is not None
    assert result.value in {c.sub_index for c in contributions}


def test_overall_is_not_a_sum_or_a_mean() -> None:
    # the two failure modes a careless implementation lands in
    result = compute_overall_aqi(
        [_contribution("PM25", 40), _contribution("NO2", 60)]
    )
    assert result is not None
    assert result.value == 60
    assert result.value != 100  # not a sum
    assert result.value != 50  # not a mean


# --- Req 12.2 the argmax and its tie-break -------------------------------

def test_driving_pollutant_is_the_argmax() -> None:
    result = compute_overall_aqi(
        [_contribution("PM25", 40), _contribution("NO2", 120)]
    )
    assert result is not None
    assert result.driving_pollutant == "NO2"


def test_default_precedence_is_pm25_before_no2() -> None:
    assert DEFAULT_SPECIES_PRECEDENCE == ("PM25", "NO2")


def test_a_tie_is_broken_by_precedence_not_input_order() -> None:
    # BOTH orders, so a stable sort that happens to agree with precedence in one
    # direction cannot pass for real tie-breaking
    pm25_first = compute_overall_aqi(
        [_contribution("PM25", 100), _contribution("NO2", 100)]
    )
    no2_first = compute_overall_aqi(
        [_contribution("NO2", 100), _contribution("PM25", 100)]
    )
    assert pm25_first is not None
    assert no2_first is not None
    assert pm25_first.driving_pollutant == "PM25"
    assert no2_first.driving_pollutant == "PM25"


def test_precedence_is_configurable() -> None:
    result = compute_overall_aqi(
        [_contribution("PM25", 100), _contribution("NO2", 100)],
        precedence=("NO2", "PM25"),
    )
    assert result is not None
    assert result.driving_pollutant == "NO2"


def test_precedence_only_matters_for_a_tie() -> None:
    # a lower-precedence species still drives when its index is genuinely higher
    result = compute_overall_aqi(
        [_contribution("PM25", 50), _contribution("NO2", 51)]
    )
    assert result is not None
    assert result.driving_pollutant == "NO2"


def test_a_species_outside_the_precedence_list_still_resolves() -> None:
    # §5: an incomplete precedence configuration must not crash the index; the listed
    # species rank first and the rest fall back to a defined order
    result = compute_overall_aqi(
        [_contribution("PM25", 100), _contribution("NO2", 100)],
        precedence=("NO2",),
    )
    assert result is not None
    assert result.driving_pollutant == "NO2"


# --- Req 12.4 the band ---------------------------------------------------

def test_band_comes_from_the_overall_value() -> None:
    result = compute_overall_aqi([_contribution("PM25", 155)])
    assert result is not None
    assert result.band == "Unhealthy"


def test_band_tracks_the_maximum_not_the_first_contributor() -> None:
    result = compute_overall_aqi(
        [_contribution("PM25", 10), _contribution("NO2", 305)]
    )
    assert result is not None
    assert result.band == "Hazardous"


# --- Req 12.5 a single species ------------------------------------------

def test_one_species_drives_on_its_own() -> None:
    result = compute_overall_aqi([_contribution("NO2", 77)])
    assert result is not None
    assert result.value == 77
    assert result.driving_pollutant == "NO2"


def test_missing_species_are_named_for_the_basis() -> None:
    # Req 12.5: the Basis must say which species had no Sub_Index
    result = compute_overall_aqi([_contribution("PM25", 40)])
    assert result is not None
    assert result.species_without_sub_index == ("NO2",)


def test_no_species_are_missing_when_both_contribute() -> None:
    result = compute_overall_aqi(
        [_contribution("PM25", 40), _contribution("NO2", 50)]
    )
    assert result is not None
    assert result.species_without_sub_index == ()


def test_missing_species_are_reported_in_a_defined_order() -> None:
    # §2: order that reaches output must be defined
    result = compute_overall_aqi([_contribution("PM25", 40)], precedence=("PM25", "NO2"))
    assert result is not None
    assert result.species_without_sub_index == tuple(
        sorted(result.species_without_sub_index)
    )


# --- Req 12.7 the method per contributor ---------------------------------

def test_each_contributor_reports_its_method() -> None:
    result = compute_overall_aqi(
        [
            _contribution("PM25", 40, method="nowcast"),
            _contribution("NO2", 50, method="hourly"),
        ]
    )
    assert result is not None
    assert result.methods == {"PM25": "nowcast", "NO2": "hourly"}


def test_the_driving_method_is_reachable() -> None:
    result = compute_overall_aqi(
        [
            _contribution("PM25", 40, method="nowcast"),
            _contribution("NO2", 150, method="hourly"),
        ]
    )
    assert result is not None
    assert result.driving_method == "hourly"


def test_an_unknown_method_is_refused() -> None:
    with pytest.raises(ValueError, match="method"):
        _contribution(method="guessed")


# --- Req 12.8 the confidence floor --------------------------------------

def test_overall_confidence_is_the_lowest_contributing_one() -> None:
    result = compute_overall_aqi(
        [
            _contribution("PM25", 40, confidence=Confidence.HIGH),
            _contribution("NO2", 120, confidence=Confidence.LOW),
        ]
    )
    assert result is not None
    assert result.confidence is Confidence.LOW


def test_the_floor_applies_even_when_the_weak_value_does_not_drive() -> None:
    # the LOW contributor here has the SMALLER index, so a naive implementation that
    # took the driving contributor's confidence would report HIGH
    result = compute_overall_aqi(
        [
            _contribution("PM25", 20, confidence=Confidence.LOW),
            _contribution("NO2", 190, confidence=Confidence.HIGH),
        ]
    )
    assert result is not None
    assert result.confidence is Confidence.LOW


def test_all_high_gives_high() -> None:
    result = compute_overall_aqi(
        [
            _contribution("PM25", 40, confidence=Confidence.HIGH),
            _contribution("NO2", 50, confidence=Confidence.HIGH),
        ]
    )
    assert result is not None
    assert result.confidence is Confidence.HIGH


def test_medium_beats_low_but_loses_to_high() -> None:
    result = compute_overall_aqi(
        [
            _contribution("PM25", 40, confidence=Confidence.MEDIUM),
            _contribution("NO2", 50, confidence=Confidence.HIGH),
        ]
    )
    assert result is not None
    assert result.confidence is Confidence.MEDIUM


# --- determinism and shape ----------------------------------------------

def test_duplicate_species_are_refused() -> None:
    # §5: two Sub_Index values for one species at one instant is a caller bug, and
    # silently taking one would make the result depend on argument order
    with pytest.raises(ValueError, match="more than once"):
        compute_overall_aqi([_contribution("PM25", 40), _contribution("PM25", 90)])


def test_the_result_is_repeatable() -> None:
    contributions = [_contribution("PM25", 100), _contribution("NO2", 100)]
    assert compute_overall_aqi(contributions) == compute_overall_aqi(contributions)


def test_the_result_is_frozen() -> None:
    result = compute_overall_aqi([_contribution("PM25", 40)])
    assert result is not None
    with pytest.raises((AttributeError, TypeError)):
        result.value = 1  # type: ignore[misc]
