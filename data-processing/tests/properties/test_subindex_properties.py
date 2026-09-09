"""Sub-index property tests (tasks 9.3, 9.4, 9.5).

Feature: ingestion-and-serving-service
- Property 16: sub-index monotonicity (Requirement 10.7)
- Property 17: breakpoint boundary exactness (Requirements 10.8, 10.1, 10.6)
- Property 18: sub-index and band agree, and the ceiling holds
  (Requirements 10.5, 10.9)

Property 17 walks EVERY band of EVERY shipped table rather than sampling, because
Requirement 10.8 is a claim about all boundaries and there are only twelve — sampling
would be strictly weaker than enumerating.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.domain.aqi import (
    DEFAULT_TABLE_ID,
    BreakpointTable,
    BreakpointTableRegistry,
)
from aqm_ingestion.domain.aqi.subindex import (
    DEFAULT_INDEX_CEILING,
    band_name_for,
    compute_sub_index,
)

_REGISTRY = BreakpointTableRegistry.with_defaults()
_SPECIES = ("PM25", "NO2")

# Well past the top breakpoint of either table (325.4 and 2049), so the extrapolation
# and ceiling paths are exercised alongside the in-table ones.
_concentration = st.floats(min_value=0.0, max_value=5_000.0, allow_nan=False)


def _table(species: str) -> BreakpointTable:
    return _REGISTRY.resolve(DEFAULT_TABLE_ID, species)


@given(
    species=st.sampled_from(_SPECIES),
    concentrations=st.tuples(_concentration, _concentration),
)
def test_property_16_sub_index_monotonicity(
    species: str, concentrations: tuple[float, float]
) -> None:
    """Feature: ingestion-and-serving-service, Property 16."""
    lower, higher = sorted(concentrations)
    table = _table(species)

    index_low = compute_sub_index(lower, table).sub_index
    index_high = compute_sub_index(higher, table).sub_index

    # Req 10.7: a higher concentration never yields a lower index. This has to hold
    # ACROSS band boundaries and through the truncation step, not merely within a band,
    # which is what makes it worth generating rather than spot-checking.
    assert index_high >= index_low


@given(species=st.sampled_from(_SPECIES), concentration=_concentration)
def test_property_16_the_index_is_never_negative_or_above_the_ceiling(
    species: str, concentration: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 16 (range)."""
    result = compute_sub_index(concentration, _table(species))
    assert 0 <= result.sub_index <= DEFAULT_INDEX_CEILING


def test_property_17_breakpoint_boundary_exactness() -> None:
    """Feature: ingestion-and-serving-service, Property 17.

    Requirement 10.8: at a band's lower breakpoint the index is EXACTLY that band's
    lower index, and at its upper breakpoint exactly its upper index. Enumerated over
    every band of both shipped tables, since the claim is universal and the set is small.
    """
    for species in _SPECIES:
        table = _table(species)
        for band in table.bands:
            at_low = compute_sub_index(band.bp_low, table)
            at_high = compute_sub_index(band.bp_high, table)
            assert at_low.sub_index == band.index_low, (
                f"{species} band {band.describe()} lower breakpoint"
            )
            assert at_high.sub_index == band.index_high, (
                f"{species} band {band.describe()} upper breakpoint"
            )
            # neither endpoint of a defined band is extrapolation
            assert at_low.extrapolated is False
            assert at_high.extrapolated is False


@given(species=st.sampled_from(_SPECIES), offset=st.integers(min_value=0, max_value=9))
def test_property_17_truncation_holds_a_value_in_its_band(
    species: str, offset: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 17 (truncation, Req 10.6).

    A concentration just above a band's upper breakpoint but below the next band's lower
    one truncates back onto the upper breakpoint, so it keeps that band's upper index.
    This is the behaviour that makes the published tables' 0.1-step layout coherent.
    """
    table = _table(species)
    step = 10.0**-table.decimals
    for band in table.bands:
        # a fraction of one reporting step above the upper breakpoint
        probe = band.bp_high + step * offset / 10.0
        result = compute_sub_index(probe, table)
        if offset == 0:
            assert result.sub_index == band.index_high
        else:
            # still truncates to bp_high, so still the band's upper index
            assert result.sub_index == band.index_high


@given(species=st.sampled_from(_SPECIES), concentration=_concentration)
def test_property_18_sub_index_and_band_agree_and_the_ceiling_holds(
    species: str, concentration: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 18."""
    table = _table(species)
    result = compute_sub_index(concentration, table)

    # Req 10.9: the ceiling is never exceeded
    assert result.sub_index <= DEFAULT_INDEX_CEILING

    if result.extrapolated:
        # Req 10.9: above the table the band is Hazardous regardless of the index — a
        # capped index of 500 and an uncapped 480 are both beyond the scale
        assert result.band == "Hazardous"
    else:
        # Req 10.5: in-table, the band is exactly the one the index maps to
        assert result.band == band_name_for(result.sub_index)

    assert result.table_id == DEFAULT_TABLE_ID


@given(
    species=st.sampled_from(_SPECIES),
    concentration=_concentration,
    ceiling=st.integers(min_value=1, max_value=1_000),
)
def test_property_18_a_configured_ceiling_is_respected(
    species: str, concentration: float, ceiling: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 18 (configured ceiling).

    Requirement 10.9 scopes the cap precisely: it applies WHERE a concentration exceeds
    the highest breakpoint. So an EXTRAPOLATED index is bounded by the ceiling, while an
    in-table index is bounded by its own table's top index — which is what the bands
    define and what Requirement 10.8 pins exactly.

    An earlier draft of this property asserted the ceiling bounded EVERY value, and
    Hypothesis refuted it with ceiling=1 on an in-table concentration returning 6. That
    was the property over-claiming, not the code misbehaving: with the default ceiling of
    500 and a top band index of 500 the two bounds coincide, so the distinction only
    appears under a ceiling configured below the table's own top — an incoherent
    configuration the requirements do not forbid.
    """
    table = _table(species)
    result = compute_sub_index(concentration, table, ceiling=ceiling)

    if result.extrapolated:
        assert result.sub_index <= ceiling
    else:
        assert result.sub_index <= table.highest.index_high


@given(species=st.sampled_from(_SPECIES), concentration=_concentration)
def test_property_18_computation_is_deterministic(
    species: str, concentration: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 18 (Req 10.10).

    Same concentration, table identifier and species, same value every evaluation.
    """
    table = _table(species)
    first = compute_sub_index(concentration, table)
    second = compute_sub_index(concentration, table)
    assert first == second
