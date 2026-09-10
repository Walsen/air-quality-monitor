"""The HTTP forecast, pollen, and meteorology adapters.

Requirements 24.3, 24.4, 24.7, 24.8, 24.9, 24.10, 8.4.

THESE ADAPTERS DO NOT RAISE. Both ports sit on paths whose requirements say the work CONTINUES
when
the external service does not answer: Requirement 24.4 serves the response without forecast
values
and explicitly must not fail the request, and Requirement 8.4's precedence ends in "no RH at
all",
with Requirement 8.6 continuing uncalibrated. An adapter that raised would hand its caller an
exception the requirement gives it no way to answer, so degradation is expressed in the RETURN
TYPE
— ``degraded=True`` for the forecast, ``None`` for an observation.

That is the opposite choice from the feed adapter next door, and deliberately so: a feed
failure must NOT look like an empty window (Requirement 5.6), while a forecast failure
must not look like a failed request. The requirement decides, not a house style.

NOTHING HERE DERIVES A POLLEN CATEGORY. Requirement 24.7 fixes a closed category set and
the spec defines pollen thresholds NOWHERE — while defining the PM2.5 and NO2 breakpoint
tables down to
individual boundaries. That asymmetry is the evidence: the provider supplies the category, and
an
adapter classifying a raw count would be inventing bands the spec withholds. An unrecognized
category therefore DEGRADES rather than being mapped to its nearest neighbour, which would
report a
clinical judgement the provider never made.
"""

from __future__ import annotations

import datetime as dt

import httpx

from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.ports.protocols import (
    ForecastResult,
    MetObservation,
    PollenCategory,
    PollenResult,
)

_logger = get_logger("adapters.forecast")

DEFAULT_FORECAST_TIMEOUT_SECONDS = 2.0
"""Requirement 24.4's default timeout.

Pinned by a test: a later edit lengthening it would make a serving request wait past what the
requirement allows, and the enricher has no way to notice.
"""

DEFAULT_PROVIDER = "http-forecast"


class HttpForecastClient:
    """The ForecastClient port over an HTTP provider (Requirement 24.9)."""

    def __init__(
        self,
        base_url: str,
        credential: str,
        provider: str = DEFAULT_PROVIDER,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = DEFAULT_FORECAST_TIMEOUT_SECONDS,
    ) -> None:
        """Build the client. The credential is folded into the headers and not retained."""
        self._provider = provider
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-API-KEY": credential},
            transport=transport,
            timeout=timeout_seconds,
        )

    def forecast(self, lat: float, lon: float) -> ForecastResult:
        """Next-day AQI for a position, degrading rather than failing (Requirement 24.4)."""
        body = self._get_json("/forecast", lat, lon)
        if body is None:
            return ForecastResult(values={}, degraded=True)

        raw = body.get("values")
        if not isinstance(raw, dict):
            _logger.warning(
                "forecast_unusable_body", failure_kind="MissingValues", provider=self._provider
            )
            return ForecastResult(values={}, degraded=True)

        values: dict[str, float] = {}
        for species, value in raw.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                # A bool IS an int in Python, so it must be excluded explicitly or True would
                # silently become a forecast of 1.
                _logger.warning(
                    "forecast_unusable_body",
                    failure_kind="NonNumericValue",
                    provider=self._provider,
                )
                return ForecastResult(values={}, degraded=True)
            values[str(species)] = float(value)

        return ForecastResult(values=values, provider=self._provider)

    def pollen(self, lat: float, lon: float) -> PollenResult:
        """Current pollen outlook as provider-supplied categories (Requirement 24.7)."""
        body = self._get_json("/pollen", lat, lon)
        if body is None:
            return PollenResult(values={}, degraded=True)

        raw = body.get("pollen")
        if not isinstance(raw, dict):
            _logger.warning(
                "pollen_unusable_body", failure_kind="MissingPollen", provider=self._provider
            )
            return PollenResult(values={}, degraded=True)

        values: dict[str, PollenCategory] = {}
        for taxon, category in raw.items():
            try:
                values[str(taxon)] = PollenCategory(str(category))
            except ValueError:
                # Outside Req 24.7's closed set. Degrade rather than choose a neighbour.
                _logger.warning(
                    "pollen_unusable_body",
                    failure_kind="UnknownCategory",
                    provider=self._provider,
                )
                return PollenResult(values={}, degraded=True)

        return PollenResult(values=values, provider=self._provider)

    def _get_json(self, path: str, lat: float, lon: float) -> dict[str, object] | None:
        """One request returning a JSON object, or None on any failure.

        Requirement 24.8 is upheld structurally: the only parameters are the coordinates, so no
        profile field can travel even by accident. Exactly ONE warning per failure names the
        KIND (Requirement 24.4), and never the credential (Requirement 24.10).
        """
        try:
            response = self._client.get(path, params={"lat": lat, "lon": lon})
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as error:
            _logger.warning(
                "forecast_request_failed",
                path=path,
                failure_kind=type(error).__name__,
                provider=self._provider,
            )
            return None
        except ValueError:
            # A 200 carrying non-JSON. Req 24.4's "unusable body", not a transport failure.
            _logger.warning(
                "forecast_request_failed",
                path=path,
                failure_kind="InvalidJson",
                provider=self._provider,
            )
            return None

        if not isinstance(body, dict):
            _logger.warning(
                "forecast_request_failed",
                path=path,
                failure_kind="NotAnObject",
                provider=self._provider,
            )
            return None
        return body

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()


