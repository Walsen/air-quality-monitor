"""The production advisory pipeline (task 21.1).

WHAT IS WORTH ASSERTING HERE, given `tests/unit/test_pipeline.py` already covers the Template
Method's ORDER against a spy subclass. These tests are about the production steps: that step 2
records a body without inflating the trajectory, that an unverified generation cannot be
published, that an escalating turn still gets an envelope, and that two turns share nothing.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_advisor.adapters.local import InMemoryAdviceAuditStore
from aqm_advisor.agent.advisory import AdvisoryTurnPipeline, build_turn_runner
from aqm_advisor.agent.audit import AuditWriter
from aqm_advisor.agent.generation import ModelGeneration
from aqm_advisor.agent.tools import RetrievalRecorder
from aqm_advisor.agent.verification import VerificationLedger, VerificationVerdict
from aqm_advisor.domain.idempotency import TurnIdentity
from aqm_advisor.domain.models import AdvisoryRequest
from aqm_advisor.domain.redflag import RedFlagRule
from aqm_advisor.observability.logging import get_logger
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
    guidance: str = "Conditions are moderate; consider a quieter route.",
    store: InMemoryAdviceAuditStore | None = None,
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
        retrieve_snapshot=lambda: served,
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


def test_a_failed_retrieval_degrades_rather_than_raising() -> None:
    # Req 21.1 wants a degraded answer naming the kind, not a failed turn.
    ledger = VerificationLedger()
    ledger.record(VerificationVerdict(passed=True, checks=("grounding",)))
    pipeline = _pipeline(ledger=ledger, served=None)

    response = pipeline.run(_request())

    assert response.degraded is True
    assert response.basis is None


# --- the unverified generation cannot be published ---------------------


def test_a_turn_with_no_recorded_verdict_fails_verification() -> None:
    # Absence is failure, not neutrality. A turn whose hook never fired has no verdict, and
    # treating that as a pass is exactly the bypass Req 31.5 forbids.
    pipeline = _pipeline(served=_SERVED)
    verdict = pipeline.verify("some guidance", RetrievalRecorder().values())

    assert verdict.passed is False
    assert "hook did not run" in " ".join(verdict.failures)


def test_run_refuses_to_assemble_without_a_verdict() -> None:
    # End to end through the Template Method: `run` raises BEFORE assembly, so an unverified
    # generation is never built into a response.
    pipeline = _pipeline(served=_SERVED)
    with pytest.raises(RuntimeError, match="not verified"):
        pipeline.run(_request())


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
