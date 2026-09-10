"""Structured output from the model, and what its failures mean (Requirements 6.3b, 6.5, 21).

**`ModelGeneration` is a statement about what the model is permitted to author.** Req 6.3b says
the Advisory_Response's structured fields come from a Pydantic model, so the field set of that
model IS the boundary. It carries the guidance and nothing else, because every other field of
`AdvisoryResponse` has a different authority:

- `escalation` is determined before generation and never by the model (Req 10.2);
- `basis` is assembled from what was retrieved, and Req 9.4 forbids deriving it;
- `envelope` is Service 2's wording, or A8a's configured fallback (Req 21.9);
- `answered_at` comes from the injected Clock (Req 25.2);
- `degraded` is this service's own judgement about its own retrieval.

A one-field schema may look like a thin use of structured output, but the alternative is worse
in a specific way: a model that emitted `escalation` would have it silently accepted and
dropped, which is indistinguishable from the model never having tried. `extra="forbid"` makes
that attempt an error instead.

**A schema failure is a model failure, never a fallback to raw text.** If the SDK could not
coerce the output into the schema then no validated text exists, and returning the raw output
"just this once" is exactly the path Req 6.3b closes.

**Req 6.5 and Req 22.2a converge, and the difference is what happens next.** Req 6.5 calls a
truncated generation a failure; Req 22 calls reaching a cap a bound. Both DISCARD the partial
text, which is the part that protects the user. The outcome recorded here is `BOUND_REACHED`,
because the same prompt would truncate again — a retry would spend budget to reproduce the
failure, where Req 22.2's degraded response is useful. A throttle is classified differently for
the mirror-image reason: nothing about the prompt caused it, so it IS worth retrying.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from strands.types.exceptions import (
    MaxTokensReachedException,
    ModelThrottledException,
    StructuredOutputException,
)

from aqm_advisor.agent.stop_reasons import TurnOutcome


class ModelGeneration(BaseModel):
    """The only field the model authors.

    `extra="forbid"` so an attempt to author a field this service owns is an ERROR rather than a
    silent drop — a silent drop looks the same as the model never having tried.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    guidance: str = Field(min_length=1)

    @field_validator("guidance")
    @classmethod
    def _require_content(cls, value: str) -> str:
        """Trim, and refuse whitespace.

        A `min_length=1` constraint alone accepts `" "`, which would present a successful turn
        that said nothing — the same trap Req 1.4 names for the inbound utterance.
        """
        trimmed = value.strip()
        if not trimmed:
            raise ValueError("the guidance must not be blank")
        return trimmed


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """A validated generation, or a named reason there is none.

    Never both. `__bool__` follows the generation rather than the outcome, so a caller testing
    the object cannot mistake a failure for success by checking the wrong attribute.
    """

    outcome: TurnOutcome
    generation: ModelGeneration | None = None
    reason: str | None = None

    def __bool__(self) -> bool:
        """True only when a validated generation exists."""
        return self.generation is not None


def obtain_structured_generation(
    invoke: Callable[[], ModelGeneration],
) -> GenerationOutcome:
    """Run the structured-output call and translate its failures.

    `invoke` is injected rather than an `Agent` being constructed here, so this stays
    offline-testable and carries no provider configuration of its own — the real adapter arrives
    in task 16.

    The reason string names a KIND and never quotes the provider's message, per Req 21.4: a
    provider error body can contain the prompt or the model's partial output, and this string
    reaches logs.

    `KeyboardInterrupt` and `SystemExit` are deliberately NOT caught. They are not model
    failures, and swallowing them would make the process unkillable mid-turn.
    """
    try:
        generation = invoke()
    except StructuredOutputException:
        return GenerationOutcome(
            outcome=TurnOutcome.MODEL_FAILED,
            reason="the model's output did not match the structured schema",
        )
    except ValidationError:
        return GenerationOutcome(
            outcome=TurnOutcome.MODEL_FAILED,
            reason="the model's output failed structured validation",
        )
    except MaxTokensReachedException:
        return GenerationOutcome(
            outcome=TurnOutcome.BOUND_REACHED,
            reason="the generation was truncated at the output cap and was discarded",
        )
    except ModelThrottledException:
        return GenerationOutcome(
            outcome=TurnOutcome.MODEL_FAILED,
            reason="the model provider throttled the request",
        )
    except Exception:
        return GenerationOutcome(
            outcome=TurnOutcome.MODEL_FAILED,
            reason="the model call failed for an unrecognised reason",
        )
    return GenerationOutcome(outcome=TurnOutcome.COMPLETED, generation=generation)


__all__ = ["GenerationOutcome", "ModelGeneration", "obtain_structured_generation"]
