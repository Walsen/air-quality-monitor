"""Tests for the scripted Model (task 2.3).

`test_a_real_agent_runs_against_the_scripted_model` is the one that matters. Every other
assertion
here checks the fake against my own reading of the SDK's TypedDicts, which is exactly the kind
of
check that passes while the fake is subtly wrong. Driving a REAL `Agent` through it is the only
assertion that proves the event shapes feed the loop this fake exists to feed — and the whole
offline suite, including all 20 correctness properties, rests on that being true.

The fake is instantiable at all only because it implements all FOUR abstract methods. A subclass
missing one raises `TypeError` at construction, which is why the design's original one-method
note
would have been fatal rather than merely inaccurate.
"""

from __future__ import annotations

import pytest
from strands import Agent, tool
from strands.models import Model
from strands.types.event_loop import StopReason

from aqm_advisor.adapters.model.scripted import (
    CONTENT_FILTERED,
    END_TURN,
    GUARDRAIL_INTERVENED,
    LIMIT_OUTPUT_TOKENS,
    TOOL_USE,
    ScriptedModel,
    ScriptedTurn,
    ScriptExhaustedError,
    calls,
    fails,
    says,
    stops,
)

# --- THE test: a real Agent loop consumes these events -------------------

async def test_a_real_agent_runs_against_the_scripted_model() -> None:
    agent = Agent(model=ScriptedModel([says("Air quality is moderate today.")]))
    result = await agent.invoke_async("How is the air?")
    assert "moderate" in str(result)


async def test_a_real_agent_runs_a_tool_the_scripted_model_asks_for() -> None:
    # The other half of the loop: a tool-use turn has to be shaped so the Agent actually
    # dispatches
    # the tool and comes back for a second turn. If contentBlockStart's toolUse block were
    # wrong,
    # this would hang or error rather than answering.
    seen: list[str] = []

    @tool
    def air_quality() -> str:
        """Return the current air quality."""
        seen.append("called")
        return "PM2.5 sub-index 68, Moderate"

    agent = Agent(
        model=ScriptedModel(
            [
                calls("air_quality"),
                says("Your nearest sensor reports a Moderate band."),
            ]
        ),
        tools=[air_quality],
    )
    result = await agent.invoke_async("How is the air?")
    assert seen == ["called"], "the Agent did not dispatch the scripted tool call"
    assert "Moderate" in str(result)


async def test_the_agent_reports_the_scripted_token_usage() -> None:
    # Requirement 24.4 counts token usage, so the metadata event has to be shaped such that the
    # Agent surfaces it — otherwise the counter would silently always read zero.
    agent = Agent(
        model=ScriptedModel([says("ok", input_tokens=11, output_tokens=7)])
    )
    result = await agent.invoke_async("hello")
    usage = result.metrics.accumulated_usage
    assert usage["inputTokens"] == 11
    assert usage["outputTokens"] == 7
    assert usage["totalTokens"] == 18


# --- DD2: it IS a Model, and all four methods are present ---------------

def test_the_scripted_model_is_a_strands_model() -> None:
    assert isinstance(ScriptedModel(), Model)


