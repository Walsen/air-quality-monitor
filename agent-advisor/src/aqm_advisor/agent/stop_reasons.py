"""Classifying every stop reason the SDK can return (Requirements 6.5a, 31.7).

`ALL_STOP_REASONS` is DERIVED from the SDK's own `StopReason` literal rather than written out.
That is the whole point of the exhaustiveness test: a hand-written list passes forever while the
SDK grows a thirteenth reason, whereas a derived one fails the moment an upgrade adds one.
Probing the installed SDK found TWELVE reasons, three of which — `cancelled`, `checkpoint`,
`interrupt` — the spec never anticipated, so the concern is not hypothetical.

**A guardrail stop is a rejection, not a failure** (Req 6.5a). The distinction is load-bearing
rather than tidy: a rejection means the guardrail did its job and the turn degrades to safe
wording, whereas a failure would be RETRIED — and retrying a blocked generation just blocks
again, while logging a safety success as an outage.

An unrecognised reason is read as a model failure. Failing closed matters most on exactly the
dimension the SDK might grow: a new reason must never be mistaken for success.
"""

from __future__ import annotations

import typing
from enum import Enum

from strands.types.event_loop import StopReason

ALL_STOP_REASONS: frozenset[str] = frozenset(typing.get_args(StopReason))
"""Every reason the installed SDK can return. Derived, so an SDK upgrade adding one fails.

Imported from `strands.types.event_loop`, which DEFINES it, rather than from
`strands.types.streaming`, which re-exports it without an `__all__` entry — strict mypy rejects
an implicit re-export, so the defining module is the only import path that typechecks.
"""


class TurnOutcome(Enum):
    """What a stop reason means for the turn.

    Deliberately coarser than the SDK's reasons: what this service does next depends on the
    CATEGORY, and a branch per reason would be twelve paths where five behaviours exist.
    """

    COMPLETED = "completed"
    NEEDS_CONTINUATION = "needs_continuation"
    GUARDRAIL_REJECTED = "guardrail_rejected"
    BOUND_REACHED = "bound_reached"
    MODEL_FAILED = "model_failed"
    """No SDK stop reason maps here, and that is not an oversight.

    The SDK has no reason meaning "the model failed": a failure arrives as an EXCEPTION
    (`ModelThrottledException`, `EventLoopException`, `StructuredOutputException`) rather than
    as a stop reason. So this outcome is reached by the unknown-reason fallback and by the
    exception path, which is why the reachability test excludes it from the reasons' image.
    """

    CANCELLED = "cancelled"


_CLASSIFICATION: dict[str, TurnOutcome] = {
    # The model finished saying what it had to say.
    "end_turn": TurnOutcome.COMPLETED,
    "stop_sequence": TurnOutcome.COMPLETED,
    # The loop is mid-flight. Reading any of these as completion would emit a half-finished
    # turn: the
    # model asked for a tool, or is waiting on a human, and has not produced its answer yet.
    "tool_use": TurnOutcome.NEEDS_CONTINUATION,
    "interrupt": TurnOutcome.NEEDS_CONTINUATION,
    "checkpoint": TurnOutcome.NEEDS_CONTINUATION,
    # A guardrail acted. Req 6.5a: a rejection to degrade from, never a failure to retry.
    "content_filtered": TurnOutcome.GUARDRAIL_REJECTED,
    "guardrail_intervened": TurnOutcome.GUARDRAIL_REJECTED,
    # A ceiling was reached. Req 22.2a: an expected outcome carrying one warning that names it.
    # `max_tokens` is the provider's per-call cap rather than one of our `limits`, but the turn
    # is
    # truncated either way and what the user sees is identical.
    "limit_turns": TurnOutcome.BOUND_REACHED,
    "limit_output_tokens": TurnOutcome.BOUND_REACHED,
    "limit_total_tokens": TurnOutcome.BOUND_REACHED,
    "max_tokens": TurnOutcome.BOUND_REACHED,
    # Nobody is waiting for the answer. Neither a failure to report nor a turn to degrade.
    "cancelled": TurnOutcome.CANCELLED,
}


def classify_stop_reason(reason: str) -> TurnOutcome:
    """Map a stop reason onto what this service should do about it.

    An unrecognised reason is a model failure rather than a completion, because the alternative
    is publishing a turn whose end state nothing understood.
    """
    return _CLASSIFICATION.get(reason, TurnOutcome.MODEL_FAILED)


__all__ = ["ALL_STOP_REASONS", "TurnOutcome", "classify_stop_reason"]
