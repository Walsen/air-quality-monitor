"""Forecast and pollen enrichment.

Requirement 24. Lives in ``serving/`` because it reaches through a port and reports.

WHAT THIS MODULE DELIBERATELY CANNOT DO, each enforced by its shape rather than by discipline:

- **It cannot derive a forecast from stored Readings** (Requirement 24.2). There is no
  ReadingsStore in scope, so there is nothing to derive one from. The requirement cites the
  finding that this service's value is FUSION rather than prediction; a modelled forecast would
  be this service inventing data and labelling it a forecast.
- **It cannot leak a profile field** (Requirement 24.8). ``enrich`` takes a POSITION, a current
  index, and a pollen-relevance flag — never a profile. The Condition never crosses the
  boundary: what crosses is the weighting's verdict on pollen, not the condition behind it.
- **It cannot leak a credential** (Requirement 24.10). The port takes only coordinates, so an
  enricher that cannot OBTAIN a credential cannot log one. Same structural argument the feed
  poller uses.

A FAILURE NEVER FAILS THE REQUEST (Requirement 24.4): current-conditions advice is useful
without a forecast, so a failure degrades the response and logs exactly ONE warning naming the
kind. Two failing calls inside one enrichment must not become two lines, or the rate of provider
trouble is misreported.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from aqm_ingestion.domain.weighting import PollenRelevance
from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import ForecastClient, PollenCategory

_logger = get_logger("serving.enrichment")

DEFAULT_TTL_MINUTES = 60
"""Requirement 24.5's default cache time-to-live."""

DEFAULT_TREND_BAND_POINTS = 5
"""Requirement 24.6's default band width, in index points."""

DEFAULT_TIMEOUT_SECONDS = 2.0
"""Requirement 24.4's default provider timeout.

Held here and handed to the adapter: the timeout is a policy this layer owns, while enforcing it
belongs to whatever actually performs I/O.
"""


class TrendDirection(StrEnum):
    """Requirement 24.6's forecast trend."""

    RISING = "rising"
    STEADY = "steady"
    FALLING = "falling"


@dataclass(frozen=True, slots=True)
class EnrichmentSettings:
    """The configured enrichment bounds (§1: narrow, not a config blob)."""

    ttl_minutes: int = DEFAULT_TTL_MINUTES
    trend_band_points: int = DEFAULT_TREND_BAND_POINTS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """Refuse a setting that would make caching or trend reporting meaningless (§5)."""
        if self.ttl_minutes < 1:
            raise ValueError(f"ttl_minutes must be at least 1, got {self.ttl_minutes}")
        if self.trend_band_points < 0:
            raise ValueError(
                f"trend band cannot be negative, got {self.trend_band_points}"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be positive, got {self.timeout_seconds}"
            )


@dataclass(frozen=True, slots=True)
class Enrichment:
    """What enrichment produced, including the fact that it failed.

    Carries no Condition, user identity or other profile field — a test asserts no field NAME
    even hints at one, so Requirement 24.8 cannot be broken by a later addition.
    """

    forecast_aqi: float | None
    trend: TrendDirection | None
    provider: str | None
    retrieved_at: dt.datetime | None
    pollen: Mapping[str, PollenCategory] | None
    degraded: bool
    failure_kind: str | None


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    """A cached provider answer and the instant it was actually retrieved."""

    forecast_aqi: float | None
    provider: str | None
    pollen: Mapping[str, PollenCategory] | None
    retrieved_at: dt.datetime
    degraded: bool
    failure_kind: str | None


def primary_position(
    positions: Sequence[tuple[float, float]],
) -> tuple[float, float] | None:
    """The primary User_Location's rounded coordinates, or None if there are none.

    A SPEC GAP, resolved here: Requirements 24.1 and 24.8 both say "the primary User_Location"
    and nothing in the spec defines which one that is. Resolved as the FIRST in the profile's
    declared order, because that is TOTAL — defined for every lawful profile — whereas keying
    on the name ``home`` would leave "primary" undefined for a user who saved only work and
    commute.
    """
    return positions[0] if positions else None


