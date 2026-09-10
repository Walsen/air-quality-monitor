"""Boundary failure-mode tests for the serving API (tasks 25.5 and 25.8).

Requirements 19.6, 19.7, 19.8, 19.9, 18.2, 18.3, 18.5, 18.11, 25.11.

ONE TEST PER DOCUMENTED STATUS, as task 25.8 enumerates them, plus the hostile-input matrix
Requirement 19.8 demands: "THE Service SHALL never respond with HTTP status 500 because of
malformed, out-of-range, or unexpected request input". Service 1's equivalent suite found that
this is the clause a boundary quietly violates — a value reaching an unguarded ``float()`` or
an unchecked index turns bad input into a 500 — so the matrix drives nan, infinity, NUL bytes,
SQL-shaped and traversal-shaped strings, oversize numbers and wrong types at every parameter.

REQUIREMENT 19.8 SITS BESIDE REQUIREMENT 25.11's DELIBERATE 500, which is the one 500 the spec
REQUIRES. They do not collide because their subjects differ: 19.8 governs bad INPUT, 25.11
governs
the service's own OUTPUT failing its own guardrail check. The forbidden-phrase test below is the
only place a 500 is asserted, and it asserts the offending body was WITHHELD.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any, cast

import httpx
import pytest
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
from aqm_ingestion.ports.protocols import VerifiedIdentity
from aqm_ingestion.serving.app import ServingSettings, build_app
from aqm_ingestion.serving.assembler import ResponseAssembler
from aqm_ingestion.serving.enrichment import Enricher
from aqm_ingestion.serving.geo import GeoSelector
from aqm_ingestion.serving.guardrails import GuardrailSettings
from aqm_ingestion.serving.profiles import ProfileService
from tests.unit.test_records import GOLDEN_METADATA_PAYLOAD

_NOW = dt.datetime(2026, 9, 8, 12, 0, 0, tzinfo=dt.UTC)
_TOKEN = "dev-token"
_USER = "u_123"
_LONDON = (51.507, -0.128)
_CONSENT = next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS)))
_SITE = "CB0086"

# Requirement 19.8's matrix. Each is a value that has, in some codebase, turned bad input into a
# 500 — a non-finite float, a numeric string too large for a double, and the two shapes a naive
# handler interpolates into a query or a path.
#
# A NUL byte is deliberately NOT here: httpx refuses to build a URL containing one, so it is not
# a reachable server input at all and a test for it would only assert that the client validates.
# It IS exercised in the profile-body matrix below, where it can actually be delivered.
_HOSTILE = (
    "nan",
    "inf",
    "-inf",
    "1e999",
    "' OR 1=1 --",
    "../../etc/passwd",
    "%2e%2e%2f",
    "9" * 400,
    "{}",
    "[]",
    "null",
    "true",
    "2026-13-45T99:99:99Z",
    # Naive: parseable as ISO-8601 but with no zone, which must be REFUSED rather than assumed
    # UTC — the same rule the Clock port applies, since it would silently shift the window.
    "2026-09-08T12:00:00",
)


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


def _reading(species: str = "PM25") -> CalibratedReading:
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
        sub_index=68,
        band="Moderate",
        method="nowcast" if species == "PM25" else "hourly",
        humidity_source="provider" if species == "PM25" else "none",
        nowcast_window_hours=12 if species == "PM25" else None,
        nowcast_hours_available=12 if species == "PM25" else None,
        nowcast_weight_factor=0.72 if species == "PM25" else None,
    )


def _app(
    *,
    settings: ServingSettings | None = None,
    guardrails: GuardrailSettings | None = None,
    with_profile: bool = True,
) -> FastAPI:
    clock = AdvanceableClock(_NOW)
    profiles_store = InMemoryProfileStore()
    audit = InMemoryAuditStore()
    registry = InMemorySensorRegistryStore()
    readings = InMemoryReadingsStore(clock=clock)
    registry.upsert(_metadata(_SITE, *_LONDON), at=_NOW)
    readings.put(_reading())

    profile_service = ProfileService(profiles=profiles_store, audit=audit)
    if with_profile:
        profile_service.write(
            VerifiedIdentity(user_id=_USER),
            {
                "user_id": _USER,
                "condition": "asthma",
                "sensitivity_level": "high",
                "locations": [
                    {"name": "home", "latitude": _LONDON[0], "longitude": _LONDON[1]}
                ],
                "consent": {"version": _CONSENT, "given_at": _NOW},
                "created_at": _NOW,
                "updated_at": _NOW,
            },
        )

    from aqm_ingestion.serving.assembler import AssemblySettings

    return build_app(
        authenticator=LocalAuthenticator({_TOKEN: _USER}),
        clock=clock,
        assembler=ResponseAssembler(
            profiles=profile_service,
            selector=GeoSelector(registry=registry, readings=readings, clock=clock),
            enricher=Enricher(client=InMemoryForecastClient(), clock=clock),
            breakpoints=BreakpointTableRegistry.with_defaults(),
            clock=clock,
            settings=AssemblySettings(guardrails=guardrails),
        ),
        profiles=profile_service,
        readings=readings,
        registry=registry,
        audit=audit,
        guardrails=guardrails,
        settings=settings or ServingSettings(),
    )


def _request(
    app: FastAPI,
    path: str,
    *,
    method: str = "GET",
    authorized: bool = True,
    body: object = None,
) -> httpx.Response:
    async def _run() -> httpx.Response:
        headers = {"Authorization": f"Bearer {_TOKEN}"} if authorized else {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, path, headers=headers, json=body)

    return asyncio.run(_run())


def _window(start: str | None = None, end: str | None = None) -> str:
    parts = [f"siteCode={_SITE}"]
    if start is not None:
        parts.append(f"startTime={start}")
    if end is not None:
        parts.append(f"endTime={end}")
    return "/v1/air-quality/history?" + "&".join(parts)


# --- 25.8: one test per documented status ------------------------------

def test_an_absent_authorization_header_is_401() -> None:
    response = _request(_app(), "/v1/air-quality/me", authorized=False)
    assert response.status_code == 401
    assert "Authorization" in response.json()["detail"]


def test_a_malformed_credential_is_401() -> None:
    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            return await client.get(
                "/v1/air-quality/me", headers={"Authorization": "Bearer not-a-token"}
            )

    response = asyncio.run(_run())
    assert response.status_code == 401
    assert response.json()["category"]


def test_a_cross_identity_request_is_403() -> None:
    response = _request(_app(), "/v1/air-quality/me?userId=someone-else")
    assert response.status_code == 403
    assert "scope" in response.json()["detail"].lower()


def test_start_after_end_is_400_naming_the_parameter() -> None:
    response = _request(
        _app(), _window("2026-09-08T12:00:00Z", "2026-09-08T06:00:00Z")
    )
    assert response.status_code == 400
    assert any("startTime" in problem for problem in response.json()["problems"])


def test_an_unparseable_instant_is_400_naming_the_parameter() -> None:
    response = _request(_app(), _window("not-an-instant", "2026-09-08T12:00:00Z"))
    assert response.status_code == 400
    problems = response.json()["problems"]
    assert any("startTime" in problem for problem in problems)
    assert any("ISO-8601" in problem for problem in problems)


def test_one_bound_without_the_other_is_400_naming_the_absent_one() -> None:
    # THE GAP THIS CYCLE FIXED. Req 19.6 lists this case and requires a 400 naming the
    # parameter; with the bounds declared as REQUIRED FastAPI answered 422 with its own body
    # before any handler ran, so the requirement was unreachable. The bounds are now optional
    # and
    # the pairing is validated in parse_history_window.
    response = _request(_app(), _window(start="2026-09-08T06:00:00Z"))
    assert response.status_code == 400
    assert any("endTime" in problem for problem in response.json()["problems"])


def test_the_other_bound_alone_is_also_400() -> None:
    response = _request(_app(), _window(end="2026-09-08T12:00:00Z"))
    assert response.status_code == 400
    assert any("startTime" in problem for problem in response.json()["problems"])


def test_neither_bound_is_accepted_as_the_default_window() -> None:
    # Req 19.6 names "one of them WITHOUT THE OTHER" as the fault, which implies neither is not
    # one — so this is the default window, not a rejection.
    response = _request(_app(), f"/v1/air-quality/history?siteCode={_SITE}")
    assert response.status_code == 200
    assert response.json()["startTime"].endswith("Z")


def test_a_bad_species_is_400_naming_the_permitted_values() -> None:
    response = _request(
        _app(),
        _window("2026-09-08T06:00:00Z", "2026-09-08T12:00:00Z") + "&species=O3",
    )
    assert response.status_code == 400
    problems = response.json()["problems"]
    assert any("species" in problem for problem in problems)
    assert any("PM25" in problem for problem in problems)


def test_an_oversize_span_is_400_naming_the_span_and_the_maximum() -> None:
    response = _request(
        _app(), _window("2020-01-01T00:00:00Z", "2026-09-08T12:00:00Z")
    )
    assert response.status_code == 400
    problems = " ".join(response.json()["problems"])
    assert "30 days" in problems
    assert "requested" in problems


def test_an_unknown_site_is_404_naming_the_site_code() -> None:
    response = _request(
        _app(),
        "/v1/air-quality/history?siteCode=NOPE"
        "&startTime=2026-09-08T06:00:00Z&endTime=2026-09-08T12:00:00Z",
    )
    assert response.status_code == 404
    assert "NOPE" in response.json()["detail"]


def test_the_rate_limit_is_429_naming_the_limit() -> None:
    app = _app(settings=ServingSettings(rate_limit_per_minute=1))

    async def _run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"Authorization": f"Bearer {_TOKEN}"}
            await client.get("/v1/air-quality/me", headers=headers)
            return await client.get("/v1/air-quality/me", headers=headers)

    response = asyncio.run(_run())
    assert response.status_code == 429
    assert response.json()["limit"] == 1
    assert response.json()["retryAfterSeconds"] == 60


def test_a_forbidden_phrase_response_is_500_with_the_body_withheld() -> None:
    # Req 25.11's DELIBERATE 500, the only one the spec requires. Forced by configuring a
    # pattern
    # that matches ordinary output, which is the honest way to reach it — the shipped patterns
    # never fire on a legitimate response, as Property 37 asserts.
    app = _app(guardrails=GuardrailSettings(forbidden_patterns=("Moderate",)))
    response = _request(app, "/v1/air-quality/me")
    assert response.status_code == 500
    body = response.json()
    # The offending body did NOT travel: no measurement, no band, no site code.
    assert "Moderate" not in response.text
    assert _SITE not in response.text
    assert "withheld" in body["detail"]


def test_the_forbidden_phrase_failure_logs_one_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from aqm_ingestion.observability.logging import configure_logging

    configure_logging("info")
    app = _app(guardrails=GuardrailSettings(forbidden_patterns=("Moderate",)))
    _request(app, "/v1/air-quality/me")
    errors = [
        json.loads(line)
        for line in capsys.readouterr().out.strip().splitlines()
        if line and json.loads(line).get("level") == "error"
    ]
    assert len(errors) == 1
    assert errors[0]["event"] == "guardrail_violation"


# --- 25.5 / Req 19.8: never 500 on input ------------------------------

@pytest.mark.parametrize("value", _HOSTILE)
def test_a_hostile_start_time_never_500s(value: str) -> None:
    response = _request(_app(), _window(value, "2026-09-08T12:00:00Z"))
    assert response.status_code < 500, f"{value!r} produced {response.status_code}"


@pytest.mark.parametrize("value", _HOSTILE)
def test_a_hostile_end_time_never_500s(value: str) -> None:
    response = _request(_app(), _window("2026-09-08T06:00:00Z", value))
    assert response.status_code < 500, f"{value!r} produced {response.status_code}"


@pytest.mark.parametrize("value", _HOSTILE)
def test_a_hostile_species_never_500s(value: str) -> None:
    response = _request(
        _app(),
        _window("2026-09-08T06:00:00Z", "2026-09-08T12:00:00Z") + f"&species={value}",
    )
    assert response.status_code < 500, f"{value!r} produced {response.status_code}"


@pytest.mark.parametrize("value", _HOSTILE)
def test_a_hostile_site_code_never_500s(value: str) -> None:
    response = _request(
        _app(),
        f"/v1/air-quality/history?siteCode={value}"
        "&startTime=2026-09-08T06:00:00Z&endTime=2026-09-08T12:00:00Z",
    )
    assert response.status_code < 500, f"{value!r} produced {response.status_code}"


@pytest.mark.parametrize("value", _HOSTILE)
def test_a_hostile_user_id_never_500s(value: str) -> None:
    response = _request(_app(), f"/v1/air-quality/me?userId={value}")
    assert response.status_code < 500, f"{value!r} produced {response.status_code}"


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "a string",
        42,
        {"condition": "nonsense"},
        {"condition": "asthma"},
        {"personal_thresholds": {"PM25": {"kind": "sub_index", "value": "nan"}}},
        {"activity_duration_hours": "1e999"},
        {"locations": [{"name": "home", "latitude": "nan", "longitude": 0}]},
        {"user_id": "someone-else"},
        # A NUL byte, which IS deliverable in a JSON body even though a URL refuses it.
        {"\x00": "\x00"},
        {"condition": "\x00"},
    ],
)
def test_a_hostile_profile_body_never_500s(body: object) -> None:
    # Req 19.8 covers a request BODY as much as a parameter, and the profile write is the only
    # route that takes one.
    response = _request(_app(), "/v1/profile/me", method="PUT", body=body)
    assert response.status_code < 500, f"{body!r} produced {response.status_code}"


@pytest.mark.parametrize(
    "body",
    [
        {"condition": "nonsense"},
        {"personal_thresholds": {"PM25": {"kind": "sub_index", "value": 9999}}},
        {"medication": "salbutamol"},
    ],
)
def test_a_rejected_profile_body_echoes_no_submitted_value(body: object) -> None:
    # §7 and Req 17.9 at the boundary: a rejection names the FIELD, never the value.
    response = _request(_app(), "/v1/profile/me", method="PUT", body=body)
    assert response.status_code == 400
    for value in ("nonsense", "9999", "salbutamol"):
        assert value not in response.text


def test_an_unknown_route_is_404_not_500() -> None:
    assert _request(_app(), "/v1/nope").status_code == 404


def test_a_wrong_method_is_405_not_500() -> None:
    assert _request(_app(), "/v1/air-quality/me", method="DELETE").status_code == 405


def test_no_rejection_body_carries_a_reading_or_profile_value() -> None:
    # Req 18.9 and 19.6 both forbid it. Checks every rejection shape in one place, over the
    # whole
    # response text rather than a named field.
    app = _app()
    for path, authorized in (
        ("/v1/air-quality/me", False),
        ("/v1/air-quality/me?userId=other", True),
        (_window("bad", "worse"), True),
        ("/v1/air-quality/history?siteCode=NOPE&startTime=2026-09-08T06:00:00Z"
         "&endTime=2026-09-08T12:00:00Z", True),
    ):
        response = _request(app, path, authorized=authorized)
        assert response.status_code >= 400
        for leaked in ("18.2", "24.1", "asthma", "Moderate", _TOKEN):
            assert leaked not in response.text, f"{path} leaked {leaked!r}"
