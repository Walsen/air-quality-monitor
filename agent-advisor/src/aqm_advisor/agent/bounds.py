"""The per-turn invocation bounds (Requirements 22.1, 22.1a, 22.2a, 22.3, 22.4).

**Req 22.1a names its own mechanism, and the reason is a good one.** The model ceiling is
expressed through Strands' invocation `limits` rather than a counter of this service's own,
because the framework enforces them inside the agent loop where a hand-rolled counter cannot see
a tool round trip. So `as_limits()` is the whole mechanism, and an AST test asserts this module
keeps no model-invocation counter alongside — one reintroduced next to the limits would drift
from them and would miss exactly the round trips the framework exists to catch.

**Serving calls are the deliberate exception.** Strands cannot observe them: they happen inside
a tool body, past the point the agent loop counts. So Req 22.3's bound IS a counter of ours, and
`ServingCallBudget` is it. The inconsistency is intentional and recorded here so it does not
read as an oversight.

**Only the turn ceiling is hard.** The SDK documents `output_tokens` and `total_tokens` as SOFT
— "a single oversized response can overshoot the budget; checked at turn boundaries, not within
an individual model call" — and its priority on a simultaneous trip is `turns`, then
`total_tokens`, then `output_tokens`. `HARD_LIMIT_KEYS` and `SOFT_LIMIT_KEYS` record the
distinction so Req 22 is not read as a hard guarantee on all three, which is what its original
wording implied.

`Limits` is `total=False` and the SDK validates each PRESENT key as a positive int, so an unset
ceiling must be OMITTED rather than passed as zero: zero would raise a TypeError instead of
lifting the cap.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from strands.types.agent import Limits

from aqm_advisor.domain.models import PriorTurn

DEFAULT_MODEL_INVOCATIONS = 2
"""Req 22.1's default: one generation plus one repair attempt after a guardrail rejection.

A default of 1 would make the repair path dead code, and Req 22.2's degraded response would be
the only outcome a rejection could ever have.
"""

HARD_LIMIT_KEYS = frozenset({"turns"})
"""The ceiling the SDK enforces exactly, checked at the top of each loop iteration."""

SOFT_LIMIT_KEYS = frozenset({"output_tokens", "total_tokens"})
"""The ceilings the SDK documents as approximate. An oversized response can overshoot them."""

_LIMIT_REASONS = {
    "limit_turns": "turns",
    "limit_output_tokens": "output_tokens",
    "limit_total_tokens": "total_tokens",
}


def _require_positive(name: str, value: int | None) -> None:
    """Reject a non-positive ceiling while the configuration is still in view.

    The SDK raises `TypeError` at invocation time, which surfaces mid-turn and names the SDK's
    own key rather than the operator's setting. Failing fast here names the offending value
    instead.
    """
    if value is not None and value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value}")


@dataclass(frozen=True, slots=True)
class InvocationBounds:
    """The configured per-turn ceilings.

    There is deliberately no counter field. Req 22.1a's mechanism is the framework's, and a
    field able to hold a running count is an invitation to enforce the bound twice, in two
    places that then disagree.
    """

    model_invocations: int = DEFAULT_MODEL_INVOCATIONS
    output_tokens: int | None = None
    total_tokens: int | None = None
    serving_calls: int = 4
    prior_turns: int = 6

    def __post_init__(self) -> None:
        """Validate every ceiling, writing one message per invalid value."""
        _require_positive("model_invocations", self.model_invocations)
        _require_positive("output_tokens", self.output_tokens)
        _require_positive("total_tokens", self.total_tokens)
        _require_positive("serving_calls", self.serving_calls)
        _require_positive("prior_turns", self.prior_turns)

    def as_limits(self) -> Limits:
        """Render the model ceilings as Strands' own `limits` mapping (Req 22.1a).

        An unset token ceiling is omitted rather than zeroed, because `Limits` is `total=False`
        and the SDK validates each present key as positive — zero would raise instead of meaning
        "no limit".
        """
        limits: Limits = {"turns": self.model_invocations}
        if self.output_tokens is not None:
            limits["output_tokens"] = self.output_tokens
        if self.total_tokens is not None:
            limits["total_tokens"] = self.total_tokens
        return limits


def limit_warning(stop_reason: str) -> str:
    """Build the single warning Req 22.2a requires, naming which limit was reached.

    Raises:
        ValueError: for a reason that is not a limit. Logging a bound warning on a healthy turn
        is how a
            warning channel becomes noise nobody reads.
    """
    if stop_reason == "max_tokens":
        return (
            "the model stopped at the provider's own per-call output cap (max_tokens); "
            "this is not one of this service's configured limits, so raising them will "
            "not change it"
        )
    key = _LIMIT_REASONS.get(stop_reason)
    if key is None:
        raise ValueError(f"{stop_reason!r} is not a limit stop reason")
    firmness = "an approximate" if key in SOFT_LIMIT_KEYS else "an exact"
    return (
        f"the turn reached its configured {key} limit, which the SDK enforces as "
        f"{firmness} ceiling; the response is degraded from retrieved data rather than "
        "carrying an unverified generation"
    )


@dataclass
class ServingCallBudget:
    """Bounds Serving_Client calls per turn (Req 22.3).

    A counter of this service's own, unlike the model bound, because the calls happen inside a
    tool body where the framework's limits cannot observe them. Refusing rather than raising: a
    turn that ran out of retrieval budget can still answer from what it already has, and Req
    21's degradation path is better than an exception.
    """

    maximum: int
    used: int = 0
    refused: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Reject a zero bound, which would make every turn degraded by configuration."""
        _require_positive("maximum", self.maximum)

    def consume(self, tool_name: str) -> bool:
        """Take one unit of budget, or report that none was left.

        A refusal does not consume budget, so `used` stays truthful about how many calls
        actually happened — which is what Req 22.5's metric records.
        """
        if self.used >= self.maximum:
            self.refused = (*self.refused, tool_name)
            return False
        self.used += 1
        return True


def recent_prior_turns(
    supplied: Sequence[PriorTurn], *, maximum: int
) -> tuple[PriorTurn, ...]:
    """Keep at most `maximum` prior turns, the MOST RECENT ones (Req 22.4).

    The direction is the substance. Keeping the oldest would drop the context the current
    utterance actually follows on from, so the model would answer a conversation that had
    stopped being the one happening. Order within the window is preserved, because reversing it
    presents the conversation backwards.

    Raises:
        ValueError: for a non-positive maximum, which would silently discard all history — and
        reads to a
            user as the agent forgetting what they just said.
    """
    _require_positive("maximum", maximum)
    return tuple(supplied)[-maximum:]


__all__ = [
    "DEFAULT_MODEL_INVOCATIONS",
    "HARD_LIMIT_KEYS",
    "SOFT_LIMIT_KEYS",
    "InvocationBounds",
    "ServingCallBudget",
    "limit_warning",
    "recent_prior_turns",
]
