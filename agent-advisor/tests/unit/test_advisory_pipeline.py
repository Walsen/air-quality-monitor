"""The production advisory pipeline (task 21.1).

WHAT IS WORTH ASSERTING HERE, given `tests/unit/test_pipeline.py` already covers the Template
Method's ORDER against a spy subclass. These tests are about the production steps: that step 2
records a body without inflating the trajectory, that an unverified generation cannot be
published, that an escalating turn still gets an envelope, and that two turns share nothing.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from aqm_advisor.adapters.local import InMemoryAdviceAuditStore, LocalGuardrailChecker
from aqm_advisor.agent.advisory import AdvisoryTurnPipeline, build_turn_runner
from aqm_advisor.agent.audit import AuditWriter
from aqm_advisor.agent.generation import ModelGeneration
from aqm_advisor.agent.tools import RetrievalRecorder
from aqm_advisor.agent.verification import VerificationLedger, VerificationVerdict
from aqm_advisor.domain.idempotency import TurnIdentity
from aqm_advisor.domain.models import AdvisoryRequest
from aqm_advisor.domain.redflag import RedFlagRule
from aqm_advisor.observability.logging import EventLogger, get_logger
from aqm_advisor.ports.clock import FixedClock

_AT = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
_EMERGENCY = "If you are struggling to breathe, call 999."
_IDENTITY = TurnIdentity(user_id="u-1", session_id="s" * 40)
_RULES = (RedFlagRule(marker="severe_breathlessness", patterns=("cannot breathe",)),)

_SERVED: dict[str, object] = {
    "nearestSensors": [{"siteCode": "AQM001", "subIndex": 88, "distanceKm": 0.4}],
    "emergencyGuidance": "Served wording: call your local emergency number.",
}


def _request(utterance: str = "How is the air today?") -> AdvisoryRequest:
    return AdvisoryRequest(utterance=utterance, credential="a-credential")  # type: ignore[arg-type]


def _pipeline(
    *,
    ledger: VerificationLedger | None = None,
    recorder: RetrievalRecorder | None = None,
    served: object = None,
    profile: object = None,
    guidance: str = "Conditions are moderate; consider a quieter route.",
    store: InMemoryAdviceAuditStore | None = None,
    guardrail: LocalGuardrailChecker | None = None,
    logger: EventLogger | None = None,
) -> AdvisoryTurnPipeline:
    def invoke() -> ModelGeneration:
        return ModelGeneration(guidance=guidance)

    return AdvisoryTurnPipeline(
        recorder=recorder or RetrievalRecorder(),
        ledger=ledger or VerificationLedger(),
        invoke=invoke,
        audit_writer=AuditWriter(
            store=store or InMemoryAdviceAuditStore(), logger=get_logger("test.advisory")
        ),
        clock=FixedClock(_AT),
        identity=_IDENTITY,
        red_flag_rules=_RULES,
        emergency_guidance=_EMERGENCY,
        forbidden_patterns=(),
        guardrail=guardrail or LocalGuardrailChecker(),
        retrieve_snapshot=lambda: served,
        retrieve_profile=lambda: profile,
        logger=logger,
    )


# --- step 2: a Serving_Client response, not a tool call ----------------


def test_the_prefetched_body_becomes_groundable() -> None:
    # The glossary decides this: a Retrieved_Value is "a number or category that came from a
    # Serving_Client RESPONSE in this turn". Step 2's fetch is one, so its numerals qualify —
    # and the basis this service SHOWS the user is built from the same body, so withholding
    # them would reject guidance quoting a value the user can see.
    recorder = RetrievalRecorder()
    pipeline = _pipeline(recorder=recorder, served=_SERVED)
    retrieved = pipeline.retrieve(_request(), None)

    assert "88" in retrieved.numerals


def test_the_prefetch_records_no_tool_call() -> None:
    # Req 35.4's trajectory is what the MODEL called, and how many times. Recording a call for
    # the service's own fetch would inflate that count and make the trajectory assertions
    # describe something the model never did.
    recorder = RetrievalRecorder()
    pipeline = _pipeline(recorder=recorder, served=_SERVED)
    retrieved = pipeline.retrieve(_request(), None)

    assert retrieved.tool_calls == ()


def test_step_two_prefetches_the_profile_so_medications_are_retrieved_this_turn() -> None:
    # The medication-closure check permits naming a medication only if it is in the profile
    # RETRIEVED THIS TURN. A live model does not reliably call profile_get, so the profile is
    # pre-fetched here exactly as the air-quality snapshot is — and by the same glossary rule
    # (a value from a Serving_Client response in this turn), its medications qualify. The
    # guarantee is unchanged: a medication NOT in the profile still fails the check. The
    # trajectory stays clean because a pre-fetch records no tool call.
    recorder = RetrievalRecorder()
    profile = {
        "condition": "asthma",
        "medications": [{"name": "salbutamol", "role": "reliever"}],
    }
    pipeline = _pipeline(recorder=recorder, served=_SERVED, profile=profile)
    retrieved = pipeline.retrieve(_request(), None)

    assert "salbutamol" in retrieved.medications
    assert retrieved.tool_calls == (), "the profile pre-fetch must not inflate the trajectory"


def test_a_failed_retrieval_degrades_rather_than_raising() -> None:
    # Req 21.1 wants a degraded answer naming the kind, not a failed turn.
    ledger = VerificationLedger()
    ledger.record(VerificationVerdict(passed=True, checks=("grounding",)))
    pipeline = _pipeline(ledger=ledger, served=None)

    response = pipeline.run(_request())

    assert response.degraded is True
    assert response.basis is None


# --- the unverified generation cannot be published ---------------------


def test_ungrounded_guidance_fails_verification() -> None:
    # The check now runs HERE rather than in a hook, because a probe showed the hook cannot see
    # the text: on `agent.structured_output`, AfterInvocationEvent.result is None and
    # agent.messages is empty. See the module docstring.
    pipeline = _pipeline(served=_SERVED, guidance="The index is 4242 right now.")
    retrieved = pipeline.retrieve(_request(), None)
    verdict = pipeline.verify("The index is 4242 right now.", retrieved)

    assert verdict.passed is False
    assert "ungrounded:4242" in verdict.failures, verdict.failures
    assert "grounding" in verdict.checks


def test_a_value_retrieved_during_generation_grounds_the_answer() -> None:
    # THE HISTORY-DEGRADATION BUG. The agent records its history/air-quality bodies DURING
    # generation (step 3), AFTER `retrieve` (step 2) captured the snapshot. If verify checks
    # grounding against that pre-generation snapshot, a reading the model correctly quoted from
    # history reads as ungrounded and the whole turn degrades. verify must use the recorder's
    # LIVE values. Here the model records a history body carrying 138 and then quotes 138.
    recorder = RetrievalRecorder()

    def invoke_recording_history() -> ModelGeneration:
        # Simulate the agent's history tool running inside the model call.
        recorder.record_body(
            {"readings": [{"correctedValue": 138.0, "species": "PM25"}]}
        )
        return ModelGeneration(guidance="One recent reading was 138.")

    pipeline = AdvisoryTurnPipeline(
        recorder=recorder,
        ledger=VerificationLedger(),
        invoke=invoke_recording_history,
        audit_writer=AuditWriter(
            store=InMemoryAdviceAuditStore(), logger=get_logger("test.advisory")
        ),
        clock=FixedClock(_AT),
        identity=_IDENTITY,
        red_flag_rules=_RULES,
        emergency_guidance=_EMERGENCY,
        forbidden_patterns=(),
        guardrail=LocalGuardrailChecker(),
        retrieve_snapshot=lambda: _SERVED,
        retrieve_profile=lambda: None,
    )
    # run() must NOT raise: 138 was retrieved this turn (during generation), so it grounds.
    response = pipeline.run(_request())
    assert response is not None
    assert response.degraded is False


def test_run_refuses_to_assemble_an_ungrounded_generation() -> None:
    # End to end through the Template Method: `run` raises BEFORE assembly, so an unverified
    # generation is never built into a response. This is what makes the checks unbypassable now
    # that they are not in a hook — the order is fixed and `run` is not overridden.
    pipeline = _pipeline(served=_SERVED, guidance="The index is 4242 right now.")
    with pytest.raises(RuntimeError, match="not verified"):
        pipeline.run(_request())


def test_a_guardrail_intervention_fails_verification() -> None:
    # Req 34.6 withholds on an intervention, and the verdict names the KIND, not the text.
    pipeline = _pipeline(
        served=_SERVED,
        guidance="Take two puffs of your inhaler now.",
        guardrail=LocalGuardrailChecker(),
    )
    retrieved = pipeline.retrieve(_request(), None)
    verdict = pipeline.verify("Take two puffs of your inhaler now.", retrieved)

    assert verdict.passed is False
    assert "guardrail" in verdict.checks
    assert not any("inhaler" in failure for failure in verdict.failures), (
        "a failure quoted the offending text, which Req 8.6 forbids"
    )


def test_an_unavailable_guardrail_also_withholds() -> None:
    # Req 34.6 fails CLOSED on UNAVAILABLE as well, and keeps it distinct from INTERVENED so an
    # operator can tell "the guardrail stopped this" from "the guardrail could not look".
    pipeline = _pipeline(
        served=_SERVED,
        guidance="Conditions are moderate.",
        guardrail=LocalGuardrailChecker(unavailable=True),
    )
    retrieved = pipeline.retrieve(_request(), None)
    verdict = pipeline.verify("Conditions are moderate.", retrieved)

    assert verdict.passed is False
    assert any("unavailable" in failure for failure in verdict.failures), verdict.failures


def test_the_verdict_names_every_check_that_ran() -> None:
    # A verdict with an empty check list is refused at construction, and a PASS naming
    # nothing is indistinguishable from a verifier that did nothing.
    pipeline = _pipeline(served=_SERVED)
    retrieved = pipeline.retrieve(_request(), None)
    verdict = pipeline.verify("Conditions are moderate today.", retrieved)

    assert verdict.checks == (
        "grounding",
        "forbidden-claims",
        "medication-closure",
        "guardrail",
    )


def test_a_verified_turn_answers_with_the_guidance() -> None:
    ledger = VerificationLedger()
    ledger.record(VerificationVerdict(passed=True, checks=("grounding", "forbidden")))
    pipeline = _pipeline(ledger=ledger, served=_SERVED, guidance="Try a quieter route.")

    response = pipeline.run(_request())

    assert response.guidance == "Try a quieter route."
    assert response.escalation is None
    assert response.answered_at == _AT


# --- escalation still gets an envelope --------------------------------


def test_an_escalating_turn_never_calls_the_model() -> None:
    called: list[str] = []

    def invoke() -> ModelGeneration:
        called.append("invoked")
        raise AssertionError("an escalating turn must not reach the model")

    pipeline = AdvisoryTurnPipeline(
        recorder=RetrievalRecorder(),
        ledger=VerificationLedger(),
        invoke=invoke,
        audit_writer=AuditWriter(
            store=InMemoryAdviceAuditStore(), logger=get_logger("test.advisory")
        ),
        clock=FixedClock(_AT),
        identity=_IDENTITY,
        red_flag_rules=_RULES,
        emergency_guidance=_EMERGENCY,
        forbidden_patterns=(),
        guardrail=LocalGuardrailChecker(),
        retrieve_snapshot=lambda: _SERVED,
    )

    response = pipeline.run(_request("I cannot breathe"))

    assert called == []
    assert response.escalation is not None
    assert response.guidance is None


def test_an_escalating_turn_prefers_the_served_emergency_wording() -> None:
    # Step 2 runs even when escalating precisely so this is possible: A8a's configured fallback
    # applies only where the envelope could NOT be retrieved.
    pipeline = _pipeline(served=_SERVED)
    response = pipeline.run(_request("I cannot breathe"))

    assert response.envelope.emergency_guidance == _SERVED["emergencyGuidance"]


def test_an_escalating_turn_falls_back_when_nothing_was_served() -> None:
    pipeline = _pipeline(served=None)
    response = pipeline.run(_request("I cannot breathe"))

    assert response.envelope.emergency_guidance == _EMERGENCY


# --- the audit record is derived, not stubbed -------------------------


def test_the_audit_record_names_the_driving_pollutant_from_the_basis() -> None:
    # The derivation ruff's ARG002 exposed: an earlier draft passed `driving_pollutant=None` and
    # `threshold_crossed=False` as constants. Asserting the RECORD's field rather than merely
    # that something was written — a non-empty store would pass against the stub too.
    store = InMemoryAdviceAuditStore()
    ledger = VerificationLedger()
    ledger.record(VerificationVerdict(passed=True, checks=("grounding",)))
    # The shape `assemble_basis` actually reads, taken from the existing basis tests rather than
    # guessed: `drivingPollutant` and `measurements` live on the SENSOR, and the threshold comes
    # from `personalized.escalationSubIndex`. My first attempt put them in the `basis` block and
    # the test skipped itself, which is how I found out.
    served = {
        "nearestSensors": [
            {
                "siteCode": "AQM001",
                "distanceKm": 0.4,
                "drivingPollutant": "PM25",
                "measurements": [
                    {
                        "species": "PM25",
                        "subIndex": 88,
                        "band": "Moderate",
                        "confidence": "high",
                    }
                ],
            }
        ],
        "personalized": {"escalationSubIndex": 70, "thresholdSource": "sensitivity_level"},
        "basis": {"breakpointTable": "epa-2024-05-06"},
    }
    pipeline = _pipeline(ledger=ledger, served=served, store=store)

    response = pipeline.run(_request())

    assert response.basis is not None, "the body shape yielded no basis; fix the fixture"
    assert response.basis.driving_pollutant == "PM25"
    assert store.records, "the turn recorded nothing"
    record = store.records[0]
    assert record.driving_pollutant == "PM25"
    # 88 >= 70, so the threshold was actually reached rather than merely present.
    assert record.threshold_crossed is True


def test_a_threshold_present_but_not_reached_is_not_recorded_as_crossed() -> None:
    # The other direction, and the one a stub would pass. Reading only the PRESENCE of a
    # threshold would record every turn with a configured threshold as a crossing.
    store = InMemoryAdviceAuditStore()
    ledger = VerificationLedger()
    ledger.record(VerificationVerdict(passed=True, checks=("grounding",)))
    served = {
        "nearestSensors": [
            {
                "siteCode": "AQM001",
                "distanceKm": 0.4,
                "drivingPollutant": "PM25",
                "measurements": [
                    {"species": "PM25", "subIndex": 40, "band": "Good", "confidence": "high"}
                ],
            }
        ],
        "personalized": {"escalationSubIndex": 70, "thresholdSource": "sensitivity_level"},
        "basis": {"breakpointTable": "epa-2024-05-06"},
    }
    pipeline = _pipeline(ledger=ledger, served=served, store=store)

    pipeline.run(_request())

    assert store.records[0].threshold_crossed is False


# --- two turns share nothing ------------------------------------------


def test_each_turn_gets_a_fresh_pipeline() -> None:
    # The concurrency-safety claim, asserted rather than asserted-in-prose: a reused recorder
    # leaks across turns AND users, and a reused ledger would report the previous turn's pass.
    made: list[AdvisoryTurnPipeline] = []

    def make(request: AdvisoryRequest) -> AdvisoryTurnPipeline:
        ledger = VerificationLedger()
        ledger.record(VerificationVerdict(passed=True, checks=("grounding",)))
        pipeline = _pipeline(ledger=ledger, served=_SERVED)
        made.append(pipeline)
        return pipeline

    run_turn = build_turn_runner(make_pipeline=make)
    run_turn(_request())
    run_turn(_request())

    assert len(made) == 2
    assert made[0] is not made[1]


# --- a withheld generation is observable (why it degraded) --------------


def test_a_failed_verification_logs_the_failure_kinds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The intermittent "degraded" turn: the model produced text that failed grounding, the
    # pipeline raised, and the top-level boundary logged only "RuntimeError" — no reason. An
    # operator could not tell a grounding miss from a guardrail block. verify() now emits a
    # WARNING naming which check withheld the generation, so the reason is in the logs.
    caplog.set_level(logging.WARNING)
    pipeline = _pipeline(
        served=_SERVED,
        guidance="The index is 4242 right now.",
        logger=get_logger("test.advisory.withheld"),
    )
    retrieved = pipeline.retrieve(_request(), None)
    verdict = pipeline.verify("The index is 4242 right now.", retrieved)

    assert verdict.passed is False
    events = [r for r in caplog.records if r.getMessage() == "verification_withheld_generation"]
    assert len(events) == 1, [r.getMessage() for r in caplog.records]
    kinds = getattr(events[0], "failure_kinds", None)
    assert kinds is not None and "ungrounded" in kinds, kinds


def test_the_withheld_log_never_carries_the_offending_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # §6: the failures carry the numeral (ungrounded:4242); the log must not. The rendered line
    # names the KIND, never the value.
    caplog.set_level(logging.WARNING)
    from aqm_advisor.observability.logging import _JsonFormatter

    pipeline = _pipeline(
        served=_SERVED,
        guidance="The index is 4242 right now.",
        logger=get_logger("test.advisory.withheld2"),
    )
    retrieved = pipeline.retrieve(_request(), None)
    pipeline.verify("The index is 4242 right now.", retrieved)

    events = [r for r in caplog.records if r.getMessage() == "verification_withheld_generation"]
    line = _JsonFormatter().format(events[0])
    assert "4242" not in line, line


def test_a_passing_verification_logs_no_withheld_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Non-vacuity: the log fires only on a WITHHELD generation, never on a clean one.
    caplog.set_level(logging.WARNING)
    recorder = RetrievalRecorder()
    pipeline = _pipeline(
        recorder=recorder,
        served=_SERVED,
        guidance="Conditions near you read a sub-index of 88; consider a quieter route.",
        logger=get_logger("test.advisory.clean"),
    )
    retrieved = pipeline.retrieve(_request(), None)
    verdict = pipeline.verify(
        "Conditions near you read a sub-index of 88; consider a quieter route.", retrieved
    )
    assert verdict.passed is True
    withheld = [
        r for r in caplog.records if r.getMessage() == "verification_withheld_generation"
    ]
    assert withheld == []


def test_verify_without_a_logger_still_works() -> None:
    # The logger is OPTIONAL: a pipeline built without one (every existing call site) must still
    # verify and withhold exactly as before — the log is an addition, never a dependency.
    pipeline = _pipeline(served=_SERVED, guidance="The index is 4242 right now.")
    retrieved = pipeline.retrieve(_request(), None)
    verdict = pipeline.verify("The index is 4242 right now.", retrieved)
    assert verdict.passed is False
