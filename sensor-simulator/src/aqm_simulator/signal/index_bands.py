"""Index-species breakpoint table and band derivation.

Each index Species (``PM25Index``, ``NO2Index``) is derived from its
concentration Species' emitted ``ScaledValue`` via a per-Species breakpoint
table (Requirement 6.3). A band's lower concentration bound is inclusive and its
upper bound exclusive, so a value on a boundary falls in the HIGHER band. A
concentration at or above the top band's upper bound clamps to the highest index
and is recorded (Requirement 6.6).

The table is validated on construction (Requirement 6.7): each Species must have
at least one band, the bands must start at 0, be contiguous and non-overlapping,
and carry strictly increasing index identifiers. An invalid table is rejected
naming the offending band, so signal generation never starts on a bad table.
"""

from __future__ import annotations

from dataclasses import dataclass

from aqm_simulator.contract.records import SpeciesName

# concentration Species -> the index Species derived from it.
INDEX_OF = {"PM25": "PM25Index", "NO2": "NO2Index"}


class BreakpointTableError(ValueError):
    """Raised when a breakpoint table is invalid (names the offending band)."""


@dataclass(frozen=True, slots=True)
class Band:
    """One index band: [lower, upper) concentration -> integer index."""

    lower: float
    upper: float
    index: int


class BreakpointTable:
    """Per-Species index bands, validated contiguous and strictly increasing."""

    def __init__(self, bands_by_species: dict[str, list[Band]]) -> None:
        self._bands: dict[str, tuple[Band, ...]] = {}
        for species, bands in bands_by_species.items():
            self._validate(species, bands)
            self._bands[species] = tuple(bands)

    @staticmethod
    def _validate(species: str, bands: list[Band]) -> None:
        if not bands:
            raise BreakpointTableError(f"{species}: table defines no bands")
        if bands[0].lower != 0.0:
            raise BreakpointTableError(
                f"{species} band 1: table must start at 0 µg/m³, got lower={bands[0].lower}"
            )
        prev_upper = bands[0].lower
        prev_index: int | None = None
        for band in bands:
            if band.lower < prev_upper:
                raise BreakpointTableError(
                    f"{species} band {band.index}: overlaps the previous band "
                    f"(lower {band.lower} < previous upper {prev_upper})"
                )
            if band.lower > prev_upper:
                raise BreakpointTableError(
                    f"{species} band {band.index}: leaves a gap "
                    f"(lower {band.lower} > previous upper {prev_upper})"
                )
            if band.upper <= band.lower:
                raise BreakpointTableError(
                    f"{species} band {band.index}: upper {band.upper} <= lower {band.lower}"
                )
            if prev_index is not None and band.index <= prev_index:
                raise BreakpointTableError(
                    f"{species} band {band.index}: index does not strictly increase"
                )
            prev_upper = band.upper
            prev_index = band.index

    def bands(self, species: str) -> tuple[Band, ...]:
        return self._bands[species]

    def top_index(self, species: str) -> int:
        return self._bands[species][-1].index


def derive_index_band(
    table: BreakpointTable, species: str, concentration: float
) -> tuple[int, bool]:
    """Return ``(index, clamped)`` for a concentration under ``species``' table.

    ``clamped`` is True when the concentration was at/above the top band's upper
    bound and the highest index was returned (Requirement 6.6).
    """
    bands = table.bands(species)
    for band in bands:
        if band.lower <= concentration < band.upper:
            return band.index, False
    # at or above the top band's upper bound -> clamp to the highest index
    return bands[-1].index, True


def _linear_bands(width: float) -> list[Band]:
    # ten contiguous bands of equal width, indices 1..10 (default table).
    return [Band(lower=i * width, upper=(i + 1) * width, index=i + 1) for i in range(10)]


def default_breakpoint_table() -> BreakpointTable:
    """The default ten-band 1..10 table for PM2.5 and NO2 (Requirement 6.3)."""
    # PM2.5 bands span 0..~150 µg/m³; NO2 bands span 0..~200 µg/m³.
    return BreakpointTable({"PM25": _linear_bands(15.0), "NO2": _linear_bands(20.0)})


def index_species_for(species: SpeciesName) -> str:
    """The index Species name paired with a concentration Species."""
    return INDEX_OF[species]
