"""Unit tests for the REST app, X-API-KEY auth, and /health (task 18.1).

- Req 14.2: every /ListSensors and /SensorData request carries an X-API-KEY whose
  FULL value matches exactly, compared case-sensitively.
- Req 14.3: a missing, empty, or non-matching key gives 401 BEFORE any query
  parameter is validated, with no records in the body and an error indication.
- Req 14.4: a 200 response is application/json with a top-level JSON ARRAY.
- Req 14.5: an authenticated query matching nothing gives 200 and [].
- Req 14.8: /health gives 200 with swarm size and the simulated timestamp, and
  needs no X-API-KEY.
- Req 14.10: the key never appears in any response body, including /health.

The app is driven in-process through httpx's ASGI transport rather than
starlette's TestClient: the installed starlette deprecates plain httpx there and
wants a separate httpx2 package, and an in-process ASGI call needs no extra
dependency and no network at all.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import httpx
import pytest

from aqm_simulator.interfaces.rest import build_app
from aqm_simulator.pipeline.determinism import build_pipeline

_KEY = "a-development-api-key-value"
_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _get(
    path: str,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    api_key: str = _KEY,
    size: int = 3,
) -> httpx.Response:
    """Issue one in-process GET against a freshly built app."""
    app = build_app(
        pipeline=build_pipeline(seed=11, size=size),
        api_key=api_key,
        now=lambda: _NOW,  # injected clock, never datetime.now() (§2)
    )

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://simulator"
        ) as client:
            return await client.get(path, params=params, headers=headers)

    return asyncio.run(call())


# --- Req 14.8 / 14.10 health ------------------------------------------------

def test_health_needs_no_api_key() -> None:
    assert _get("/health").status_code == 200


def test_health_reports_swarm_size_and_simulated_timestamp() -> None:
    body = _get("/health", size=4).json()
    assert body["SwarmSize"] == 4
    assert body["SimulatedTimestamp"] == "2026-07-01T12:00:00Z"


def test_health_never_leaks_the_api_key() -> None:
    assert _KEY not in _get("/health").text


# --- Req 14.2 / 14.3 auth ---------------------------------------------------

def test_missing_key_is_401() -> None:
    response = _get("/ListSensors")
    assert response.status_code == 401
    assert "authentication" in response.text.lower()


def test_empty_key_is_401() -> None:
    assert _get("/ListSensors", headers={"X-API-KEY": ""}).status_code == 401


def test_non_matching_key_is_401() -> None:
    assert _get("/ListSensors", headers={"X-API-KEY": "wrong"}).status_code == 401


def test_key_comparison_is_case_sensitive() -> None:
    assert _get("/ListSensors", headers={"X-API-KEY": _KEY.upper()}).status_code == 401


def test_partial_key_prefix_is_401() -> None:
    # the FULL value must match, not a prefix
    assert _get("/ListSensors", headers={"X-API-KEY": _KEY[:-1]}).status_code == 401


def test_longer_key_with_correct_prefix_is_401() -> None:
    assert _get("/ListSensors", headers={"X-API-KEY": _KEY + "x"}).status_code == 401


def test_auth_runs_before_query_validation() -> None:
    """Req 14.3: 401 must win over a query-parameter fault, not 400/422."""
    response = _get(
        "/SensorData",
        params={"RadiusKM": "not-a-number", "Species": "BOGUS"},
        headers={"X-API-KEY": "wrong"},
    )
    assert response.status_code == 401


def test_non_ascii_key_header_is_401_not_500() -> None:
    # §5: hostile input must never produce a 500. HTTP headers are latin-1 on the
    # wire, so the value is sent as raw BYTES — a conforming client refuses to
    # encode a non-ASCII str, but a hostile peer can still put these bytes on the
    # wire, and the server must answer 401 rather than fault.
    app = build_app(
        pipeline=build_pipeline(seed=11, size=2), api_key=_KEY, now=lambda: _NOW
    )

    async def call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://simulator"
        ) as client:
            return await client.get(
                "/ListSensors", headers={b"X-API-KEY": b"k\xe9y-with-accent"}
            )

    assert asyncio.run(call()).status_code == 401


def test_401_body_carries_no_records() -> None:
    response = _get("/SensorData")
    assert "SiteCode" not in response.text
    assert isinstance(response.json(), dict)  # an error object, never an array


def test_valid_key_is_accepted() -> None:
    assert _get("/ListSensors", headers={"X-API-KEY": _KEY}).status_code == 200


# --- Req 14.4 / 14.5 response shape ----------------------------------------

def test_200_is_json_array() -> None:
    response = _get("/ListSensors", headers={"X-API-KEY": _KEY})
    assert response.headers["content-type"].startswith("application/json")
    assert isinstance(response.json(), list)  # top-level ARRAY


def test_responses_never_include_the_api_key() -> None:
    for path in ("/ListSensors", "/SensorData"):
        assert _KEY not in _get(path, headers={"X-API-KEY": _KEY}).text


# --- Req 14.9 startup validation -------------------------------------------

def test_empty_api_key_is_rejected_at_build_time() -> None:
    with pytest.raises(ValueError, match="API key"):
        build_app(pipeline=build_pipeline(seed=1, size=1), api_key="", now=lambda: _NOW)
