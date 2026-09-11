"""Tests for symptom diary capture (task 14.2). Validates Reqs 28.1 to 28.8.

**Req 28.6 is the clause that shapes the module, and it is enforced by control flow.** A diary
description is an utterance like any other, so the red-flag check applies to it — and where a
red flag is described, "SHALL NOT let recording the entry displace the escalation". So
`plan_diary_turn` returns an outcome that carries an escalation and NO write plan: the write is
unreachable on that path, rather than being something a caller must remember to skip. That is
the same short-circuit `TurnPipeline.run` uses, and for the same reason.

**Req 28.7 is enforced by absence of arithmetic.** "Deteriorating" or "improving" as a clinical
finding requires comparing this entry against earlier ones, so the module contains no arithmetic
and no severity comparison — an AST test asserts it. A module that cannot compare two severities
cannot report a trend in them.

**Req 28.2 puts the user above the agent's own reading.** Inferring a severity from prose is a
judgement about someone's health, and they are the authority on it — so a correction returns an
UNCONFIRMED draft, because being told you misread someone is not the same as their agreeing to
whatever you substituted.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib

import pytest

from aqm_advisor.domain.diary import (
    NOTE_PURPOSE_TEXT,
    DiaryTurnOutcome,
    plan_diary_turn,
    replacement_warning,
    restate_entry_for_confirmation,
)
from aqm_advisor.domain.forbidden import forbidden_matches
from aqm_advisor.domain.records import SymptomEntryDraft
from aqm_advisor.domain.redflag import DEFAULT_RED_FLAG_RULES

_DATE = dt.date(2026, 7, 1)
_EMERGENCY = "If you are struggling to breathe, call emergency services now."


def _draft(**kwargs: object) -> SymptomEntryDraft:
    defaults: dict[str, object] = {
        "date": _DATE,
        "severity": 3,
        "markers": ("cough", "wheeze"),
        "reliever_used": True,
    }
    return SymptomEntryDraft(**{**defaults, **kwargs})  # type: ignore[arg-type]


def _plan(description: str, **kwargs: object) -> DiaryTurnOutcome:
    return plan_diary_turn(
        description=description,
        draft=_draft(),
        existing_dates=frozenset(),
        rules=DEFAULT_RED_FLAG_RULES,
        emergency_guidance=_EMERGENCY,
        **kwargs,  # type: ignore[arg-type]
    )


# --- Req 28.6: escalation first, and recording cannot displace it -------


def test_a_red_flag_in_a_diary_description_escalates() -> None:
    # Req 28.6. A diary description is an utterance, and a red flag in one is exactly as urgent
    # as a red
    # flag in a question about the air — the fact that someone is filing it as history does not
    # make it
    # historical.
    outcome = _plan("today my lips look blue and my reliever is not helping")
    assert outcome.escalation is not None


def test_an_escalating_diary_turn_produces_no_write_plan() -> None:
    # THE clause: recording must not displace the escalation. The write is UNREACHABLE on this
    # path rather
    # than something a caller must remember to skip.
    outcome = _plan("today my lips look blue and my reliever is not helping")
    assert outcome.confirmation_request is None
    assert outcome.may_write is False


def test_an_ordinary_diary_description_does_not_escalate() -> None:
    # Non-vacuity: a planner that escalated on everything would make the diary unusable and
    # train the user
    # to ignore the escalation.
    outcome = _plan("a bit wheezy this afternoon but nothing unusual")
    assert outcome.escalation is None
    assert outcome.may_write is True


def test_an_ordinary_description_yields_a_confirmation_request() -> None:
    outcome = _plan("a bit wheezy this afternoon")
    assert outcome.confirmation_request is not None
    assert "?" in outcome.confirmation_request


def test_the_escalating_outcome_carries_the_emergency_guidance() -> None:
    # The wording is the envelope's, never this service's own.
    outcome = _plan("i am struggling to breathe")
    assert outcome.escalation is not None
    assert outcome.escalation.guidance == _EMERGENCY


# --- Req 28.2: restate, and the user's correction wins ------------------


def test_the_restatement_names_the_severity_and_every_marker() -> None:
    # Req 28.2. A restatement that omitted a marker would obtain confirmation for an entry
    # different from
    # the one written.
    restatement = restate_entry_for_confirmation(_draft())
    assert "3" in restatement
    for marker in ("cough", "wheeze"):
        assert marker in restatement


def test_the_restatement_states_whether_a_reliever_was_used() -> None:
    # Part of the entry, so part of what is confirmed. It is also the field most likely to be
    # inferred
    # wrongly from prose.
    assert "reliever" in restate_entry_for_confirmation(_draft()).casefold()


def test_the_restatement_asks_rather_than_asserts() -> None:
    assert restate_entry_for_confirmation(_draft()).rstrip().endswith("?")


def test_a_correction_returns_an_unconfirmed_draft() -> None:
    # Req 28.2's substance. Being told you misread someone is not the same as their agreeing to
    # whatever
    # you substituted, so the corrected draft goes back for confirmation.
    corrected = _draft(confirmed=True).correct(severity=5)
    assert corrected.severity == 5
    assert corrected.confirmed is False


def test_the_restatement_of_a_corrected_draft_shows_the_correction() -> None:
    # Otherwise the second confirmation would restate the agent's original reading, and the user
    # would be
    # asked to confirm the thing they had just rejected.
    corrected = _draft().correct(severity=5, markers=("chest tightness",))
    restatement = restate_entry_for_confirmation(corrected)
    assert "5" in restatement
    assert "chest tightness" in restatement
    assert "wheeze" not in restatement


# --- Req 28.3: no write without an explicit instruction -----------------


def test_an_unconfirmed_draft_refuses_to_produce_a_write_body() -> None:
    # Req 28.3, fail-closed: the caller gets an exception rather than an unconfirmed body, which
    # is a bug
    # report rather than a silent write to someone's health diary.
    with pytest.raises(RuntimeError, match="confirm"):
        _draft().write_body()


def test_a_confirmed_draft_produces_a_write_body() -> None:
    body = _draft().confirm().write_body()
    assert body["severity"] == 3
    assert body["markers"] == ["cough", "wheeze"]


def test_the_write_body_omits_an_absent_note() -> None:
    # Req 28.4: a note only where the user asked for one. Sending an explicit null would be this
    # service
    # asserting there is no note rather than declining to send one.
    assert "note" not in _draft().confirm().write_body()


def test_the_write_body_includes_a_requested_note() -> None:
    body = _draft(note="worse after the school run").confirm().write_body()
    assert body["note"] == "worse after the school run"


# --- Req 28.4: what a note is for ---------------------------------------


def test_the_note_purpose_text_says_it_computes_nothing() -> None:
    # Req 28.4 is explicit. Someone who believes their words feed a calculation will word them
    # for the
    # machine rather than for themselves, which makes the diary worse at the one thing it is
    # for.
    lowered = NOTE_PURPOSE_TEXT.casefold()
    assert "not used to compute" in lowered or "computes nothing" in lowered


def test_the_note_purpose_text_says_it_is_for_their_recall() -> None:
    lowered = NOTE_PURPOSE_TEXT.casefold()
    assert "recall" in lowered or "remember" in lowered


def test_the_note_purpose_text_is_publishable() -> None:
    assert forbidden_matches(NOTE_PURPOSE_TEXT) == ()


# --- Req 28.5: warn before replacing -----------------------------------


def test_a_date_with_an_existing_entry_warns_before_replacing() -> None:
    outcome = plan_diary_turn(
        description="a bit wheezy",
        draft=_draft(),
        existing_dates=frozenset({_DATE}),
        rules=DEFAULT_RED_FLAG_RULES,
        emergency_guidance=_EMERGENCY,
    )
    assert outcome.replacement_warning is not None
    assert "replace" in outcome.replacement_warning.casefold()


def test_a_date_without_an_existing_entry_gives_no_warning() -> None:
    # Non-vacuity: warning every time would train the user to click through it, which is how a
    # real
    # replacement gets confirmed without being read.
    outcome = _plan("a bit wheezy")
    assert outcome.replacement_warning is None


def test_the_replacement_warning_names_the_date() -> None:
    # "An entry will be replaced" leaves the user unsure which day they are about to overwrite.
    warning = replacement_warning(_DATE)
    assert "2026-07-01" in warning


def test_the_replacement_warning_asks_for_confirmation() -> None:
    assert replacement_warning(_DATE).rstrip().endswith("?")


# --- Req 28.7: never characterise an entry clinically -------------------


def test_no_produced_text_characterises_the_entry_clinically() -> None:
    # Req 28.7. A diary is a record, not an assessment, and the user reads a clinical word in it
    # as a
    # finding whatever hedging surrounds it.
    texts = [
        restate_entry_for_confirmation(_draft()),
        replacement_warning(_DATE),
        NOTE_PURPOSE_TEXT,
    ]
    for text in texts:
        lowered = text.casefold()
        for clinical in (
            "deteriorat",
            "improv",
            "worsening",
            "exacerbation",
            "attack",
            "uncontrolled",
            "diagnos",
            "suggests you have",
        ):
            assert clinical not in lowered, (clinical, text)


_SOURCE = pathlib.Path("src/aqm_advisor/domain/diary.py").read_text(encoding="utf-8")


def test_the_module_performs_no_arithmetic() -> None:
    # Req 28.7 structurally. "Deteriorating" or "improving" as a clinical finding requires
    # comparing this
    # entry against earlier ones, and a module with no arithmetic cannot produce that
    # comparison.
    tree = ast.parse(_SOURCE)
    operators = [
        type(node.op).__name__
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult | ast.Div | ast.Add | ast.Sub | ast.Pow | ast.FloorDiv)
    ]
    assert operators == [], f"the diary module computes: {operators}"


def test_the_module_never_compares_two_severities() -> None:
    # The specific derivation Req 28.7 forbids. An ordering comparison is how a trend would be
    # produced
    # without any arithmetic operator appearing at all.
    tree = ast.parse(_SOURCE)
    ordering = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(
            isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in node.ops
        )
    ]
    assert ordering == []


def test_the_module_aggregates_nothing() -> None:
    tree = ast.parse(_SOURCE)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called.isdisjoint({"sum", "min", "max", "sorted", "mean", "median"})


def test_the_comparison_detector_would_catch_a_trend() -> None:
    # Self-check: a detector that collected nothing would report this guarantee for free.
    planted = ast.parse("def trend(a, b):\n    return a > b\n")
    found = [node for node in ast.walk(planted) if isinstance(node, ast.Compare)]
    assert found != []


# --- Req 28.8: nothing is stored ---------------------------------------


def test_the_planner_returns_no_mutable_state() -> None:
    # Req 28.8. A planner holding the description would be storing it, and "for the turn only"
    # survives
    # only if there is nowhere for it to persist.
    outcome = _plan("a bit wheezy this afternoon")
    assert not hasattr(outcome, "description")
    assert not hasattr(outcome, "utterance")


def test_the_outcome_does_not_carry_the_description() -> None:
    # The same claim as a field check, because the description is the user's own words and Req
    # 19.2 keeps
    # those out of anything that outlives the turn.
    outcome = _plan("a very distinctive description QZX")
    assert "QZX" not in repr(outcome)
