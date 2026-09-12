"""Property 19: claims require retrieval.

Feature: agent-advisor-service, Property 19

*For all* turns whose guidance makes a claim about current conditions, the trajectory
contains an air-quality retrieval. Validates Requirements 2.1 and 35.5.

THE DESIGN STATES THE MECHANISM: "A turn producing plausible text while retrieving nothing
fails Property 19." So this is a trajectory assertion, and the decidable part comes from the
production grounding functions rather than a text heuristic invented here. With no retrieval
at all the permitted set is empty, so any numeral the guidance quotes is ungrounded, which
Req 7.2 turns into a generation that is not returned. Retrieving nothing and sounding
confident is not a style problem; it is a failed verification.

WHAT GROUNDING PROVES, AND WHAT IT DOES NOT — measured, not assumed. My first draft of this
file claimed grounding enforced the AIR-QUALITY call specifically. It does not, and a probe
settled it: a turn that called only `profile_get`, whose body carried a personal threshold
of 87, grounds the sentence "the air quality index is 87 right now" with no air-quality
retrieval anywhere in its trajectory.

That is not a defect in grounding. Req 7.1 asks that a quoted value be one "this turn
actually retrieved", and the union of the turn's retrievals is exactly that.

So this file asserts the half that is decidable — a conditions claim requires SOME retrieval
carrying the value — and pins the residual gap as a test of its own rather than leaving a
reader to trip over it. The tool-IDENTITY half of Property 19 belongs to the golden-turn
trajectory assertions of Req 35.4, which need a composed pipeline and so arrive with task 21.

WHY THE PROPERTY IS NOT FRAMED OVER A CLAIM DETECTOR. No production function in this service
decides "this sentence makes a claim about conditions", and writing one HERE would mean the
property tested my heuristic rather than the service. What does exist is `numerals`,
`permitted_values` and `ungrounded`, and a quantitative claim is exactly what they decide.

KNOWN LIMITATION, RECORDED RATHER THAN HIDDEN — the same shape as the one
`domain/grounding.py` already records for number words. A QUALITATIVE claim carries no
digits: "the air is poor today" states something about current conditions that this property
cannot see, because grounding cannot see it either. Covering it needs a condition-vocabulary
detector, which is a design decision rather than a test one, and inventing it in a test file
is how a property comes to assert something the service never promised. Req 35.5's teeth are
on the quantitative case, which is where an invented value reaches a user as a number.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from aqm_advisor.adapters.local import ScriptedServingClient
from aqm_advisor.agent.tools import RetrievalRecorder, build_retrieval_tools
from aqm_advisor.domain.grounding import (
    normalise_numeral,
    numerals,
    permitted_values,
    ungrounded,
)
from aqm_advisor.domain.idempotency import TurnIdentity
from aqm_advisor.ports.clock import FixedClock

_AT = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
_CREDENTIAL = "SENTINEL-CRED-P19-do-not-log"
_IDENTITY = TurnIdentity(user_id="u-p19", session_id="s" * 40)

# Sub-index values a snapshot plausibly carries, kept clear of the species names grounding
# masks (`PM2.5`, `NO2`) so a generated numeral is never confused with a masked token.
_SUB_INDEX = st.integers(min_value=1, max_value=500)


def _tools() -> tuple[RetrievalRecorder, dict[str, object]]:
    """One turn's recorder and its five tools, built the way a turn must build them."""
    recorder = RetrievalRecorder()
    tools = build_retrieval_tools(
        client=ScriptedServingClient(),
        credential=_CREDENTIAL,
        recorder=recorder,
        clock=FixedClock(_AT),
        identity=_IDENTITY,
    )
    return recorder, {tool.tool_name: tool for tool in tools}


def _called(recorder: RetrievalRecorder) -> set[str]:
    return {call.name for call in recorder.values().tool_calls}


@given(value=_SUB_INDEX)
def test_a_conditions_claim_with_nothing_retrieved_is_ungrounded(value: int) -> None:
    """The property itself: nothing retrieved, so a quoted value cannot be grounded.

    Note the precise scope — this turn retrieved NOTHING AT ALL, which is the case the
    design names. A turn that retrieved something else carrying the same number is the gap
    pinned further down.
    """
    recorder, _by_name = _tools()
    assert _called(recorder) == set(), "this case requires an empty trajectory"

    guidance = f"The air quality index is {value} right now, so conditions are fine."
    permitted = permitted_values(recorder.values(), constants=())
    failures = ungrounded(guidance, permitted)

    assert failures, "a claim was made with nothing retrieved and grounding did not object"
    assert normalise_numeral(str(value)) in failures


