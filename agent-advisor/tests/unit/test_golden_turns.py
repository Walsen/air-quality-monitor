"""Golden turns and trajectory assertions (task 19.2, Reqs 35.3, 35.4, 35.5, 35.6).

TWO KINDS OF ASSERTION LIVE HERE, and the design draws the line between them.

(a) GOLDEN TURNS assert the STRUCTURED outcome of a turn — the escalation, the basis, the
    degraded flag, and the guardrail/verification verdict — and NEVER the prose (Req 35.3).
    Prose is the part a model may legitimately vary, so pinning it would make a test fail on a
    reworded-but-correct answer; the structured fields are the contract. Each golden turn pairs
    a scripted retrieval and utterance with the fields it must produce, driven through the real
    `AdvisoryTurnPipeline`.

(b) TRAJECTORY assertions assert WHICH tools a turn called, in what ORDER, and how many TIMES
    (Req 35.4), by driving a REAL Strands `Agent` through the `ScriptedModel` with the
    production retrieval tools bound to a recorder. "A turn producing plausible text while
    retrieving nothing fails" — so a turn making a conditions claim must have called the
    air-quality retrieval (Req 35.5), and a repeated call shows as multiple trajectory entries
    rather than being collapsed.

Req 35.6 is covered under the golden turns: a red-flag utterance escalates, and it STILL
escalates when the Serving_Client fails, because red-flag recognition runs first and depends
on nothing.

All offline: the pipeline runs against a scripted `invoke`, and the trajectory turns run
against `ScriptedModel`, so no live model and no network are involved.
"""

from __future__ import annotations

import datetime as dt

from strands import Agent

from aqm_advisor.adapters.local import (
    InMemoryAdviceAuditStore,
    LocalGuardrailChecker,
    ScriptedServingClient,
    canned_air_quality,
)
from aqm_advisor.adapters.model.scripted import ScriptedModel, calls, says
from aqm_advisor.agent.advisory import AdvisoryTurnPipeline
from aqm_advisor.agent.audit import AuditWriter
from aqm_advisor.agent.generation import ModelGeneration
from aqm_advisor.agent.tools import RetrievalRecorder, build_retrieval_tools
from aqm_advisor.agent.verification import VerificationLedger
from aqm_advisor.domain.grounding import permitted_values, structural_constants, ungrounded
from aqm_advisor.domain.idempotency import TurnIdentity
from aqm_advisor.domain.models import AdvisoryRequest
from aqm_advisor.domain.redflag import DEFAULT_RED_FLAG_RULES, RedFlagRule
from aqm_advisor.observability.logging import get_logger
from aqm_advisor.ports.clock import FixedClock
from aqm_advisor.ports.protocols import GuardrailVerdict

_AT = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
_EMERGENCY = "If you are struggling to breathe, call your local emergency number."
_IDENTITY = TurnIdentity(user_id="u-gold", session_id="s" * 40)
_CREDENTIAL = "SENTINEL-CRED-golden-do-not-log"

# The default red-flag rules plus the explicit `cannot breathe` phrase, so a red-flag golden
# turn is judged by production recognition.
_RULES: tuple[RedFlagRule, ...] = DEFAULT_RED_FLAG_RULES


def _request(utterance: str) -> AdvisoryRequest:
    return AdvisoryRequest(utterance=utterance, credential="a-credential")  # type: ignore[arg-type]


def _pipeline(
    *,
    served: object,
    guidance: str,
    store: InMemoryAdviceAuditStore | None = None,
) -> AdvisoryTurnPipeline:
    def invoke() -> ModelGeneration:
        return ModelGeneration(guidance=guidance)

    return AdvisoryTurnPipeline(
        recorder=RetrievalRecorder(),
        ledger=VerificationLedger(),
        invoke=invoke,
        audit_writer=AuditWriter(
            store=store or InMemoryAdviceAuditStore(), logger=get_logger("test.golden")
        ),
        clock=FixedClock(_AT),
        identity=_IDENTITY,
        red_flag_rules=_RULES,
        emergency_guidance=_EMERGENCY,
        forbidden_patterns=None,  # type: ignore[arg-type]
        guardrail=LocalGuardrailChecker(),
        retrieve_snapshot=lambda: served,
        retrieve_profile=lambda: None,
    )


# ======================================================================
# (a) GOLDEN TURNS — structured outcome, never the prose (Req 35.3)
# ======================================================================