class Enricher:
    """Retrieves and caches forecast and pollen values."""

    def __init__(
        self,
        client: ForecastClient,
        clock: Clock,
        settings: EnrichmentSettings | None = None,
    ) -> None:
        """Hold the port, the clock, and the configured bounds.

        Deliberately NO readings store: see the module docstring on Requirement 24.2.
        """
        self._client = client
        self._clock = clock
        self._settings = settings or EnrichmentSettings()
        self._cache: dict[tuple[float, float], _CacheEntry] = {}

    def enrich(
        self,
        position: tuple[float, float],
        current_overall_aqi: int | None,
        pollen: PollenRelevance,
    ) -> Enrichment:
        """Retrieve enrichment for a position, degrading rather than failing.

        ``current_overall_aqi`` is used ONLY to derive the trend and is never sent to the
        provider. The trend is computed on every call, including a cache hit: the forecast is
        cached but the current index is not, so a cached trend would go stale the moment a new
        reading arrived and would report a rise that had already happened.
        """
        entry = self._cached(position)
        if entry is None:
            entry = self._retrieve(position, pollen)
            if not entry.degraded:
                # A failure is NOT cached: caching one would stretch a transient outage across
                # the whole time-to-live.
                self._cache[position] = entry

        return Enrichment(
            forecast_aqi=entry.forecast_aqi,
            trend=self._trend(entry.forecast_aqi, current_overall_aqi),
            provider=entry.provider,
            retrieved_at=entry.retrieved_at,
            # Requirement 24.7: pollen is served only where the weighting marks it relevant,
            # even if a cached entry happens to hold some.
            pollen=entry.pollen if pollen.is_relevant else None,
            degraded=entry.degraded,
            failure_kind=entry.failure_kind,
        )

    def _cached(self, position: tuple[float, float]) -> _CacheEntry | None:
        """Return a cache entry still inside its time-to-live (Requirement 24.5)."""
        entry = self._cache.get(position)
        if entry is None:
            return None
        age = self._clock.now() - entry.retrieved_at
        if age > dt.timedelta(minutes=self._settings.ttl_minutes):
            return None
        return entry

    def _retrieve(
        self, position: tuple[float, float], pollen: PollenRelevance
    ) -> _CacheEntry:
        """Call the provider, converting any failure into a degraded entry.

        Catches the expected transport and decoding failures by type (§5) rather than bare
        ``except``, and logs exactly ONE warning for the whole enrichment.
        """
        latitude, longitude = position
        retrieved_at = self._clock.now()

        forecast_aqi: float | None = None
        provider: str | None = None
        pollen_values: Mapping[str, PollenCategory] | None = None
        failure_kind: str | None = None
        degraded = False

        try:
            result = self._client.forecast(latitude, longitude)
        except (TimeoutError, OSError, ValueError) as error:
            failure_kind = _failure_kind(error)
            degraded = True
        else:
            if result.degraded or "aqi" not in result.values:
                degraded = True
                failure_kind = failure_kind or "unusable_body"
            else:
                forecast_aqi = result.values["aqi"]
                provider = result.provider or None

        # Requirement 24.7: not requested at all when irrelevant — not asking is stronger
        # minimisation than asking and discarding (§7).
        if pollen.is_relevant:
            try:
                pollen_result = self._client.pollen(latitude, longitude)
            except (TimeoutError, OSError, ValueError) as error:
                failure_kind = failure_kind or _failure_kind(error)
                degraded = True
            else:
                if pollen_result.degraded:
                    degraded = True
                    failure_kind = failure_kind or "unusable_body"
                else:
                    # Sorted so the taxa order reaching the response is defined (§2).
                    pollen_values = {
                        taxon: pollen_result.values[taxon]
                        for taxon in sorted(pollen_result.values)
                    }
                    provider = provider or pollen_result.provider or None

        if degraded:
            # ONE warning for the whole enrichment (Requirement 24.4), naming the kind but
            # never a credential or a profile field.
            _logger.warning(
                "enrichment_degraded",
                failure_kind=failure_kind,
                latitude=latitude,
                longitude=longitude,
            )

        return _CacheEntry(
            forecast_aqi=forecast_aqi,
            provider=provider,
            pollen=pollen_values,
            retrieved_at=retrieved_at,
            degraded=degraded,
            failure_kind=failure_kind,
        )

    def _trend(
        self, forecast_aqi: float | None, current_overall_aqi: int | None
    ) -> TrendDirection | None:
        """Derive Requirement 24.6's trend, or None when either side is missing.

        None rather than ``steady``: Requirement 12.6 allows no Overall_AQI at all, and calling
        that "steady" would assert the air is unchanged when nothing is known about it.

        The band's own width counts as steady. A strict comparison would both call a move of
        exactly the band width a rise AND make the band one point narrower than configured.
        """
        if forecast_aqi is None or current_overall_aqi is None:
            return None
        difference = forecast_aqi - current_overall_aqi
        if abs(difference) <= self._settings.trend_band_points:
            return TrendDirection.STEADY
        return TrendDirection.RISING if difference > 0 else TrendDirection.FALLING


def _failure_kind(error: Exception) -> str:
    """Name a failure kind for the log and the response (Requirement 24.4).

    Kinds, not messages: a provider's error text can carry a URL with a query string, and
    Requirement 24.10 forbids a credential reaching a log.
    """
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "transport"
    return "unusable_body"


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TREND_BAND_POINTS",
    "DEFAULT_TTL_MINUTES",
    "Enricher",
    "Enrichment",
    "EnrichmentSettings",
    "TrendDirection",
    "primary_position",
]
