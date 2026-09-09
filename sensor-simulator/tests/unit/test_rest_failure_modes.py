"""Unit tests for the five named REST failure modes (task 18.6).

Requirement 16.4 names exactly five documented failure modes. Each must return
its documented status with a JSON body naming the offending parameter, and bad
input must NEVER produce a 500 (engineering-practices §5):

1. RadiusKM without both coordinates      -> 400  (Req 1.11, 2.18)
2. startTime later than endTime           -> 400  (Req 2.14)
3. Species outside the four permitted     -> 400 listing them (Req 2.15)
4. omitted X-API-KEY                      -> 401  (Req 14.3)
5. non-matching X-API-KEY                 -> 401  (Req 14.3)
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
) -> httpx.Response:
    app = build_app(
        pipeline=build_pipeline(seed=41, size=3), api_key=_KEY, now=lambda: _NOW
    )

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://simulator"
        ) as client:
            return await client.get(path, params=params, headers=headers)

    return asyncio.run(call())


_AUTH = {"X-API-KEY": _KEY}


# --- 1. RadiusKM without both coordinates ----------------------------------

@pytest.mark.parametrize("path", ["/ListSensors", "/SensorData"])
@pytest.mark.parametrize(
    "params",
    [
        {"RadiusKM": "5"},
        {"RadiusKM": "5", "Latitude": "-17.4"},
        {"RadiusKM": "5", "Longitude": "-66.1"},
    ],
)
def test_radius_without_both_coordinates_is_400(
    path: str, params: dict[str, str]
) -> None:
    response = _get(path, params, _AUTH)
    assert response.status_code == 400
    body = str(response.json())
    assert "Latitude" in body or "Longitude" in body  # names what is missing
    assert "ScaledValue" not in body  # no records in the body


# --- 2. startTime later than endTime ---------------------------------------

def test_start_later_than_end_is_400_naming_the_range() -> None:
    response = _get(
        "/SensorData",
        {"startTime": "2026-07-01T11:00:00Z", "endTime": "2026-07-01T09:00:00Z"},
        _AUTH,
    )
    assert response.status_code == 400
    body = str(response.json())
    assert "startTime" in body and "endTime" in body
    assert "ScaledValue" not in body


# --- 3. Species outside the four permitted values --------------------------

@pytest.mark.parametrize("bad", ["BOGUS", "pm25", "no2", "PM2.5", "Ozone"])
def test_bad_species_is_400_listing_permitted_values(bad: str) -> None:
    response = _get("/SensorData", {"Species": bad}, _AUTH)
    assert response.status_code == 400
    body = str(response.json())
    for permitted in ("PM25", "NO2", "PM25Index", "NO2Index"):
        assert permitted in body  # the body LISTS the permitted values


# --- 4. omitted X-API-KEY --------------------------------------------------

@pytest.mark.parametrize("path", ["/ListSensors", "/SensorData"])
def test_omitted_api_key_is_401(path: str) -> None:
    response = _get(path)
    assert response.status_code == 401
    assert "X-API-KEY" in str(response.json())


# --- 5. non-matching X-API-KEY --------------------------------------------

@pytest.mark.parametrize("path", ["/ListSensors", "/SensorData"])
@pytest.mark.parametrize("bad", ["wrong", "", _KEY.upper(), _KEY[:-1], _KEY + "x"])
def test_non_matching_api_key_is_401(path: str, bad: str) -> None:
    response = _get(path, headers={"X-API-KEY": bad})
    assert response.status_code == 401


# --- never a 500 ----------------------------------------------------------

@pytest.mark.parametrize("path", ["/ListSensors", "/SensorData"])
@pytest.mark.parametrize(
    "params",
    [
        {"Latitude": "abc"},
        {"Longitude": "1e999"},
        {"RadiusKM": "-1"},
        {"RadiusKM": "nan"},
        {"Latitude": "nan", "Longitude": "nan", "RadiusKM": "nan"},
        {"startTime": "yesterday", "endTime": "tomorrow"},
        {"startTime": "2026-02-30T00:00:00Z", "endTime": "2026-07-01T00:00:00Z"},
        {"Species": "\x00"},
        {"SiteCode": "' OR 1=1 --"},
        {"Borough": "../../etc/passwd"},
    ],
)
def test_hostile_input_never_yields_500(path: str, params: dict[str, str]) -> None:
    response = _get(path, params, _AUTH)
    assert response.status_code in (200, 400)
    assert response.status_code != 500