def test_a_normal_moderate_turn_advises_without_escalating() -> None:
    """A moderate snapshot: no escalation, a basis is present, and the turn is not degraded.

    The prose is NOT asserted. The guidance says nothing quantitative it did not retrieve, so
    it grounds; the structured fields are what the golden turn pins.
    """
    served = canned_air_quality(sub_index=68, band="Moderate")
    pipeline = _pipeline(
        served=served,
        guidance="Conditions are moderate today; consider a quieter route away from traffic.",
    )

    response = pipeline.run(_request("How is the air today?"))

    assert response.escalation is None
    assert response.basis is not None
    assert response.basis.driving_pollutant == "PM25"
    assert response.degraded is False
    assert response.guidance is not None  # a guidance WAS published


def test_a_red_flag_turn_escalates_and_never_calls_the_model() -> None:
    """A red-flag utterance escalates, publishes no advisory guidance, and skips generation.

    Mirrors `test_pipeline.py::test_an_escalating_turn_never_calls_the_model` as a golden case:
    the structured outcome is escalation set, guidance None. Generation is proven unreached by
    scripting `invoke` to raise — a turn that reached it would fail loudly rather than quietly.
    """
    called: list[str] = []

    def invoke() -> ModelGeneration:
        called.append("invoked")
        raise AssertionError("a red-flag turn must never reach the model")

    pipeline = AdvisoryTurnPipeline(
        recorder=RetrievalRecorder(),
        ledger=VerificationLedger(),
        invoke=invoke,
        audit_writer=AuditWriter(
            store=InMemoryAdviceAuditStore(), logger=get_logger("test.golden")
        ),
        clock=FixedClock(_AT),
        identity=_IDENTITY,
        red_flag_rules=_RULES,
        emergency_guidance=_EMERGENCY,
        forbidden_patterns=None,  # type: ignore[arg-type]
        guardrail=LocalGuardrailChecker(),
        retrieve_snapshot=lambda: canned_air_quality(),
        retrieve_profile=lambda: None,
    )

    response = pipeline.run(_request("I cannot breathe and my lips are going blue."))

    assert called == []
    assert response.escalation is not None
    assert response.escalation.markers  # names what triggered it (Req 10.5)
    assert response.guidance is None


def test_a_serving_failure_turn_is_degraded_with_no_basis() -> None:
    """Req 35.6 companion: when the snapshot retrieval fails, the turn degrades cleanly.

    `retrieve_snapshot` returns None (the serving failure), so the turn is degraded, carries no
    basis, and — with nothing retrieved — the model's guidance is withheld rather than being
    grounded against an empty permitted set. The structured fields are the contract.
    """
    pipeline = _pipeline(
        served=None,
        guidance="Conditions look manageable; keep to indoor routes if you can.",
    )

    response = pipeline.run(_request("How is the air today?"))

    assert response.degraded is True
    assert response.basis is None
    assert response.escalation is None


def test_a_red_flag_still_escalates_when_the_serving_client_fails() -> None:
    """Req 35.6: escalation STILL occurs when the Serving_Client fails.

    Red-flag recognition runs first and depends on nothing, so a snapshot that could not be
    retrieved (None) does not prevent the escalation. The structured outcome is escalation set
    AND degraded true — the turn both directs onward and reports that retrieval failed.
    """
    pipeline = _pipeline(
        served=None,  # the Serving_Client failed
        guidance="unused — the model is not reached on an escalating turn",
    )

    response = pipeline.run(_request("I cannot breathe."))

    assert response.escalation is not None, "a red flag must escalate even with no snapshot"
    assert response.escalation.markers
    assert response.guidance is None
    # The envelope falls back to the configured emergency wording, since none was served.
    assert response.envelope.emergency_guidance == _EMERGENCY
    assert response.degraded is True


def test_the_guardrail_verdict_field_is_asserted_not_the_prose() -> None:
    """Req 35.3 names the guardrail verdict among the structured fields.

    Asserted directly against the local checker: a clean generation passes, and a dosing
    generation intervenes. The verdict names the KIND, never the offending text (Req 8.6).
    """
    checker = LocalGuardrailChecker()

    clean = checker.check("Conditions are moderate; a quieter route may help.")
    assert clean.verdict is GuardrailVerdict.PASSED

    dosing = checker.check("Take two puffs of your inhaler now.")
    assert dosing.verdict is GuardrailVerdict.INTERVENED
    assert dosing.categories  # names the category
    assert not any("inhaler" in c for c in dosing.categories), "a verdict quoted the text"


