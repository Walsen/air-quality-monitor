"""Overall AQI property tests (tasks 11.2, 11.3).

Feature: ingestion-and-serving-service
- Property 19: Overall AQI is the maximum and the driving pollutant is its argmax
  (Requirements 12.1, 12.2, 12.3)
- Property 20: index species never influence a computed index (Requirements 1.7, 12.1)

Property 20 is unusual: the exclusion is STRUCTURAL, so there is no "compute with an index
species and check it was ignored" path to generate over — construction refuses first. The
property therefore asserts the refusal itself holds for every generated shape, and that
adding an index species to an otherwise valid set cannot change an answer, because it
cannot enter the set at all.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.domain.aqi.overall import (
    DEFAULT_SPECIES_PRECEDENCE,
    OverallAqi,
    SpeciesContribution,
    compute_overall_aqi,
)
from aqm_ingestion.domain.aqi.subindex import band_name_for
from aqm_ingestion.domain.models import Confidence, IndexMethod, confidence_rank

_MASS_SPECIES = ("PM25", "NO2")
_INDEX_SPECIES = ("PM25Index", "NO2Index")
_sub_index = st.integers(min_value=0, max_value=500)
# Typed as the IndexMethod literal so the composite below needs no cast, and a method
# name that drifted from the literal would fail the type check rather than a test.
_method: st.SearchStrategy[IndexMethod] = st.sampled_from(["nowcast", "hourly"])
_confidence = st.sampled_from(list(Confidence))


@st.composite
def _contributions(
    draw: st.DrawFn,
) -> list[SpeciesContribution]:
    """One contribution per chosen species, so no duplicates are generated."""
    species = draw(
        st.lists(st.sampled_from(_MASS_SPECIES), min_size=1, max_size=2, unique=True)
    )
    return [
        SpeciesContribution(
            species=name,
            sub_index=draw(_sub_index),
            method=draw(_method),
            confidence=draw(_confidence),
        )
        for name in species
    ]


@given(contributions=_contributions())
def test_property_19_overall_is_the_maximum_and_driving_is_its_argmax(
    contributions: list[SpeciesContribution],
) -> None:
    """Feature: ingestion-and-serving-service, Property 19."""
    result = compute_overall_aqi(contributions)
    assert result is not None

    indices = [contribution.sub_index for contribution in contributions]

    # Req 12.1: the maximum
    assert result.value == max(indices)

    # Req 12.3: >= every contributor AND equal to at least one. The second half is what
    # rules out an average, which would also satisfy the first half when all are equal.
    assert all(result.value >= index for index in indices)
    assert result.value in indices

    # Req 12.2: the driving pollutant's own sub-index IS the overall value
    driving = next(
        contribution
        for contribution in contributions
        if contribution.species == result.driving_pollutant
    )
    assert driving.sub_index == result.value

    # Req 12.4: the band follows the overall value through the Req 10.5 mapping
    assert result.band == band_name_for(result.value)

    # Req 12.8: the lowest contributing confidence, which is NOT generally the driving
    # contributor's own
    assert result.confidence == min(
        (contribution.confidence for contribution in contributions),
        key=confidence_rank,
    )


@given(contributions=_contributions())
def test_property_19_the_tie_break_is_order_independent(
    contributions: list[SpeciesContribution],
) -> None:
    """Feature: ingestion-and-serving-service, Property 19 (order independence).

    Requirement 12.2 exists so the result is deterministic, which means reversing the
    input cannot change the driving pollutant — including when the sub-indices tie, where
    a stable sort alone would follow input order instead of precedence.
    """
    forward = compute_overall_aqi(contributions)
    backward = compute_overall_aqi(list(reversed(contributions)))
    assert forward is not None
    assert backward is not None
    assert forward.driving_pollutant == backward.driving_pollutant
    assert forward.value == backward.value
    assert forward.confidence == backward.confidence


@given(
    contributions=_contributions(),
    precedence=st.permutations(_MASS_SPECIES),
)
def test_property_19_precedence_only_decides_ties(
    contributions: list[SpeciesContribution], precedence: list[str]
) -> None:
    """Feature: ingestion-and-serving-service, Property 19 (precedence scope).

    Whatever the configured precedence, the driving pollutant's sub-index still equals the
    maximum — precedence chooses AMONG the maxima, it never overrides them.
    """
    result = compute_overall_aqi(contributions, precedence=tuple(precedence))
    assert result is not None
    driving = next(
        contribution
        for contribution in contributions
        if contribution.species == result.driving_pollutant
    )
    assert driving.sub_index == max(c.sub_index for c in contributions)


@given(
    species=st.sampled_from(_INDEX_SPECIES),
    sub_index=_sub_index,
    method=_method,
    confidence=_confidence,
)
def test_property_20_index_species_never_influence_a_computed_index(
    species: str, sub_index: int, method: str, confidence: Confidence
) -> None:
    """Feature: ingestion-and-serving-service, Property 20."""
    # Req 1.7: refused at CONSTRUCTION, for every value it could carry — so there is no
    # index-species contribution anywhere for a maximum to have to skip
    with pytest.raises(ValueError, match="index species"):
        SpeciesContribution(
            species=species,
            sub_index=sub_index,
            method=method,  # type: ignore[arg-type]
            confidence=confidence,
        )


@given(contributions=_contributions(), species=st.sampled_from(_INDEX_SPECIES))
def test_property_20_an_index_species_cannot_join_a_valid_set(
    contributions: list[SpeciesContribution], species: str
) -> None:
    """Feature: ingestion-and-serving-service, Property 20 (no influence on a real set).

    The observable consequence of the structural exclusion: an index species cannot be
    added to a valid set of contributions, so it cannot shift an Overall_AQI it should
    have no part in. The baseline result is computed first to show there WAS an answer to
    corrupt.
    """
    baseline = compute_overall_aqi(contributions)
    assert isinstance(baseline, OverallAqi)

    with pytest.raises(ValueError, match="index species"):
        contributions.append(
            SpeciesContribution(
                species=species,
                sub_index=500,  # would dominate every generated maximum
                method="hourly",
                confidence=Confidence.HIGH,
            )
        )

    # the set is unchanged, so the answer is too
    assert compute_overall_aqi(contributions) == baseline


@given(contributions=_contributions())
def test_property_20_only_mass_concentration_species_are_ever_reported(
    contributions: list[SpeciesContribution],
) -> None:
    """Feature: ingestion-and-serving-service, Property 20 (outputs stay clean).

    Neither the driving pollutant nor the reported methods nor the missing-species list may
    name an index species, since Requirement 12's Basis is about the indices this service
    computes.
    """
    result = compute_overall_aqi(contributions)
    assert result is not None
    named = {result.driving_pollutant, *result.methods, *result.species_without_sub_index}
    assert named <= set(_MASS_SPECIES)
    assert not named & set(_INDEX_SPECIES)


def test_property_20_the_default_precedence_names_no_index_species() -> None:
    """Feature: ingestion-and-serving-service, Property 20 (configuration).

    Example-based because the default is one fixed value: were an index species listed in
    the precedence, it would look like a legitimate participant to a reader.
    """
    assert set(DEFAULT_SPECIES_PRECEDENCE) == set(_MASS_SPECIES)
