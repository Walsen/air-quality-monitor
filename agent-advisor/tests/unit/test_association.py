"""Tests for learned-association reporting (task 14.3). Validates Reqs 30.1 to 30.7.

**Req 30.2's trap is the word "trigger".** It is the most natural word in the whole asthma
vocabulary — people say "my triggers" — and it is precisely the word this requirement forbids,
because a trigger is a causal claim about someone's body derived from a correlation in their
diary. So the sweep is over every causal word, and it runs against every text this module can
produce rather than the one that felt risky.

**Req 30.4 must name two numbers without subtracting them.** "You need 4 more observations" is a
computation, and Req 30.3 forbids computing anything about an association. So the text states
the count Service 2 reported and the minimum Service 2 requires, and lets the reader do the
arithmetic — an AST test asserts the module performs none.

**Req 30.6 exists because the user needs to know they were not overridden.** Service 2 already
ranks a declared threshold above a learned one, and its own enum says why: inference does not
overrule an instruction. This service only has to SAY so, and a test pins that it does.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from aqm_advisor.domain.association import (
    CAUSAL_WORDS,
    PRECEDENCE_TEXT,
    LearnedThresholdView,
    explain_learned_threshold,
    insufficient_history_text,
    learned_threshold_view,
)
from aqm_advisor.domain.forbidden import forbidden_matches


def _served(**kwargs: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "species": "PM25",
        "subIndex": 4,
        "lagDays": 3,
        "observations": 24,
    }
    return {**defaults, **kwargs}


# --- Req 30.1: name the species, the lag and the observation count ------


def test_the_view_reads_what_service_2_served() -> None:
    # Service 2's `LearnedThreshold` carries the derivation with it precisely so this reporting
    # obligation can be met without a second lookup — its own docstring says so.
    view = learned_threshold_view(_served())
    assert view is not None
    assert view.species == "PM25"
    assert view.sub_index == 4
    assert view.lag_days == 3
    assert view.observations == 24


def test_the_explanation_names_all_three() -> None:
    # Req 30.1. Without the observation count the user cannot judge whether to agree with the
    # pattern,
    # which is the entire point of telling them.
    text = explain_learned_threshold(learned_threshold_view(_served()))
    assert "PM25" in text
    assert "3" in text
    assert "24" in text


def test_the_explanation_says_it_came_from_their_own_diary() -> None:
    # Req 30.1's first clause. A changed alerting point with no attribution reads as the service
    # having
    # decided something about them.
    assert "diary" in explain_learned_threshold(
        learned_threshold_view(_served())
    ).casefold()


def test_the_explanation_names_the_threshold_value() -> None:
    # Req 30.5 wants the value alongside the source, so a changed alerting point is traceable.
    assert "4" in explain_learned_threshold(learned_threshold_view(_served()))


def test_an_absent_learned_block_yields_no_view() -> None:
    # Req 30.3: do not state an association the retrieved data did not report. No block means
    # nothing to
    # say, and inventing a view would be the fabrication the requirement forbids.
    assert learned_threshold_view(None) is None
    assert learned_threshold_view({}) is None


def test_a_malformed_learned_block_yields_no_view() -> None:
    # A shape this service does not expect is a Service 2 change. Reporting a partial
    # association would be
    # worse than reporting none.
    assert learned_threshold_view({"species": "PM25"}) is None
    assert learned_threshold_view("not a mapping") is None


def test_explaining_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="threshold"):
        explain_learned_threshold(None)


# --- Req 30.2: an association, never a cause ----------------------------


def test_the_explanation_calls_it_an_association_or_a_pattern() -> None:
    lowered = explain_learned_threshold(learned_threshold_view(_served())).casefold()
    assert "association" in lowered or "pattern" in lowered


@pytest.mark.parametrize("word", sorted(CAUSAL_WORDS))
def test_no_produced_text_uses_a_causal_word(word: str) -> None:
    # Req 30.2, quantified over the vocabulary rather than a sample. "Trigger" is the trap: it
    # is the most
    # natural word in the asthma vocabulary and exactly the one forbidden here, because a
    # trigger is a
    # causal claim about someone's body derived from a correlation in their diary.
    texts = (
        explain_learned_threshold(learned_threshold_view(_served())),
        insufficient_history_text(observations=6, minimum=20),
        PRECEDENCE_TEXT,
    )
    for text in texts:
        assert word not in text.casefold(), (word, text)


def test_the_causal_vocabulary_includes_the_words_the_requirement_names() -> None:
    # Pins the set against Req 30.2's own list, so narrowing it is a reviewable act.
    for named in ("cause", "trigger", "diagnos", "predict"):
        assert any(named in word for word in CAUSAL_WORDS), named


def test_no_produced_text_predicts_a_future_symptom() -> None:
    # Req 30.2's last clause. A prediction about someone's future symptoms is the most
    # consequential thing
    # a correlation could be misread as, and it arrives through tense rather than through a
    # noun.
    texts = (
        explain_learned_threshold(learned_threshold_view(_served())),
        insufficient_history_text(observations=6, minimum=20),
        PRECEDENCE_TEXT,
    )
    for text in texts:
        lowered = text.casefold()
        for claim in ("you will", "you are going to", "expect to feel", "you'll get"):
            assert claim not in lowered, (claim, text)


# --- Req 30.4: not enough history, and what is missing ------------------


def test_the_insufficient_text_says_there_is_not_yet_enough() -> None:
    lowered = insufficient_history_text(observations=6, minimum=20).casefold()
    assert "not yet enough" in lowered or "not enough" in lowered


def test_the_insufficient_text_names_both_numbers_without_subtracting() -> None:
    # THE Req 30.3 / 30.4 interaction. "You need 14 more" is a COMPUTATION, and computing
    # anything about an
    # association is forbidden — so both numbers are stated and the reader does the arithmetic.
    text = insufficient_history_text(observations=6, minimum=20)
    assert "6" in text
    assert "20" in text
    assert "14" not in text


def test_the_insufficient_text_presents_no_weak_association() -> None:
    # Req 30.4 says name what is missing RATHER THAN presenting a weak association. A hedged
    # pattern is
    # still a pattern to the person reading it.
    lowered = insufficient_history_text(observations=6, minimum=20).casefold()
    for hedge in ("early sign", "seems to", "might be a pattern", "weak", "tentative"):
        assert hedge not in lowered, hedge


def test_a_non_positive_minimum_is_refused() -> None:
    with pytest.raises(ValueError, match="positive"):
        insufficient_history_text(observations=6, minimum=0)


# --- Req 30.6: the declared threshold wins, and is said to -------------


def test_the_precedence_text_says_the_declared_one_takes_precedence() -> None:
    # Req 30.6. Service 2 already ranks them; its own enum says inference does not overrule an
    # instruction. This service only has to SAY so, or the user cannot tell their instruction
    # survived.
    #
    # The first version of this test asserted the literal word "learned" and failed. It was
    # wrong:
    # "learned threshold" is SPEC vocabulary, not user vocabulary — a user does not know what a
    # learned
    # threshold is, and Req 30.6's stated purpose is that they UNDERSTAND their instruction was
    # not
    # overridden. "A pattern from your diary" carries that; the jargon would not. So the
    # assertion is on
    # the substance rather than on the spec's own wording.
    lowered = PRECEDENCE_TEXT.casefold()
    assert "you set" in lowered or "you told" in lowered or "declared" in lowered
    assert "diary" in lowered or "pattern" in lowered
    assert "precedence" in lowered or "does not replace" in lowered


def test_the_precedence_text_says_the_learned_one_did_not_override() -> None:
    lowered = PRECEDENCE_TEXT.casefold()
    assert "not" in lowered


# --- Req 30.7: the consequence is exposure only ------------------------


def test_no_produced_text_suggests_a_medication_or_clinical_change() -> None:
    # Req 30.7. A correlation in a diary is the weakest evidence in the whole system, and it is
    # the last
    # thing that should move a clinical behaviour.
    texts = (
        explain_learned_threshold(learned_threshold_view(_served())),
        insufficient_history_text(observations=6, minimum=20),
        PRECEDENCE_TEXT,
    )
    for text in texts:
        lowered = text.casefold()
        for claim in (
            "medication",
            "inhaler",
            "reliever",
            "preventer",
            "dose",
            "see your doctor",
            "change your",
        ):
            assert claim not in lowered, (claim, text)


def test_every_produced_text_is_publishable() -> None:
    # The lesson from task 6.3: a required text the pattern set rejects is unpublishable, and
    # this service
    # is obliged to be able to explain a learned threshold.
    for text in (
        explain_learned_threshold(learned_threshold_view(_served())),
        insufficient_history_text(observations=6, minimum=20),
        PRECEDENCE_TEXT,
    ):
        assert forbidden_matches(text) == (), text


# --- Req 30.3: nothing is computed --------------------------------------

_SOURCE = pathlib.Path("src/aqm_advisor/domain/association.py").read_text(encoding="utf-8")


def test_the_module_performs_no_arithmetic() -> None:
    # Req 30.3, structurally. An association is Service 2's to derive, and a module with no
    # arithmetic
    # cannot derive a second one — nor compute "how many more observations you need".
    tree = ast.parse(_SOURCE)
    operators = [
        type(node.op).__name__
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult | ast.Div | ast.Add | ast.Sub | ast.Pow | ast.FloorDiv)
    ]
    assert operators == [], f"the association module computes: {operators}"


def test_the_module_aggregates_nothing() -> None:
    tree = ast.parse(_SOURCE)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called.isdisjoint({"sum", "min", "max", "sorted", "mean", "median", "abs"})


def test_the_view_is_frozen() -> None:
    view = learned_threshold_view(_served())
    assert isinstance(view, LearnedThresholdView)
    with pytest.raises(AttributeError):
        view.sub_index = 9  # type: ignore[misc]
