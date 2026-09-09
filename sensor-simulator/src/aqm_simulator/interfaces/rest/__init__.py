"""The authenticated REST pull interface.

Requirement 14 gates ``/ListSensors`` and ``/SensorData`` behind an ``X-API-KEY``
header whose full value must match exactly, compared case-sensitively, and
exempts ``/health``.

Why the auth check is MIDDLEWARE rather than a route dependency: Requirement 14.3
demands 401 *before validating any query parameter*, and FastAPI validates a
route's query model before running that route's dependencies — a bad key together
with a bad parameter would answer 422 instead of 401. Middleware runs ahead of
routing and validation entirely, so the ordering the requirement specifies holds
for every gated route without repeating the check per handler.

The key is supplied at startup from the environment or an injected secret, never
read from source or a committed file (Requirement 14.6), and is excluded from
every response and diagnostic (Requirement 14.10) — nothing here ever puts it in
a body or a log.
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from aqm_simulator.interfaces.rest.list_sensors import (
    QueryError,
    metadata_for,
    parse_list_sensors_query,
    select_sensors,
)
from aqm_simulator.interfaces.rest.sensor_data import (
    parse_sensor_data_query,
    resolve_window,
    select_records,
)
from aqm_simulator.pipeline.driver import backfill
from aqm_simulator.pipeline.publish import PublishPipeline

_EXEMPT_PATHS = frozenset({"/health", "/docs", "/openapi.json", "/redoc"})
_GATED_PREFIXES = ("/ListSensors", "/SensorData")
_MIN_KEY_LENGTH = 16
_MAX_KEY_LENGTH = 256
_MIN_RETENTION_DAYS = 1
_MAX_RETENTION_DAYS = 365

NowFn = Callable[[], dt.datetime]


def _iso_z(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _unauthorized() -> JSONResponse:
    """401 with an error indication and no records (Requirement 14.3)."""
    return JSONResponse(
        status_code=401,
        content={"error": "authentication failed", "parameter": "X-API-KEY"},
    )


def _bad_request(error: QueryError) -> JSONResponse:
    """400 naming every offending parameter, with no records (Req 1.11/1.13)."""
    return JSONResponse(
        status_code=400,
        content={"error": "invalid query parameter", "problems": error.problems},
    )


def build_app(
    pipeline: PublishPipeline,
    api_key: str,
    now: NowFn,
    retention_days: int = 30,
) -> FastAPI:
    """Build the REST application.

    ``now`` is the injected simulated-time source, so no handler calls
    ``datetime.now()`` (engineering-practices §2). An absent or empty key is
    refused here rather than at first request, so the process can exit non-zero
    before accepting any HTTP traffic (Requirement 14.9). ``retention_days``
    bounds what /SensorData will serve (Requirement 14.7).
    """
    if not api_key:
        raise ValueError(
            "the REST API key secret is missing: supply a value of "
            f"{_MIN_KEY_LENGTH}-{_MAX_KEY_LENGTH} characters via the environment "
            "or an injected secret"
        )
    if not _MIN_RETENTION_DAYS <= retention_days <= _MAX_RETENTION_DAYS:
        raise ValueError(
            f"retention window must be between {_MIN_RETENTION_DAYS} and "
            f"{_MAX_RETENTION_DAYS} simulated days; got {retention_days}"
        )

    app = FastAPI(title="Sensor Simulator", docs_url=None, redoc_url=None)
    expected_key = api_key.encode("utf-8")

    @app.middleware("http")
    async def require_api_key(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path not in _EXEMPT_PATHS and path.startswith(_GATED_PREFIXES):
            supplied = request.headers.get("X-API-KEY", "")
            # Compared as BYTES: compare_digest raises TypeError on a non-ASCII
            # str, which would turn a hostile header into a 500 (§5 forbids that).
            # compare_digest also keeps it constant-time and exact-length, so
            # neither a prefix nor a differing case can match.
            if not supplied or not secrets.compare_digest(
                supplied.encode("utf-8"), expected_key
            ):
                return _unauthorized()
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, object]:
        """Swarm size and simulated timestamp, unauthenticated (Req 14.8)."""
        return {
            "SwarmSize": pipeline.swarm_size,
            "SimulatedTimestamp": _iso_z(now()),
        }

    @app.get("/ListSensors")
    async def list_sensors(request: Request) -> Response:
        """Sensor_Metadata_Records, filtered and ordered (Req 1.8-1.11, 1.13, 1.14).

        The raw query mapping is read directly rather than through a strict model
        because Requirement 1.14 requires an unrecognized parameter name to be
        IGNORED; a strict model would reject it.
        """
        try:
            query = parse_list_sensors_query(dict(request.query_params))
        except QueryError as error:
            return _bad_request(error)
        records = [metadata_for(s, pipeline.sensor_contract) for s in pipeline.swarm]
        selected = select_sensors(records, query)
        return JSONResponse(
            content=[r.model_dump(mode="json") for r in selected]
        )

    @app.get("/SensorData")
    async def sensor_data(request: Request) -> Response:
        """Sensor_Data_Records for the resolved window (Req 2.10-2.18, 14.7, 14.11)."""
        try:
            query = parse_sensor_data_query(dict(request.query_params))
        except QueryError as error:
            return _bad_request(error)

        simulated_now = now()
        start, end = resolve_window(
            query, simulated_now, pipeline.publish_minutes, retention_days
        )
        records = list(
            backfill(pipeline, start=start, end=end, reference_time=simulated_now)
        )
        metadata = [metadata_for(s, pipeline.sensor_contract) for s in pipeline.swarm]
        selected = select_records(
            records,
            query,
            {m.SiteCode: m.model_dump(mode="json") for m in metadata},
            {s.site_code: (float(s.latitude), float(s.longitude)) for s in pipeline.swarm},
        )
        return JSONResponse(content=[r.model_dump(mode="json") for r in selected])

    return app
