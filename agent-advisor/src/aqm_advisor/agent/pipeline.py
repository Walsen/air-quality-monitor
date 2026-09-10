"""The `TurnPipeline` Template Method (Requirements 10.2, 20.4, 34.7).

The engineering practices name Template Method for a fixed sequence with pluggable steps, and a
turn is exactly that: red-flag check, retrieve, generate, verify, assemble, audit. `run` owns
the ORDER and the subclass owns what each step does — so a subclass cannot reorder the sequence,
only fill it in.

**The order is guaranteed twice.** Behaviourally, the steps record themselves through an
injected recorder and tests assert over the resulting sequence, because a response with the
right shape can be produced by steps that ran in the wrong order or that never ran at all.
Structurally, an AST test reads `run` itself and checks the calls appear in `STEP_ORDER` — which
holds for every subclass, where a behavioural test only ever covers the one pipeline it
instantiated.

**An escalating turn does not consult the model, and the BASE is what guarantees it.**
Escalation is determined in step one, before generation, and when it fires `run` does not call
`generate` at all. The first version of this class passed the escalation INTO `generate` and
left the subclass to honour it — which is a guarantee every future subclass has to remember, and
therefore not a guarantee. A test caught it.

The consequence is that Req 10.2's substance holds structurally: the model cannot hedge, cannot
re-assess whether the emergency is real (Req 10.5 forbids this service doing that at all), and
cannot fail in a way that loses the direction to emergency care. An escalating turn's step
sequence OMITS generation, and that absence is itself the evidence no model was consulted —
which is why the test asserts over the sequence rather than over a call counter a subclass could
forget to increment.

**Every turn is verified, including an escalating one.** Exempting the escalating path is the
obvious simplification and it would leave exactly one route to a user that no verifier inspected
— the highest-stakes route in the service. The emergency wording passes because task 6.3's sweep
already proves the required texts are not rejected by the pattern set, so the general path costs
nothing and the exemption is unnecessary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum

from aqm_advisor.agent.verification import VerificationVerdict
from aqm_advisor.domain.models import AdvisoryRequest, Escalation


class TurnStep(Enum):
    """The six steps. The value is the method name, which is what the AST guard matches on."""

    RED_FLAG = "check_red_flags"
    RETRIEVE = "retrieve"
    GENERATE = "generate"
    VERIFY = "verify"
    ASSEMBLE = "assemble"
    AUDIT = "audit"


STEP_ORDER: tuple[TurnStep, ...] = (
    TurnStep.RED_FLAG,
    TurnStep.RETRIEVE,
    TurnStep.GENERATE,
    TurnStep.VERIFY,
    TurnStep.ASSEMBLE,
    TurnStep.AUDIT,
)
"""The fixed order. Declared once so a reordering must edit this tuple and be reviewed."""


@dataclass
class StepRecorder:
    """Records the steps as they run, so the ORDER is observable rather than inferred.

    Injected rather than global: two turns running against one recorder would interleave, and
    the resulting sequence would look like a reordering that never happened.
    """

    _steps: list[TurnStep] = field(default_factory=list)

    def record(self, step: TurnStep) -> None:
        """Note that a step ran."""
        self._steps.append(step)

    def sequence(self) -> tuple[TurnStep, ...]:
        """The steps in the order they ran."""
        return tuple(self._steps)


class TurnPipeline[RetrievedT, ResponseT](ABC):
    """The Template Method. `run` is the invariant; the six steps are the variation.

    Generic in `RetrievedT` (whatever the retrieve step gathers) and `ResponseT` (what assembly
    produces — `AdvisoryResponse` in production, a simpler record in a test).
    Parameterised rather than typed `Any`, so a subclass states its own shapes and the
    linter's ban on dynamic annotations is met with real types, not a suppression.

    `run` is deliberately not overridable in spirit — a subclass that replaced it would discard
    every ordering guarantee this class exists to provide. The AST test reads THIS method, so an
    override elsewhere would be invisible to it; that is a known limit, and the mitigation is
    that a subclass overriding `run` is a reviewable event rather than a silent one.
    """

    def run(self, request: AdvisoryRequest) -> ResponseT:
        """Execute one advisory turn in the fixed order.

        Raises:
            RuntimeError: when verification did not pass. Raised BEFORE assembly, so an
                unverified generation is never built into a response — the fail-closed rule from
                task 10.1 expressed as control flow, not a check somebody must remember.
        """
        escalation = self.check_red_flags(request)
        retrieved = self.retrieve(request, escalation)
        generated = None if escalation is not None else self.generate(
            request, escalation, retrieved
        )
        verdict = self.verify(generated, retrieved)
        if not verdict.passed:
            raise RuntimeError(
                "refusing to assemble a turn whose generation was not verified: "
                f"{', '.join(verdict.failures) or 'no reason recorded'}"
            )
        response = self.assemble(request, escalation, retrieved, generated, verdict)
        self.audit(request, response, retrieved, verdict)
        return response

    @abstractmethod
    def check_red_flags(self, request: AdvisoryRequest) -> Escalation | None:
        """Step 1. Decide whether this turn must direct the user to emergency care.

        Returns the escalation, or None to carry on. Takes no model and no client, which is what
        makes the determination independent of everything that can fail.
        """

    @abstractmethod
    def retrieve(
        self, request: AdvisoryRequest, escalation: Escalation | None
    ) -> RetrievedT:
        """Step 2. Retrieve the snapshot and whatever else the turn needs.

        Runs even when escalating, because the emergency wording itself comes from Service 2's
        envelope where it could be retrieved (A8a's fallback applies only when it could not).
        """

    @abstractmethod
    def generate(
        self,
        request: AdvisoryRequest,
        escalation: Escalation | None,
        retrieved: RetrievedT,
    ) -> str | None:
        """Step 3. Produce the exposure guidance.

        Never called on an escalating turn: `run` short-circuits, so this method does not
        have to check the escalation it is handed.
        """

    @abstractmethod
    def verify(
        self, generated: str | None, retrieved: RetrievedT
    ) -> VerificationVerdict:
        """Step 4. Run the grounding, forbidden-claim, medication-closure and guardrail checks.

        Must return a verdict. `run` refuses to continue on a verdict that did not pass.
        """

    @abstractmethod
    def assemble(
        self,
        request: AdvisoryRequest,
        escalation: Escalation | None,
        retrieved: RetrievedT,
        generated: str | None,
        verdict: VerificationVerdict,
    ) -> ResponseT:
        """Step 5. Build the response, escalation first (Req 10.2's field order)."""

    @abstractmethod
    def audit(
        self,
        request: AdvisoryRequest,
        response: ResponseT,
        retrieved: RetrievedT,
        verdict: VerificationVerdict,
    ) -> None:
        """Step 6. Record what happened (Req 20.4). Last, because it records the rest."""


__all__ = ["STEP_ORDER", "StepRecorder", "TurnPipeline", "TurnStep"]
