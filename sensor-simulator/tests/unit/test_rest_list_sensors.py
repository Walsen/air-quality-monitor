"""Unit tests for GET /ListSensors (task 18.2).

- Req 1.8: no parameters returns one record per Virtual_Sensor, ascending SiteCode.
- Req 1.9: SiteCode/Borough/Sponsor/Facility match the WHOLE field value
  case-insensitively (Sponsor against SponsorName); all supplied filters must match.
- Req 1.10: Latitude+Longitude+RadiusKM returns sensors within the great-circle
  radius, boundary inclusive.
- Req 1.11: RadiusKM without both coordinates is 400 naming the missing parameters.
- Req 1.13: non-numeric or out-of-range coordinates/radius are 400 naming each
  offending parameter and its permitted range.
- Req 1.14: an unrecognized parameter name is IGNORED, not an error.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import httpx

from aqm_simulator.interfaces.rest import build_app
from aqm_simulator.pipeline.determinism import build_pipeline

_KEY = "a-development-api-key-value"
_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_AUTH = {"X-API-KEY": _KEY}


def _get(path: str, params: dict[str, str] | None = None, size: int = 5) -> httpx.Response:
    app = build_app(
        pipeline=build_pipeline(seed=21, size=size), api_key=_KEY, now=lambda: _NOW
    )

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://simulator"
        ) as client:
            return await client.get(path, params=params, headers=_AUTH)

    return asyncio.run(call())


# --- Req 1.8 full list, ascending SiteCode ---------------------------------

def test_no_parameters_returns_every_sensor() -> None:
    body = _get("/ListSensors", size=6).json()
    assert len(body) == 6


def test_ordered_by_ascending_sitecode() -> None:
    codes = [r["SiteCode"] for r in _get("/ListSensors", size=6).json()]
    assert codes == sorted(codes)


def test_records_carry_the_full_metadata_field_set() -> None:
    record = _get("/ListSensors", size=2).json()[0]
    # the twenty-field contract shape, Location included (Req 1.1/1.3)
    for field in ("SiteCode", "SiteName", "Latitude", "Longitude", "Borough",
                  "SiteClassification", "SponsorName", "PowerTag", "Location",
                  "SensorContract"):
        assert field in record
    assert record["Location"]["geometry"]["coordinates"] == [
        record["Latitude"], record["Longitude"]
    ]


# --- Req 1.9 whole-field, case-insensitive filters --------------------------

def test_sitecode_filter_matches_case_insensitively() -> None:
    first = _get("/ListSensors").json()[0]["SiteCode"]
    body = _get("/ListSensors", {"SiteCode": first.lower()}).json()
    assert [r["SiteCode"] for r in body] == [first]


def test_filter_matches_whole_field_not_substring() -> None:
    first = _get("/ListSensors").json()[0]["SiteCode"]
    assert _get("/ListSensors", {"SiteCode": first[:3]}).json() == []


def test_borough_filter_matches_case_insensitively() -> None:
    borough = _get("/ListSensors").json()[0]["Borough"]
    body = _get("/ListSensors", {"Borough": borough.upper()}).json()
    assert body
    assert all(r["Borough"].lower() == borough.lower() for r in body)


def test_sponsor_filter_matches_sponsorname() -> None:
    sponsor = _get("/ListSensors").json()[0]["SponsorName"]
    body = _get("/ListSensors", {"Sponsor": sponsor}).json()
    assert body
    assert all(r["SponsorName"] == sponsor for r in body)


def test_all_supplied_filters_must_match() -> None:
    record = _get("/ListSensors").json()[0]
    both = _get(
        "/ListSensors",
        {"SiteCode": record["SiteCode"], "Borough": "definitely-not-a-borough"},
    ).json()
    assert both == []  # every supplied parameter must match


# --- Req 1.14 unknown parameters ignored -----------------------------------

def test_unknown_parameter_is_ignored() -> None:
    full = _get("/ListSensors", size=4).json()
    with_junk = _get("/ListSensors", {"NotAFilter": "whatever"}, size=4).json()
    assert with_junk == full


# --- Req 1.10 radius filtering ---------------------------------------------

def test_radius_filters_to_nearby_sensors() -> None:
    first = _get("/ListSensors").json()[0]
    body = _get(
        "/ListSensors",
        {"Latitude": first["Latitude"], "Longitude": first["Longitude"], "RadiusKM": "1"},
    ).json()
    assert any(r["SiteCode"] == first["SiteCode"] for r in body)  # itself, distance 0


def test_large_radius_returns_whole_swarm() -> None:
    first = _get("/ListSensors").json()[0]
    body = _get(
        "/ListSensors",
        {"Latitude": first["Latitude"], "Longitude": first["Longitude"],
         "RadiusKM": "500"},
    ).json()
    assert len(body) == 5


# --- Req 1.11 / 1.13 failure modes ----------------------------------------

def test_radius_without_coordinates_is_400_naming_them() -> None:
    response = _get("/ListSensors", {"RadiusKM": "5"})
    assert response.status_code == 400
    body = response.json()
    assert "Latitude" in str(body) and "Longitude" in str(body)
    assert "SiteCode" not in str(body)  # no records in the body


def test_radius_with_only_latitude_is_400() -> None:
    response = _get("/ListSensors", {"RadiusKM": "5", "Latitude": "-17.4"})
    assert response.status_code == 400
    assert "Longitude" in str(response.json())


def test_non_numeric_latitude_is_400_naming_it() -> None:
    response = _get(
        "/ListSensors", {"Latitude": "abc", "Longitude": "-66.1", "RadiusKM": "5"}
    )
    assert response.status_code == 400
    assert "Latitude" in str(response.json())


def test_out_of_range_latitude_is_400_with_range() -> None:
    response = _get(
        "/ListSensors", {"Latitude": "91", "Longitude": "-66.1", "RadiusKM": "5"}
    )
    assert response.status_code == 400
    body = str(response.json())
    assert "Latitude" in body and "-90" in body and "90" in body


def test_out_of_range_longitude_is_400_with_range() -> None:
    response = _get(
        "/ListSensors", {"Latitude": "-17.4", "Longitude": "181", "RadiusKM": "5"}
    )
    assert response.status_code == 400
    assert "Longitude" in str(response.json())


def test_zero_radius_is_400() -> None:
    response = _get(
        "/ListSensors", {"Latitude": "-17.4", "Longitude": "-66.1", "RadiusKM": "0"}
    )
    assert response.status_code == 400  # must be greater than 0
    assert "RadiusKM" in str(response.json())


def test_radius_above_500_is_400() -> None:
    response = _get(
        "/ListSensors", {"Latitude": "-17.4", "Longitude": "-66.1", "RadiusKM": "501"}
    )
    assert response.status_code == 400
    assert "RadiusKM" in str(response.json())


def test_all_offending_parameters_are_named_together() -> None:
    response = _get(
        "/ListSensors", {"Latitude": "abc", "Longitude": "999", "RadiusKM": "-2"}
    )
    assert response.status_code == 400
    body = str(response.json())
    for name in ("Latitude", "Longitude", "RadiusKM"):
        assert name in body  # one message per offending value, not the first only


def test_bad_input_never_returns_500() -> None:
    for params in (
        {"RadiusKM": "abc"},
        {"Latitude": "", "Longitude": "", "RadiusKM": ""},
        {"Latitude": "nan", "Longitude": "inf", "RadiusKM": "1e999"},
    ):
        assert _get("/ListSensors", params).status_code == 400
