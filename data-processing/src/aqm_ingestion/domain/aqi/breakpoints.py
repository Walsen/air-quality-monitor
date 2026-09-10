"""Breakpoint tables, as validated data identified by a table identifier.

Requirement 10.2 makes a table DATA rather than code, so a revised scale is a new
identifier instead of an edit to a formula, and every reading and Serving_Response can
name the table it was computed against.

The validator (Requirement 10.11) runs at CONSTRUCTION, so a malformed table cannot
reach computation — a bad table would otherwise produce plausible-looking indices from
nonsense bands, which is worse than failing to start.

The rule that shapes it: the published bands step 9.0 -> 9.1 (PM2.5) and 53 -> 54 (NO2),
so consecutive bands do NOT share a boundary value. They are adjacent at the table's
REPORTING PRECISION, which is why ``decimals`` is part of the table. A naive check for
``next.bp_low == previous.bp_high`` would reject the shipped tables, while treating the
step as a gap would too. Requirement 10.6's truncation makes the convention sound: every
concentration truncates onto one band, so nothing falls between two of them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_TABLE_ID = "epa-2024-05-06"
"""Requirement 10.2's default table identifier."""

# The unit each species' table is defined over. NO2's being ppb rather than µg/m³ is the
# entire reason Requirement 9's conversion exists.
_SPECIES_UNIT: dict[str, str] = {"PM25": "ug.m-3", "NO2": "ppb"}


@dataclass(frozen=True, slots=True)
class BreakpointBand:
    """One band: an index range over a concentration range, all bounds inclusive."""

    index_low: int
    index_high: int
    bp_low: float
    bp_high: float

    def describe(self) -> str:
        """Name this band for an error message (Requirement 10.11)."""
        return (
            f"index {self.index_low}-{self.index_high} over "
            f"{self.bp_low}-{self.bp_high}"
        )

    def contains(self, concentration: float) -> bool:
        """Whether a truncated concentration falls in this band."""
        return self.bp_low <= concentration <= self.bp_high


class BreakpointTableError(ValueError):
    """A breakpoint table is malformed, or a lookup does not resolve (Req 10.11)."""

    def __init__(
        self, message: str, constraint: str | None = None, band: str | None = None
    ) -> None:
        """Keep the constraint and band addressable so a caller can report them (§5)."""
        self.constraint = constraint
        self.band = band
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class BreakpointTable:
    """A species' breakpoints, validated on construction (Requirement 10.11)."""

    table_id: str
    species: str
    unit: str
    averaging_period: str
    decimals: int
    bands: tuple[BreakpointBand, ...]

    def __post_init__(self) -> None:
        """Reject every Requirement 10.11 condition, naming band and constraint."""
        if not self.bands:
            raise BreakpointTableError(
                f"breakpoint table {self.table_id!r} for {self.species} must define at "
                "least one band",
                constraint="empty",
            )

        expected_unit = _SPECIES_UNIT.get(self.species)
        if expected_unit is not None and self.unit != expected_unit:
            raise BreakpointTableError(
                f"breakpoint table {self.table_id!r} declares unit {self.unit!r} for "
                f"{self.species}, whose unit is {expected_unit!r}",
                constraint="unit_mismatch",
            )

        step = 10.0**-self.decimals
        previous: BreakpointBand | None = None
        for band in self.bands:
            self._validate_band(band)
            if previous is not None:
                self._validate_adjacency(previous, band, step)
            previous = band

    def _validate_band(self, band: BreakpointBand) -> None:
        """Each band's own breakpoint and index sequence must increase."""
        for label, value in (("bp_low", band.bp_low), ("bp_high", band.bp_high)):
            if not math.isfinite(value):
                raise BreakpointTableError(
                    f"band {band.describe()} has a non-finite {label}: {value}",
                    constraint="non_finite",
                    band=band.describe(),
                )
        if band.bp_low >= band.bp_high:
            raise BreakpointTableError(
                f"band {band.describe()} must have a strictly increasing breakpoint "
                "range",
                constraint="non_increasing_breakpoints",
                band=band.describe(),
            )
        if band.index_low >= band.index_high:
            raise BreakpointTableError(
                f"band {band.describe()} must have a strictly increasing index range",
                constraint="non_increasing_index",
                band=band.describe(),
            )

    def _validate_adjacency(
        self, previous: BreakpointBand, band: BreakpointBand, step: float
    ) -> None:
        """Consecutive bands must be adjacent at the table's reporting precision."""
        if band.index_low <= previous.index_high:
            raise BreakpointTableError(
                f"band {band.describe()} has a non-increasing index sequence after "
                f"band {previous.describe()}",
                constraint="non_increasing_index",
                band=band.describe(),
            )
        if band.bp_low <= previous.bp_high:
            raise BreakpointTableError(
                f"band {band.describe()} overlaps the preceding band "
                f"{previous.describe()}",
                constraint="overlap",
                band=band.describe(),
            )
        # Adjacent means exactly one reporting step apart. Compared with a tolerance
        # because 9.0 + 0.1 is not exactly 9.1 in binary floating point.
        if not math.isclose(band.bp_low, previous.bp_high + step, rel_tol=1e-9):
            raise BreakpointTableError(
                f"band {band.describe()} leaves a gap after band "
                f"{previous.describe()}: at {self.decimals} decimal places the next "
                f"band must start at {previous.bp_high + step}",
                constraint="gap",
                band=band.describe(),
            )

    @property
    def highest(self) -> BreakpointBand:
        """The top band, whose slope Requirement 10.9 extrapolates above the table."""
        return self.bands[-1]

    def band_for(self, concentration: float) -> BreakpointBand | None:
        """The band containing a truncated concentration, or None if above the table."""
        for band in self.bands:
            if band.contains(concentration):
                return band
        return None


