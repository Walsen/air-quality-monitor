"""Property 16: the health endpoint never gets wedged. Validates Reqs 32.4, 32.4a, 32.4b.

**WHAT THIS PROPERTY ASSERTS, AND WHAT IT DELIBERATELY DOES NOT.** "Ping stays live while a turn
is in
flight" has two halves. The half that needs real concurrency — that `GET /ping` answers while
`/invocations` is executing — is covered by a concrete example in
`tests/unit/test_agentcore_app.py`, driven through a real ASGI client with the turn gated on an
event.

The half that generalises is the one here: for ANY interleaving of turns starting and finishing,
the
reported status is busy exactly while work is outstanding, and always returns to `Healthy` once
the
work is balanced. That is what "stays live" means operationally — a session wedged permanently
`HealthyBusy` is the failure Req 32.4 warns about, because the idle timeout then never fires and
sessions persist to `MaxLifetime`, which can exhaust the account's session quota.

**SYNCHRONOUS ON PURPOSE.** All 99 other `@given` uses in this suite are sync, and none of the
property tests are async. Combining hypothesis with an event loop and an ASGI transport is where
hanging tests come from — this task already lost ninety minutes to one hang — and the
concurrency that
would justify the risk is already pinned by the example test. So this drives the SDK's own task
bookkeeping directly, which is the state `/ping` reads.
"""

from __future__ import annotations

import datetime as dt

import pytest
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from hypothesis import given
from hypothesis import strategies as st

from aqm_advisor.agentcore.app import build_app
from aqm_advisor.domain.models import AdvisoryResponse, GuardrailEnvelope
from aqm_advisor.ports.clock import FixedClock

pytestmark = pytest.mark.property

_AT = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
_EMERGENCY = "If you are struggling to breathe, call 999."

_HEALTHY = "Healthy"
_BUSY = "HealthyBusy"


def _app() -> BedrockAgentCoreApp:
    return build_app(
        run_turn=lambda _request: AdvisoryResponse(
            escalation=None,
            guidance="ok",
            basis=None,
            envelope=GuardrailEnvelope(emergency_guidance=_EMERGENCY),
            degraded=False,
            answered_at=_AT,
        ),
        emergency_guidance=_EMERGENCY,
        turn_budget_seconds=30,
        clock=FixedClock(_AT),
    )


def _status(app: BedrockAgentCoreApp) -> str:
    return str(app.get_current_ping_status().value)


@given(starts=st.integers(min_value=1, max_value=12))
def test_any_number_of_outstanding_turns_reports_busy(starts: int) -> None:
    # One outstanding turn or twelve, the answer is the same: busy. A status that depended on
    # the
    # COUNT rather than on emptiness would report something else here.
    app = _app()
    ids = [app.add_async_task("advisory_turn") for _ in range(starts)]
    try:
        assert _status(app) == _BUSY
    finally:
        for task_id in ids:
            app.complete_async_task(task_id)


@given(
    total=st.integers(min_value=1, max_value=12),
    completed=st.integers(min_value=0, max_value=12),
)
def test_status_is_busy_exactly_while_work_is_outstanding(
    total: int, completed: int
) -> None:
    # THE property. Busy iff something is outstanding — asserted as an equivalence, in both
    # directions, so neither a permanently-busy nor a never-busy implementation satisfies it.
    app = _app()
    ids = [app.add_async_task("advisory_turn") for _ in range(total)]
    done = min(completed, total)
    for task_id in ids[:done]:
        app.complete_async_task(task_id)
    outstanding = total - done
    try:
        assert (_status(app) == _BUSY) is (outstanding > 0)
        assert (_status(app) == _HEALTHY) is (outstanding == 0)
    finally:
        for task_id in ids[done:]:
            app.complete_async_task(task_id)


@given(rounds=st.integers(min_value=1, max_value=8))
def test_balanced_turns_always_return_the_session_to_healthy(rounds: int) -> None:
    # No leak across repetitions. This is the assertion that a wedged session would fail: the
    # entrypoint completes its task in a `finally`, so however many turns run, the session ends
    # idle
    # and the platform's idle timeout can still fire.
    app = _app()
    for _ in range(rounds):
        task_id = app.add_async_task("advisory_turn")
        assert _status(app) == _BUSY
        app.complete_async_task(task_id)
        assert _status(app) == _HEALTHY
    assert _status(app) == _HEALTHY


@given(rounds=st.integers(min_value=1, max_value=8))
def test_the_timestamp_moves_only_when_the_status_changes(rounds: int) -> None:
    # Req 32.4's other half, generalised: reading the status repeatedly without changing it must
    # not
    # advance `time_of_last_update`. A timestamp that always moved would signal a continuous
    # status
    # change and stop the idle timeout from ever firing.
    app = _app()
    for _ in range(rounds):
        _status(app)
    idle_stamp = app._last_status_update_time
    for _ in range(rounds):
        _status(app)
    assert app._last_status_update_time == idle_stamp

    task_id = app.add_async_task("advisory_turn")
    _status(app)
    assert app._last_status_update_time > idle_stamp, (
        "a real status change must advance the timestamp, or the platform never learns of it"
    )
    app.complete_async_task(task_id)
