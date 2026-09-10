"""Tests for the `TurnPipeline` Template Method (task 10.2).

Task 10.2 asks for the order to be "recorded by tests that observe the sequence of operations
through injected ports rather than asserting on the result alone", and that distinction is the
point: a response with the right shape can be produced by steps that ran in the wrong order, or
by steps that never ran. So the ports record themselves and the tests assert over the resulting
sequence.

Two invariants matter more than the order itself.

`test_an_escalating_turn_never_calls_the_model` is Req 10.2's substance. Escalation is
determined BEFORE generation, and when it fires the model is not invoked at all — so the model
cannot hedge, cannot re-assess whether the emergency is real (Req 10.5 forbids that), and cannot
fail in a way that loses the emergency direction.

`test_every_turn_is_verified_including_an_escalating_one` closes the hole the obvious design
would open. Exempting the escalating path from verification would create exactly one route to a
user that no verifier inspected — and it would be the highest-stakes route. Instead every turn
is verified, and the emergency text passes because task 6.3's sweep already proves the required
texts are not rejected by the pattern set.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib

import pytest

from aqm_advisor.agent.pipeline import (
    STEP_ORDER,
    StepRecorder,
    TurnPipeline,
    TurnStep,
)
from aqm_advisor.agent.verification import VerificationLedger, VerificationVerdict
from aqm_advisor.domain.models import AdvisoryRequest, Escalation, GuardrailEnvelope

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_ENVELOPE = GuardrailEnvelope(
    emergency_guidance=(
        "If you are struggling to breathe, your reliever is not helping, or your lips or face "
        "look blue, call emergency services now."
    )
)


class _SpyPipeline(TurnPipeline[dict[str, object], dict[str, object]]):
    """A pipeline whose steps only record themselves, so the ORDER is what is under test."""

    def __init__(
        self,
        *,
        recorder: StepRecorder,
        ledger: VerificationLedger,
        escalate: bool = False,
        generation: str | None = "Air quality is Moderate today.",
    ) -> None:
        self._recorder = recorder
        self._ledger = ledger
        self._escalate = escalate
        self._generation = generation
        self.model_calls = 0

    def check_red_flags(self, request: AdvisoryRequest) -> Escalation | None:
        self._recorder.record(TurnStep.RED_FLAG)
        if not self._escalate:
            return None
        return Escalation(
            kind="emergency",
            markers=("struggling to breathe",),
            guidance=_ENVELOPE.emergency_guidance,
        )

    def retrieve(
        self, request: AdvisoryRequest, escalation: Escalation | None
    ) -> dict[str, object]:
        self._recorder.record(TurnStep.RETRIEVE)
        return {"nearestSensors": []}

    def generate(
        self,
        request: AdvisoryRequest,
        escalation: Escalation | None,
        retrieved: dict[str, object],
    ) -> str | None:
        self._recorder.record(TurnStep.GENERATE)
        self.model_calls += 1
        return self._generation

    def verify(
        self, generated: str | None, retrieved: dict[str, object]
    ) -> VerificationVerdict:
        self._recorder.record(TurnStep.VERIFY)
        verdict = VerificationVerdict(passed=True, checks=("grounding",))
        self._ledger.record(verdict)
        return verdict

    def assemble(
        self,
        request: AdvisoryRequest,
        escalation: Escalation | None,
        retrieved: dict[str, object],
        generated: str | None,
        verdict: VerificationVerdict,
    ) -> dict[str, object]:
        self._recorder.record(TurnStep.ASSEMBLE)
        return {"escalated": escalation is not None, "guidance": generated}

    def audit(
        self,
        request: AdvisoryRequest,
        response: dict[str, object],
        retrieved: dict[str, object],
        verdict: VerificationVerdict,
    ) -> None:
        self._recorder.record(TurnStep.AUDIT)


def _request(utterance: str = "How is the air today?") -> AdvisoryRequest:
    return AdvisoryRequest(utterance=utterance, credential="a-credential")  # type: ignore[arg-type]


def _run(**kwargs: object) -> tuple[StepRecorder, _SpyPipeline, object]:
    recorder = StepRecorder()
    ledger = VerificationLedger()
    pipeline = _SpyPipeline(recorder=recorder, ledger=ledger, **kwargs)  # type: ignore[arg-type]
    result = pipeline.run(_request())
    return recorder, pipeline, result


# --- Req 10.2 / 34.7: the six steps, in that order ----------------------


def test_the_six_steps_run_in_the_declared_order() -> None:
    # Observed through the injected steps, not inferred from the result. A response with the
    # right shape
    # can be produced by steps that ran in the wrong order.
    recorder, _pipeline, _result = _run()
    assert recorder.sequence() == STEP_ORDER


def test_the_declared_order_is_the_one_the_task_names() -> None:
    # Pins the order itself, so a reordering has to change this line and be reviewed rather than
    # sliding
    # through as an implementation detail.
    assert STEP_ORDER == (
        TurnStep.RED_FLAG,
        TurnStep.RETRIEVE,
        TurnStep.GENERATE,
        TurnStep.VERIFY,
        TurnStep.ASSEMBLE,
        TurnStep.AUDIT,
    )


def test_every_step_runs_exactly_once() -> None:
    # A step running twice is a retry loop nobody declared; a step running zero times is the bug
    # this
    # whole test file exists to catch.
    recorder, _pipeline, _result = _run()
    for step in STEP_ORDER:
        assert recorder.sequence().count(step) == 1, step


def test_red_flag_precedes_generation() -> None:
    # Req 10.2 stated as the relation it actually is, so it survives a future step being
    # inserted between
    # the two.
    sequence = _run()[0].sequence()
    assert sequence.index(TurnStep.RED_FLAG) < sequence.index(TurnStep.GENERATE)


def test_verification_precedes_assembly() -> None:
    # Assembling before verifying would build the response the verifier is meant to be able to
    # withhold.
    sequence = _run()[0].sequence()
    assert sequence.index(TurnStep.VERIFY) < sequence.index(TurnStep.ASSEMBLE)


def test_audit_is_last() -> None:
    # Req 20.4: the audit records what happened, so it cannot run before the thing it records.
    assert _run()[0].sequence()[-1] is TurnStep.AUDIT


# --- Req 10.2 / 10.5: an escalating turn does not consult the model -----


def test_an_escalating_turn_never_calls_the_model() -> None:
    # THE safety invariant. The model cannot hedge, cannot re-assess whether the emergency is
    # real (Req
    # 10.5 forbids the service doing that at all), and cannot fail in a way that loses the
    # direction to
    # emergency care.
    _recorder, pipeline, _result = _run(escalate=True)
    assert pipeline.model_calls == 0


def test_a_normal_turn_does_call_the_model() -> None:
    # Non-vacuity: a pipeline that never generated would pass the test above trivially.
    _recorder, pipeline, _result = _run()
    assert pipeline.model_calls == 1


def test_an_escalating_turn_omits_generation_from_the_sequence() -> None:
    # The sequence's ABSENCE is the evidence. `run` does not call `generate` at all when
    # escalating, so
    # the guarantee does not depend on a subclass remembering to check the escalation it was
    # handed —
    # which is how the first version of this class got it wrong, and what this test caught.
    recorder, _pipeline, _result = _run(escalate=True)
    assert TurnStep.GENERATE not in recorder.sequence()
    assert recorder.sequence() == tuple(
        step for step in STEP_ORDER if step is not TurnStep.GENERATE
    )


def test_the_declared_order_is_still_the_order_of_the_steps_that_ran() -> None:
    # Omitting a step must not reorder the rest.
    recorder, _pipeline, _result = _run(escalate=True)
    ran = recorder.sequence()
    positions = [STEP_ORDER.index(step) for step in ran]
    assert positions == sorted(positions)


def test_every_turn_is_verified_including_an_escalating_one() -> None:
    # Exempting the escalating path would leave exactly one route to a user that no verifier
    # inspected,
    # and it would be the highest-stakes route.
    recorder, _pipeline, _result = _run(escalate=True)
    assert TurnStep.VERIFY in recorder.sequence()


# --- fail-closed: an unverified turn does not produce a response --------


class _SkippingPipeline(_SpyPipeline):
    """A pipeline with a deliberately added early return, as task 10.1's test demands."""

    def verify(
        self, generated: str | None, retrieved: dict[str, object]
    ) -> VerificationVerdict:
        # Deliberately records NOTHING in the ledger — the bypass this design must survive.
        self._recorder.record(TurnStep.VERIFY)
        return VerificationVerdict(passed=False, checks=("grounding",), failures=("skipped",))


