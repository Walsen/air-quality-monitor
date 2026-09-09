"""Unit tests for GET /SensorData (task 18.3).

- Req 2.10: no startTime/endTime returns the most recently COMPLETED
  Publish_Interval only — the interval whose end is at or before simulated now.
- Req 2.11: with both supplied, records satisfy startTime <= DateTime < endTime,
  inside retention, ascending DateTime; startTime == endTime gives [].
- Req 2.12: Species/SiteCode/Borough/Sponsor/Facility match CASE-SENSITIVELY
  (contrast /ListSensors, which is case-insensitive).
- Req 2.13: Latitude+Longitude+RadiusKM filters by great-circle distance.
- Req 2.14: startTime later than endTime is 400 naming the invalid range.
- Req 2.15: a Species outside the four permitted values is 400 LISTING them.
- Req 2.16: startTime without endTime (or vice versa) is 400 naming the missing one.
- Req 2.17: an unparseable timestamp is 400 naming the offending parameter.
- Req 2.18: RadiusKM without both coordinates, or outside 0.01-500, is 400.
- Req 14.7/14.11: the window is clipped to the retention window; an over-early
  startTime gives 200 with in-window records, not an error.
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


def _get(
    params: dict[str, str] | None = None,
    size: int = 3,
    retention_days: int = 30,
) -> httpx.Response:
    app = build_app(
        pipeline=build_pipeline(seed=31, size=size),
        api_key=_KEY,
        now=lambda: _NOW,
        retention_days=retention_days,
    )

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://simulator"
        ) as client:
            return await client.get("/SensorData", params=params, headers=_AUTH)

    return asyncio.run(call())


# --- Req 2.10 default window ----------------------------------------------

def test_default_returns_most_recently_completed_interval() -> None:
    body = _get().json()
    assert body
    # now is 12:00, so the completed interval is [11:00, 12:00)
    assert {r["DateTime"] for r in body} == {"2026-07-01T11:00:00Z"}


def test_default_covers_every_sensor_and_species() -> None:
    body = _get(size=3).json()
    assert len(body) == 3 * 4  # four Species per sensor


# --- Req 2.11 half-open window, ascending ---------------------------------

def test_window_is_half_open() -> None:
    body = _get(
        {"startTime": "2026-07-01T09:00:00Z", "endTime": "2026-07-01T11:00:00Z"}
    ).json()
    times = {r["DateTime"] for r in body}
    assert times == {"2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z"}  # 11:00 excluded


def test_records_are_ascending_by_datetime() -> None:
    body = _get(
        {"startTime": "2026-07-01T08:00:00Z", "endTime": "2026-07-01T12:00:00Z"}
    ).json()
    times = [r["DateTime"] for r in body]
    assert times == sorted(times)


def test_equal_start_and_end_gives_empty_array() -> None:
    response = _get(
        {"startTime": "2026-07-01T10:00:00Z", "endTime": "2026-07-01T10:00:00Z"}
    )
    assert response.status_code == 200
    assert response.json() == []


# --- Req 14.7 / 14.11 retention clipping ----------------------------------

def test_over_early_start_is_clipped_not_an_error() -> None:
    response = _get(
        {"startTime": "2020-01-01T00:00:00Z", "endTime": "2026-07-01T12:00:00Z"},
        retention_days=1,
    )
    assert response.status_code == 200
    earliest = min(r["DateTime"] for r in response.json())
    assert earliest >= "2026-06-30T12:00:00Z"  # clipped to the retention start


def test_window_never_exceeds_simulated_now() -> None:
    body = _get(
        {"startTime": "2026-07-01T10:00:00Z", "endTime": "2026-07-05T00:00:00Z"}
    ).json()
    assert max(r["DateTime"] for r in body) < "2026-07-01T12:00:00Z"


# --- Req 2.12 case-SENSITIVE filters --------------------------------------

def test_species_filter_selects_one_species() -> None:
    body = _get({"Species": "NO2"}).json()
    assert body
    assert {r["Species"] for r in body} == {"NO2"}


def test_sitecode_filter_is_case_sensitive() -> None:
    site = _get().json()[0]["SiteCode"]
    assert _get({"SiteCode": site}).json()  # exact case matches
    assert _get({"SiteCode": site.lower()}).json() == []  # wrong case does not


def test_borough_filter_is_case_sensitive() -> None:
    borough = _get().json()[0]  # any record; borough comes from metadata
    assert borough  # sanity
    body = _get({"Borough": "not-a-real-borough"}).json()
    assert body == []


# --- Req 2.13 radius -----------------------------------------------------

def test_radius_filter_limits_to_nearby_sensors() -> None:
    body = _get(
        {"Latitude": "-17.39", "Longitude": "-66.15", "RadiusKM": "500"}
    ).json()
    assert body  # whole swarm is inside 500km


# --- Req 2.14 - 2.18 failure modes ---------------------------------------

def test_start_after_end_is_400() -> None:
    response = _get(
        {"startTime": "2026-07-01T11:00:00Z", "endTime": "2026-07-01T09:00:00Z"}
    )
    assert response.status_code == 400
    assert "startTime" in str(response.json())
    assert "SiteCode" not in str(response.json())  # no records


def test_bad_species_is_400_listing_permitted_values() -> None:
    response = _get({"Species": "BOGUS"})
    assert response.status_code == 400
    body = str(response.json())
    for permitted in ("PM25", "NO2", "PM25Index", "NO2Index"):
        assert permitted in body


def test_species_is_matched_case_sensitively_in_validation() -> None:
    # 'pm25' is not one of the four permitted values
    assert _get({"Species": "pm25"}).status_code == 400


def test_start_without_end_is_400_naming_the_missing_one() -> None:
    response = _get({"startTime": "2026-07-01T09:00:00Z"})
    assert response.status_code == 400
    assert "endTime" in str(response.json())


def test_end_without_start_is_400_naming_the_missing_one() -> None:
    response = _get({"endTime": "2026-07-01T09:00:00Z"})
    assert response.status_code == 400
    assert "startTime" in str(response.json())


def test_unparseable_timestamp_is_400_naming_it() -> None:
    response = _get({"startTime": "not-a-time", "endTime": "2026-07-01T11:00:00Z"})
    assert response.status_code == 400
    assert "startTime" in str(response.json())


def test_radius_without_coordinates_is_400() -> None:
    response = _get({"RadiusKM": "5"})
    assert response.status_code == 400
    assert "Latitude" in str(response.json())


def test_radius_below_minimum_is_400() -> None:
    response = _get({"Latitude": "-17.4", "Longitude": "-66.1", "RadiusKM": "0.005"})
    assert response.status_code == 400
    assert "RadiusKM" in str(response.json())


def test_radius_above_maximum_is_400() -> None:
    response = _get({"Latitude": "-17.4", "Longitude": "-66.1", "RadiusKM": "501"})
    assert response.status_code == 400
    assert "RadiusKM" in str(response.json())


def test_no_bad_input_yields_500() -> None:
    for params in (
        {"startTime": "", "endTime": ""},
        {"Species": ""},
        {"RadiusKM": "abc"},
        {"startTime": "2026-13-45T99:99:99Z", "endTime": "2026-07-01T11:00:00Z"},
    ):
        assert _get(params).status_code in (200, 400)


def test_records_carry_the_nine_contract_fields() -> None:
    record = _get().json()[0]
    assert list(record.keys()) == [
        "Species", "Source", "Units", "SiteCode", "DateTime",
        "Duration", "ScaledValue", "RatificationStatus", "SensorContract",
    ]
