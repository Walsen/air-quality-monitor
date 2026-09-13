"""Tests for the association job's AWS Lambda handler (Feature: personal-diary-memory, Task 9).

The handler is the thin adapter an EventBridge SCHEDULED invoke calls. It owns no
derivation logic — that is `jobs/entrypoint.py` and `jobs/association.py`, already
tested — so these tests pin only the handler's contract:

* it runs a derivation cycle for the users WITH diaries and reports a summary;
* a per-user failure is isolated (the cycle still completes) — inherited from
  `run_for_all`, asserted here end to end through the handler;
* it never raises out of the Lambda boundary (§5: a handled failure is logged and
  returned, not thrown), and it never logs health detail (§6, Req 5.4).

Everything runs against the in-memory adapters a default config builds, so the
suite needs no AWS. The scheduled event payload is irrelevant to a whole-cycle
derivation, so the handler ignores it.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import cast

import pytest

from aqm_ingestion.composition import Runtime, build_runtime
from aqm_ingestion.config.loader import resolve_and_validate
from aqm_ingestion.domain.profile import UserProfile
from aqm_ingestion.domain.symptoms import build_symptom_entry
from aqm_ingestion.jobs import lambda_handler as lh
from aqm_ingestion.ports.clock import FixedClock

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _runtime() -> Runtime:
    config = resolve_and_validate(env={"AQM_LOG_LEVEL": "info"}, file_data={})
    return build_runtime(config, FixedClock(_NOW))


def _seed_diary(runtime: Runtime, user_id: str, days: int = 20) -> None:
    store = runtime.ports["symptom_log_store"]
    for offset in range(days):
        store.put(  # type: ignore[attr-defined]
            build_symptom_entry(
                {
                    "user_id": user_id,
                    "entry_date": _NOW.date() - dt.timedelta(days=offset),
                    "severity": 1 + (offset % 5),
                    "markers": ["cough"],
                    "reliever_used": False,
                },
                now=_NOW,
            )
        )


def test_the_handler_runs_a_cycle_and_returns_a_summary() -> None:
    # The happy path: with a seeded diary the handler runs the cycle and reports
    # how many users it processed, without raising.
    runtime = _runtime()
    _seed_diary(runtime, "user-1")
    result = lh.handler({"source": "aws.events"}, None, runtime=runtime)
    assert result["ok"] is True
    assert result["users"] >= 1


def test_the_handler_is_a_noop_when_nobody_has_a_diary() -> None:
    # "Ran and found nobody" is the normal early state, not a failure.
    runtime = _runtime()
    result = lh.handler({"source": "aws.events"}, None, runtime=runtime)
    assert result["ok"] is True
    assert result["users"] == 0


def test_a_per_user_failure_does_not_break_the_cycle() -> None:
    # Error isolation, end to end through the handler: one user's derivation
    # raising must not stop the others (Property 4 additive / error isolation).
    runtime = _runtime()
    _seed_diary(runtime, "good-user")
    _seed_diary(runtime, "bad-user")

    real_get = runtime.ports["profile_store"].get  # type: ignore[attr-defined]

    def _explode(user_id: str) -> UserProfile | None:
        if user_id == "bad-user":
            raise RuntimeError("boom deriving bad-user")
        return cast("UserProfile | None", real_get(user_id))

    runtime.ports["profile_store"].get = _explode  # type: ignore[attr-defined]
    result = lh.handler({}, None, runtime=runtime)
    assert result["ok"] is True
    # Both users are attempted; the cycle completes rather than aborting.
    assert result["users"] == 2


def test_the_handler_never_raises_out_of_the_boundary() -> None:
    # §5 top-level boundary: even a startup-shaped failure is caught, logged, and
    # returned as ok=False — never thrown, which a scheduler would retry wholesale.
    class _Boom:
        @property
        def ports(self) -> object:
            raise RuntimeError("runtime unusable")

    result = lh.handler({}, None, runtime=_Boom())
    assert result["ok"] is False
    assert "error" in result


def test_the_handler_logs_no_health_detail(caplog: pytest.LogCaptureFixture) -> None:
    # Req 5.4 / §6: a diary write or derivation log names only the pseudonymous id
    # and counts — never a severity, marker, or note. Seed a distinctive marker and
    # assert it never appears in any log record the cycle emits.
    runtime = _runtime()
    store = runtime.ports["symptom_log_store"]
    store.put(  # type: ignore[attr-defined]
        build_symptom_entry(
            {
                "user_id": "user-1",
                "entry_date": _NOW.date(),
                "severity": 5,
                "markers": ["wheeze"],
                "reliever_used": True,
            },
            now=_NOW,
        )
    )
    with caplog.at_level(logging.DEBUG):
        lh.handler({}, None, runtime=runtime)
    # Health detail — the marker and the severity — must never reach a log.
    assert "wheeze" not in caplog.text
    assert "severity" not in caplog.text.lower()