def test_a_failing_verdict_stops_the_turn_before_assembly() -> None:
    # The fail-closed half, observed through the sequence: assembly and audit must not have run
    # on a
    # verdict that did not pass.
    recorder = StepRecorder()
    pipeline = _SkippingPipeline(recorder=recorder, ledger=VerificationLedger())
    with pytest.raises(RuntimeError, match="verif"):
        pipeline.run(_request())
    assert TurnStep.ASSEMBLE not in recorder.sequence()


def test_the_failed_turn_still_recorded_the_steps_that_did_run() -> None:
    # Diagnosability: the sequence up to the failure is what tells an operator where it stopped.
    recorder = StepRecorder()
    pipeline = _SkippingPipeline(recorder=recorder, ledger=VerificationLedger())
    with pytest.raises(RuntimeError):
        pipeline.run(_request())
    assert recorder.sequence()[:4] == STEP_ORDER[:4]


# --- the order is structural, not only tested --------------------------

_PIPELINE_SOURCE = pathlib.Path(
    "src/aqm_advisor/agent/pipeline.py"
).read_text(encoding="utf-8")


def _step_calls_in_source_order(run: ast.FunctionDef) -> list[str]:
    """The step calls inside `run`, ordered by POSITION in the source.

    `ast.walk` is breadth-first, so it does not preserve source order once a call is nested
    inside an expression — the escalation short-circuit puts `self.generate(...)` inside a
    conditional, and walk order then reported it last. Sorting by `(lineno, col_offset)` is what
    actually answers "in what order does this method call them".
    """
    names = {step.value for step in TurnStep}
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(run):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in names:
            found.append((node.lineno, node.col_offset, func.attr))
    found.sort()
    return [attr for _line, _col, attr in found]


