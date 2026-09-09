"""Geographic selection: which sites a response is built from.

Requirement 20. Lives in ``serving/`` rather than ``domain/`` because it REPORTS — it reaches
through the registry and readings ports and declares gaps and fallbacks — following the task-5
precedent that pure rules live in ``domain/`` and reporting lives beside the caller.

THREE CLAUSES WHERE THE OBVIOUS IMPLEMENTATION IS THE WRONG ONE:

**A stale site is INCLUDED, not filtered out** (Requirement 20.9). Dropping it would hide that
the sensor nearest the user has gone quiet — which is precisely what they would want to know. So
it appears with an empty measurement set and no ``as_of``, and the response reports no
Overall_AQI for it rather than an older value dressed up as current.

**An unserved location is DECLARED, not rescued** (Requirement 20.5). Widening the radius looks
helpful and would present a sensor 40 km away as though it described the user's street.

**A site serving two locations appears ONCE, naming both** (Requirement 20.4). A per-location
list is the natural shape and duplicates it, which would double-count that site in everything
downstream.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field

from aqm_ingestion.domain.models import CalibratedReading
from aqm_ingestion.domain.profile import LocationName, UserProfile
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import ReadingsStore, SensorRegistryStore

DEFAULT_NEAREST_N = 3
"""Requirement 20.1's default N."""

DEFAULT_RADIUS_KM = 10.0
"""Requirement 20.2's default maximum selection radius, inclusive at the boundary."""

DEFAULT_DISTANCE_DECIMALS = 2
"""Requirement 20.3's default reporting precision for a distance."""

DEFAULT_FRESHNESS_HOURS = 3
"""Requirement 20.8's default freshness window."""

DEFAULT_GEOGRAPHIC_CENTRE = (51.507, -0.128)
"""Requirement 20.6's default geographic centre for the fallback selection.

Central London, matching the network the contract describes. Held at the same reduced precision
a User_Location is stored at (Requirement 17.5), so the fallback and a real location are
measured on identical terms.
"""


@dataclass(frozen=True, slots=True)
class SelectionSettings:
    """The configured selection bounds (§1: a narrow parameter object, not a config blob)."""

    nearest_n: int = DEFAULT_NEAREST_N
    radius_km: float = DEFAULT_RADIUS_KM
    distance_decimals: int = DEFAULT_DISTANCE_DECIMALS
    freshness_hours: int = DEFAULT_FRESHNESS_HOURS
    fallback_centre: tuple[float, float] = DEFAULT_GEOGRAPHIC_CENTRE

    def __post_init__(self) -> None:
        """Refuse a setting that would silently empty every response (§5)."""
        if self.nearest_n < 1:
            raise ValueError(f"nearest_n must be at least 1, got {self.nearest_n}")
        if self.radius_km <= 0:
            raise ValueError(f"radius_km must be positive, got {self.radius_km}")
        if self.freshness_hours < 1:
            raise ValueError(
                f"freshness_hours must be at least 1, got {self.freshness_hours}: "
                "a zero window would admit no Reading at all"
            )
        if self.distance_decimals < 0:
            raise ValueError(
                f"distance_decimals cannot be negative, got {self.distance_decimals}"
            )


@dataclass(frozen=True, slots=True)
class SelectedSite:
    """One site in a response, with why it was selected and what it currently reports."""

    site_code: str
    distance_km: float
    location_names: tuple[LocationName, ...]
    measurements: tuple[CalibratedReading, ...]
    as_of: dt.datetime | None
    """The newest selected interval start, or None when nothing fresh exists.

    None rather than an older instant: Requirement 20.9 forbids reporting a stale value as
    current, and an ``as_of`` outside the freshness window would do exactly that.
    """


@dataclass(frozen=True, slots=True)
class Selection:
    """Every selected site, plus what could not be served."""

    sites: tuple[SelectedSite, ...]
    unserved_locations: tuple[LocationName, ...]
    used_fallback: bool


@dataclass
class _Candidate:
    """A site accumulated across locations, so Requirement 20.4 merges, not duplicates."""

    site_code: str
    distance_km: float
    location_names: list[LocationName] = field(default_factory=list)


