"""Property tests for the serving routes.

Feature: ingestion-and-serving-service, Properties 38, 40.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any, cast

import httpx
from fastapi import FastAPI
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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
from aqm_ingestion.ports.protocols import VerifiedIdentity
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
_SITE = "CB0086"

_conditions = st.sampled_from(["asthma", "copd", "allergic_rhinitis", "none_declared"])
_sensitivities = st.sampled_from(["standard", "elevated", "high"])
_sub_indexes = st.integers(min_value=1, max_value=400)


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


def _reading(species: str, sub_index: int) -> CalibratedReading:
    return CalibratedReading(
        key=DedupKey(
            site_code=_SITE,
            species=species,
            interval_start=_NOW - dt.timedelta(hours=1),
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
        archive_id=f"archive-{species}",
        sub_index=sub_index,
        band="Moderate",
        method="nowcast" if species == "PM25" else "hourly",
        humidity_source="provider" if species == "PM25" else "none",
        nowcast_window_hours=12 if species == "PM25" else None,
        nowcast_hours_available=12 if species == "PM25" else None,
        nowcast_weight_factor=0.72 if species == "PM25" else None,
    )


def _fresh_app(
    condition: str, sensitivity: str, sub_index: int, *, with_profile: bool = True
) -> FastAPI:
    """A COMPLETELY fresh stack, so determinism is not an artefact of shared state."""
    clock = AdvanceableClock(_NOW)
    profiles_store = InMemoryProfileStore()
    audit = InMemoryAuditStore()
    registry = InMemorySensorRegistryStore()
    readings = InMemoryReadingsStore(clock=clock)
    registry.upsert(_metadata(_SITE, *_LONDON), at=_NOW)
    readings.put(_reading("PM25", sub_index))
    readings.put(_reading("NO2", max(1, sub_index // 2)))

    profile_service = ProfileService(profiles=profiles_store, audit=audit)
    if with_profile:
        profile_service.write(
            VerifiedIdentity(user_id=_USER),
            {
                "user_id": _USER,
                "condition": condition,
                "sensitivity_level": sensitivity,
                "locations": [
                    {"name": "home", "latitude": _LONDON[0], "longitude": _LONDON[1]}
                ],
                "consent": {"version": _CONSENT, "given_at": _NOW},
                "created_at": _NOW,
                "updated_at": _NOW,
            },
        )

    forecast = InMemoryForecastClient()
    return build_app(
        authenticator=LocalAuthenticator({_TOKEN: _USER}),
        clock=clock,
        assembler=ResponseAssembler(
            profiles=profile_service,
            selector=GeoSelector(registry=registry, readings=readings, clock=clock),
            enricher=Enricher(client=forecast, clock=clock),
            breakpoints=BreakpointTableRegistry.with_defaults(),
            clock=clock,
        ),
        profiles=profile_service,
        readings=readings,
        registry=registry,
        audit=audit,
        settings=ServingSettings(),
    )


def _fetch(app: FastAPI, path: str, headers: dict[str, str]) -> httpx.Response:
    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get(path, headers=headers)

    return asyncio.run(_run())


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(condition=_conditions, sensitivity=_sensitivities, sub_index=_sub_indexes)
def test_property_38_serving_responses_are_deterministic(
    condition: str, sensitivity: str, sub_index: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 38.

    Serving responses are deterministic.
    Validates Requirements 27.2 and 27.4.

    Compares the SERIALISED bodies from two COMPLETELY FRESH stacks rather than two calls
    against
    one app, which is the shape Property 39 used for the same reason: two calls on one stack
    could
    agree because of cached or accumulated state, while two independent stacks agree only if the
    computation itself is reproducible. The clock is fixed, so ``generatedAt`` is part of what
    must
    match rather than an excuse for a difference.
    """
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    first = _fetch(
        _fresh_app(condition, sensitivity, sub_index), "/v1/air-quality/me", headers
    )
    second = _fetch(
        _fresh_app(condition, sensitivity, sub_index), "/v1/air-quality/me", headers
    )

    assert first.status_code == 200
    assert second.status_code == 200
    # Byte-identical, not merely equal as parsed objects: Requirement 27.4's reproducibility is
    # about the document, and two bodies differing only in member ORDER would compare equal as
    # dicts while being different responses to a client that streams or hashes them.
    assert first.text == second.text


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(condition=_conditions, sensitivity=_sensitivities, sub_index=_sub_indexes)
def test_property_38_the_history_response_is_deterministic(
    condition: str, sensitivity: str, sub_index: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 38.

    The history route is deterministic too — asserted separately because Requirement 25.12's
    pattern of naming the history endpoint applies here as well: a property that only covered
    the
    primary route would leave the one that gets forgotten uncovered.
    """
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    path = (
        f"/v1/air-quality/history?siteCode={_SITE}"
        "&startTime=2026-09-08T00:00:00Z&endTime=2026-09-08T12:00:00Z"
    )
    first = _fetch(_fresh_app(condition, sensitivity, sub_index), path, headers)
    second = _fetch(_fresh_app(condition, sensitivity, sub_index), path, headers)
    assert first.status_code == 200
    assert first.text == second.text


_bad_headers = st.sampled_from(
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic abc"},
        {"Authorization": "bearer wrong-token"},
        {"Authorization": "Bearer not-a-token"},
        {"Authorization": "Token abc"},
        {"Authorization": "Bearer  "},
    ]
)

_bad_params = st.sampled_from(
    [
        "",
        "?startTime=nonsense",
        "?startTime=nonsense&endTime=also-nonsense",
        "?siteCode=UNKNOWN&startTime=2026-13-45T00:00:00Z&endTime=x",
        "?species=O3",
        "?userId=someone-else",
        "?siteCode=" + "9" * 300,
        "?startTime=2020-01-01T00:00:00Z&endTime=2026-09-08T12:00:00Z",
    ]
)


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(
    headers=_bad_headers,
    params=_bad_params,
    route=st.sampled_from(
        ["/v1/air-quality/me", "/v1/air-quality/history", "/v1/profile/me"]
    ),
)
def test_property_40_authentication_precedes_validation_and_leaks_nothing(
    headers: dict[str, str], params: str, route: str
) -> None:
    """Feature: ingestion-and-serving-service, Property 40.

    Authentication precedes validation and leaks nothing.
    Validates Requirements 18.2, 18.3, 18.4, 18.5 and 18.9.

    Requirement 18.4's precedence is asserted as 401 WINNING over every parameter fault,
    whatever
    the fault: a bad instant, an unknown site, an oversize span, a cross-identity attempt. That
    is
    the direction that matters — a gate placed after validation would answer 400, 404 or 422 for
    these instead.
    """
    app = _fresh_app("asthma", "high", 68)
    response = _fetch(app, route + params, headers)

    # Req 18.2 / 18.3 / 18.4: an unverifiable credential always wins, whatever else is wrong.
    assert response.status_code == 401, (
        f"{route}{params} with {headers} answered {response.status_code}"
    )

    # Req 18.3: the body carries the detail and the CATEGORY, and nothing more.
    body = response.json()
    assert set(body) == {"detail", "category"}

    # Req 18.9: no Reading value, no profile value, no site metadata.
    for leaked in ("18.2", "24.1", "asthma", "high", "Moderate", _SITE, "Site CB0086"):
        assert leaked not in response.text, f"leaked {leaked!r}"

    # And no credential material, however malformed (Requirement 18.7).
    supplied = headers.get("Authorization", "")
    if supplied.strip():
        assert supplied not in response.text


@settings(suppress_health_check=[HealthCheck.too_slow])
@given(params=_bad_params)
def test_property_40_an_authenticated_request_is_not_401(params: str) -> None:
    """Feature: ingestion-and-serving-service, Property 40.

    The counterpart, and the half that stops the property above being vacuous: WITH a valid
    credential the same requests do NOT answer 401, so the 401s above are evidence of the gate
    rather than of a service that refuses everything.
    """
    app = _fresh_app("asthma", "high", 68)
    response = _fetch(
        app, "/v1/air-quality/history" + params, {"Authorization": f"Bearer {_TOKEN}"}
    )
    assert response.status_code != 401
    # Req 19.8: and never a 500 either, whatever the parameter fault.
    assert response.status_code < 500