def _pm25_table(table_id: str) -> BreakpointTable:
    """Requirement 10.3's PM2.5 table, over a 24-hour average in µg/m³."""
    return BreakpointTable(
        table_id=table_id,
        species="PM25",
        unit="ug.m-3",
        averaging_period="PT24H",
        decimals=1,
        bands=(
            BreakpointBand(0, 50, 0.0, 9.0),
            BreakpointBand(51, 100, 9.1, 35.4),
            BreakpointBand(101, 150, 35.5, 55.4),
            BreakpointBand(151, 200, 55.5, 125.4),
            BreakpointBand(201, 300, 125.5, 225.4),
            BreakpointBand(301, 500, 225.5, 325.4),
        ),
    )


def _no2_table(table_id: str) -> BreakpointTable:
    """Requirement 10.4's NO2 table, over a 1-hour mixing ratio in ppb."""
    return BreakpointTable(
        table_id=table_id,
        species="NO2",
        unit="ppb",
        averaging_period="PT1H",
        decimals=0,
        bands=(
            BreakpointBand(0, 50, 0.0, 53.0),
            BreakpointBand(51, 100, 54.0, 100.0),
            BreakpointBand(101, 150, 101.0, 360.0),
            BreakpointBand(151, 200, 361.0, 649.0),
            BreakpointBand(201, 300, 650.0, 1249.0),
            BreakpointBand(301, 500, 1250.0, 2049.0),
        ),
    )


class BreakpointTableRegistry:
    """Resolves breakpoint tables by identifier and species (Requirement 10.2).

    Same open/closed shape as the calibration registry: a revised scale is a new
    registration, not a branch in the lookup (§1).
    """

    def __init__(self) -> None:
        """Start empty; use `with_defaults` for the shipped tables."""
        self._tables: dict[tuple[str, str], BreakpointTable] = {}

    @classmethod
    def with_defaults(cls) -> BreakpointTableRegistry:
        """A registry holding the shipped `epa-2024-05-06` tables."""
        registry = cls()
        registry.register(_pm25_table(DEFAULT_TABLE_ID))
        registry.register(_no2_table(DEFAULT_TABLE_ID))
        return registry

    def register(self, table: BreakpointTable) -> None:
        """Add a table for one species under its identifier."""
        key = (table.table_id, table.species)
        if key in self._tables:
            raise BreakpointTableError(
                f"breakpoint table {table.table_id!r} already defines "
                f"{table.species}",
                constraint="duplicate",
            )
        self._tables[key] = table

    def get(self, table_id: str, species: str) -> BreakpointTable | None:
        """The table for an identifier and species, or None if undefined.

        None rather than an exception is the Requirement 10.12 path: a species the
        table does not define gets NO sub-index, which the caller must handle by
        omitting one rather than approximating.
        """
        return self._tables.get((table_id, species))

    def resolve(self, table_id: str, species: str) -> BreakpointTable:
        """The table for an identifier and species.

        Raises:
            BreakpointTableError: naming the requested pair and the registered
                identifiers.
        """
        table = self.get(table_id, species)
        if table is None:
            raise BreakpointTableError(
                f"no breakpoint table {table_id!r} defining {species!r}; registered "
                f"identifiers are: {', '.join(self.table_ids())}",
                constraint="unknown_table",
            )
        return table

    def table_ids(self) -> tuple[str, ...]:
        """Every registered identifier, sorted so messages are reproducible (§2)."""
        return tuple(sorted({table_id for table_id, _ in self._tables}))

    def species_for(self, table_id: str) -> tuple[str, ...]:
        """The species an identifier defines, sorted (§2)."""
        return tuple(
            sorted(
                species
                for registered_id, species in self._tables
                if registered_id == table_id
            )
        )
