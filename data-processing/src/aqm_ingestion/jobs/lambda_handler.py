"""AWS Lambda entrypoint for the scheduled association derivation (Task 9).

Feature: personal-diary-memory. This is the handler `association_stack.py` names
(`aqm_ingestion.jobs.lambda_handler.handler`); an EventBridge schedule invokes it.

IT ADDS NO LOGIC. The derivation, the user enumeration, the per-user error
isolation and the idempotency all live in `jobs/entrypoint.py` and
`jobs/association.py`, which are tested on their own. This module only adapts the
Lambda calling convention to `entrypoint.run`, and enforces the one thing a
scheduled boundary must: it NEVER lets an exception escape.

WHY THE BOUNDARY SWALLOWS EVERYTHING (§5). A scheduled Lambda that throws is
retried wholesale, so one odd user or a transient store error would re-derive
everyone repeatedly. `entrypoint.run` already isolates a per-user failure via
`run_for_all`; this handler adds the outer catch for a failure BEFORE the cycle
starts (a runtime that would not build, a store unreachable at cold start). Both
paths log the fault and return a summary dict rather than raising, and neither
logs any health detail — only the pseudonymous identity and counts (§6, Req 5.4),
which is a property of the loggers the cycle uses, not of this module adding one.

THE SCHEDULED EVENT IS IGNORED. A whole-cycle derivation takes no input from the
trigger — it derives for every user with a diary — so the `event` and `context`
are accepted and unused. An operator re-running one user does it through the CLI
(`python -m aqm_ingestion.jobs.entrypoint <user_id>`), not by shaping an event.
"""

from __future__ import annotations

from typing import Any

from aqm_ingestion.jobs.entrypoint import run
from aqm_ingestion.observability.logging import configure_logging, get_logger

_logger = get_logger(__name__)


def handler(
    event: Any,  # noqa: ANN401 - the Lambda event shape is the platform's, not ours
    context: Any = None,  # noqa: ANN401 - the Lambda context, unused
    *,
    runtime: Any = None,  # noqa: ANN401 - an injected Runtime in tests; None in production
) -> dict[str, Any]:
    """Run one association-derivation cycle. Never raises out of the boundary.

    Args:
        event: the scheduled-invoke event. Ignored — a whole-cycle derivation takes
            no input from the trigger.
        context: the Lambda context. Ignored.
        runtime: an already-built ``Runtime``, injected by tests so the cycle runs
            against in-memory adapters with no AWS. In production it is None and the
            runtime is loaded from the environment.

    Returns:
        A small JSON-safe summary: ``{"ok": True, "users": <count>}`` on a
        completed cycle (including a cycle that found nobody), or
        ``{"ok": False, "error": "<kind>"}`` when the cycle could not start. The
        error is a KIND, never a stack trace or a store body.
    """
    del event, context
    try:
        if runtime is None:
            import os

            from aqm_ingestion.composition import load_runtime
            from aqm_ingestion.ports.clock import SystemClock

            configure_logging(os.environ.get("AQM_LOG_LEVEL", "info"))
            runtime = load_runtime(dict(os.environ), SystemClock())

        symptoms = runtime.ports["symptom_log_store"]
        users = tuple(symptoms.user_ids_with_entries())
        run(runtime, users or None)
        return {"ok": True, "users": len(users)}
    except Exception as error:
        # The one broad catch a scheduled boundary is allowed (§5): log the TYPE,
        # never the message (which could quote a store body), and return rather than
        # throw, so the scheduler does not retry the whole cycle to fix one fault.
        # The repo's structured _EventLogger has no .exception; log the TYPE via .error,
        # consistent with how handled failures are logged elsewhere (never the message).
        _logger.error("association_cycle_failed", error_type=type(error).__name__)
        return {"ok": False, "error": type(error).__name__}


__all__ = ["handler"]
