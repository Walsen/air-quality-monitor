"""Unit tests for the serving routes (tasks 25.2, 25.3, 25.4).

Requirements 19.1-19.5, 19.9-19.13, 25.12.

REQUIREMENT 25.12 IS WHY THE HISTORY ROUTE IS TESTED FOR THE ENVELOPE AND BASIS EXPLICITLY: the
clause says criteria 1 through 6 apply to "every data-bearing endpoint ... including the history
endpoint, not only the primary per-user endpoint". A requirement that names one endpoint
specifically is telling you which one gets forgotten.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any, cast

import httpx
from fastapi import FastAPI

from aqm_ingestion.adapters.memory import (
    InMemoryAuditStore,
    InMemoryForecastClient,
    InMemoryProfileStore,
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
    LocalAuthenticator,
)
from aqm_ingestion.contract.records import SensorMetadataRecord
from aqm_ingestion.domain.aqi.breakpoints import BreakpointTableRegistry
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.domain.profile import RECOGNIZED_CONSENT_VERSIONS
from aqm_ingestion.ports.clock import AdvanceableClock
from aqm_ingestion.ports.protocols import PollenCategory
from aqm_ingestion.serving.app import ServingSettings, build_app
from aqm_ingestion.serving.assembler import ResponseAssembler
from aqm_ingestion.serving.enrichment import Enricher
from aqm_ingestion.serving.geo import GeoSelector
from aqm_ingestion.serving.profiles import ProfileService
from tests.unit.test_records import GOLDEN_METADATA_PAYLOAD

_NOW = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC)
_TOKEN = "dev-token"
_USER = "u_123"
_LONDON = (51.507, -0.128)
_CONSENT = next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS)))


def _metadata(site_code: str, lat: float, lon: float) -> SensorMetadataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_METADATA_PAYLOAD))
    location = cast("dict[str, Any]", fields["Location"])
    cast("dict[str, Any]", location["geometry"])["coordinates"] = [
        f"{lat:.7f}",
        f"{lon:.7f}",
    ]
    fields |= {
        "SiteCode": site_code,
        "SiteName": f"Site {site_code}",
        "Latitude": f"{lat:.7f}",
        "Longitude": f"{lon:.7f}",
    }
    return SensorMetadataRecord(**fields)


def _reading(
    site_code: str = "CB0086",
    species: str = "PM25",
    *,
    instant: dt.datetime | None = None,
    sub_index: int = 68,
) -> CalibratedReading:
    return CalibratedReading(
        key=DedupKey(
            site_code=site_code,
            species=species,
            interval_start=instant or _NOW - dt.timedelta(hours=1),
            duration="PT1H",
        ),
        reported_value=24.1,
        corrected_value=18.2,
        units="ug.m-3" if species == "PM25" else "ppb",
        quality_flag=QualityFlag.CALIBRATED,
        confidence=Confidence.HIGH,
        calibration_strategy="rh_linear" if species == "PM25" else "identity",
        breakpoint_table="epa-2024-05-06",
        ratification_status="R",
        ingested_at=_NOW,
        archive_id=f"archive-{site_code}-{species}",
        sub_index=sub_index,
        band="Moderate",
        method="nowcast" if species == "PM25" else "hourly",
        humidity_source="provider" if species == "PM25" else "none",
        conversion_source="default" if species == "NO2" else None,
        conversion_temperature_k=288.15 if species == "NO2" else None,
        conversion_pressure_pa=74000.0 if species == "NO2" else None,
        nowcast_window_hours=12 if species == "PM25" else None,
        nowcast_hours_available=12 if species == "PM25" else None,
        nowcast_weight_factor=0.72 if species == "PM25" else None,
    )


class _Stack:
    """Everything a serving app needs, wired against in-memory adapters."""

    def __init__(self) -> None:
        """Build the stack at a fixed instant."""
        self.clock = AdvanceableClock(_NOW)
        self.profiles_store = InMemoryProfileStore()
        self.audit = InMemoryAuditStore()
        self.registry = InMemorySensorRegistryStore()
        self.readings = InMemoryReadingsStore(clock=self.clock)
        self.forecast = InMemoryForecastClient()
        self.breakpoints = BreakpointTableRegistry.with_defaults()

        self.profile_service = ProfileService(
            profiles=self.profiles_store, audit=self.audit
        )
        self.selector = GeoSelector(
            registry=self.registry, readings=self.readings, clock=self.clock
        )
        self.enricher = Enricher(client=self.forecast, clock=self.clock)
        self.assembler = ResponseAssembler(
            profiles=self.profile_service,
            selector=self.selector,
            enricher=self.enricher,
            breakpoints=self.breakpoints,
            clock=self.clock,
        )

    def app(self) -> FastAPI:
        return build_app(
            authenticator=LocalAuthenticator({_TOKEN: _USER}),
            clock=self.clock,
            assembler=self.assembler,
            profiles=self.profile_service,
            readings=self.readings,
            registry=self.registry,
            audit=self.audit,
            breakpoints=self.breakpoints,
            settings=ServingSettings(),
        )

    def with_profile(self, **overrides: object) -> _Stack:
        from aqm_ingestion.ports.protocols import VerifiedIdentity

        fields: dict[str, object] = {
            "user_id": _USER,
            "condition": "asthma",
            "sensitivity_level": "high",
            "locations": [
                {"name": "home", "latitude": _LONDON[0], "longitude": _LONDON[1]}
            ],
            "consent": {"version": _CONSENT, "given_at": _NOW},
            "created_at": _NOW,
            "updated_at": _NOW,
        }
        self.profile_service.write(VerifiedIdentity(user_id=_USER), fields | overrides)
        return self

    def with_site(self, site_code: str = "CB0086", *, near: bool = True) -> _Stack:
        lat, lon = _LONDON if near else (48.857, 2.352)
        self.registry.upsert(_metadata(site_code, lat, lon), at=_NOW)
        return self

    def with_reading(self, *readings: CalibratedReading) -> _Stack:
        for reading in readings:
            self.readings.put(reading)
        return self


def _get(stack: _Stack, path: str, *, authorized: bool = True) -> httpx.Response:
    async def _run() -> httpx.Response:
        headers = {"Authorization": f"Bearer {_TOKEN}"} if authorized else {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack.app()),
            base_url="http://test",
        ) as client:
            return await client.get(path, headers=headers)

    return asyncio.run(_run())


def _send(stack: _Stack, method: str, path: str, body: object = None) -> httpx.Response:
    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack.app()),
            base_url="http://test",
        ) as client:
            return await client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {_TOKEN}"},
                json=body,
            )

    return asyncio.run(_run())


# --- 25.2 / Req 19.1, 19.2: GET /v1/air-quality/me ---------------------

def _me(stack: _Stack) -> dict[str, Any]:
    response = _get(stack, "/v1/air-quality/me")
    assert response.status_code == 200, response.text
    return cast("dict[str, Any]", response.json())


def test_the_me_route_returns_the_pinned_top_level_members() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    assert set(_me(stack)) == {
        "user",
        "generatedAt",
        "locations",
        "nearestSensors",
        "personalized",
        "forecast",
        "basis",
        "advisoryScope",
        "emergencyGuidance",
        "disclaimer",
    }


def test_the_me_route_names_the_verified_user() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    assert _me(stack)["user"] == _USER


def test_the_me_route_reports_the_users_locations() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    assert _me(stack)["locations"] == [
        {"name": "home", "lat": _LONDON[0], "lon": _LONDON[1]}
    ]


def test_the_me_route_reports_the_selected_site_and_its_measurement() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    sensors = _me(stack)["nearestSensors"]
    assert len(sensors) == 1
    assert sensors[0]["siteCode"] == "CB0086"
    assert sensors[0]["siteName"] == "Site CB0086"
    assert [m["species"] for m in sensors[0]["measurements"]] == ["PM25"]


def test_every_measurement_carries_a_flag_and_a_confidence() -> None:
    # Req 25.6 / 13.10 at the wire.
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    for sensor in _me(stack)["nearestSensors"]:
        for measurement in sensor["measurements"]:
            assert measurement["qualityFlag"]
            assert measurement["confidence"]


def test_the_me_route_reports_the_overall_aqi_and_driving_pollutant() -> None:
    stack = (
        _Stack()
        .with_profile()
        .with_site()
        .with_reading(
            _reading("CB0086", "PM25", sub_index=68),
            _reading("CB0086", "NO2", sub_index=26),
        )
    )
    sensor = _me(stack)["nearestSensors"][0]
    assert sensor["overallAqi"] == 68
    assert sensor["drivingPollutant"] == "PM25"


def test_a_site_with_no_fresh_reading_reports_null_aqi() -> None:
    # Req 20.9 and Req 12.6 together: the site is present, the index is null, not zero.
    stack = _Stack().with_profile().with_site()
    sensor = _me(stack)["nearestSensors"][0]
    assert sensor["measurements"] == []
    assert sensor["overallAqi"] is None
    assert sensor["band"] is None


def test_the_measurements_are_ordered_by_the_weighted_focus() -> None:
    # asthma weights PM25 first (Req 21.2), and PM25 also leads the default precedence, so the
    # discriminating case is copd, which weights NO2 first.
    stack = (
        _Stack()
        .with_profile(condition="copd")
        .with_site()
        .with_reading(_reading("CB0086", "PM25"), _reading("CB0086", "NO2"))
    )
    sensor = _me(stack)["nearestSensors"][0]
    assert [m["species"] for m in sensor["measurements"]] == ["NO2", "PM25"]


def test_the_personalized_member_reports_the_weighted_focus_and_gaps() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    personalized = _me(stack)["personalized"]
    assert personalized["condition"] == "asthma"
    assert personalized["weightedFocus"] == ["PM25"]
    # O3 and NO2 are weighted for asthma but not available here.
    assert set(personalized["unavailableWeightedSpecies"]) == {"O3", "NO2"}


def test_the_default_profile_is_declared() -> None:
    # Req 17.12 at the wire: no profile written, so the default is used and said so.
    stack = _Stack().with_site().with_reading(_reading())
    assert _me(stack)["personalized"]["usedDefaultProfile"] is True


def test_a_crossing_is_reported_with_its_basis() -> None:
    # sensitivity high escalates at 51 (Req 22.2), and the reading's sub-index is 68.
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    personalized = _me(stack)["personalized"]
    assert personalized["thresholdCrossed"] is True
    assert personalized["escalationSubIndex"] == 51
    assert personalized["thresholdSource"] == "sensitivity_level"
    assert personalized["crossings"][0]["species"] == "PM25"


def test_no_crossing_is_reported_below_the_threshold() -> None:
    stack = (
        _Stack()
        .with_profile()
        .with_site()
        .with_reading(_reading(sub_index=10))
    )
    assert _me(stack)["personalized"]["thresholdCrossed"] is False


def test_pollen_is_reported_when_the_condition_marks_it_relevant() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    stack.forecast.set_pollen(*_LONDON, {"grass": PollenCategory.HIGH})
    assert _me(stack)["personalized"]["pollen"] == {"grass": "high"}


def test_pollen_is_null_for_a_condition_that_does_not_mark_it_relevant() -> None:
    stack = _Stack().with_profile(condition="copd").with_site().with_reading(_reading())
    stack.forecast.set_pollen(*_LONDON, {"grass": PollenCategory.HIGH})
    assert _me(stack)["personalized"]["pollen"] is None


def test_the_inhaled_dose_is_null_without_activity_inputs() -> None:
    # Req 23.3: an assumed dose is not a measured one.
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    assert _me(stack)["personalized"]["inhaledDose"] is None


def test_the_inhaled_dose_is_reported_with_activity_inputs() -> None:
    stack = (
        _Stack()
        .with_profile(activity_level="moderate", activity_duration_hours=1.5)
        .with_site()
        .with_reading(_reading())
    )
    # 18.2 ug/m3 * 2.0 m3/h * 1.5 h
    assert _me(stack)["personalized"]["inhaledDose"] == 54.6


def test_the_forecast_is_reported_with_its_provider_and_trend() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    stack.forecast.set_forecast(*_LONDON, {"aqi": 88.0})
    # Pollen must be configured too: asthma marks pollen relevant (Req 21.2), so an unconfigured
    # pollen answer is a genuine gap and Req 24.4 would rightly report the response as degraded.
    stack.forecast.set_pollen(*_LONDON, {"grass": PollenCategory.LOW})
    forecast = _me(stack)["forecast"]
    assert forecast["tomorrowAqi"] == 88.0
    assert forecast["trend"] == "rising"
    assert forecast["source"] == "in-memory"
    assert forecast["degraded"] is False


def test_a_pollen_gap_degrades_the_response_but_still_serves_the_forecast() -> None:
    # §5's per-item isolation at the wire: losing pollen must not cost the user the forecast,
    # and the gap must be DISCLOSED rather than passing as a complete answer.
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    stack.forecast.set_forecast(*_LONDON, {"aqi": 88.0})
    body = _me(stack)
    assert body["forecast"]["tomorrowAqi"] == 88.0
    assert body["forecast"]["degraded"] is True
    assert body["personalized"]["pollen"] is None


def test_an_unconfigured_forecast_degrades_without_failing() -> None:
    # Req 24.4: current-conditions advice remains useful without a forecast.
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    forecast = _me(stack)["forecast"]
    assert forecast["tomorrowAqi"] is None
    assert forecast["degraded"] is True


def test_the_basis_is_populated() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    basis = _me(stack)["basis"]
    assert basis["breakpointTable"] == "epa-2024-05-06"
    assert basis["calibrationStrategies"] == {"PM25": "rh_linear"}
    assert basis["humiditySource"] == "provider"
    assert basis["nowcast"]["windowHours"] == 12
    assert len(basis["records"]) == 1


def test_the_guardrail_envelope_is_present() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    body = _me(stack)
    assert body["advisoryScope"] == "exposure-reduction"
    assert body["disclaimer"].strip()
    assert body["emergencyGuidance"].strip()


def test_every_instant_is_the_z_form() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    rendered = json.dumps(_me(stack))
    assert "+00:00" not in rendered
    assert _me(stack)["generatedAt"].endswith("Z")


def test_the_me_route_appends_one_audit_record() -> None:
    # Req 25.7: one Audit_Record per served response.
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    _me(stack)
    assert stack.audit.de_identified_count() == 1
    record = stack.audit.records_for(_USER)[0]
    assert record.route == "/v1/air-quality/me"
    assert record.threshold_crossed is True
    assert record.breakpoint_table == "epa-2024-05-06"


def test_the_audit_record_holds_no_health_adjacent_value() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    _me(stack)
    rendered = json.dumps(
        {
            "route": stack.audit.records_for(_USER)[0].route,
            "strategies": list(stack.audit.records_for(_USER)[0].calibration_strategies),
        }
    )
    assert "asthma" not in rendered
    assert "high" not in rendered


# --- 25.3 / Req 19.3, 19.9: GET /v1/air-quality/history ---------------

def _history_stack() -> _Stack:
    stack = _Stack().with_profile().with_site()
    for hour in range(3):
        stack.with_reading(
            _reading(instant=_NOW - dt.timedelta(hours=hour + 1), sub_index=40 + hour)
        )
    return stack


def test_the_history_route_returns_readings_ascending() -> None:
    stack = _history_stack()
    response = _get(
        stack,
        "/v1/air-quality/history?siteCode=CB0086"
        f"&startTime={_iso(_NOW - dt.timedelta(hours=6))}&endTime={_iso(_NOW)}",
    )
    assert response.status_code == 200, response.text
    readings = response.json()["readings"]
    instants = [r["dateTime"] for r in readings]
    assert instants == sorted(instants)


def test_each_history_reading_carries_the_required_members() -> None:
    stack = _history_stack()
    response = _get(
        stack,
        "/v1/air-quality/history?siteCode=CB0086"
        f"&startTime={_iso(_NOW - dt.timedelta(hours=6))}&endTime={_iso(_NOW)}",
    )
    reading = response.json()["readings"][0]
    assert set(reading) == {
        "dateTime",
        "species",
        "correctedValue",
        "units",
        "qualityFlag",
        "confidence",
        "subIndex",
        "band",
    }


def test_the_history_route_carries_the_guardrail_envelope() -> None:
    # Req 25.12 names the history endpoint SPECIFICALLY because it is the one that gets
    # forgotten — so this is asserted here rather than assumed from the /me route.
    stack = _history_stack()
    body = _get(
        stack,
        "/v1/air-quality/history?siteCode=CB0086"
        f"&startTime={_iso(_NOW - dt.timedelta(hours=6))}&endTime={_iso(_NOW)}",
    ).json()
    assert body["advisoryScope"] == "exposure-reduction"
    assert body["disclaimer"].strip()
    assert body["emergencyGuidance"].strip()


def test_the_history_route_carries_the_basis() -> None:
    stack = _history_stack()
    body = _get(
        stack,
        "/v1/air-quality/history?siteCode=CB0086"
        f"&startTime={_iso(_NOW - dt.timedelta(hours=6))}&endTime={_iso(_NOW)}",
    ).json()
    assert body["basis"]["breakpointTable"] == "epa-2024-05-06"


def test_the_history_route_filters_by_species() -> None:
    stack = _history_stack().with_reading(_reading(species="NO2"))
    body = _get(
        stack,
        "/v1/air-quality/history?siteCode=CB0086&species=NO2"
        f"&startTime={_iso(_NOW - dt.timedelta(hours=6))}&endTime={_iso(_NOW)}",
    ).json()
    assert {r["species"] for r in body["readings"]} == {"NO2"}


def test_the_history_route_appends_an_audit_record() -> None:
    stack = _history_stack()
    _get(
        stack,
        "/v1/air-quality/history?siteCode=CB0086"
        f"&startTime={_iso(_NOW - dt.timedelta(hours=6))}&endTime={_iso(_NOW)}",
    )
    assert stack.audit.records_for(_USER)[0].route == "/v1/air-quality/history"


def _iso(instant: dt.datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- 25.4 / Req 19.4, 19.5, 19.10: profile and health -----------------

def test_the_profile_route_returns_the_stored_profile() -> None:
    stack = _Stack().with_profile()
    body = _get(stack, "/v1/profile/me").json()
    assert body["condition"] == "asthma"
    assert body["usedDefaultProfile"] is False


def test_the_profile_route_declares_the_default_when_none_exists() -> None:
    body = _get(_Stack(), "/v1/profile/me").json()
    assert body["usedDefaultProfile"] is True
    assert body["condition"] == "none_declared"


def test_the_profile_route_replaces_the_profile() -> None:
    stack = _Stack().with_profile()
    response = _send(
        stack,
        "PUT",
        "/v1/profile/me",
        {
            "condition": "copd",
            "sensitivity_level": "standard",
            "consent": {"version": _CONSENT, "given_at": _iso(_NOW)},
            "created_at": _iso(_NOW),
            "updated_at": _iso(_NOW),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["condition"] == "copd"


def test_a_profile_write_rejecting_a_forbidden_field_is_400_not_500() -> None:
    # §5: bad input never 500. Req 17.4's allowlist rejection is bad INPUT, distinct from
    # Req 25.11's deliberate 500 for a guardrail-violating OUTPUT.
    stack = _Stack().with_profile()
    response = _send(
        stack,
        "PUT",
        "/v1/profile/me",
        {
            "condition": "asthma",
            "sensitivity_level": "standard",
            "medication": "salbutamol",
            "consent": {"version": _CONSENT, "given_at": _iso(_NOW)},
            "created_at": _iso(_NOW),
            "updated_at": _iso(_NOW),
        },
    )
    assert response.status_code == 400
    assert "salbutamol" not in response.text


def test_the_profile_route_deletes_the_profile() -> None:
    stack = _Stack().with_profile()
    response = _send(stack, "DELETE", "/v1/profile/me")
    assert response.status_code == 200
    assert response.json()["profileDeleted"] is True
    assert _get(stack, "/v1/profile/me").json()["usedDefaultProfile"] is True


def test_the_health_route_reports_the_resolved_table_and_strategies() -> None:
    # Req 19.10.
    body = _get(_Stack(), "/health", authorized=False).json()
    assert body["status"] == "ok"
    assert body["breakpointTable"] == "epa-2024-05-06"
    assert "rh_linear" in body["calibrationStrategies"]


def test_the_health_route_carries_no_reading_profile_or_credential() -> None:
    stack = _Stack().with_profile().with_site().with_reading(_reading())
    body = _get(stack, "/health", authorized=False).json()
    assert set(body) == {
        "status",
        "checkedAt",
        "breakpointTable",
        "calibrationStrategies",
    }
    rendered = json.dumps(body)
    assert "asthma" not in rendered
    assert _TOKEN not in rendered
    assert "18.2" not in rendered
