"""NowCast: placing an hourly PM2.5 value on a 24-hour breakpoint scale.

The PM2.5 breakpoint table is defined over a 24-hour average, so a single hourly reading
cannot be placed on it directly (Requirement 11.1). A plain 24-hour mean would be honest
about the scale but would flatten recent change — exactly the change an advisory needs to
convey — so NowCast takes a weighted average whose weight shrinks with age, and whose
weighting gets *sharper* the more variable the window is.

Two details are easy to implement subtly wrong:

- **The exponent is the hour's AGE, not its position among the available values**
  (Requirement 11.3, "skipping hours with no value in both sums"). With a gap at the most
  recent hour, the next value down still carries ``w**1``. Re-indexing over only the
  present values would silently promote an older reading to "most recent" and defeat the
  whole point of the weighting.
- **The 0.5 floor on the weight factor is a CLAMP, not a substitution**
  (Requirement 11.4). A window whose minimum is 0 produces a raw factor of 0, which would
  discard every hour but the newest; the floor keeps some history in view.

Pure by construction: the series arrives as a parameter, so this reads no clock and no
store (§2). Requirement 11.11's determinism follows from that rather than from care.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aqm_ingestion.domain.models import Confidence

DEFAULT_NOWCAST_WINDOW_HOURS = 12
"""Requirement 11.2's default NowCast_Window length."""

MIN_RECENT_HOURS_REQUIRED = 2
"""Requirement 11.5: at least this many of the three most recent hours must have a value."""

_RECENT_HOURS_CONSIDERED = 3
"""Requirement 11.5 judges coverage on the three most recent hours."""

_WEIGHT_FLOOR = 0.5
"""Requirement 11.4's lower bound on the weight factor."""

# Requirement 11.10: NowCast applies to PM2.5 alone. NO2's table is already defined over
# a 1-hour interval, so smoothing it over twelve would place the value on a scale it was
# never built for; the index species are the emitting network's own values (Req 1.7).
_NOWCAST_SPECIES: frozenset[str] = frozenset({"PM25"})


@dataclass(frozen=True, slots=True)
class NowCastResult:
    """A NowCast, or the hourly fallback, with everything needed to explain it.

    ``value`` is always the concentration to compute the Sub_Index from, so a caller
    never has to choose between two fields. ``used_nowcast`` says which it is.
    """

    value: float
    used_nowcast: bool
    hours_available: int
    window_hours: int
    weight_factor: float | None = None
    confidence_ceiling: Confidence | None = None


def supports_nowcast(species: str) -> bool:
    """Whether NowCast applies to a species (Requirement 11.10)."""
    return species in _NOWCAST_SPECIES


def nowcast_weight_factor(values: Sequence[float]) -> float:
    """Compute the weight factor over a window's available values (Req 11.4).

    ``w = 1 - (c_max - c_min) / c_max``, floored at 0.5, and 1 when ``c_max`` is 0.

    A steady window gives w near 1, so old and new hours weigh almost alike; a volatile
    one drives w down toward the floor, concentrating weight on recent hours. That is the
    behaviour NowCast exists for.
    """
    if not values:
        # No range to measure. 1 keeps the weighted mean well-defined and equal to a
        # plain mean, which is the honest reading of "no variability information".
        return 1.0
    highest = max(values)
    lowest = min(values)
    if highest == 0:
        # Requirement 11.4's explicit case: the formula would divide by zero. Every
        # value is 0 here, so any factor gives the same NowCast.
        return 1.0
    raw = 1.0 - (highest - lowest) / highest
    return max(_WEIGHT_FLOOR, raw)


def compute_nowcast(
    series: Sequence[float | None],
    *,
    hourly_value: float,
    window_hours: int = DEFAULT_NOWCAST_WINDOW_HOURS,
) -> NowCastResult:
    """Compute the NowCast for one interval, or fall back to the hourly value.

    Args:
        series: corrected hourly concentrations, index 0 the MOST RECENT hour and
            increasing into the past, with None for an hour that has no value. Longer
            than ``window_hours`` is truncated to it (Requirement 11.2).
        hourly_value: the Reading's own corrected value, used when coverage is
            insufficient (Requirement 11.5).
        window_hours: the configured window length.

    Returns:
        The concentration to compute the Sub_Index from, whether it is a NowCast, and
        the coverage metadata Requirement 11.9 records.

    Raises:
        ValueError: for a non-positive window, or a negative value in the series.
            Corrected values are clamped at 0 (Requirement 8.9), so a negative one is a
            caller bug rather than upstream data (§5).
    """
    if window_hours <= 0:
        raise ValueError(f"NowCast window must be at least one hour, got {window_hours}")

    window = list(series[:window_hours])
    for value in window:
        if value is not None and value < 0:
            raise ValueError(
                f"NowCast series carries a negative corrected value: {value}"
            )

    available = [value for value in window if value is not None]
    recent_present = sum(
        1 for value in window[:_RECENT_HOURS_CONSIDERED] if value is not None
    )

    if recent_present < MIN_RECENT_HOURS_REQUIRED:
        # Requirement 11.5. Older hours cannot substitute however many there are: a
        # NowCast that leant on twelve-hour-old data while the recent hours were missing
        # would be reported as current when it is not.
        return NowCastResult(
            value=hourly_value,
            used_nowcast=False,
            hours_available=len(available),
            window_hours=len(window),
            weight_factor=None,  # none was used; claiming one would misdescribe it
            confidence_ceiling=Confidence.LOW,
        )

    weight = nowcast_weight_factor(available)
    numerator = 0.0
    denominator = 0.0
    for age, value in enumerate(window):
        if value is None:
            continue  # skipped in BOTH sums (Requirement 11.3)
        # `age` is the hour's distance from the present, NOT its rank among the values
        # that happen to be present.
        factor = weight**age
        numerator += factor * value
        denominator += factor

    return NowCastResult(
        value=numerator / denominator,
        used_nowcast=True,
        hours_available=len(available),
        window_hours=len(window),
        weight_factor=weight,
    )
