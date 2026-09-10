"""Describing a retrieved history series without summarising its values (Requirement 3.4).

**This module never reads a measurement value.** Not as a convention — as the mechanism. Req 3.4
forbids computing a trend, an average or an exceedance count and presenting it as a measurement,
and the robust way to guarantee that is not to detect such a computation afterwards but to build
a module that never sees a value to compute one from. It reads instants, species, units,
confidence, the truncation flag and the record COUNT, and never `correctedValue`,
`reportedValue` or `subIndex`. A module that cannot read the numbers cannot average them, cannot
compare two into a direction, and cannot count exceedances of a threshold.

Tests enforce it from three directions: the forbidden field names appear nowhere in this source,
there is no arithmetic operator, and `sum`/`min`/`max`/`sorted` are never called — which is how
an average or a range would otherwise arrive with no operator in sight.

**What is left is the honest part.** Req 3.4 requires that a summary be described AS a summary
and that it name the number of readings it covers, so both are in the text. The count is the
reader's only way to judge how much the description rests on: "the air has been poor" over 2
readings and over 200 are different claims. The values themselves stay in the retrieved body,
where the model quotes them individually and grounding permits each one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_NO_READINGS_TEXT = "no readings"


@dataclass(frozen=True, slots=True)
class HistoryView:
    """What a retrieved history series can honestly be said to be.

    There is deliberately nowhere to put an average, a trend, a minimum or a maximum. As with
    `ConditionView` and its absent weight field, a field able to hold a derived value is an
    invitation to derive one, so the prohibition is structural rather than remembered.
    """

    site_code: str | None
    start: str | None
    end: str | None
    reading_count: int
    species: tuple[str, ...]
    units: tuple[str, ...]
    confidences: tuple[str, ...]
    truncated: bool
    text: str


def _distinct_in_order(values: list[str]) -> tuple[str, ...]:
    """Distinct values in first-appearance order.

    Deterministic without sorting. Sorting would impose an order Service 2 did not choose, and
    Req 12.2's reasoning — the order is the server's — applies to any sequence this service
    relays.
    """
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return tuple(seen)


def _optional_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    return str(value) if isinstance(value, (str, int)) else None


def _describe(view_parts: dict[str, Any]) -> str:
    """Render the summary, labelled as a summary and naming its count.

    The label is not decoration. Without it a reader takes a description of a series for a
    measurement, which is the exact conflation Req 3.4 exists to prevent.
    """
    count: int = view_parts["count"]
    species: tuple[str, ...] = view_parts["species"]
    if count == 0:
        base = (
            "This is a summary of the retrieved history: it contains "
            f"{_NO_READINGS_TEXT} for the requested period."
        )
    else:
        named = ", ".join(species) if species else "the requested species"
        base = (
            f"This is a summary of the retrieved history, not a measurement: {count} readings "
            f"for {named} over the requested period. The individual readings carry the values."
        )
    if view_parts["truncated"]:
        base = (
            f"{base} The series was truncated by the serving API, so it does not cover the "
            "whole period requested."
        )
    return base


def history_view(body: object) -> HistoryView:
    """Read a retrieved history body into something describable.

    A malformed or absent readings block reads as NO readings rather than raising. A shape this
    service does not expect is a Service 2 change, and failing the turn over it would lose the
    air-quality answer too — the part the user actually asked for.
    """
    if not isinstance(body, dict):
        return HistoryView(
            site_code=None,
            start=None,
            end=None,
            reading_count=0,
            species=(),
            units=(),
            confidences=(),
            truncated=False,
            text=_describe({"count": 0, "species": (), "truncated": False}),
        )

    raw = body.get("readings")
    readings = (
        [entry for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []
    )

    def field(key: str) -> tuple[str, ...]:
        return _distinct_in_order([str(entry.get(key, "")) for entry in readings])

    species = field("species")
    units = field("units")
    confidences = field("confidence")
    truncated = bool(body.get("truncated", False))
    count = len(readings)

    return HistoryView(
        site_code=_optional_str(body, "siteCode"),
        start=_optional_str(body, "startTime"),
        end=_optional_str(body, "endTime"),
        reading_count=count,
        species=species,
        units=units,
        confidences=confidences,
        truncated=truncated,
        text=_describe({"count": count, "species": species, "truncated": truncated}),
    )


__all__ = ["HistoryView", "history_view"]