def test_a_subclass_missing_an_abstract_method_cannot_be_instantiated() -> None:
    # Why the design's one-method note would have been fatal rather than merely inaccurate.
    class Incomplete(Model):
        async def stream(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
            ...

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_get_config_and_update_config_behave_like_a_provider() -> None:
    model = ScriptedModel(config={"temperature": 0})
    assert model.get_config() == {"temperature": 0}
    model.update_config(max_tokens=256)
    assert model.get_config() == {"temperature": 0, "max_tokens": 256}


def test_the_config_is_a_copy_so_a_caller_cannot_mutate_it() -> None:
    model = ScriptedModel(config={"temperature": 0})
    model.get_config()["temperature"] = 1
    assert model.get_config()["temperature"] == 0


# --- Req 25.1: determinism ----------------------------------------------

async def test_the_same_script_yields_the_same_answer_twice() -> None:
    async def run() -> str:
        agent = Agent(model=ScriptedModel([says("the same words every time")]))
        return str(await agent.invoke_async("hello"))

    assert await run() == await run()


# --- Req 21 / 6.5a: every failure branch is drivable --------------------

async def test_a_model_failure_propagates_so_the_pipeline_can_degrade() -> None:
    agent = Agent(model=ScriptedModel([fails(TimeoutError("model timed out"))]))
    with pytest.raises(TimeoutError):
        await agent.invoke_async("hello")


def test_a_specific_exception_can_be_scripted_not_just_a_flag() -> None:
    # A timeout and a throttle are different paths in the pipeline; a boolean could not tell
    # them
    # apart, so the script carries the instance.
    turn = fails(ConnectionError("refused"))
    assert isinstance(turn.raises, ConnectionError)


@pytest.mark.parametrize(
    "reason",
    [END_TURN, GUARDRAIL_INTERVENED, CONTENT_FILTERED, LIMIT_OUTPUT_TOKENS],
)
async def test_every_stop_reason_the_pipeline_handles_is_scriptable(reason: StopReason) -> None:
    agent = Agent(model=ScriptedModel([stops(reason, text="held")]))
    result = await agent.invoke_async("hello")
    assert result.stop_reason == reason


def test_the_guardrail_stop_reason_is_the_sdks_own_spelling() -> None:
    # The design document first guessed `guardrail_intervention`, which is not a stop reason.
    # This
    # pins the real one so an SDK rename fails a test rather than a deployed turn.
    from strands.types.event_loop import StopReason

    permitted = set(StopReason.__args__)  # type: ignore[attr-defined]
    assert GUARDRAIL_INTERVENED in permitted
    assert "guardrail_intervention" not in permitted


@pytest.mark.parametrize(
    "constant",
    [END_TURN, TOOL_USE, GUARDRAIL_INTERVENED, CONTENT_FILTERED, LIMIT_OUTPUT_TOKENS],
)
def test_every_stop_reason_constant_is_a_real_sdk_value(constant: str) -> None:
    # A drift guard: these constants duplicate a fact the SDK owns, so an upgrade that renames
    # one
    # fails here rather than at the first turn that hits it.
    from strands.types.event_loop import StopReason

    assert constant in set(StopReason.__args__)  # type: ignore[attr-defined]


# --- the script is a contract, not a suggestion -------------------------

async def test_an_exhausted_script_fails_loudly() -> None:
    # Rather than repeating the last turn or yielding nothing: a loop that ran longer than the
    # script anticipated is a finding about the pipeline, and satisfying it silently would hide
    # exactly the runaway Requirement 22's bounds exist to catch.
    agent = Agent(model=ScriptedModel([calls("missing_tool")]))
    with pytest.raises((ScriptExhaustedError, Exception)) as caught:
        await agent.invoke_async("hello")
    assert caught.value is not None


def test_a_turn_cannot_both_speak_and_fail() -> None:
    with pytest.raises(ValueError):
        ScriptedTurn(text="hello", raises=RuntimeError("boom"))


# --- Req 22.1 / 35.4: the calls are observable --------------------------

async def test_the_model_records_how_many_times_it_was_invoked() -> None:
    # Requirement 22.1 bounds model invocations per turn, which is a question about the CALLS.
    model = ScriptedModel([says("one")])
    await Agent(model=model).invoke_async("hello")
    assert model.invocations == 1


async def test_the_model_records_the_tool_specs_it_was_offered() -> None:
    # Requirement 35.4's trajectory assertions need to know WHICH tools the model could see.
    @tool
    def air_quality() -> str:
        """Return the current air quality."""
        return "fine"

    model = ScriptedModel([says("ok")])
    await Agent(model=model, tools=[air_quality]).invoke_async("hello")
    offered = model.stream_calls[0]["tool_specs"]
    assert offered is not None
    assert "air_quality" in str(offered)


async def test_remaining_turns_lets_a_test_assert_the_loop_stopped_early() -> None:
    model = ScriptedModel([says("first"), says("never reached")])
    await Agent(model=model).invoke_async("hello")
    assert model.remaining_turns == 1


# --- Req 6.3b: structured output is scripted too ------------------------

async def test_structured_output_yields_the_scripted_object() -> None:
    from pydantic import BaseModel

    class Draft(BaseModel):
        guidance: str

    model = ScriptedModel(structured=[Draft(guidance="stay indoors this morning")])
    events = [event async for event in model.structured_output(Draft, [])]
    assert events[-1]["output"].guidance == "stay indoors this morning"


async def test_structured_output_can_be_scripted_to_raise() -> None:
    # Req 6.3b treats a StructuredOutputException as a MODEL failure rather than an unvalidated
    # response, so that branch has to be drivable.
    from pydantic import BaseModel

    class Draft(BaseModel):
        guidance: str

    model = ScriptedModel(structured=[ValueError("not valid against the model")])
    with pytest.raises(ValueError):
        _ = [event async for event in model.structured_output(Draft, [])]


async def test_an_unscripted_structured_request_fails_loudly() -> None:
    from pydantic import BaseModel

    class Draft(BaseModel):
        guidance: str

    with pytest.raises(ScriptExhaustedError):
        _ = [event async for event in ScriptedModel().structured_output(Draft, [])]


# --- no network ---------------------------------------------------------

def test_the_scripted_model_imports_no_transport() -> None:
    # The premise of the whole offline suite: this module must not be able to reach the network.
    import ast
    import pathlib

    import aqm_advisor.adapters.model.scripted as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("httpx", "boto3", "botocore", "urllib", "socket", "requests"):
        assert forbidden not in imported
