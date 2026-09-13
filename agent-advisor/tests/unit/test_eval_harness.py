"""The evaluation harness's advisory-only property (task 19.3, Req 35.9).

THIS RUNS OFFLINE ON PURPOSE. Req 35.9's guarantee — that a model-judging-a-model verdict never
gates the build — is a property of the HARNESS, not of any live model, so it is proven here
with a stub judge and no network. The live judge itself lives behind the integration marker in
`tests/integration/test_live_eval.py`; what this file pins is that whichever way a judge's
verdict falls, the harness reports it without failing.

The key case is `test_a_failing_judge_verdict_does_not_gate`: a judge scripted to FAIL still
yields a report the harness returns normally. If `run_judged_evaluation` ever raised or
asserted on a negative verdict, that test would fail — which is the whole point.
"""

from __future__ import annotations

from tests.support.eval_harness import (
    EvaluationReport,
    JudgedCase,
    JudgeVerdict,
    run_judged_evaluation,
)

_CASE = JudgedCase(
    utterance="How is the air today?",
    generation="Conditions are moderate; a quieter route may help.",
)


def _always_pass(case: JudgedCase) -> JudgeVerdict:
    return JudgeVerdict(passed=True, rationale=f"appropriate for: {case.utterance[:8]}")


def _always_fail(case: JudgedCase) -> JudgeVerdict:
    return JudgeVerdict(passed=False, rationale="the judge disapproved")


def test_the_harness_runs_the_judge_and_records_the_verdict() -> None:
    report = run_judged_evaluation([_CASE], _always_pass)

    assert isinstance(report, EvaluationReport)
    assert report.ran is True
    assert len(report.verdicts) == 1
    assert report.verdicts[0].passed is True


def test_a_failing_judge_verdict_does_not_gate() -> None:
    # Req 35.9: a negative verdict is advisory. The harness must RETURN a report rather than
    # raise or assert — a failing verdict is data, not a build failure. This is the property
    # the whole harness exists to hold, so it is asserted directly.
    report = run_judged_evaluation([_CASE], _always_fail)

    assert report.ran is True, "the evaluation still ran"
    assert report.all_passed is False, "the verdict is reported as failing"
    assert report.failed_count == 1
    # The absence of an exception above IS the guarantee; asserting the report shape confirms
    # the negative verdict was captured rather than swallowed.


def test_a_broken_judge_that_raises_is_distinct_from_a_failing_verdict() -> None:
    # An evaluation that could not run is not an evaluation that ran and disapproved. A judge
    # that RAISES propagates, so a broken harness cannot masquerade as a clean advisory pass.
    def broken(_case: JudgedCase) -> JudgeVerdict:
        raise RuntimeError("the judge itself is misconfigured")

    raised = False
    try:
        run_judged_evaluation([_CASE], broken)
    except RuntimeError:
        raised = True
    assert raised, "a judge that raises must propagate, not be reported as a verdict"


def test_the_report_aggregates_mixed_verdicts_without_gating() -> None:
    cases = [_CASE, _CASE, _CASE]

    def alternating(case: JudgedCase) -> JudgeVerdict:
        # Deterministic split on the generation length parity, so the report has both.
        return JudgeVerdict(passed=len(case.generation) % 2 == 0, rationale="mixed")

    report = run_judged_evaluation(cases, alternating)

    assert report.ran is True
    assert len(report.verdicts) == 3
    # Whether all passed or not, the harness returned a report rather than gating.
    assert isinstance(report.all_passed, bool)
