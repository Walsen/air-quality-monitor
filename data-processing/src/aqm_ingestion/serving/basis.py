"""The Basis: what every reported value can be independently reviewed against.

Requirements 25.5, 8.11, 9.7, 10.2, 11.9.

Every field here is READ from provenance the pipeline already recorded on the Calibrated_Reading
—
nothing is recomputed. That is the point: a basis derived by re-running the calculation would
agree with the reported value by construction and could never expose a disagreement, which is
exactly what a transparency requirement exists to allow.

The per-species split is not cosmetic. Requirements 8.11 and 25.5 both say the
Calibration_Strategy PER SPECIES, and NO2 defaults to `identity` while PM2.5 uses `rh_linear`,
so
a single shared strategy field would misreport one of them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from aqm_ingestion.domain.models import CalibratedReading


@dataclass(frozen=True, slots=True)
class RecordReference:
    """The identifying fields of one contributing Reading (Requirement 25.5).

    ``archive_id`` is included so a reviewer can reach the raw bytes the value came from, which
    is what makes the review independent rather than a second look at the same derived record.
    """

    site_code: str
    species: str
    interval_start: dt.datetime
    duration: str
    archive_id: str


@dataclass(frozen=True, slots=True)
class SpeciesBasis:
    """How one species' values were produced (Requirements 8.11, 9.7, 11.9)."""

    species: str
    calibration_strategy: str
    humidity_source: str
    conversion_source: str | None
    conversion_temperature_k: float | None
    conversion_pressure_pa: float | None
    nowcast_window_hours: int | None
    nowcast_hours_available: int | None
    nowcast_weight_factor: float | None


@dataclass(frozen=True, slots=True)
class Basis:
    """The reviewable basis for a whole response (Requirement 25.5).

    ``breakpoint_table`` is None only when no Reading contributed — Requirement 20.9 permits a
    site with an empty measurement set, so an empty basis is a real state rather than an error.
    """

    breakpoint_table: str | None
    species: tuple[SpeciesBasis, ...]
    records: tuple[RecordReference, ...]


def assemble_basis(readings: Iterable[CalibratedReading]) -> Basis:
    """Assemble the Basis from the readings that contributed to a response.

    Raises:
        ValueError: if the readings were indexed against DIFFERENT Breakpoint_Tables.
            Requirement 25.5 names "the Breakpoint_Table identifier", singular; two tables
            cannot
            be described by one identifier and picking either would misreport the other, so it
            is
            a caller bug rather than something to paper over (§5).
    """
    ordered = sorted(
        readings, key=lambda r: (r.key.site_code, r.key.species, r.key.interval_start)
    )

    tables = {reading.breakpoint_table for reading in ordered}
    if len(tables) > 1:
        raise ValueError(
            "a response must rest on one Breakpoint_Table, but its readings name "
            f"{sorted(tables)}"
        )

    # One entry per SPECIES, not per reading: two sites reporting PM2.5 share a strategy, and
    # listing it twice would imply they might not.
    per_species: dict[str, SpeciesBasis] = {}
    for reading in ordered:
        per_species.setdefault(
            reading.key.species,
            SpeciesBasis(
                species=reading.key.species,
                calibration_strategy=reading.calibration_strategy,
                humidity_source=reading.humidity_source,
                conversion_source=reading.conversion_source,
                conversion_temperature_k=reading.conversion_temperature_k,
                conversion_pressure_pa=reading.conversion_pressure_pa,
                nowcast_window_hours=reading.nowcast_window_hours,
                nowcast_hours_available=reading.nowcast_hours_available,
                nowcast_weight_factor=reading.nowcast_weight_factor,
            ),
        )

    return Basis(
        breakpoint_table=next(iter(tables)) if tables else None,
        species=tuple(per_species[name] for name in sorted(per_species)),
        records=tuple(
            RecordReference(
                site_code=reading.key.site_code,
                species=reading.key.species,
                interval_start=reading.key.interval_start,
                duration=reading.key.duration,
                archive_id=reading.archive_id,
            )
            for reading in ordered
        ),
    )


__all__ = ["Basis", "RecordReference", "SpeciesBasis", "assemble_basis"]
