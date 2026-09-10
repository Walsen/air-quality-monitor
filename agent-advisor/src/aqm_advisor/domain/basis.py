"""Assembling the Basis_Summary from a retrieved response (Requirement 9).

Every field is COPIED from what Service 2 served. Req 9.4 forbids recomputing or re-deriving any
part of the basis, and two tests enforce that as a shape: this module calls no `sorted`, `min`,
`max` or `sum`, and contains no arithmetic operator.

**Which sensor is the basis, and why it is not a choice.** Service 2 returns `nearestSensors` as
a list — one per configured location — while a Basis_Summary names ONE site, because Req 9.1
asks for "the site the reading came from". Choosing among them by any criterion (highest
sub-index, nearest, worst confidence) would be this service deriving part of the basis. So the
FIRST as served is taken: the order is Service 2's, and deferring to it is the only option that
derives nothing.

**Req 9.6 is a prohibition, not a value.** With no basis retrieved this returns `None`, so there
is nothing for a claim to rest on. An empty `BasisSummary` would satisfy a "basis present" check
while carrying no provenance at all, which is the failure mode that looks like success.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence

from aqm_advisor.domain.instants import parse_iso_z
from aqm_advisor.domain.models import (
    BasisSummary,
    NowcastBasis,
    RecordReference,
    SpeciesBasis,
)


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, (list, tuple)) else ()


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _instant(value: object) -> dt.datetime | None:
    """Read a served wire-form instant, or None when it is absent or malformed.

    A malformed instant is read as absent rather than raised on: a shape this service does not
    expect is a Service 2 change, and failing the whole response over a timestamp would lose the
    reading as well.
    """
    if value is None:
        return None
    try:
        return parse_iso_z(str(value))
    except ValueError:
        return None


def _species(measurements: Sequence[object]) -> tuple[SpeciesBasis, ...]:
    """One SpeciesBasis per served measurement, in the order Service 2 returned them.

    Order is preserved because it reaches the guidance: reordering would present a different
    pollutant as the leading one.
    """
    entries: list[SpeciesBasis] = []
    for raw in measurements:
        block = _mapping(raw)
        if block is None:
            continue
        sub_index = block.get("subIndex")
        entries.append(
            SpeciesBasis(
                species=str(block.get("species", "")),
                sub_index=None if sub_index is None else int(sub_index),  # type: ignore[call-overload]
                band=_text(block.get("band")),
                confidence=str(block.get("confidence", "")),
            )
        )
    return tuple(entries)


def _nowcast(block: Mapping[str, object] | None) -> NowcastBasis | None:
    """The served nowcast weighting, carried unchanged (Req 9.3).

    A partial window survives as served. Normalising it to a full one would erase the very thing
    Req 9.3 makes traceable.
    """
    if block is None:
        return None
    return NowcastBasis(
        window_hours=int(block.get("windowHours", 0)),  # type: ignore[call-overload]
        hours_available=int(block.get("hoursAvailable", 0)),  # type: ignore[call-overload]
        weight_factor=float(block.get("weightFactor", 0.0)),  # type: ignore[arg-type]
    )


def _records(raw_records: Sequence[object]) -> tuple[RecordReference, ...]:
    """The Readings the claim rests on (Req 20.2 stores their identifiers)."""
    references: list[RecordReference] = []
    for raw in raw_records:
        block = _mapping(raw)
        if block is None:
            continue
        instant = _instant(block.get("dateTime"))
        if instant is None:
            continue
        references.append(
            RecordReference(
                site_code=str(block.get("siteCode", "")),
                species=str(block.get("species", "")),
                date_time=instant,
                duration=str(block.get("duration", "")),
            )
        )
    return tuple(references)


def assemble_basis(body: Mapping[str, object] | None) -> BasisSummary | None:
    """Build the Basis_Summary from a retrieved air-quality response, or `None` (Req 9.6).

    Returns `None` when there is no `basis` block or no sensor to attribute a reading to. Req
    9.6 says emit no claim that would require a basis, and the honest way to guarantee that is
    to have no basis object at all rather than an empty one.

    Available whether or not the user asked for it (Req 9.5): the caller decides what to say,
    and this always produces the record when the data supports one.
    """
    if body is None:
        return None

    basis_block = _mapping(body.get("basis"))
    sensors = _sequence(body.get("nearestSensors"))
    if basis_block is None or not sensors:
        return None

    # The FIRST as served. See the module docstring: choosing would be deriving.
    sensor = _mapping(sensors[0])
    if sensor is None:
        return None

    personalized = _mapping(body.get("personalized")) or {}
    threshold = personalized.get("escalationSubIndex")
    distance = sensor.get("distanceKm")
    strategies = _mapping(basis_block.get("calibrationStrategies")) or {}

    return BasisSummary(
        driving_pollutant=_text(sensor.get("drivingPollutant")),
        site_code=str(sensor.get("siteCode", "")),
        distance_km=float(distance) if distance is not None else 0.0,  # type: ignore[arg-type]
        as_of=_instant(sensor.get("asOf")),
        per_species=_species(_sequence(sensor.get("measurements"))),
        threshold=None if threshold is None else int(threshold),  # type: ignore[call-overload]
        threshold_source=_text(personalized.get("thresholdSource")),
        breakpoint_table=_text(basis_block.get("breakpointTable")),
        calibration_strategies={str(k): str(v) for k, v in strategies.items()},
        nowcast=_nowcast(_mapping(basis_block.get("nowcast"))),
        records=_records(_sequence(basis_block.get("records"))),
    )


__all__ = ["assemble_basis"]
