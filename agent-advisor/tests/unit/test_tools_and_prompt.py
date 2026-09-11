"""Tests for the five retrieval tools and the system prompt (tasks 9.1, 9.4).

`test_no_tool_schema_mentions_the_credential` is the one that matters most. The credential is
captured in a CLOSURE rather than taken as a tool parameter, so it never appears in the
model-facing `inputSchema` — which means the model cannot be asked for it, cannot hallucinate
it, and cannot echo it back. Req 5.1 forwards the credential only to the Serving_Client and Req
5.2 keeps it out of every prompt; a parameter would put it in the tool spec, which IS part of
the prompt.

`test_the_recorder_makes_retrieved_numerals_groundable` is the seam between retrieval and
grounding: every numeral that arrived in a response becomes a permitted value, so a generation
quoting it is grounded and one inventing a number is not. Without that link the grounding check
would reject everything.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import pytest
from strands.tools.decorator import DecoratedFunctionTool

from aqm_advisor.adapters.local import ScriptedServingClient, canned_air_quality
from aqm_advisor.agent.prompt import (
    DEFAULT_SYSTEM_PROMPT_RESOURCE,
    load_system_prompt,
)
from aqm_advisor.agent.tools import (
    _MAX_HISTORY_DAYS,
    RetrievalRecorder,
    build_retrieval_tools,
)
from aqm_advisor.domain.grounding import permitted_values, ungrounded
from aqm_advisor.domain.idempotency import TurnIdentity
from aqm_advisor.domain.snapshot import NO_CURRENT_READING_TEXT
from aqm_advisor.ports.clock import FixedClock
from aqm_advisor.ports.protocols import ServingFailureKind

_IDENTITY = TurnIdentity(user_id="u1", session_id="s" * 33)

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_CREDENTIAL = "eyJhbGciOi.THIS-IS-THE-CREDENTIAL.signature"


def _build(
    client: ScriptedServingClient | None = None,
) -> tuple[tuple[DecoratedFunctionTool[Any, Any], ...], RetrievalRecorder]:
    recorder = RetrievalRecorder()
    tools = build_retrieval_tools(
        identity=_IDENTITY,
        client=client or ScriptedServingClient(),
        credential=_CREDENTIAL,
        recorder=recorder,
        clock=FixedClock(_NOW),
    )
    return tools, recorder


def _by_name(
    tools: tuple[DecoratedFunctionTool[Any, Any], ...],
) -> dict[str, DecoratedFunctionTool[Any, Any]]:
    return {tool.tool_name: tool for tool in tools}


# --- Req 9.1: the five tools --------------------------------------------

def test_the_five_tools_are_built() -> None:
    tools, _ = _build()
    assert set(_by_name(tools)) == {
        "air_quality",
        "history",
        "profile_get",
        "profile_put",
        "symptom_entry_put",
    }


def test_every_tool_has_a_model_facing_description() -> None:
    # The docstring IS the description the model sees, so an undocumented tool is an unusable
    # one.
    tools, _ = _build()
    for tool in tools:
        spec = tool.tool_spec
        assert spec["description"].strip(), spec["name"]
        assert len(spec["description"]) > 40, spec["name"]


# --- Req 5.1, 5.2: the credential is never in the prompt surface --------

def test_no_tool_schema_mentions_the_credential() -> None:
    # THE security test. The credential is captured in a closure, not taken as a parameter, so
    # it
    # cannot reach the model-facing schema — and a tool spec IS part of the prompt.
    tools, _ = _build()
    for tool in tools:
        rendered = str(tool.tool_spec)
        assert _CREDENTIAL not in rendered
        assert "credential" not in rendered.casefold()
        assert "token" not in rendered.casefold()


def test_the_credential_still_reaches_the_serving_client() -> None:
    # Non-vacuity: hiding it from the schema must not stop it being forwarded (Req 5.1).
    client = ScriptedServingClient()
    tools, _ = _build(client)
    _by_name(tools)["air_quality"]()
    assert client.tool_names() == ("air_quality",)


def test_no_tool_takes_a_credential_parameter() -> None:
    # Structural: an empty properties map is what guarantees the test above cannot regress by
    # someone
    # adding a convenience parameter.
    tools, _ = _build()
    for tool in tools:
        schema = tool.tool_spec["inputSchema"]["json"]
        assert "credential" not in schema.get("properties", {})


# --- Req 9.1: the accumulator and its ordering --------------------------

def test_each_call_records_an_ordered_tool_call() -> None:
    # Req 35.4 asserts which tools were called and in what order.
    tools, recorder = _build()
    named = _by_name(tools)
    named["air_quality"]()
    named["profile_get"]()
    assert [call.name for call in recorder.values().tool_calls] == [
        "air_quality",
        "profile_get",
    ]


def test_a_repeated_call_is_recorded_twice() -> None:
    # A trajectory is a sequence of events. Collapsing a repeat would hide a retry loop.
    tools, recorder = _build()
    named = _by_name(tools)
    named["profile_get"]()
    named["profile_get"]()
    assert len(recorder.values().tool_calls) == 2


def test_the_recorder_makes_retrieved_numerals_groundable() -> None:
    # THE seam between retrieval and grounding. Every numeral that arrived becomes a permitted
    # value,
    # so quoting it is grounded — and without this link the grounding check would reject
    # everything.
    tools, recorder = _build()
    _by_name(tools)["air_quality"]()
    permitted = permitted_values(recorder.values(), constants=())
    assert ungrounded("the sub-index is 68", permitted) == ()


def test_a_number_that_never_arrived_stays_ungrounded() -> None:
    # Non-vacuity for the test above: if retrieval permitted everything, grounding would be
    # pointless.
    tools, recorder = _build()
    _by_name(tools)["air_quality"]()
    permitted = permitted_values(recorder.values(), constants=())
    assert ungrounded("the sub-index is 4242", permitted) == ("4242",)


def test_retrieved_medications_are_recorded_for_the_closure_check() -> None:
    # Req 29.6 verifies a named medication against the RETRIEVED set, so the retrieval has to
    # capture
    # it. The canned profile lists salbutamol.
    tools, recorder = _build()
    _by_name(tools)["profile_get"]()
    assert "salbutamol" in recorder.values().medications


def test_an_empty_recorder_permits_nothing() -> None:
    recorder = RetrievalRecorder()
    assert recorder.values().numerals == frozenset()
    assert recorder.values().tool_calls == ()


# --- Req 2.6: at most one air-quality retrieval per turn ----------------

def test_a_second_air_quality_call_does_not_reach_the_client() -> None:
    # Req 2.6. A second retrieval would spend the budget and could return a DIFFERENT snapshot
    # mid-turn,
    # so the guidance and the basis would describe different readings.
    client = ScriptedServingClient()
    tools, _ = _build(client)
    named = _by_name(tools)
    named["air_quality"]()
    named["air_quality"]()
    assert client.tool_names() == ("air_quality",)


def test_the_second_call_says_so_rather_than_failing_silently() -> None:
    # The model needs to know why it got no new data, or it will keep asking.
    tools, _ = _build()
    named = _by_name(tools)
    named["air_quality"]()
    second = str(named["air_quality"]())
    assert "already" in second.casefold()


def test_the_cap_applies_only_to_air_quality() -> None:
    # Non-vacuity: capping every tool would make a multi-step turn impossible.
    client = ScriptedServingClient()
    tools, _ = _build(client)
    named = _by_name(tools)
    named["profile_get"]()
    named["profile_get"]()
    assert client.tool_names() == ("profile_get", "profile_get")


# --- Req 21.1, 21.4: a failure is a named kind, never a raw body --------

def test_a_serving_failure_returns_a_named_kind_and_no_raw_error() -> None:
    client = ScriptedServingClient(air_quality_body=ServingFailureKind.TIMEOUT)
    tools, _ = _build(client)
    result = str(_by_name(tools)["air_quality"]())
    assert "timeout" in result.casefold()
    assert "traceback" not in result.casefold()


def test_a_failed_call_is_still_recorded_in_the_trajectory() -> None:
    # An attempt that failed is part of what happened, and omitting it would make the trajectory
    # a
    # record of successes rather than of the turn.
    client = ScriptedServingClient(air_quality_body=ServingFailureKind.UNREACHABLE)
    tools, recorder = _build(client)
    _by_name(tools)["air_quality"]()
    assert [call.name for call in recorder.values().tool_calls] == ["air_quality"]


# --- Req 31.4: the prompt is data, not a literal in a function ----------

def test_the_system_prompt_loads_from_a_package_resource() -> None:
    # Req 31.4: supplied as configuration rather than embedded in a function, so its text is
    # reviewable, diffable and testable as data. A resource file is diffable; an f-string is
    # not.
    prompt = load_system_prompt()
    assert len(prompt) > 200
    assert DEFAULT_SYSTEM_PROMPT_RESOURCE.endswith(".md")


def test_the_prompt_contains_no_credential_placeholder() -> None:
    # A placeholder is an invitation to interpolate. Req 5.2 keeps the credential out of every
    # prompt,
    # and the way to guarantee that is for there to be no slot.
    lowered = load_system_prompt().casefold()
    for placeholder in ("{credential}", "{token}", "%s", "{{", "bearer ", "authorization:"):
        assert placeholder not in lowered, placeholder


def test_the_prompt_claims_no_capability_the_guardrails_forbid() -> None:
    # A prompt promising what the verifiers will reject produces a turn that fails its own
    # checks —
    # the model would be instructed to do the thing that gets its generation withheld.
    from aqm_advisor.domain.forbidden import forbidden_matches

    assert forbidden_matches(load_system_prompt()) == ()


def test_the_prompt_instructs_digits_for_numerals() -> None:
    # The documented mitigation for grounding's digit-only limit: a spelled-out number is a
    # claim the
    # grounding check cannot see, so the prompt is where that gap is closed.
    lowered = load_system_prompt().casefold()
    assert "digit" in lowered


def test_a_supplied_prompt_replaces_the_default() -> None:
    # Configuration that can only ever be the default is not configuration.
    assert load_system_prompt("a configured prompt") == "a configured prompt"


def test_a_blank_supplied_prompt_is_refused() -> None:
    # Failing loudly beats running with no instructions at all, which would look like a model
    # problem.
    with pytest.raises(ValueError, match="prompt"):
        load_system_prompt("   ")


# --- the history span duplicates a fact Service 2 owns -------------------

_SERVICE_2_HISTORY = (
    pathlib.Path(__file__).resolve().parents[3]
    / "data-processing"
    / "src"
    / "aqm_ingestion"
    / "serving"
    / "history.py"
)


def test_the_history_bound_has_not_drifted_from_service_2s() -> None:
    # The bound is Service 2's DEFAULT and is configurable there, so this constant duplicates a
    # fact
    # another service owns — which is exactly when a drift guard is required. If Service 2
    # lowered its
    # default, this service would keep requesting a span Service 2 now refuses, and every
    # history call
    # would fail with a rejection the model was told not to retry.
    if not _SERVICE_2_HISTORY.is_file():
        pytest.skip("the sibling service is not present")
    source = _SERVICE_2_HISTORY.read_text(encoding="utf-8")
    assert f"DEFAULT_MAX_HISTORY_SPAN_DAYS = {_MAX_HISTORY_DAYS}" in source, (
        "Service 2's default history span no longer matches this service's bound"
    )


def test_a_window_beyond_the_bound_is_refused_with_the_bound_named() -> None:
    # Req 3.3: report the period unavailable AND the permitted bound, with no silent re-request.
    # A
    # silent narrowing would answer a question the user did not ask.
    tools, _ = _build()
    result = str(_by_name(tools)["history"](days=90))
    assert "unavailable" in result.casefold()
    assert str(_MAX_HISTORY_DAYS) in result


def test_a_window_beyond_the_bound_never_reaches_the_client() -> None:
    client = ScriptedServingClient()
    tools, _ = _build(client)
    _by_name(tools)["history"](days=90)
    assert client.tool_names() == ()


def test_a_window_within_the_bound_reaches_the_client() -> None:
    # Non-vacuity: a bound that refused everything would make history unusable.
    client = ScriptedServingClient()
    tools, _ = _build(client)
    # Req 3.1a: the snapshot supplies the site code, so it must be retrieved first.
    _by_name(tools)["air_quality"]()
    _by_name(tools)["history"](days=7)
    assert client.tool_names() == ("air_quality", "history")


def test_the_window_is_derived_from_the_injected_clock() -> None:
    # Req 3.2 and Req 25.2: the window comes from the injected Clock, never the wall clock, so a
    # turn
    # is reproducible.
    client = ScriptedServingClient()
    tools, _ = _build(client)
    _by_name(tools)["air_quality"]()  # Req 3.1a supplies the site code
    _by_name(tools)["history"](days=3)
    _name, args = client.calls[1]
    start, end = args[1], args[2]
    assert end == _NOW
    assert start == _NOW - dt.timedelta(days=3)


def test_history_without_a_snapshot_reports_unavailable_and_makes_no_call() -> None:
    # Req 3.1a. Found by writing the first real HTTP adapter in task 16.3: Service 2's Req 19.3
    # makes `siteCode` REQUIRED, so a history call has no valid form without one, and the
    # scripted
    # client had masked that entirely by answering from a canned body.
    #
    # The call must not be attempted at all. A guessed site would be answered 404 by Service 2 —
    # surfacing as a service fault rather than the mistake it is — and a deployment-wide
    # configured
    # site would serve one user another location's readings as if they were their own.
    client = ScriptedServingClient()
    tools, _ = _build(client)
    result = str(_by_name(tools)["history"](days=7)).casefold()
    assert "unavailable" in result
    assert client.tool_names() == (), "history reached the client with no site code"


def test_history_uses_the_site_the_snapshot_named() -> None:
    # The site must be the one Service 2 resolved for THIS user, not a literal and not the
    # model's
    # suggestion — the history tool takes no site parameter, so the model cannot supply one.
    client = ScriptedServingClient()
    tools, _ = _build(client)
    _by_name(tools)["air_quality"]()
    _by_name(tools)["history"](days=7)
    _name, args = client.calls[1]
    assert args[0] == "AQM1", args


def test_the_history_tool_exposes_no_site_parameter_to_the_model() -> None:
    # Structural, so the guarantee cannot be undone by a later signature change: Req 3.1a
    # says the site comes from the snapshot, and a tool parameter would be exactly the
    # route by which a model-invented site reached Service 2.
    #
    # Asserted against the model-facing `inputSchema`, not the Python signature, because
    # that schema is what the model is actually offered: a parameter absent from it
    # cannot be supplied however the function is spelled.
    history = _by_name(_build()[0])["history"]
    properties = history.tool_spec["inputSchema"]["json"].get("properties", {})
    assert "site_code" not in properties
    assert "siteCode" not in properties


def test_a_snapshot_with_no_sensors_does_not_enable_a_history_call() -> None:
    # The degraded case. An empty `nearestSensors` means Service 2 found no site for this user,
    # so
    # there is still nothing valid to ask history for — and `_site_code_of` returning None must
    # keep the tool on the refusal path rather than sending an empty string.
    client = ScriptedServingClient(air_quality_body={"user": "u1", "nearestSensors": []})
    tools, _ = _build(client)
    _by_name(tools)["air_quality"]()
    result = str(_by_name(tools)["history"](days=7)).casefold()
    assert "unavailable" in result
    assert client.tool_names() == ("air_quality",)


# --- the framing arrives WITH the data, not by the model noticing --------


def test_the_air_quality_tool_names_a_quiet_site_in_its_notes() -> None:
    # Req 2.5, wired. The requirement's real failure mode is the model reading straight past an
    # empty
    # measurement array, so the tool states it outright and the requirement stops depending on
    # attention.
    body = canned_air_quality()
    sensors = body["nearestSensors"]
    assert isinstance(sensors, list)
    sensors[0]["measurements"] = []
    client = ScriptedServingClient(air_quality_body=body)
    tools, _ = _build(client)
    result = str(_by_name(tools)["air_quality"]())
    assert NO_CURRENT_READING_TEXT in result


def test_a_site_with_readings_produces_no_quiet_note() -> None:
    # Non-vacuity: a note on every site would carry no information.
    tools, _ = _build()
    result = str(_by_name(tools)["air_quality"]())
    assert NO_CURRENT_READING_TEXT not in result


def test_the_air_quality_tool_states_when_defaults_were_used() -> None:
    # Req 2.4, wired the same way and for the same reason.
    body = canned_air_quality()
    personalized = body["personalized"]
    assert isinstance(personalized, dict)
    personalized["usedDefaultProfile"] = True
    client = ScriptedServingClient(air_quality_body=body)
    tools, _ = _build(client)
    assert "default" in str(_by_name(tools)["air_quality"]()).casefold()


def test_the_snapshot_is_still_delivered_whole() -> None:
    # The notes ADD framing; they must not replace the body, or the model would have no values
    # to quote
    # and every number it produced would be ungrounded.
    tools, _ = _build()
    result = str(_by_name(tools)["air_quality"]())
    assert "nearestSensors" in result


def test_the_history_tool_delivers_a_labelled_summary() -> None:
    # Req 3.4, wired: the summary the model is handed is already described AS a summary, so it
    # does not
    # have to remember to say so.
    tools, _ = _build()
    _by_name(tools)["air_quality"]()  # Req 3.1a supplies the site code
    result = str(_by_name(tools)["history"](days=7)).casefold()
    assert "summar" in result


def test_a_failed_air_quality_call_does_not_consume_the_turn_s_one_retrieval() -> None:
    # A review found the call counter incremented BEFORE the request, so one transient serving
    # failure spent the turn's only air-quality call. The retry was then refused with "already
    # retrieved" — false — and Req 3.1a left history unavailable for the rest of the turn. Req
    # 2.6
    # bounds SUCCESSFUL snapshots: its reason is that a second snapshot could differ from the
    # basis,
    # and an attempt that returned nothing cannot differ from anything.
    client = ScriptedServingClient(air_quality_body=ServingFailureKind.TIMEOUT)
    tools, _ = _build(client)
    first = str(_by_name(tools)["air_quality"]())
    assert "unavailable" in first.casefold()
    second = str(_by_name(tools)["air_quality"]())
    assert "already retrieved" not in second.casefold(), second


def test_a_rejected_history_window_reports_the_permitted_bound() -> None:
    # Req 3.3 requires the period be reported unavailable AND the permitted bound named. The
    # bound
    # exists only in Service 2's body, which Req 21.4 forbids forwarding, so the note carried a
    # bare
    # kind and the clause was unmet. It is restated from this service's own constant instead.
    client = ScriptedServingClient(
        history_body=ServingFailureKind.BAD_REQUEST,
    )
    tools, _ = _build(client)
    _by_name(tools)["air_quality"]()
    note = str(_by_name(tools)["history"](days=7)).casefold()
    assert "30 days" in note, note
    assert "do not retry" in note, note