class GeoSelector:
    """Selects the sites and Readings a Serving_Response is built from."""

    def __init__(
        self,
        registry: SensorRegistryStore,
        readings: ReadingsStore,
        clock: Clock,
        settings: SelectionSettings | None = None,
    ) -> None:
        """Hold the ports and the configured bounds.

        ``clock`` is required, not defaulted: the freshness window is measured from it, and a
        SystemClock default would both read the wall clock in a testable unit (§2) and make the
        window impossible to exercise.
        """
        self._registry = registry
        self._readings = readings
        self._clock = clock
        self._settings = settings or SelectionSettings()

    def select(self, profile: UserProfile | None) -> Selection:
        """Resolve the sites for a profile (Requirement 20).

        Takes ONE profile and reaches for nothing wider, which is what keeps Requirement 17.10's
        isolation structural: there is no second user's data in scope to leak.
        """
        locations = tuple(profile.locations) if profile is not None else ()
        if not locations:
            return self._fallback_selection()

        candidates: dict[str, _Candidate] = {}
        unserved: list[LocationName] = []

        for location in locations:
            nearby = self._registry.nearest(
                location.latitude,
                location.longitude,
                self._settings.nearest_n,
                self._settings.radius_km,
            )
            if not nearby:
                # Requirement 20.5: declared, never widened.
                unserved.append(location.name)
                continue
            for near in nearby:
                self._accumulate(candidates, near, location.name)

        return Selection(
            sites=self._build_sites(candidates),
            unserved_locations=tuple(unserved),
            used_fallback=False,
        )

    def _fallback_selection(self) -> Selection:
        """Requirement 20.6: the N nearest to the configured centre, declared as a fallback."""
        latitude, longitude = self._settings.fallback_centre
        nearby = self._registry.nearest(
            latitude, longitude, self._settings.nearest_n, self._settings.radius_km
        )
        candidates: dict[str, _Candidate] = {}
        for near in nearby:
            # No User_Location to name — inventing one would misreport where the user is.
            self._accumulate(candidates, near, None)
        return Selection(
            sites=self._build_sites(candidates),
            unserved_locations=(),
            used_fallback=True,
        )

    def _accumulate(
        self,
        candidates: dict[str, _Candidate],
        near: object,
        location_name: LocationName | None,
    ) -> None:
        """Merge a nearest-site hit into the accumulator (Requirement 20.4).

        A site already seen for another location keeps the SMALLER distance: a site 1 km from
        the user's home and 8 km from their work is a 1 km site, and reporting the larger would
        understate its relevance.
        """
        site_code = near.entry.record.SiteCode  # type: ignore[attr-defined]
        distance = round(
            near.distance_km,  # type: ignore[attr-defined]
            self._settings.distance_decimals,
        )
        existing = candidates.get(site_code)
        if existing is None:
            candidates[site_code] = _Candidate(
                site_code=site_code,
                distance_km=distance,
                location_names=[] if location_name is None else [location_name],
            )
            return
        existing.distance_km = min(existing.distance_km, distance)
        if location_name is not None and location_name not in existing.location_names:
            existing.location_names.append(location_name)

    def _build_sites(self, candidates: dict[str, _Candidate]) -> tuple[SelectedSite, ...]:
        """Attach fresh Readings and order the result (Requirements 20.7, 20.8, 20.9)."""
        if not candidates:
            return ()

        not_before = self._clock.now() - dt.timedelta(hours=self._settings.freshness_hours)
        fresh = self._readings.latest_per_species(
            sorted(candidates), not_before=not_before
        )

        sites = [
            SelectedSite(
                site_code=candidate.site_code,
                distance_km=candidate.distance_km,
                # Sorted so the reported names do not depend on which location was walked first.
                location_names=tuple(sorted(candidate.location_names)),
                measurements=tuple(fresh.get(candidate.site_code, ())),
                as_of=_newest_interval(fresh.get(candidate.site_code, ())),
            )
            for candidate in candidates.values()
        ]
        # Requirement 20.7: ascending distance, SiteCode breaking a tie so the order is total.
        sites.sort(key=lambda site: (site.distance_km, site.site_code))
        return tuple(sites)


def _newest_interval(
    readings: Sequence[CalibratedReading],
) -> dt.datetime | None:
    """The newest interval start among the selected Readings, or None if there are none.

    Requirement 20.8 reports ``asOf`` so staleness is visible, so with several species present
    the reported instant is the newest — the one a reader checks to ask "how current is this?".
    """
    if not readings:
        return None
    return max(reading.key.interval_start for reading in readings)


__all__ = [
    "DEFAULT_DISTANCE_DECIMALS",
    "DEFAULT_FRESHNESS_HOURS",
    "DEFAULT_GEOGRAPHIC_CENTRE",
    "DEFAULT_NEAREST_N",
    "DEFAULT_RADIUS_KM",
    "GeoSelector",
    "SelectedSite",
    "Selection",
    "SelectionSettings",
]
