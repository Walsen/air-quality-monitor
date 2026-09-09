"""Unit tests for authentication and per-user scoping (task 23.1).

Requirements 18.1-18.3, 18.5-18.7, 18.9-18.11.

REQUIREMENT 18.4 IS WHY THE GATE IS MIDDLEWARE AND NOT A ROUTE DEPENDENCY, and the spec names
the failure mode itself: "authenticate before validating query parameters, so that an
unauthenticated request carrying a malformed parameter receives 401 rather than 400". FastAPI
validates a route's query model BEFORE its dependencies run, so a dependency-based gate answers
422 where the spec demands 401. Service 1 hit exactly this and the finding carries over.

REQUIREMENT 18.1 SAYS "identity AND CLAIM SET" WHILE THE PORT RETURNS ONLY AN IDENTITY, and that
is deliberate rather than a shortfall. Requirement 18.7 forbids any claim value beyond the user
identity reaching a response, a log, or an Audit_Record; the strongest way to honour that is for
the claim set never to leave the adapter that verified it. 18.1 describes what the Authenticator
RESOLVES; 18.6 requires the identity be derived from those verified claims, which the adapter
did.

Tests drive the ASGI app in-process through httpx.ASGITransport rather than starlette's
TestClient, which now needs a separate httpx2 package this project does not pin.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

import httpx
import pytest

from aqm_ingestion.adapters.memory import LocalAuthenticator
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.ports.clock import AdvanceableClock
from aqm_ingestion.ports.protocols import (
    Authenticator,
    AuthRejectedError,
    RejectionCategory,
    VerifiedIdentity,
)
from aqm_ingestion.serving.app import (
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    HEALTH_PATH,
    ServingSettings,
    build_app,
)

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_TOKEN = "dev-token-abc"
_USER = "user-a"


class _RejectingAuthenticator:
    """An authenticator that always refuses with a configured category."""

    def __init__(self, category: RejectionCategory) -> None:
        """Hold the category to refuse with."""
        self._category = category
        self.credentials_seen: list[str] = []

    def verify(self, credential: str) -> VerifiedIdentity:
        """Always refuse, recording what was offered."""
        self.credentials_seen.append(credential)
        raise AuthRejectedError(self._category)


def _client(
    authenticator: Authenticator | None = None,
    settings: ServingSettings | None = None,
) -> tuple[httpx.AsyncClient, AdvanceableClock]:
    clock = AdvanceableClock(_NOW)
    app = build_app(
        authenticator=authenticator or LocalAuthenticator({_TOKEN: _USER}),
        clock=clock,
        settings=settings or ServingSettings(),
    )
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test"), clock


def _get(
    path: str,
    headers: dict[str, str] | None = None,
    authenticator: Authenticator | None = None,
    settings: ServingSettings | None = None,
) -> httpx.Response:
    async def _run() -> httpx.Response:
        client, _clock = _client(authenticator, settings)
        async with client:
            return await client.get(path, headers=headers or {})

    return asyncio.run(_run())


def _authorized() -> dict[str, str]:
    return {"Authorization": f"Bearer {_TOKEN}"}


def _events(captured: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in captured.strip().splitlines() if line]


# --- Req 19.10: the health endpoint is the only exemption ---------------

def test_the_health_endpoint_needs_no_credential() -> None:
    response = _get(HEALTH_PATH)
    assert response.status_code == 200


def test_the_health_endpoint_reveals_no_data() -> None:
    # Req 18.9 applies here too: the one unauthenticated endpoint must carry nothing.
    body = _get(HEALTH_PATH).json()
    assert set(body) <= {"status", "checkedAt"}


def test_a_data_endpoint_needs_a_credential() -> None:
    assert _get("/v1/advice").status_code == 401


# --- Req 18.2: absent, malformed, or empty header ----------------------

def test_an_absent_authorization_header_is_401() -> None:
    response = _get("/v1/advice")
    assert response.status_code == 401
    assert "Authorization" in response.json()["detail"]


def test_a_non_bearer_scheme_is_401() -> None:
    response = _get("/v1/advice", {"Authorization": "Basic abc123"})
    assert response.status_code == 401
    assert "bearer" in response.json()["detail"].lower()


def test_an_empty_bearer_credential_is_401() -> None:
    response = _get("/v1/advice", {"Authorization": "Bearer "})
    assert response.status_code == 401


def test_a_bare_bearer_word_is_401() -> None:
    response = _get("/v1/advice", {"Authorization": "Bearer"})
    assert response.status_code == 401


def test_a_malformed_header_reaches_no_authenticator() -> None:
    # Req 18.2 requires NO store access; the same reasoning applies to the authenticator — a
    # malformed header is refused at the edge rather than handed onward.
    authenticator = _RejectingAuthenticator(RejectionCategory.INVALID_SIGNATURE)
    _get("/v1/advice", {"Authorization": "Basic abc"}, authenticator)
    assert authenticator.credentials_seen == []


def test_the_401_body_is_json() -> None:
    # §5: never a raw trace, always a JSON body.
    response = _get("/v1/advice")
    assert response.headers["content-type"].startswith("application/json")


# --- Req 18.3: the category, and nothing more --------------------------

def test_a_rejected_credential_reports_its_category() -> None:
    response = _get(
        "/v1/advice",
        _authorized(),
        _RejectingAuthenticator(RejectionCategory.EXPIRED),
    )
    assert response.status_code == 401
    assert response.json()["category"] == "expired"


def test_a_wrong_audience_credential_is_expressible() -> None:
    # Req 18.3 names a different audience as one of the three conditions, so the category set
    # has to be able to say it.
    response = _get(
        "/v1/advice",
        _authorized(),
        _RejectingAuthenticator(RejectionCategory.WRONG_AUDIENCE),
    )
    assert response.json()["category"] == "wrong_audience"


def test_the_401_body_discloses_nothing_beyond_the_category() -> None:
    # Req 18.3 forbids disclosing WHICH condition applied beyond the category, so the body must
    # not carry an authenticator message or exception text.
    response = _get(
        "/v1/advice",
        _authorized(),
        _RejectingAuthenticator(RejectionCategory.INVALID_SIGNATURE),
    )
    body = response.json()
    assert set(body) == {"detail", "category"}


def test_the_response_never_contains_the_credential() -> None:
    # Req 18.7. Checks the WHOLE serialised response, not one field, so a leak into any part
    # of the body or headers fails.
    response = _get(
        "/v1/advice",
        _authorized(),
        _RejectingAuthenticator(RejectionCategory.EXPIRED),
    )
    assert _TOKEN not in response.text
    assert _TOKEN not in json.dumps(dict(response.headers))


# --- Req 18.4: authentication precedes parameter validation ------------

def test_an_unauthenticated_request_with_a_bad_parameter_is_401_not_400() -> None:
    # The clause that dictates middleware over a route dependency. FastAPI validates the query
    # model BEFORE dependencies, so a dependency-based gate would answer 422 here.
    response = _get("/v1/advice?limit=not-a-number")
    assert response.status_code == 401


def test_the_bad_parameter_really_would_fail_validation() -> None:
    # Proves the race the test above depends on ACTUALLY EXISTS. Without this, `limit` could be
    # an undeclared parameter FastAPI silently ignores — there would be no validation to lose
    # to, and the 401 above would be satisfied by a dependency-based gate as well, which is the
    # arrangement Req 18.4 exists to forbid.
    response = _get("/v1/advice?limit=not-a-number", _authorized())
    assert response.status_code == 422


def test_a_valid_parameter_is_accepted_when_authenticated() -> None:
    response = _get("/v1/advice?limit=5", _authorized())
    assert response.status_code == 200
    assert response.json()["limit"] == 5


def test_an_authenticated_request_with_a_bad_parameter_is_not_401() -> None:
    # The counterpart: without it the test above would pass on a gate that rejects everything.
    response = _get("/v1/advice?limit=not-a-number", _authorized())
    assert response.status_code != 401


def test_an_authenticated_request_with_a_bad_parameter_is_not_500() -> None:
    # §5: bad input never produces a 500.
    response = _get("/v1/advice?limit=not-a-number", _authorized())
    assert response.status_code < 500


# --- Req 18.5, 18.6: per-user scoping ---------------------------------

def test_a_request_naming_another_identity_is_403() -> None:
    response = _get("/v1/advice?userId=someone-else", _authorized())
    assert response.status_code == 403
    assert "scope" in response.json()["detail"].lower()


def test_a_request_naming_the_callers_own_identity_is_allowed() -> None:
    # Redundant but harmless in the request, and refusing it would be wrong.
    response = _get(f"/v1/advice?userId={_USER}", _authorized())
    assert response.status_code == 200


def test_the_served_identity_comes_from_the_credential_not_the_parameter() -> None:
    # Req 18.6. The route echoes the identity it would use; a handler reading the parameter
    # would echo the other user even when the parameter matched nothing verified.
    body = _get("/v1/advice", _authorized()).json()
    assert body["userId"] == _USER


def test_a_403_carries_no_data() -> None:
    # Req 18.9: no Reading, profile, or site metadata in an out-of-scope response.
    body = _get("/v1/advice?userId=someone-else", _authorized()).json()
    assert set(body) == {"detail"}


def test_a_401_carries_no_data() -> None:
    body = _get("/v1/advice").json()
    assert "measurements" not in body
    assert "sites" not in body


# --- Req 18.10: one warning per rejection ----------------------------

def test_a_rejection_logs_exactly_one_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    _get("/v1/advice", _authorized(), _RejectingAuthenticator(RejectionCategory.EXPIRED))
    warnings = [
        event
        for event in _events(capsys.readouterr().out)
        if event.get("level") == "warning"
    ]
    assert len(warnings) == 1


def test_the_warning_names_the_route_and_category(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    _get("/v1/advice", _authorized(), _RejectingAuthenticator(RejectionCategory.EXPIRED))
    warning = next(
        event
        for event in _events(capsys.readouterr().out)
        if event.get("level") == "warning"
    )
    assert warning["route"] == "/v1/advice"
    assert warning["category"] == "expired"


def test_the_warning_carries_no_credential_material(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    _get("/v1/advice", _authorized(), _RejectingAuthenticator(RejectionCategory.EXPIRED))
    assert _TOKEN not in capsys.readouterr().out


def test_a_successful_request_logs_no_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Without this, "exactly one warning" could pass on a gate that warns unconditionally.
    configure_logging("info")
    _get("/v1/advice", _authorized())
    warnings = [
        event
        for event in _events(capsys.readouterr().out)
        if event.get("level") == "warning"
    ]
    assert warnings == []


# --- Req 18.11: the per-identity rate limit -------------------------

def test_the_default_rate_limit_is_sixty_per_minute() -> None:
    assert DEFAULT_RATE_LIMIT_PER_MINUTE == 60


def test_requests_within_the_limit_are_served() -> None:
    async def _run() -> list[int]:
        client, _clock = _client(settings=ServingSettings(rate_limit_per_minute=3))
        async with client:
            return [
                (await client.get("/v1/advice", headers=_authorized())).status_code
                for _ in range(3)
            ]

    assert asyncio.run(_run()) == [200, 200, 200]


def test_exceeding_the_limit_is_429() -> None:
    async def _run() -> int:
        client, _clock = _client(settings=ServingSettings(rate_limit_per_minute=2))
        async with client:
            for _ in range(2):
                await client.get("/v1/advice", headers=_authorized())
            return (await client.get("/v1/advice", headers=_authorized())).status_code

    assert asyncio.run(_run()) == 429


def test_the_429_names_the_limit_and_retry_interval() -> None:
    async def _run() -> dict[str, Any]:
        client, _clock = _client(settings=ServingSettings(rate_limit_per_minute=1))
        async with client:
            await client.get("/v1/advice", headers=_authorized())
            response = await client.get("/v1/advice", headers=_authorized())
            return dict(response.json())

    body = asyncio.run(_run())
    assert body["limit"] == 1
    assert body["retryAfterSeconds"] == 60


def test_the_limit_window_moves_with_the_clock() -> None:
    # §2: the window is measured from the injected Clock, so it is testable without sleeping.
    async def _run() -> int:
        client, clock = _client(settings=ServingSettings(rate_limit_per_minute=1))
        async with client:
            await client.get("/v1/advice", headers=_authorized())
            clock.advance(dt.timedelta(seconds=61))
            return (await client.get("/v1/advice", headers=_authorized())).status_code

    assert asyncio.run(_run()) == 200


def test_the_limit_is_per_identity() -> None:
    # One user exhausting their allowance must not lock out another.
    async def _run() -> int:
        clock = AdvanceableClock(_NOW)
        app = build_app(
            authenticator=LocalAuthenticator({_TOKEN: _USER, "token-b": "user-b"}),
            clock=clock,
            settings=ServingSettings(rate_limit_per_minute=1),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            await client.get("/v1/advice", headers=_authorized())
            return (
                await client.get(
                    "/v1/advice", headers={"Authorization": "Bearer token-b"}
                )
            ).status_code

    assert asyncio.run(_run()) == 200


def test_the_health_endpoint_is_not_rate_limited() -> None:
    # It is unauthenticated, so there is no identity to count against — and a liveness probe
    # that starts failing under load would take a healthy service out of rotation.
    async def _run() -> int:
        client, _clock = _client(settings=ServingSettings(rate_limit_per_minute=1))
        async with client:
            for _ in range(3):
                last = await client.get(HEALTH_PATH)
            return last.status_code

    assert asyncio.run(_run()) == 200


def test_a_rate_limited_response_carries_no_data() -> None:
    async def _run() -> dict[str, Any]:
        client, _clock = _client(settings=ServingSettings(rate_limit_per_minute=1))
        async with client:
            await client.get("/v1/advice", headers=_authorized())
            return dict((await client.get("/v1/advice", headers=_authorized())).json())

    assert set(asyncio.run(_run())) == {"detail", "limit", "retryAfterSeconds"}


# --- shape ----------------------------------------------------------

def test_a_non_positive_rate_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="rate_limit"):
        ServingSettings(rate_limit_per_minute=0)


def test_the_verified_identity_carries_only_a_user_id() -> None:
    # Req 18.7's structural half: no claim value beyond the identity can reach a log or a
    # response because none crosses the port. See the module docstring on Req 18.1.
    assert set(VerifiedIdentity.__dataclass_fields__) == {"user_id"}


def test_an_unexpected_handler_failure_is_not_a_raw_trace() -> None:
    # §5: never a raw stack trace to a client, even for an unhandled fault.
    response = _get("/v1/boom", _authorized())
    assert response.status_code in (404, 500)
    assert "Traceback" not in response.text