# ======================================================================
# (b) TRAJECTORY — which tools, in what order, how many times (Req 35.4, 35.5)
# ======================================================================


def _agent_and_recorder(
    model: ScriptedModel,
) -> tuple[Agent, RetrievalRecorder]:
    """A real Agent wired to the production tools over a scripted serving client.

    The recorder is the per-turn trajectory: `recorder.values().tool_calls` is the ordered
    record Req 35.4 asserts over, written by the real tools as the Agent dispatches them.
    """
    recorder = RetrievalRecorder()
    tools = build_retrieval_tools(
        client=ScriptedServingClient(),
        credential=_CREDENTIAL,
        recorder=recorder,
        clock=FixedClock(_AT),
        identity=_IDENTITY,
    )
    agent = Agent(model=model, tools=list(tools))
    return agent, recorder


def _names(recorder: RetrievalRecorder) -> tuple[str, ...]:
    return tuple(call.name for call in recorder.values().tool_calls)


async def test_a_conditions_turn_calls_air_quality_first() -> None:
    """Req 35.5: a turn answering about conditions has called the air-quality retrieval.

    The golden trajectory is `air_quality` then a text turn. A turn that produced a plausible
    conditions answer with an EMPTY trajectory (the negative below) is a failing shape.
    """
    model = ScriptedModel(
        [
            calls("air_quality"),
            says("Your nearest sensor is in the Moderate band today."),
        ]
    )
    agent, recorder = _agent_and_recorder(model)

    await agent.invoke_async("How is the air today?")

    assert _names(recorder) == ("air_quality",), _names(recorder)


async def test_a_turn_that_retrieved_nothing_cannot_ground_a_conditions_claim() -> None:
    """The failing shape Req 35.5 targets: plausible text, empty trajectory, invented number.

    The Agent answers straight away with a quantitative conditions claim, calling no tool. Its
    trajectory is empty, so the permitted set is empty and the quoted number is ungrounded —
    which the pipeline's verification turns into a withheld generation. A concrete golden
    counterpart to Property 19.
    """
    model = ScriptedModel([says("The air quality index is 42 right now, so you are fine.")])
    agent, recorder = _agent_and_recorder(model)

    await agent.invoke_async("How is the air today?")

    assert _names(recorder) == (), "the turn retrieved nothing, as the failing shape requires"
    permitted = permitted_values(recorder.values(), constants=structural_constants())
    failures = ungrounded("The air quality index is 42 right now.", permitted)
    assert failures, "a conditions number with nothing retrieved must be ungrounded"
    assert "42" in failures


async def test_the_trajectory_preserves_order_across_several_tools() -> None:
    """Req 35.4: WHICH tools and in what ORDER — air_quality, then history, then profile_get.

    History needs a site from a snapshot retrieved this turn, so the order is not incidental:
    air_quality must precede history for history to succeed, and the trajectory records the
    sequence the model actually drove.
    """
    model = ScriptedModel(
        [
            calls("air_quality"),
            calls("history", input_json='{"days": 7}'),
            calls("profile_get"),
            says("Conditions have been steady over the past week."),
        ]
    )
    agent, recorder = _agent_and_recorder(model)

    await agent.invoke_async("How has the air been this week?")

    assert _names(recorder) == ("air_quality", "history", "profile_get"), _names(recorder)


async def test_a_repeated_tool_call_shows_as_multiple_trajectory_entries() -> None:
    """Req 35.4 / the design's note: a repeated call is NOT collapsed.

    The model calls `air_quality` twice. The second is refused by the once-per-turn bound, but
    the ATTEMPT is still recorded — the trajectory is a record of what the model did, and
    "how many times" is one of the questions Req 35.4 asks. Collapsing repeats would hide a
    runaway that Requirement 22's bounds exist to catch.
    """
    model = ScriptedModel(
        [
            calls("air_quality"),
            calls("air_quality"),
            says("Conditions are moderate today."),
        ]
    )
    agent, recorder = _agent_and_recorder(model)

    await agent.invoke_async("How is the air, and again please?")

    names = _names(recorder)
    assert names == ("air_quality", "air_quality"), names
    assert names.count("air_quality") == 2, "a repeated call must not be collapsed"
