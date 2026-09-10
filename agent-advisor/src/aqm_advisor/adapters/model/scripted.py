"""The scripted Model: the offline half of design decision DD2.

DD2 makes the Strands ``Model`` abstract class the Model_Port itself, and this is what makes
that
decision pay: a ``Model`` subclass whose ``stream`` yields scripted events performs NO network
call,
so the whole advisory path — and therefore all 20 correctness properties — runs with no AWS
credentials.

**ALL FOUR abstract methods are implemented, because a subclass missing any of them cannot be
instantiated at all.** The research note behind DD2 recorded only ``stream``; the real surface
of
``strands-agents==1.55.1`` is ``stream``, ``structured_output``, ``get_config`` and
``update_config``. A fake built to that note would have failed at construction with no obvious
cause, and `tests/unit/test_scaffolding.py` now pins the set.

**THE EVENT SHAPES ARE VERIFIED BY DRIVING A REAL ``Agent``, not by matching the SDK's
TypedDicts by
eye.** A fake whose events are subtly wrong passes its own tests and then fails against the loop
it
exists to feed, so `test_a_real_agent_runs_against_the_scripted_model` is the assertion that
matters. Everything else here is convenience around it.

The stop reason for a guardrail is ``guardrail_intervened`` — the SDK's own spelling, which is
not
the one the design document first guessed.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from strands.models import Model
from strands.types.event_loop import StopReason
from strands.types.streaming import StreamEvent

T = TypeVar("T")

# Typed as the SDK's own `StopReason` literal rather than `str`, so a typo in one of these is a
# TYPE error rather than a runtime surprise on a deployed turn. That matters here: the design
# document first guessed `guardrail_intervention`, which is not a stop reason at all.
END_TURN: StopReason = "end_turn"
TOOL_USE: StopReason = "tool_use"
GUARDRAIL_INTERVENED: StopReason = "guardrail_intervened"
"""The SDK's own spelling. `guardrail_intervention` is not a stop reason and never was."""
CONTENT_FILTERED: StopReason = "content_filtered"
LIMIT_OUTPUT_TOKENS: StopReason = "limit_output_tokens"
LIMIT_TOTAL_TOKENS: StopReason = "limit_total_tokens"
LIMIT_TURNS: StopReason = "limit_turns"
MAX_TOKENS: StopReason = "max_tokens"


class ScriptExhaustedError(RuntimeError):
    """The agent asked for more model turns than the script provides.

    An explicit failure rather than repeating the last turn or yielding nothing: a loop that ran
    longer than the script anticipated is a finding about the pipeline, and silently satisfying
    it
    would hide exactly the runaway Requirement 22's bounds exist to catch.
    """


@dataclass(frozen=True, slots=True)
class ScriptedToolUse:
    """One tool call the scripted model asks for."""

    name: str
    tool_use_id: str = "tool-1"
    input_json: str = "{}"


@dataclass(frozen=True, slots=True)
class ScriptedTurn:
    """One model turn: what it says, what it asks for, and how it ends.

    ``raises`` covers Requirement 21's model-failure path. It is an EXCEPTION INSTANCE rather
    than a
    flag so a test can script the specific failure a branch handles — a timeout and a throttle
    are
    different paths in the pipeline, and a boolean could not tell them apart.
    """

    text: str = ""
    tool_use: ScriptedToolUse | None = None
    stop_reason: StopReason = END_TURN
    raises: BaseException | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        """Refuse a turn that both speaks and fails, which no real turn does."""
        if self.raises is not None and (self.text or self.tool_use is not None):
            raise ValueError(
                "a scripted turn cannot both produce content and raise; a real model turn "
                "either streams or fails"
            )


