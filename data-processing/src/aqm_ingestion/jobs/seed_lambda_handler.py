"""AWS Lambda entrypoint for the scheduled demo-data refresh (Feature: demo-data-refresh).

This is the handler `demo_data_refresh_stack.py` names
(`aqm_ingestion.jobs.seed_lambda_handler.handler`); an EventBridge schedule invokes
it every couple of hours so a CURRENT demo reading always exists during the demo
window.

IT ADDS NO LOGIC. The seeding — the catalogue, the idempotent upserts, the daily
history plus the recent hourly tail stamped at the current hour — all lives in
`jobs/seed_readings.py`, which is tested on its own. This module only adapts the
Lambda calling convention to that job, and enforces the one thing a scheduled
boundary must: it NEVER lets an exception escape.

WHY THE BOUNDARY SWALLOWS EVERYTHING (§5). A scheduled Lambda that throws is
retried wholesale. `seed_readings` is idempotent, so a retry is harmless, but a
handled fault should still be logged and reported rather than thrown — and the
NEXT scheduled run self-heals any gap, which (because the cadence is inside the
freshness window) cannot grow large enough to read as "unavailable".

THE SCHEDULED EVENT IS IGNORED. A whole-catalogue seed takes no input from the
trigger, so `event` and `context` are accepted and unused. An operator re-running
by hand does it through the CLI (`python -m aqm_ingestion.jobs.seed_readings`) or
the `just deploy-demo-refresh` recipe, not by shaping an event.
"""

from __future__ import annotations

from typing import Any

from aqm_ingestion.observability.logging import get_logger

_logger = get_logger(__name__)


def handler(
    event: Any,  # noqa: ANN401 - the Lambda event shape is the platform's, not ours
    context: Any = None,  # noqa: ANN401 - the Lambda context, unused
    *,
    runtime: Any = None,  # noqa: ANN401 - an injected Runtime in tests; None in production
) -> dict[str, Any]:
    """Run one idempotent seeding pass. Never raises out of the boundary.

    Args:
        event: the scheduled-invoke event. Ignored — a whole-catalogue seed takes no
            input from the trigger.
        context: the Lambda context. Ignored.
        runtime: an already-built ``Runtime``, injected by tests so the seed runs
            against in-memory adapters with no AWS. In production it is None and the
            job loads the runtime from the environment via ``seed_readings.main``.

    Returns:
        A small JSON-safe status: ``{"ok": True}`` on a completed pass, or
        ``{"ok": False, "error": "<kind>"}`` when it could not run. The error is a
        KIND, never a stack trace or a store body.
    """
    del event, context
    try:
        from aqm_ingestion.jobs import seed_readings

        if runtime is not None:
            # Test path: seed through the injected in-memory runtime, at its clock.
            seed_readings.run(runtime, seed_readings._DEFAULT_SEED)
            return {"ok": True}

        # Production path: main resolves config, builds the runtime with a
        # SystemClock, runs one pass, and returns 0 on success / non-zero (logged)
        # on a startup fault.
        code = seed_readings.main(argv=())
        return {"ok": code == 0} if code == 0 else {"ok": False, "error": "startup"}
    except Exception as error:
        # The one broad catch a scheduled boundary is allowed (§5): log the TYPE,
        # never the message (which could quote a store body), and return rather than
        # throw, so the scheduler does not retry the whole pass to fix one fault.
        _logger.error("demo_data_refresh_failed", error_type=type(error).__name__)
        return {"ok": False, "error": type(error).__name__}


__all__ = ["handler"]
