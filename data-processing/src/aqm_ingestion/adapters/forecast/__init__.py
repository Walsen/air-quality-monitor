"""HTTP adapters for the forecast, pollen, and meteorology providers.

Both degrade in their RETURN TYPE rather than raising, because Requirements 24.4 and 8.4 define
paths that continue when the external service does not answer.
"""

from aqm_ingestion.adapters.forecast.http_client import (
    DEFAULT_FORECAST_TIMEOUT_SECONDS,
    DEFAULT_PROVIDER,
    HttpForecastClient,
    HttpMeteorologyProvider,
)

__all__ = [
    "DEFAULT_FORECAST_TIMEOUT_SECONDS",
    "DEFAULT_PROVIDER",
    "HttpForecastClient",
    "HttpMeteorologyProvider",
]
