"""Tests for the AgentCore deployment boundary (task 17). Reqs 21.5, 32.1-32.8a, 33.8, 33.9.

Driven through `httpx.ASGITransport`, because `BedrockAgentCoreApp` IS a Starlette ASGI app.
That is
the pattern Service 2 already uses, and it was chosen there for a reason worth repeating:
Starlette's
own `TestClient` needs an httpx-adjacent package this project does not pin. No socket is bound,
no
port is chosen, nothing reaches AWS — so this whole file lives in the offline suite Req 26.5a
requires.

**THE POINT OF Req 32.4b IS A TRAP THE SDK SETS, AND THESE TESTS SPRING IT.**
`_handle_invocation`
never touches `_active_tasks`, and `get_current_ping_status` returns `HealthyBusy` only when
that set
is non-empty. So an entrypoint that merely runs a turn reports `Healthy` throughout — the health
contract looks implemented and is not. The turn must therefore register an async task, which is
what
Reqs 32.4 and 33.8 already say to do, and what the concurrency test below proves it does.

**THE SDK'S OWN ERROR PATH IS WHAT Req 32.5 FORBIDS.** Read from the installed source: the SDK
catches an entrypoint exception and answers `JSONResponse({"error": str(e)}, status_code=500)`.
AgentCore surfaces a container 5xx to the caller as an opaque `424 RuntimeClientError`, which
replaces a documented degraded answer and loses the Guardrail_Envelope and any Escalation with
it —
and `str(e)` puts the exception's own words in the body, which Reqs 5.2 and 21.4 forbid. So this
service's broad catch has to fire FIRST, and a test below asserts the SDK's never runs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import threading

import httpx
import pytest
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from aqm_advisor.agentcore.app import build_app
from aqm_advisor.domain.models import AdvisoryRequest, AdvisoryResponse, GuardrailEnvelope
from aqm_advisor.ports.clock import FixedClock

_AT = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
_EMERGENCY = "If you are struggling to breathe, call 999."
_CREDENTIAL = "SENTINEL-CRED-Q7X-do-not-log"


def _response(*, degraded: bool = False) -> AdvisoryResponse:
    return AdvisoryResponse(
        escalation=None,
        guidance="Conditions are moderate today.",
        basis=None,
        envelope=GuardrailEnvelope(emergency_guidance=_EMERGENCY),
        degraded=degraded,
        answered_at=_AT,
    )


def _payload(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"utterance": "how is the air today?"}
    body.update(overrides)
    return body


def _app(run_turn: object = None, **kwargs: object) -> BedrockAgentCoreApp:
    settings: dict[str, object] = {
        "run_turn": run_turn or (lambda _request: _response()),
        "emergency_guidance": _EMERGENCY,
        "turn_budget_seconds": 30,
        "clock": FixedClock(_AT),
    }
    settings.update(kwargs)
    return build_app(**settings)  # type: ignore[arg-type]


def _client(app: object) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://agentcore.test",
    )


# --- Req 32.1: the two routes exist -----------------------------------


async def test_ping_answers_healthy_when_idle() -> None:
    async with _client(_app()) as client:
        reply = await client.get("/ping")
    assert reply.status_code == 200
    assert reply.json()["status"] == "Healthy"


async def test_invocations_returns_the_advisory_response_body() -> None:
    # Req 32.3: the Advisory_Response IS the /invocations body, not a second wire shape.
    async with _client(_app()) as client:
        reply = await client.post("/invocations", json=_payload())
    assert reply.status_code == 200
    body = reply.json()
    assert body["guidance"] == "Conditions are moderate today."
    assert body["envelope"]["emergency_guidance"] == _EMERGENCY


# --- Req 32.4 / 32.4b: ping stays live AND reports HealthyBusy --------


async def test_ping_reports_healthybusy_while_a_turn_is_in_flight() -> None:
    # THE requirement that does not reproduce locally by default (Req 32.4b).
    #
    # This test is also what proves the entrypoint registers an async task at all: without
    # `add_async_task`, `_active_tasks` stays empty and /ping answers `Healthy` right through
    # the
    # turn — the SDK does NOT infer busy from an in-flight request.
    #
    # The turn blocks on a THREADING event, not an asyncio one, because `run_turn` executes on a
    # worker thread via `to_thread`. An earlier version put an `asyncio.Event` seam in the
    # production
    # builder for this; blocking inside the test's own runner needs no seam in shipped code.
    gate = threading.Event()
    seen: list[str] = []

    def blocking_turn(_request: AdvisoryRequest) -> AdvisoryResponse:
        gate.wait(timeout=10)  # bounded, so a broken test cannot hang the suite
        return _response()

    async with _client(_app(run_turn=blocking_turn)) as client:

        async def turn() -> httpx.Response:
            return await client.post("/invocations", json=_payload())

        async def probe() -> httpx.Response:
            reply = await client.get("/ping")
            for _ in range(200):
                reply = await client.get("/ping")
                seen.append(reply.json()["status"])
                if reply.json()["status"] == "HealthyBusy":
                    break
                await asyncio.sleep(0.005)
            gate.set()
            return reply

        turn_reply, ping_reply = await asyncio.gather(turn(), probe())

    assert turn_reply.status_code == 200
    assert ping_reply.status_code == 200, "ping did not stay responsive during the turn"
    assert "HealthyBusy" in seen, seen


async def test_ping_returns_to_healthy_after_the_turn() -> None:
    # The other half: a task that is never completed would pin the session busy forever, which
    # is
    # what Req 32.4's session-quota warning is about.
    app = _app()
    async with _client(app) as client:
        await client.post("/invocations", json=_payload())
        reply = await client.get("/ping")
    assert reply.json()["status"] == "Healthy"


async def test_the_ping_timestamp_does_not_advance_on_every_ping() -> None:
    # Req 32.4: a timestamp that always moves signals a continuous status change, which stops
    # the
    # idle session timeout from ever firing and can exhaust the account's session quota. The SDK
    # already guards this, and this service must not defeat it with a custom handler — which is
    # exactly why no `@app.ping` handler is registered.
    async with _client(_app()) as client:
        first = (await client.get("/ping")).json()["time_of_last_update"]
        await asyncio.sleep(0.02)
        second = (await client.get("/ping")).json()["time_of_last_update"]
    assert first == second


# --- Req 32.5 / 21.5: a failure is a response, never a container 5xx --


async def test_a_failing_turn_returns_200_with_a_degraded_response() -> None:
    # Req 32.5. If this service's catch did not fire first, the SDK's own handler would answer
    # 500,
    # AgentCore would surface an opaque 424 RuntimeClientError, and the Guardrail_Envelope and
    # any
    # Escalation would be lost with it.
    def boom(_request: AdvisoryRequest) -> AdvisoryResponse:
        raise RuntimeError("SENTINEL-ERR-Q7X the model exploded")

    async with _client(_app(run_turn=boom)) as client:
        reply = await client.post("/invocations", json=_payload())
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["degraded"] is True
    assert body["envelope"]["emergency_guidance"] == _EMERGENCY


async def test_a_failure_response_discloses_neither_the_message_nor_the_credential() -> None:
    # The SDK's error path puts `str(e)` in the body. Ours must not: an exception's words can
    # carry a
    # prompt, a partial generation or a provider's error body (Reqs 5.2, 21.4).
    def boom(_request: AdvisoryRequest) -> AdvisoryResponse:
        raise RuntimeError(f"SENTINEL-ERR-Q7X {_CREDENTIAL}")

    async with _client(_app(run_turn=boom)) as client:
        reply = await client.post("/invocations", json=_payload(credential=_CREDENTIAL))
    assert "SENTINEL-ERR-Q7X" not in reply.text
    assert _CREDENTIAL not in reply.text


async def test_a_malformed_request_is_a_response_not_a_validation_error() -> None:
    # A bad body must not become a container fault either. Req 1's validation belongs to the
    # model,
    # and the answer is still an Advisory_Response.
    async with _client(_app()) as client:
        reply = await client.post("/invocations", json={"not_a_field": 1})
    assert reply.status_code == 200, reply.text
    assert reply.json()["degraded"] is True


# --- Req 32.13 / 32.8a: the turn budget is enforced -------------------


@pytest.mark.parametrize("budget", [0, -1])
def test_a_non_positive_budget_is_refused_at_build_time(budget: int) -> None:
    # THE REGRESSION THAT COST NINETY MINUTES. The first version wrote
    # `asyncio.timeout(turn_budget_seconds or None)`, and `0 or None` is None — meaning NO
    # timeout.
    # A zero budget therefore DISABLED the Req 32.13 bound instead of expiring, and a test with
    # a
    # zero budget hung until a watchdog killed it. "No budget" is not a state Req 32.13 permits.
    with pytest.raises(ValueError, match="positive"):
        _app(turn_budget_seconds=budget)


async def test_a_turn_exceeding_its_budget_is_a_degraded_response() -> None:
    # Req 32.13 bounds an ordinary turn, and Req 32.8a needs that bound so a credential valid at
    # the
    # start is still valid at the last retrieval. `turn_budget_seconds` existed in configuration
    # and
    # was consumed NOWHERE before this entrypoint.
    #
    # Note what is asserted: the CALLER gets a bounded answer. The worker thread is not killed —
    # Python cannot kill a thread — so `release` exists to let it finish rather than leak.
    release = threading.Event()

    def overrunning_turn(_request: AdvisoryRequest) -> AdvisoryResponse:
        release.wait(timeout=10)
        return _response()

    try:
        async with _client(_app(run_turn=overrunning_turn, turn_budget_seconds=1)) as client:
            reply = await client.post("/invocations", json=_payload())
        assert reply.status_code == 200
        assert reply.json()["degraded"] is True
    finally:
        release.set()


# --- Req 32.8 / 5.6: the credential is forwarded, never inspected -----


async def test_the_inbound_authorization_header_reaches_the_turn() -> None:
    # Req 32.8: the credential arrives through the Runtime's request-header allowlist and is
    # forwarded unmodified. Captured here to prove it is read from the HEADER, not the body.
    seen: list[str] = []

    def capture(request: AdvisoryRequest) -> AdvisoryResponse:
        seen.append(request.credential.get_secret_value())
        return _response()

    async with _client(_app(run_turn=capture)) as client:
        await client.post(
            "/invocations",
            json=_payload(),
            headers={"Authorization": f"Bearer {_CREDENTIAL}"},
        )
    assert seen == [_CREDENTIAL], "the bearer credential did not reach the turn unmodified"


async def test_the_credential_is_never_echoed_in_a_successful_response() -> None:
    async with _client(_app()) as client:
        reply = await client.post(
            "/invocations",
            json=_payload(),
            headers={"Authorization": f"Bearer {_CREDENTIAL}"},
        )
    assert _CREDENTIAL not in reply.text


# --- the debug surface must stay shut --------------------------------


async def test_debug_actions_are_not_reachable() -> None:
    # `BedrockAgentCoreApp(debug=True)` exposes `_agent_core_app_action` on /invocations,
    # including
    # `force_healthy` and `force_busy`. With debug on, ANY caller could make the container lie
    # about
    # its health to the platform. Not in the requirements — found by reading the SDK — so it is
    # pinned here rather than left to the default staying put.
    async with _client(_app()) as client:
        reply = await client.post(
            "/invocations", json={"_agent_core_app_action": "force_healthy"}
        )
    assert "forced_status" not in reply.text, reply.text
