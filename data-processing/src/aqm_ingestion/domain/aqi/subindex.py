"""Computing a Sub_Index from a concentration and a breakpoint table.

Three details here are easy to get subtly wrong, and each has its own reason:

- **Truncation, not rounding** (Requirement 10.6). A PM2.5 value of 9.09 truncates to
  9.0 and lands in the Good band; rounding it to 9.1 would push it into Moderate. The
  tables are published at a reporting precision and the bands are adjacent at that
  precision, so truncation is what makes every value land in exactly one band.
- **Ties away from zero** (Requirement 10.1). Python's built-in ``round`` uses banker's
  rounding, so ``round(50.5)`` is 50 — one lower than the requirement asks for. Decimal
  with ROUND_HALF_UP is used instead, and the difference is pinned by test.
- **The top band's slope is extrapolated, and the CAP is applied after**
  (Requirement 10.9). Capping the concentration instead of the index would flatten every
  extreme value onto the same number without recording that it was extrapolated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from aqm_ingestion.domain.aqi.breakpoints import BreakpointTable

DEFAULT_INDEX_CEILING = 500
"""Requirement 10.9's configured ceiling for a reported Sub_Index."""

# Requirement 10.5's band names, as (inclusive lower bound of the index range, name),
# highest first so the first match wins. A table rather than a comparison chain, so a
# revised scale is an edit to data (§1).
_BAND_NAMES: tuple[tuple[int, str], ...] = (
    (301, "Hazardous"),
    (201, "Very Unhealthy"),
    (151, "Unhealthy"),
    (101, "Unhealthy for Sensitive Groups"),
    (51, "Moderate"),
    (0, "Good"),
)


@dataclass(frozen=True, slots=True)
class SubIndexResult:
    """A Sub_Index with everything needed to explain it."""

    sub_index: int
    band: str
    table_id: str
    extrapolated: bool = False


def round_half_away_from_zero(value: float) -> int:
    """Round to the nearest integer, resolving a tie away from zero (Req 10.1).

    Deliberately NOT the built-in ``round``, whose banker's rounding sends 0.5 to 0 and
    2.5 to 2 — both one short of what the requirement specifies.
    """
    return int(Decimal(repr(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def truncate_to(value: float, decimals: int) -> float:
    """Truncate toward zero at a number of decimal places (Requirement 10.6).

    Truncation discards rather than rounds: at one decimal place 9.19 becomes 9.1, not
    9.2, which is what keeps a concentration in the band the published table intends.
    """
    scale = 10.0**decimals
    return math.floor(value * scale) / scale


def band_name_for(sub_index: int) -> str:
    """The Band name for a Sub_Index (Requirement 10.5)."""
    for lower, name in _BAND_NAMES:
        if sub_index >= lower:
            return name
    # Unreachable for a non-negative index, since the table ends at 0.
    return "Good"


def compute_sub_index(
    concentration: float,
    table: BreakpointTable,
    ceiling: int = DEFAULT_INDEX_CEILING,
) -> SubIndexResult:
    """Compute a Sub_Index by interpolating within the containing band.

    Args:
        concentration: the corrected concentration, in the table's own unit — µg/m³ for
            PM2.5, ppb for NO2, which is why Requirement 9's conversion runs first.
        table: the breakpoint table to interpolate against.
        ceiling: the reported maximum (Requirement 10.9).

    Returns:
        The index, its band, the table identifier, and whether it was extrapolated
        above the table.

    Raises:
        ValueError: for a negative concentration. Calibration clamps at 0
            (Requirement 8.9), so a negative value here is a caller bug rather than
            upstream data, and interpolating one would produce a meaningless index (§5).
    """
    if concentration < 0:
        raise ValueError(
            f"cannot compute a Sub_Index for a negative concentration: {concentration}"
        )

    truncated = truncate_to(concentration, table.decimals)
    band = table.band_for(truncated)

    if band is None:
        # Requirement 10.9: above the highest breakpoint, extrapolate the top band's
        # slope, then cap the INDEX. Capping the concentration instead would lose the
        # distinction between "just above the table" and "far above it".
        top = table.highest
        slope = (top.index_high - top.index_low) / (top.bp_high - top.bp_low)
        raw = slope * (truncated - top.bp_low) + top.index_low
        capped = min(round_half_away_from_zero(raw), ceiling)
        return SubIndexResult(
            sub_index=capped,
            band="Hazardous",
            table_id=table.table_id,
            extrapolated=True,
        )

    slope = (band.index_high - band.index_low) / (band.bp_high - band.bp_low)
    raw = slope * (truncated - band.bp_low) + band.index_low
    sub_index = round_half_away_from_zero(raw)
    return SubIndexResult(
        sub_index=sub_index,
        band=band_name_for(sub_index),
        table_id=table.table_id,
    )