@given(value=_SUB_INDEX)
def test_the_same_claim_is_grounded_once_the_air_quality_call_supplied_it(value: int) -> None:
    """The converse, so the property cannot be satisfied by a verifier rejecting everything.

    Same sentence, same numeral. The only difference is that this turn performed the
    retrieval and the value came from its body.
    """
    recorder, _by_name = _tools()
    recorder.record_call("air_quality")
    recorder.record_body({"nearestSensors": [{"subIndex": value}]})

    assert "air_quality" in _called(recorder)

    guidance = f"The air quality index is {value} right now, so conditions are fine."
    permitted = permitted_values(recorder.values(), constants=())

    assert ungrounded(guidance, permitted) == ()


@given(claimed=_SUB_INDEX, retrieved=_SUB_INDEX)
def test_a_value_the_retrieval_did_not_return_is_ungrounded_even_with_the_call(
    claimed: int, retrieved: int
) -> None:
    """Having called the tool is not enough — the VALUE has to have come from it.

    Without this, "the trajectory contains an air-quality retrieval" could be satisfied by
    a turn that called the tool and then quoted a number of its own invention: the same
    defect wearing evidence of a retrieval.
    """
    recorder, _by_name = _tools()
    recorder.record_call("air_quality")
    recorder.record_body({"nearestSensors": [{"subIndex": retrieved}]})

    guidance = f"The air quality index is {claimed} right now."
    permitted = permitted_values(recorder.values(), constants=())
    failures = ungrounded(guidance, permitted)

    if normalise_numeral(str(claimed)) == normalise_numeral(str(retrieved)):
        assert failures == ()
    else:
        assert normalise_numeral(str(claimed)) in failures


@given(value=_SUB_INDEX)
def test_another_retrieval_carrying_the_value_also_grounds_a_conditions_claim(
    value: int,
) -> None:
    """THE GAP, PINNED. Grounding tracks values, not which tool returned them.

    A turn calling only `profile_get` — whose body can carry a personal threshold — grounds
    a sentence quoting that same number as if it were a current index, with no air-quality
    call in the trajectory. Not a bug in grounding: Req 7.1 asks that a quoted value be one
    "this turn actually retrieved", and the union of the turn's retrievals is exactly that.

    It IS the boundary of what the tests above prove, so it is asserted rather than
    described. If a future change made grounding tool-aware, this fails and whoever made it
    reads why the union was correct — and Property 19's tool-identity half moves here from
    the golden-turn assertions instead of being quietly claimed twice.
    """
    recorder, _by_name = _tools()
    recorder.record_call("profile_get")
    recorder.record_body({"personalThreshold": value})

    assert "air_quality" not in _called(recorder)

    guidance = f"The air quality index is {value} right now."
    permitted = permitted_values(recorder.values(), constants=())

    assert ungrounded(guidance, permitted) == (), (
        "grounding became tool-aware; the tool-identity half can now be asserted here"
    )


def test_the_real_air_quality_tool_records_the_call_it_is_asserted_on() -> None:
    """Non-vacuity for the trajectory half: the production tool must record the name.

    Every assertion above reads `air_quality` out of the recorder. If the tool recorded a
    different name — or nothing — the property would assert over a trajectory the service
    never writes, and would pass no matter what a turn did.

    An example test rather than a generated one: there is no input to vary, and inventing a
    parameter the body ignores would make the check look broader than it is.
    """
    recorder, by_name = _tools()
    by_name["air_quality"]()  # type: ignore[operator]

    assert "air_quality" in _called(recorder)
    # The returned body is harvested too, so a value from it is groundable afterwards —
    # which is what makes the passing direction above reachable through the real tool.
    assert permitted_values(recorder.values(), constants=()) != frozenset()


def test_a_qualitative_claim_is_outside_this_property() -> None:
    """The recorded limitation, asserted so it cannot be mistaken for coverage.

    "The air is poor today" claims something about current conditions and carries no digits,
    so grounding — and therefore this property — is silent on it. Pinning the silence means
    a future reader sees a stated boundary rather than assuming the property is total.
    """
    recorder, _by_name = _tools()
    permitted = permitted_values(recorder.values(), constants=())

    assert numerals("the air is poor today") == ()
    assert ungrounded("the air is poor today", permitted) == ()