def test_the_run_method_calls_the_steps_in_the_declared_order() -> None:
    # The structural guard. The behavioural tests above use ONE pipeline; this one reads `run`
    # itself, so
    # it holds for every subclass rather than for the spy that happened to be tested.
    tree = ast.parse(_PIPELINE_SOURCE)
    run = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    assert _step_calls_in_source_order(run) == [step.value for step in STEP_ORDER]


def test_the_order_detector_would_catch_a_swap() -> None:
    # Self-check: a detector that collected nothing, or that reported walk order, would report
    # this
    # guarantee for free.
    planted = ast.parse(
        "class P:\n"
        "    def run(self):\n"
        "        self.verify()\n"
        "        self.generate()\n"
    )
    run = next(
        node
        for node in ast.walk(planted)
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    assert _step_calls_in_source_order(run) == ["verify", "generate"]


def test_the_detector_survives_a_call_nested_in_an_expression() -> None:
    # The specific bug this helper fixes: a nested call must still be reported in source order.
    planted = ast.parse(
        "class P:\n"
        "    def run(self):\n"
        "        a = None if x else self.generate()\n"
        "        self.verify()\n"
    )
    run = next(
        node
        for node in ast.walk(planted)
        if isinstance(node, ast.FunctionDef) and node.name == "run"
    )
    assert _step_calls_in_source_order(run) == ["generate", "verify"]


def test_the_recorder_starts_empty() -> None:
    assert StepRecorder().sequence() == ()