class HttpMeteorologyProvider:
    """The MeteorologyProvider port over an HTTP provider (Requirement 8.4)."""

    def __init__(
        self,
        base_url: str,
        credential: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = DEFAULT_FORECAST_TIMEOUT_SECONDS,
    ) -> None:
        """Build the provider. The credential is folded into the headers and not retained."""
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-API-KEY": credential},
            transport=transport,
            timeout=timeout_seconds,
        )

    def observation(self, site_code: str, at: dt.datetime) -> MetObservation | None:
        """Meteorology for a site and instant, or None (Requirement 8.4's third source).

        None rather than an exception, and none of the three fields is ever DEFAULTED: a
        substituted humidity would fabricate a correction and let it be reported as calibrated,
        which is the trap task 7 recorded. Absent must stay absent all the way to Req 8.6.
        """
        try:
            response = self._client.get(
                "/observation", params={"site": site_code, "at": at.isoformat()}
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as error:
            _logger.warning(
                "meteorology_request_failed",
                site_code=site_code,
                failure_kind=type(error).__name__,
            )
            return None
        except ValueError:
            _logger.warning(
                "meteorology_request_failed", site_code=site_code, failure_kind="InvalidJson"
            )
            return None

        if not isinstance(body, dict):
            _logger.warning(
                "meteorology_request_failed", site_code=site_code, failure_kind="NotAnObject"
            )
            return None

        try:
            return MetObservation(
                temperature_k=_optional_number(body, "temperature_k"),
                pressure_pa=_optional_number(body, "pressure_pa"),
                relative_humidity_pct=_optional_number(body, "relative_humidity_pct"),
            )
        except ValueError:
            # A non-numeric value where a number belongs. Coercing it would carry a wrong
            # temperature into the Req 9 conversion, which is worse than having none at all.
            _logger.warning(
                "meteorology_request_failed",
                site_code=site_code,
                failure_kind="NonNumericValue",
            )
            return None

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()


def _optional_number(body: dict[str, object], key: str) -> float | None:
    """Read an optional numeric field, distinguishing absent from unusable.

    Raises:
        ValueError: when the key is present but not a number, so the caller can reject the whole
            observation rather than keeping a half-read one.
    """
    if key not in body or body[key] is None:
        return None
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} is not a number")
    return float(value)


__all__ = [
    "DEFAULT_FORECAST_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER",
    "HttpForecastClient",
    "HttpMeteorologyProvider",
]
