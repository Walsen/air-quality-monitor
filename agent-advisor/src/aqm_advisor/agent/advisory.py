"""The production `TurnPipeline` (Requirement 31, task 21.1).

THE PIPELINE INSTANCE IS PER TURN, AND THAT IS FORCED RATHER THAN CHOSEN. `TurnPipeline.run`
takes only the request, so anything a step needs must live on the instance — and three of
those things are per turn by their own construction: the `RetrievalRecorder` accumulates what
THIS turn retrieved, the `VerificationLedger` records whether THIS turn's generation may be
published, and `build_retrieval_tools` closes over this turn's credential. A process-wide
pipeline would share all three.

That is not a style preference. A reused recorder leaks `retrieved_site_code` from one turn
into the next — so a history call could be answered for a site the current user never asked
about, and the leak crosses USERS as well as turns. A reused ledger is worse:
`is_publishable()` would still be true from the previous turn, and `release()` would hand
back text nothing had verified.

`build_turn_runner` therefore returns a closure that constructs a fresh pipeline per call,
which is also what makes the injected `run_turn` concurrency-safe for Req 32.4b's concurrent
turns: two turns share no mutable state at all, so there is nothing to serialise.

THE HOOK CANNOT VETO, SO `verify` READS THE LEDGER RATHER THAN RE-CHECKING. Req 31.5 wants
the output checks registered through Strands hooks "so no return path can bypass them", but
`AfterInvocationEvent` carries only `result` and `resume` — a hook cannot refuse a response.
The shape that satisfies the requirement is the one `agent/verification.py` documents: the
hook RUNS the checks and RECORDS a verdict, and the pipeline refuses to assemble without a
positive one. So `verify` here consults the ledger; it does not run the checks a second time.
Running them twice would be worse than pointless — two verdicts can disagree, and then which
one gated the response depends on call order.

WHAT THIS MODULE DOES NOT IMPORT. No `bedrock_agentcore` (Req 32.2's fence), and no provider
configuration. The model arrives as an injected `invoke` callable, exactly as
`obtain_structured_generation` expects, so this file is executable in the offline suite
against the scripted model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final

from aqm_advisor.agent.audit import AuditWriter, build_advice_record
from aqm_advisor.agent.generation import ModelGeneration, obtain_structured_generation
from aqm_advisor.agent.pipeline import TurnPipeline
from aqm_advisor.agent.tools import RetrievalRecorder
from aqm_advisor.agent.verification import VerificationLedger, VerificationVerdict
from aqm_advisor.domain.basis import assemble_basis
from aqm_advisor.domain.envelope import resolve_envelope
from aqm_advisor.domain.forbidden import forbidden_matches, unlisted_medications
from aqm_advisor.domain.grounding import permitted_values, structural_constants, ungrounded
from aqm_advisor.domain.idempotency import TurnIdentity
from aqm_advisor.domain.models import (
    AdvisoryRequest,
    AdvisoryResponse,
    BasisSummary,
    Escalation,
    GuardrailEnvelope,
)
from aqm_advisor.domain.records import RetrievedValues
from aqm_advisor.domain.redflag import RedFlagRule
from aqm_advisor.domain.turn import determine_escalation, escalating_response
from aqm_advisor.ports.clock import Clock
from aqm_advisor.ports.protocols import GuardrailChecker, GuardrailVerdict

ROUTE: Final = "invocations"
"""The route recorded in the audit record. One entry point, per Req 33.9."""


class AdvisoryTurnPipeline(TurnPipeline[RetrievedValues, AdvisoryResponse]):
    """One advisory turn, assembled from injected ports.

    Every collaborator is passed in. Nothing here reads configuration, constructs a client, or
    names a provider: `composition.py` selects the adapters and `build_turn_runner` binds them,
    which is what keeps this class executable offline against local adapters.
    """

    def __init__(
        self,
        *,
        recorder: RetrievalRecorder,
        ledger: VerificationLedger,
        invoke: Callable[[], ModelGeneration],
        audit_writer: AuditWriter,
        clock: Clock,
        identity: TurnIdentity,
        red_flag_rules: Sequence[RedFlagRule],
        emergency_guidance: str,
        forbidden_patterns: Sequence[str],
        guardrail: GuardrailChecker,
        retrieve_snapshot: Callable[[], object],
    ) -> None:
        """Bind one turn's collaborators. All injected; none read from config here."""
        self._recorder = recorder
        self._ledger = ledger
        self._invoke = invoke
        self._audit_writer = audit_writer
        self._clock = clock
        self._identity = identity
        self._red_flag_rules = red_flag_rules
        self._emergency_guidance = emergency_guidance
        self._forbidden_patterns = forbidden_patterns
        self._guardrail = guardrail
        self._retrieve_snapshot = retrieve_snapshot
        self._served_body: object = None
        self._degraded = False

    # --- step 1 ---------------------------------------------------------

    def check_red_flags(self, request: AdvisoryRequest) -> Escalation | None:
        """Decide escalation before anything that can fail (Req 10).

        Takes no client and no model, so a red flag is recognised even when every retrieval is
        down — which is the whole reason this step is first.
        """
        return determine_escalation(
            utterance=request.utterance,
            prior_turns=request.prior_turns,
            rules=self._red_flag_rules,
            emergency_guidance=self._emergency_guidance,
        )

    # --- step 2 ---------------------------------------------------------

    def retrieve(
        self,
        request: AdvisoryRequest,  # noqa: ARG002 — the credential is already in the closure
        escalation: Escalation | None,  # noqa: ARG002 — step 2 runs either way, see below
    ) -> RetrievedValues:
        """Retrieve the snapshot for the envelope and the basis.

        RUNS EVEN WHEN ESCALATING, which is why the escalation argument is ignored rather than
        branched on: the emergency wording comes from Service 2's envelope where it can be
        retrieved, and A8a's configured fallback applies only when it cannot. An escalating turn
        skips generation, so no tool ever runs and this is the only retrieval it gets.

        THE BODY IS RECORDED, AND THE GLOSSARY IS WHY. I first thought a pre-fetch here must not
        make its values groundable — that the model could then quote a number without having
        called a tool. The definition settles it against that instinct: a Retrieved_Value is "a
        number or category that came from a Serving_Client RESPONSE in this turn", not one that
        came from a tool call. This fetch is a Serving_Client response in this turn, so its
        values qualify, and Req 7.1's user story names the real concern — a number "the API
        actually returned" rather than one "the model invented".

        Withholding them would have been actively wrong: the basis this service SHOWS the
        user is built from this body, so guidance quoting the sub-index it displays would be
        rejected as ungrounded, and Req 7.3 would then have it say a retrieved value was
        unavailable.

        NO TOOL CALL IS RECORDED, because none happened. `record_call` and `record_body` are
        separate methods, and the trajectory of Req 35.4 is what the MODEL called — how many
        times, in what order. Recording a call here would inflate that count with the service's
        own fetch and make the trajectory assertions describe something the model never did.
        """
        body = self._retrieve_snapshot()
        self._served_body = body
        self._degraded = body is None
        if body is not None:
            self._recorder.record_body(body)
        return self._recorder.values()

    # --- step 3 ---------------------------------------------------------

    def generate(
        self,
        request: AdvisoryRequest,  # noqa: ARG002 — the prompt is already in the closure
        escalation: Escalation | None,  # noqa: ARG002 — `run` never calls this when escalating
        retrieved: RetrievedValues,  # noqa: ARG002 — the AGENT retrieves, through its own tools
    ) -> str | None:
        """Obtain the guidance through the injected model call.

        THE THREE ARGUMENTS ARE DELIBERATELY UNUSED, and each for its own reason. The prompt and
        this turn's tools are bound into `invoke` by the composition root, because
        `obtain_structured_generation` takes a no-argument callable. The escalation cannot be
        anything but None here — `run` short-circuits — so branching on it would be dead code.
        And `retrieved` is what the accumulator holds SO FAR: the agent does its own retrieving
        during this call, through the five tools, so the interesting values arrive after this
        method starts rather than before it.
        """
        outcome = obtain_structured_generation(self._invoke)
        if not outcome or outcome.generation is None:
            self._degraded = True
            return None
        return outcome.generation.guidance

    # --- step 4 ---------------------------------------------------------

    def verify(
        self,
        generated: str | None,
        retrieved: RetrievedValues,
    ) -> VerificationVerdict:
        """Run the output checks on the generated text, and record the verdict.

        THIS IS NOT WHERE Req 31.5 ASKED FOR THE CHECKS, and the reason is measured rather than
        assumed. The requirement wants them "registered through Strands hooks... so no return
        path can bypass them". A probe against the pinned SDK shows a hook CANNOT do it: on
        `agent.structured_output`, `AfterInvocationEvent.result` is `None` — documented and
        observed — and `agent.messages` is empty, so the event exposes no generated text. Nor
        can the hook read it from this object: the hook fires INSIDE the model call, before the
        text has returned to the pipeline, so there is nothing here yet to read.

        A hook that cannot obtain the generation is a guarantee in name only. What actually
        makes these checks unbypassable is the Template Method: `run` fixes the order, raises
        before assembly on a failed verdict, is not overridden by this class, and an AST test
        reads it. The ledger keeps the fail-closed half — an absent verdict is a failure, not
        neutrality — so a future path that skipped verification still could not publish.

        `VerificationHook` remains the right shape for anything a hook CAN see, and is left in
        place for that. The divergence from Req 31.5's literal wording is recorded in tasks.md.

        A turn with nothing generated is verified vacuously: there is no model text to check,
        and the escalation wording is this service's own. The check list still names what ran,
        because a verdict with an empty list is refused at construction.
        """
        if generated is None:
            return VerificationVerdict(passed=True, checks=("no-generation",))

        checks: list[str] = []
        failures: list[str] = []

        checks.append("grounding")
        permitted = permitted_values(retrieved, constants=structural_constants())
        failures += [f"ungrounded:{value}" for value in ungrounded(generated, permitted)]

        checks.append("forbidden-claims")
        failures += [
            f"forbidden:{marker}"
            for marker in forbidden_matches(generated, self._forbidden_patterns)
        ]

        checks.append("medication-closure")
        failures += [
            f"unlisted-medication:{name}"
            for name in unlisted_medications(generated, retrieved.medications)
        ]

        checks.append("guardrail")
        result = self._guardrail.check(generated)
        if result.verdict is not GuardrailVerdict.PASSED:
            # Req 34.6 fails closed on UNAVAILABLE as well as INTERVENED: an operator must be
            # able to tell "the guardrail stopped this" from "the guardrail could not look", and
            # both withhold the text.
            failures.append(f"guardrail:{result.verdict.value}")
            failures += [f"guardrail-category:{name}" for name in result.categories]

        verdict = VerificationVerdict(
            passed=not failures, checks=tuple(checks), failures=tuple(failures)
        )
        self._ledger.record(verdict)
        return verdict

    # --- step 5 ---------------------------------------------------------

    def assemble(
        self,
        request: AdvisoryRequest,  # noqa: ARG002 — nothing from the request reaches the response
        escalation: Escalation | None,
        retrieved: RetrievedValues,  # noqa: ARG002 — the basis comes from the BODY, not numerals
        generated: str | None,
        verdict: VerificationVerdict,  # noqa: ARG002 — `run` already refused a failed verdict
    ) -> AdvisoryResponse:
        """Build the response, escalation first (Req 10.2's field order).

        `verdict` is unused because `run` raises before reaching assembly on anything but a
        pass, so re-checking it here would be a second gate that can only ever agree with the
        first. `retrieved` holds normalised numerals and the trajectory, which is the wrong
        shape for the basis — that is assembled from the served body this turn's step 2 kept.
        """
        envelope = self._envelope()
        answered_at = self._clock.now()

        if escalation is not None:
            return escalating_response(
                markers=escalation.markers,
                envelope=envelope,
                answered_at=answered_at,
                degraded=self._degraded,
            )

        # `release` rather than the local variable: the ledger raises if this text was never
        # verified, so an assembly reached on a bug cannot publish unverified guidance.
        guidance = self._ledger.release(generated) if generated is not None else None
        return AdvisoryResponse(
            escalation=None,
            guidance=guidance,
            basis=self._basis(),
            envelope=envelope,
            degraded=self._degraded,
            answered_at=answered_at,
        )

    # --- step 6 ---------------------------------------------------------

    def audit(
        self,
        request: AdvisoryRequest,  # noqa: ARG002 — identity, not the request, names the turn
        response: AdvisoryResponse,
        retrieved: RetrievedValues,  # noqa: ARG002 — the basis already carries what is recorded
        verdict: VerificationVerdict,
    ) -> None:
        """Record the turn (Req 20.4). Last, because it records the rest.

        `threshold_crossed` and `driving_pollutant` are DERIVED FROM THE BASIS rather than
        passed as constants. An earlier draft hardcoded `False` and `None`, which ruff's ARG002
        exposed by flagging `retrieved` as unused — the parameter was unused because the
        derivation was missing, not because the step had no need of the data.

        The threshold counts as crossed when Service 2 reported one AND the driving species'
        own sub-index reached it. Reading only the presence of a threshold would record every
        turn with a configured threshold as a crossing, the opposite of the audit's purpose.
        """
        basis = response.basis
        record = build_advice_record(
            identity=self._identity,
            turn_at=response.answered_at,
            route=ROUTE,
            escalation=response.escalation,
            threshold_crossed=self._threshold_crossed(basis),
            driving_pollutant=basis.driving_pollutant if basis else None,
            basis=basis,
            guardrail_rejected=not verdict.passed,
            rejection_category=verdict.failures[0] if verdict.failures else None,
        )
        self._audit_writer.write(record)

    @staticmethod
    def _threshold_crossed(basis: BasisSummary | None) -> bool:
        """True only when a reported threshold was actually reached by the driving species."""
        if basis is None or basis.threshold is None:
            return False
        driving = basis.driving_pollutant
        for species in basis.per_species:
            if species.species == driving and species.sub_index is not None:
                return species.sub_index >= basis.threshold
        return False

    # --- helpers --------------------------------------------------------

    def _envelope(self) -> GuardrailEnvelope:
        """The served envelope where one was retrieved, else the configured fallback (A8a)."""
        return resolve_envelope(
            served=self._served_envelope(),
            cached=None,
            configured_emergency_guidance=self._emergency_guidance,
        ).envelope

    def _served_envelope(self) -> GuardrailEnvelope | None:
        body = self._served_body
        if not isinstance(body, dict):
            return None
        wording = body.get("emergencyGuidance")
        if not isinstance(wording, str) or not wording.strip():
            return None
        return GuardrailEnvelope(emergency_guidance=wording)

    def _basis(self) -> BasisSummary | None:
        body = self._served_body
        return assemble_basis(body) if isinstance(body, dict) else None

    def grounding_failures(self, text: str) -> tuple[str, ...]:
        """The ungrounded numerals in `text`, for the hook's check callable.

        Exposed rather than private because the hook is constructed with a callable that must
        reach this turn's retrieved values, and the recorder is held here.
        """
        permitted = permitted_values(
            self._recorder.values(), constants=structural_constants()
        )
        return ungrounded(text, permitted)


def build_turn_runner(
    *,
    make_pipeline: Callable[[AdvisoryRequest], AdvisoryTurnPipeline],
) -> Callable[[AdvisoryRequest], AdvisoryResponse]:
    """A `run_turn` for `build_app`, constructing a FRESH pipeline per turn.

    The freshness is the point, and it is what makes the runner concurrency-safe: the recorder,
    the ledger and the retrieval tools are per-turn state, so two concurrent turns sharing one
    pipeline would cross-contaminate all three. Nothing is shared, so nothing needs a lock.
    """

    def run_turn(request: AdvisoryRequest) -> AdvisoryResponse:
        return make_pipeline(request).run(request)

    return run_turn


__all__ = ["ROUTE", "AdvisoryTurnPipeline", "build_turn_runner"]