class ScriptedModel(Model):
    """A Model whose turns are supplied, not generated. Performs no network call.

    Requirement 25.1's byte-identical claim is provable because this is deterministic by
    construction: the same script yields the same events in the same order, every time.
    """

    def __init__(
        self,
        turns: Sequence[ScriptedTurn] | None = None,
        structured: Sequence[object] = (),
        config: dict[str, Any] | None = None,
    ) -> None:
        """Hold the script.

        Args:
            turns: the model turns, consumed in order. An empty script with a request is an
            error,
                not a silent empty response.
            structured: the objects ``structured_output`` yields, consumed in order.
            config: what ``get_config`` reports.
        """
        self._turns = list(turns or ())
        self._structured = list(structured)
        self._config: dict[str, Any] = dict(config or {})
        self._stream_calls: list[dict[str, object]] = []
        self._structured_calls: list[object] = []

    # --- the observable record, for trajectory assertions ---------------

    @property
    def stream_calls(self) -> Sequence[dict[str, object]]:
        """Every ``stream`` invocation's arguments, in order.

        Requirement 35.4 asserts WHICH tools were offered and in what order, and Requirement
        22.1
        bounds how many times the model was invoked — both are questions about the calls rather
        than the answers, so the calls are recorded.
        """
        return tuple(self._stream_calls)

    @property
    def invocations(self) -> int:
        """How many times the model was asked to stream."""
        return len(self._stream_calls)

    @property
    def remaining_turns(self) -> int:
        """Turns left in the script, so a test can assert the loop stopped early."""
        return len(self._turns)

    # --- the four abstract methods --------------------------------------

    def update_config(self, **model_config: Any) -> None:
        """Merge configuration, as a real provider does."""
        self._config.update(model_config)

    def get_config(self) -> Any:
        """Report the configuration."""
        return dict(self._config)

    async def stream(
        self,
        messages: Any,
        tool_specs: Any = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterable[StreamEvent]:
        """Yield one scripted turn's events.

        Signature widened with ``**kwargs`` deliberately: the SDK's own ``stream`` takes
        ``tool_choice``, ``system_prompt_content``, ``invocation_state`` and ``cancel_signal``,
        and
        a fake that enumerated them would break on the next release that adds one. Accepting
        them
        loosely is what keeps this fake surviving an SDK upgrade.
        """
        self._stream_calls.append(
            {
                "messages": messages,
                "tool_specs": tool_specs,
                "system_prompt": system_prompt,
                **kwargs,
            }
        )
        if not self._turns:
            raise ScriptExhaustedError(
                f"the agent asked for model turn {self.invocations} but the script provides "
                f"{self.invocations - 1}"
            )
        turn = self._turns.pop(0)
        if turn.raises is not None:
            raise turn.raises
        for event in _events_for(turn):
            yield event

    def structured_output(
        self,
        output_model: type[T],
        prompt: Any,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        """Yield the scripted structured result.

        Implemented because it is ABSTRACT, not because every test needs it: Requirement 6.3b
        routes
        the response's structured fields through it, so the pipeline depends on it — and a
        subclass
        omitting it could not be instantiated at all.
        """
        return self._structured_output(output_model, prompt)

    async def _structured_output(
        self, output_model: type[T], prompt: object
    ) -> AsyncGenerator[dict[str, T | Any], None]:
        """The async generator ``structured_output`` returns."""
        self._structured_calls.append(prompt)
        if not self._structured:
            raise ScriptExhaustedError(
                "structured output was requested but the script provides none"
            )
        result = self._structured.pop(0)
        if isinstance(result, BaseException):
            raise result
        yield {"output": result}


def _events_for(turn: ScriptedTurn) -> Iterable[StreamEvent]:
    """Render one turn as the event sequence a real provider emits.

    The order is the SDK's: a message start, then per content block a start, its deltas and a
    stop,
    then the message stop carrying the stop reason, then metadata carrying usage. Getting this
    wrong
    is exactly what the real-Agent test catches.
    """
    events: list[StreamEvent] = [{"messageStart": {"role": "assistant"}}]

    if turn.tool_use is not None:
        events.append(
            {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {
                        "toolUse": {
                            "toolUseId": turn.tool_use.tool_use_id,
                            "name": turn.tool_use.name,
                        }
                    },
                }
            }
        )
        events.append(
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": turn.tool_use.input_json}},
                }
            }
        )
        events.append({"contentBlockStop": {"contentBlockIndex": 0}})
    elif turn.text:
        events.append({"contentBlockStart": {"contentBlockIndex": 0, "start": {}}})
        events.append(
            {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"text": turn.text},
                }
            }
        )
        events.append({"contentBlockStop": {"contentBlockIndex": 0}})

    events.append({"messageStop": {"stopReason": turn.stop_reason}})
    events.append(
        {
            "metadata": {
                "usage": {
                    "inputTokens": turn.input_tokens,
                    "outputTokens": turn.output_tokens,
                    "totalTokens": turn.input_tokens + turn.output_tokens,
                },
                "metrics": {"latencyMs": 0},
            }
        }
    )
    return events


def says(text: str, **kwargs: Any) -> ScriptedTurn:
    """A turn that answers in text. Convenience for the common case."""
    return ScriptedTurn(text=text, **kwargs)


def calls(name: str, input_json: str = "{}", **kwargs: Any) -> ScriptedTurn:
    """A turn that asks for a tool."""
    return ScriptedTurn(
        tool_use=ScriptedToolUse(name=name, input_json=input_json),
        stop_reason=TOOL_USE,
        **kwargs,
    )


def fails(error: BaseException) -> ScriptedTurn:
    """A turn that raises, for Requirement 21's model-failure path."""
    return ScriptedTurn(raises=error)


def stops(reason: StopReason, text: str = "") -> ScriptedTurn:
    """A turn ending on a specific stop reason — a guardrail, a filter, or a limit."""
    return ScriptedTurn(text=text, stop_reason=reason)


__all__ = [
    "CONTENT_FILTERED",
    "END_TURN",
    "GUARDRAIL_INTERVENED",
    "LIMIT_OUTPUT_TOKENS",
    "LIMIT_TOTAL_TOKENS",
    "LIMIT_TURNS",
    "MAX_TOKENS",
    "TOOL_USE",
    "ScriptExhaustedError",
    "ScriptedModel",
    "ScriptedToolUse",
    "ScriptedTurn",
    "calls",
    "fails",
    "says",
    "stops",
]
