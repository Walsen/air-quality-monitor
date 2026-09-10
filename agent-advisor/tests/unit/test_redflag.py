"""Tests for the deterministic red-flag matcher (tasks 4.1 and 4.2).

**The asymmetry these tests encode.** A false positive costs one unnecessary sentence directing
someone to emergency care. A false negative could cost a life. Those are not comparable, so the
rules are permissive on purpose and several tests below assert that a *marginal* phrasing still
matches. A test that tightened the matcher to reduce false positives would be optimising the
cheap direction.

**Why so much of this is structural.** Req 10.3 requires escalation even when the air quality is
good, and Req 10.4 even when the Serving_Client is unavailable. Those are usually written as
behavioural tests through the whole pipeline — but the stronger statement is that
`match_red_flags` takes NEITHER an air-quality reading NOR a client, so it cannot consult them.
A signature cannot be bypassed by a later refactor the way a behavioural expectation can.

The pipeline-level half of task 4.2 — that the escalation reaches the response when the
Serving_Client raises — is deferred with the degraded-envelope decision, and recorded as task
4.4 rather than left implicit.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import SecretStr

from aqm_advisor.domain.models import PriorTurn
from aqm_advisor.domain.redflag import (
    DEFAULT_RED_FLAG_RULES,
    RedFlagRule,
    match_red_flags,
    match_request_red_flags,
    normalise,
)

_MARKERS = {rule.marker for rule in DEFAULT_RED_FLAG_RULES}


# --- Req 10.7: the research's three defaults ----------------------------

def test_the_defaults_are_the_researchs_three() -> None:
    assert {
        "severe_breathlessness",
        "reliever_not_working",
        "blue_lips_or_face",
    } == _MARKERS


def test_every_default_rule_has_patterns() -> None:
    # A rule with no patterns can never fire, and would sit in the set looking like protection.
    for rule in DEFAULT_RED_FLAG_RULES:
        assert rule.patterns, f"{rule.marker} has no patterns"


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("I can't breathe", "severe_breathlessness"),
        ("I am struggling to breathe", "severe_breathlessness"),
        ("too breathless to finish a sentence", "severe_breathlessness"),
        ("my inhaler isn't working", "reliever_not_working"),
        ("the reliever is not helping at all", "reliever_not_working"),
        ("my lips are blue", "blue_lips_or_face"),
        ("she is turning blue around the mouth", "blue_lips_or_face"),
    ],
)
def test_each_default_rule_fires_on_its_own_phrasing(utterance: str, expected: str) -> None:
    assert expected in match_red_flags(utterance, DEFAULT_RED_FLAG_RULES)


def test_an_ordinary_utterance_matches_nothing() -> None:
    # Non-vacuity for every test above: if the matcher returned every marker regardless, the
    # whole
    # file would pass while telling us nothing.
    assert match_red_flags("Is it safe to run this evening?", DEFAULT_RED_FLAG_RULES) == ()


@pytest.mark.parametrize(
    "benign",
    [
        "the air quality is bad today",
        "I want to breathe cleaner air on my run",
        "my blue jacket is in the wash",
        "the inhaler is working fine now",
        "pollen is not helping my hay fever",
    ],
)
def test_a_benign_utterance_does_not_escalate(benign: str) -> None:
    # The permissive direction has a limit: a matcher that fired on "breathe" or "blue" alone
    # would
    # escalate on ordinary conversation and train the user to ignore the direction entirely.
    assert match_red_flags(benign, DEFAULT_RED_FLAG_RULES) == ()


# --- normalisation ------------------------------------------------------

def test_matching_is_case_insensitive() -> None:
    assert match_red_flags("I CAN'T BREATHE", DEFAULT_RED_FLAG_RULES) != ()


def test_a_curly_apostrophe_still_matches() -> None:
    # A REAL false-negative source, not a hypothetical: phone keyboards and word processors emit
    # U+2019, so the apostrophe in "can't" arrives as that codepoint instead. A matcher keyed
    # on the ASCII form would miss
    # the
    # most common way this sentence is actually typed.
    assert match_red_flags("I can\u2019t breathe", DEFAULT_RED_FLAG_RULES) != ()


def test_collapsed_whitespace_still_matches() -> None:
    assert match_red_flags("I  can't\n\tbreathe", DEFAULT_RED_FLAG_RULES) != ()


def test_normalisation_is_idempotent() -> None:
    once = normalise("I  CAN\u2019T   breathe")
    assert normalise(once) == once


# --- Req 10.5: it recognises, it does not diagnose ----------------------

def test_the_matcher_returns_markers_and_not_a_clinical_verdict() -> None:
    # Req 10.5: the service directs the user to emergency care and does NOT state whether the
    # symptoms are or are not an emergency. The return TYPE is what enforces that: rule names
    # that
    # matched, never a determination about the person.
    result = match_red_flags("I can't breathe", DEFAULT_RED_FLAG_RULES)
    assert isinstance(result, tuple)
    assert set(result) <= _MARKERS


def test_no_marker_names_a_diagnosis() -> None:
    # A marker becomes a public-ish label on an Escalation and on a metric. A marker called
    # "asthma_attack" would be the clinical determination Req 10.5 forbids, smuggled in as a
    # name.
    for marker in _MARKERS:
        diagnoses = ("attack", "exacerbation", "asthma", "copd", "infection", "anaphylaxis")
        for diagnosis in diagnoses:
            assert diagnosis not in marker, f"{marker} names a diagnosis"


# --- Req 10.3 / 10.4: escalation cannot be made conditional -------------

def test_the_matcher_cannot_consult_air_quality_or_a_client() -> None:
    # Req 10.3 and 10.4 as a SIGNATURE rather than a behaviour. The matcher takes an utterance
    # and a
    # rule set; there is no parameter through which a reading, a client, a model or a config
    # could
    # reach it, so no future change can make escalation depend on one without changing this
    # test.
    parameters = set(inspect.signature(match_red_flags).parameters)
    assert parameters == {"utterance", "rules"}


def test_the_matcher_is_pure_and_reads_no_clock_or_network() -> None:
    import ast
    import pathlib

    from aqm_advisor.domain import redflag

    tree = ast.parse(pathlib.Path(redflag.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("httpx", "boto3", "socket", "urllib", "requests", "random", "time"):
        assert forbidden not in imported, f"the matcher imports {forbidden}"


def test_the_same_utterance_always_yields_the_same_markers() -> None:
    # Determinism (practices §2). A matcher whose output varied could escalate on one attempt
    # and
    # not the next, which is the least acceptable place for nondeterminism in this service.
    utterance = "my lips are blue and I can't breathe"
    first = match_red_flags(utterance, DEFAULT_RED_FLAG_RULES)
    for _ in range(8):
        assert match_red_flags(utterance, DEFAULT_RED_FLAG_RULES) == first


def test_markers_are_returned_in_rule_order_not_match_order() -> None:
    # Practices §2 requires a defined iteration order anywhere it reaches output. Rule order is
    # stable across runs; match order would depend on where in the sentence each phrase
    # appeared.
    rules = (
        RedFlagRule(marker="first", patterns=("alpha",)),
        RedFlagRule(marker="second", patterns=("beta",)),
    )
    assert match_red_flags("beta then alpha", rules) == ("first", "second")


def test_a_marker_is_reported_once_even_when_several_patterns_match() -> None:
    rule = RedFlagRule(marker="only", patterns=("alpha", "beta"))
    assert match_red_flags("alpha and beta", (rule,)) == ("only",)


# --- Req 10.7: the rule set is configuration ----------------------------

def test_a_deployment_can_widen_the_rule_set() -> None:
    # Req 10.7 makes the set configuration precisely so a deployment can add a phrasing its
    # population uses. The asymmetry means widening must be easy.
    local = RedFlagRule(marker="local_phrase", patterns=("tight chest",))
    widened = (*DEFAULT_RED_FLAG_RULES, local)
    assert match_red_flags("my chest is tight", widened) == ()
    assert match_red_flags("I have a tight chest", widened) == ("local_phrase",)


def test_an_empty_rule_set_matches_nothing_rather_than_raising() -> None:
    # A misconfiguration must not break the turn. It should be visible as "nothing matched" and
    # caught by configuration validation, not by an exception on the escalation path.
    assert match_red_flags("I can't breathe", ()) == ()


# --- Req 10.7: prior turns, and the trap in them ------------------------

def test_a_red_flag_in_a_prior_utterance_is_matched() -> None:
    # Req 10.7 applies the set to prior turns in the same request: someone may describe the
    # symptom
    # in one message and ask the question in the next.
    prior = (
        PriorTurn(utterance=SecretStr("I can't breathe"), guidance=SecretStr("Seek care.")),
    )
    assert match_request_red_flags("what should I do?", prior, DEFAULT_RED_FLAG_RULES) != ()


_CLINICAL_EMERGENCY_WORDING = (
    "If you are severely breathless, your reliever inhaler is not helping, or your lips or "
    "face look blue, seek emergency care now."
)
"""The emergency guidance Service 2 returns — the same three symptoms, worded by a clinician."""


def test_the_matcher_recognises_the_clinical_wording_of_its_own_three_symptoms() -> None:
    # This test found a real false negative. The clinical form uses a COMPOUND subject — "your
    # lips or
    # face look blue" — which matched neither "lips look blue" (interrupted by "or face") nor
    # "face looks blue" (plural agreement makes it "look"). The highest-stakes rule missed the
    # exact
    # phrasing a clinician would use.
    #
    # It is kept because Service 2's emergency text is the best available sample of how these
    # symptoms
    # are actually described: if the matcher cannot recognise the wording the system itself uses
    # to
    # describe a red flag, it will not recognise a user echoing it back.
    assert set(match_red_flags(_CLINICAL_EMERGENCY_WORDING, DEFAULT_RED_FLAG_RULES)) == _MARKERS


def test_the_agents_own_prior_guidance_is_not_scanned() -> None:
    # THE defect this test exists for. The emergency guidance Service 2 returns contains all
    # three
    # default red flags — so if prior GUIDANCE were scanned, every turn after an escalation
    # would
    # re-escalate on the agent's own words, forever, and it would look like correct caution.
    #
    # Non-vacuity is carried by the test above, which proves that text really does trip all
    # three
    # rules. Without that, this assertion would pass whenever the text happened to be harmless.
    prior = (
        PriorTurn(
            utterance=SecretStr("is it safe to run?"),
            guidance=SecretStr(_CLINICAL_EMERGENCY_WORDING),
        ),
    )
    assert match_request_red_flags("and tomorrow?", prior, DEFAULT_RED_FLAG_RULES) == ()


def test_the_current_utterance_still_matches_when_prior_turns_are_present() -> None:
    prior = (PriorTurn(utterance=SecretStr("hello"), guidance=SecretStr("hi")),)
    assert match_request_red_flags("I can't breathe", prior, DEFAULT_RED_FLAG_RULES) != ()


def test_request_markers_are_deduplicated_across_turns() -> None:
    prior = (
        PriorTurn(utterance=SecretStr("I can't breathe"), guidance=SecretStr("Seek care.")),
    )
    assert match_request_red_flags("I can't breathe", prior, DEFAULT_RED_FLAG_RULES) == (
        "severe_breathlessness",
    )


def test_no_prior_turns_is_the_same_as_matching_the_utterance_alone() -> None:
    both = match_request_red_flags("I can't breathe", (), DEFAULT_RED_FLAG_RULES)
    alone = match_red_flags("I can't breathe", DEFAULT_RED_FLAG_RULES)
    assert both == alone
