"""The evaluation harness: an LLM-as-judge whose verdict is ADVISORY (task 19.3, Req 35.9).

WHERE A MODEL JUDGES A MODEL, ITS VERDICT NEVER GATES THE BUILD. Req 35.9 is explicit: a
non-deterministic judge cannot gate a deterministic suite, so an LLM-as-judge evaluation may
RUN, but its pass/fail is reported and logged, not asserted. This harness makes that property
structural rather than a rule a caller must remember: `run_judged_evaluation` returns an
`EvaluationReport` and NEVER raises on a failing verdict. A judge that returns "fail" produces
a report whose `verdict.passed is False` and whose `ran is True` — the harness ran, the verdict
is recorded, and nothing about the build depends on which way the verdict went.

THE JUDGE IS INJECTED, so the harness is offline-testable without a live model. A `Judge` is a
callable from a `JudgedCase` to a `JudgeVerdict`. The offline suite exercises the ADVISORY-ONLY
property with a stub judge scripted to fail, proving the harness reports the failure without
gating. The live judge — a real model scoring a real generation — lives behind the integration
marker (`tests/integration/test_live_eval.py`) and skips when no model is configured, so
nothing here reaches the network in the offline suite.

WHAT THE HARNESS DOES NOT DO. It does not import a model provider, add a production dependency,
or decide a build outcome. It runs a judge, captures a verdict, logs it at INFO, and hands back
a report. The decision to act on that report — file it, review it, ignore it — is the
reviewer's, exactly as an advisory signal should be.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from aqm_advisor.observability.logging import get_logger

_LOGGER = get_logger("aqm_advisor.tests.eval_harness")


@dataclass(frozen=True, slots=True)
class JudgedCase:
    """One case put to the judge: the utterance and the generation to be scored.

    Deliberately carries no expected answer. An LLM-as-judge scores whether the generation is
    appropriate for the utterance; a golden expected string would make it an exact-match test,
    which is what the deterministic golden turns already are.
    """

    utterance: str
    generation: str
    criterion: str = "appropriate, non-diagnostic exposure-reduction guidance"


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """A judge's opinion on one case. Advisory: it records a view, it does not gate.

    `passed` is the judge's own call; `rationale` is its stated reason. Neither is asserted by
    the harness — they are reported so a reviewer can read them.
    """

    passed: bool
    rationale: str


# A judge maps a case to a verdict. Injected so the harness runs offline against a stub and
# online against a real model, with no change to the harness itself.
Judge = Callable[[JudgedCase], JudgeVerdict]


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """The outcome of a judged evaluation.

    `ran` says the harness executed the judge over the cases — which is the thing an offline
    test asserts, because Req 35.9 lets the evaluation RUN. `verdicts` records each judge
    opinion, advisory and un-gating. `all_passed` is a convenience for a reviewer reading the
    report; it is NEVER the harness's own gate.
    """

    ran: bool
    verdicts: tuple[JudgeVerdict, ...] = field(default_factory=tuple)

    @property
    def all_passed(self) -> bool:
        """Whether every judge verdict passed. A REPORTED figure, not a build gate."""
        return all(verdict.passed for verdict in self.verdicts)

    @property
    def failed_count(self) -> int:
        """How many verdicts the judge failed. Reported for the reviewer, never asserted."""
        return sum(1 for verdict in self.verdicts if not verdict.passed)


def run_judged_evaluation(cases: Sequence[JudgedCase], judge: Judge) -> EvaluationReport:
    """Run `judge` over `cases`, log each verdict, and return a report. NEVER gates.

    The advisory-only guarantee (Req 35.9) is here: this function does not raise, assert, or
    otherwise fail the build when a verdict is negative. It logs the verdict at INFO — an
    operational event a reviewer can read — and records it in the report. A failing verdict is
    data, not a failure.

    A judge that itself raises is a broken JUDGE, not a failing GENERATION, so the exception
    propagates: an evaluation that could not run is different from one that ran and disapproved,
    and collapsing the two would let a broken harness masquerade as a clean pass. The offline
    advisory-only test scripts a judge that FAILS (returns a negative verdict), never one that
    raises, so this distinction stays clean.
    """
    verdicts: list[JudgeVerdict] = []
    for case in cases:
        verdict = judge(case)
        verdicts.append(verdict)
        _LOGGER.info(
            "llm_judge_verdict",
            judge_passed=verdict.passed,
            judge_criterion=case.criterion,
            # The rationale is the judge's own words about the generation, which is safe to
            # log; the utterance and the generation are NOT logged here, since they are the
            # user's prose and the model's reply that Req 19.2 keeps out of logs.
            judge_rationale=verdict.rationale,
        )
    return EvaluationReport(ran=True, verdicts=tuple(verdicts))


__all__ = [
    "EvaluationReport",
    "Judge",
    "JudgeVerdict",
    "JudgedCase",
    "run_judged_evaluation",
]
