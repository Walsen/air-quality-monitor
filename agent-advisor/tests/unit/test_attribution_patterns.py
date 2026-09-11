"""Tests for causal-attribution patterns.

Req 30.2's generated-output enforcement, plus Req 21.5.

**Req 30.2 forbids a CLAIM, not a WORD.** "THE Service SHALL describe an Exposure_Association as
an association or a pattern, and SHALL NOT describe it as a cause, a trigger, a diagnosis, or a
prediction about the user's future symptoms." The prohibition is on binding a causal word TO THE
USER. Req 21.5 draws the same line from the other side — "SHALL NOT name a specific allergen as
the user's trigger" — where again it is the possessive that offends.

**A word ban would make a required text unpublishable.** `domain/actions.py` already ships, as
service-authored guidance, "Both irritant and allergic triggers can matter on the same day, so
it is worth watching how you respond rather than assuming one cause." That text uses `trigger`
AND `cause`, and it is *anti*-causal — it exists to tell someone NOT to assume one cause.
Banning the nouns would delete the very text that does the right thing. This is the fourth time
a broad guardrail has nearly made a required text unpublishable, so the action templates now
join task 6.3's required-texts sweep, which they had never been in.

**Why this belongs in the Forbidden_Claim set.** Req 8.2 checks generated Guidance against the
pattern set before returning it, and Req 8.7 makes that set configuration. So the pattern set IS
the sanctioned extension point for a generated-output rule, and Req 30.2 needed no new mechanism
— only entries. (An earlier note in `tasks.md` claimed task 18 owned this. That was wrong: task
18 is the asynchronous association trigger.)
"""

from __future__ import annotations

import pathlib

import pytest

from aqm_advisor.domain.actions import CONDITION_ACTIONS
from aqm_advisor.domain.forbidden import (
    ATTRIBUTION_PATTERNS,
    DEFAULT_FORBIDDEN_PATTERNS,
    forbidden_matches,
)

# --- the claim is caught ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "PM2.5 is your trigger.",
        "Pollen is the trigger for your symptoms.",
        "That pollution triggered your cough.",
        "NO2 triggers your wheeze.",
        "The particulates caused your symptoms yesterday.",
        "PM2.5 causes your breathlessness.",
        "This is causing your cough.",
        "Your symptoms are because of the ozone.",
        "The high reading is responsible for your wheeze.",
        "Ozone is what makes you worse.",
        "That exposure will make you wheeze tomorrow.",
        "Expect your symptoms to worsen tomorrow.",
        "You will have symptoms tomorrow.",
        "This predicts your symptoms for the next few days.",
        "Dust mites are your allergen trigger.",
    ],
)
def test_a_causal_claim_about_the_user_is_rejected(text: str) -> None:
    # Req 30.2 and Req 21.5. Each of these binds a causal word to THE USER, which is the claim a
    # diary correlation cannot support — and the prediction cases are the most consequential
    # thing
    # a correlation could be misread as.
    assert "causal_attribution" in forbidden_matches(text), text


# --- the required texts survive ----------------------------------------


def _action_texts() -> tuple[str, ...]:
    """Every action template this service can emit."""
    return tuple(
        template.text
        for templates in CONDITION_ACTIONS.values()
        for template in templates
    )


@pytest.mark.parametrize("text", _action_texts())
def test_no_action_template_is_rejected_by_its_own_guardrails(text: str) -> None:
    # THE reason this rule is pattern-based rather than word-based. One template says "Both
    # irritant and allergic triggers can matter on the same day, so it is worth watching how you
    # respond rather than assuming one cause" — it uses both nouns and is ANTI-causal, existing
    # precisely to stop someone assuming one cause. A word ban would delete it.
    #
    # The action templates had never been in task 6.3's sweep at all, which is the gap that
    # would
    # have let this break silently.
    assert forbidden_matches(text) == (), text


def test_the_anti_causal_template_is_actually_present() -> None:
    # Guards the test above from becoming vacuous if the template is reworded. The test is only
    # meaningful while a template really does use these nouns.
    joined = " ".join(_action_texts()).casefold()
    assert "trigger" in joined
    assert "cause" in joined


def test_the_association_module_texts_survive() -> None:
    # The service is OBLIGED to be able to explain a learned threshold (Req 30.1). A rule that
    # rejected its own explanation would make the requirement unmeetable.
    from aqm_advisor.domain.association import (
        PRECEDENCE_TEXT,
        explain_learned_threshold,
        insufficient_history_text,
        learned_threshold_view,
    )

    view = learned_threshold_view(
        {"species": "PM25", "subIndex": 4, "lagDays": 3, "observations": 24}
    )
    for text in (
        explain_learned_threshold(view),
        insufficient_history_text(observations=6, minimum=20),
        PRECEDENCE_TEXT,
    ):
        assert forbidden_matches(text) == (), text


@pytest.mark.parametrize(
    "text",
    [
        # Generic, non-attributive uses of the same nouns.
        "Both irritant and allergic triggers can matter on the same day.",
        "It is worth watching how you respond rather than assuming one cause.",
        "Common triggers include cold air and pollen.",
        "Air quality is one of many things that can matter for symptoms.",
        # The association framing Req 30.2 explicitly PERMITS.
        "Your entries line up with PM25 readings about 3 days earlier.",
        "It is a pattern in what you recorded, not a statement about why you felt that way.",
        "There is not yet enough diary history to draw a pattern for you.",
        # Forecast attribution, which Req 15 requires.
        "Tomorrow's forecast is for higher PM2.5.",
    ],
)
def test_permitted_framing_is_not_rejected(text: str) -> None:
    # The false-positive boundary, stated as tests rather than hoped for. A guardrail that
    # rejected these would push the model toward vaguer prose, which is a worse answer, not a
    # safer one.
    assert forbidden_matches(text) == (), text


# --- the set's shape ---------------------------------------------------


def test_the_attribution_patterns_are_in_the_default_set() -> None:
    # Req 8.2 checks generated Guidance against the DEFAULT set, so a pattern outside it would
    # never run. Req 8.7's replace-not-extend rule means a deployment that overrides the set
    # takes
    # responsibility for all of it, which is why membership here is what matters.
    for pattern in ATTRIBUTION_PATTERNS:
        assert pattern in DEFAULT_FORBIDDEN_PATTERNS, pattern


def test_the_category_is_reported_and_the_text_is_not() -> None:
    # Req 8.6: log the pattern CATEGORY, never the rejected text — the rejected text is the
    # thing
    # that must not be recorded.
    categories = forbidden_matches("PM2.5 is your trigger and it caused your cough.")
    assert categories == ("causal_attribution",)
    assert all("trigger" not in category for category in categories)


def test_a_diagnosis_claim_still_reports_its_own_category() -> None:
    # The new category must not swallow the existing ones.
    assert "diagnosis" in forbidden_matches("You are having an asthma attack.")


def test_every_pattern_compiles_and_is_case_insensitive() -> None:
    import re

    for pattern in ATTRIBUTION_PATTERNS:
        re.compile(pattern)
    assert "causal_attribution" in forbidden_matches("pm2.5 IS YOUR TRIGGER")


# --- a coverage FLOOR, not a completeness claim -------------------------

_KNOWN_FORBIDDEN_SHAPES: tuple[str, ...] = (
    # Possessive trigger, both word orders, hedged and not.
    "PM2.5 is your trigger.",
    "Your trigger is ragweed.",
    "Pollen is the trigger for your symptoms.",
    "Grass pollen appears to be your trigger.",
    "It looks like tree pollen triggers you.",
    "NO2 triggers your wheeze.",
    "That pollution triggered your cough.",
    # Causal verbs beyond `cause` and `trigger`.
    "High PM2.5 aggravates your asthma.",
    "The smoke worsens your breathing.",
    "Traffic fumes irritate your airways.",
    "Ozone provokes your wheeze.",
    "Cold air flares your symptoms.",
    "The pollution brings on your cough.",
    "Pollen sets off your symptoms.",
    "The ozone sets you off.",
    "You react to pollen.",
    "Your body reacts to nitrogen dioxide.",
    # Passive voice.
    "Your cough was caused by today's ozone.",
    "Your symptoms were brought on by the high pollen.",
    "Your wheeze has been triggered by the traffic fumes.",
    # Nominalised and idiomatic attribution.
    "The cause of your symptoms is the pollen.",
    "The reason for your wheeze is the poor air.",
    "Pollen is behind your symptoms.",
    "The air quality is to blame for your breathlessness.",
    "That explains your cough.",
    "The high reading is responsible for your wheeze.",
    # Prediction about the user's future symptoms.
    "You will feel worse this afternoon.",
    "You will get symptoms tomorrow.",
    "Tomorrow you are going to struggle to breathe.",
    "Expect your symptoms to worsen tomorrow.",
    "This predicts your symptoms for the next few days.",
    "That exposure will make you wheeze.",
)
"""Shapes known to be caught. NOT a claim that all forbidden claims are.

The framing matters more than the list. An adversarial review composed 77 sentences an LLM would
plausibly produce and the first version of these patterns caught 6 — so a test named "every
causal claim is rejected" would be exactly the vacuous guarantee this codebase keeps getting
bitten by. What this corpus does is stop a future pattern edit from silently regressing a shape
that IS handled. Req 30.2's authority is Req 34.5's managed guardrail; see
`ATTRIBUTION_PATTERNS`.
"""

_MUST_REMAIN_SAYABLE: tuple[str, ...] = (
    # Exposure framing the system prompt explicitly asks for (Reqs 14.2, 14.4). Every one of
    # these
    # was REJECTED by the first version's unbound prediction pattern.
    "If you go out in the morning you will get less exposure.",
    "By choosing a quieter street you will get away from the worst of the traffic.",
    "You will feel more comfortable exercising earlier in the day.",
    "You will experience cleaner air away from the main road.",
    "Going earlier, you will get the benefit of lower afternoon ozone.",
    # Preparedness language Req 8.4 permits.
    "You will have your reliever with you, which is sensible today.",
    "It is worth keeping your reliever to hand today.",
    # Generic, non-attributive use of the same nouns.
    "Both irritant and allergic triggers can matter on the same day.",
    "It is worth watching how you respond rather than assuming one cause.",
    "Common triggers include cold air and pollen.",
    # The association framing Req 30.2 PERMITS.
    "Your entries line up with PM25 readings about 3 days earlier.",
    "It is a pattern in what you recorded, not a statement about why you felt that way.",
    "There is not yet enough diary history to draw a pattern for you.",
    # Forecast attribution Req 15 requires.
    "Tomorrow's forecast is for higher PM2.5.",
    "The provider's forecast suggests higher particulates overnight.",
    # Clinician deference Req 11 requires.
    "It is worth raising this with your clinician.",
)
"""Sentences a well-behaved advisor MUST still be able to say.

This is the half that is easy to forget. Req 8.2 DISCARDS a response the pattern set matches, so
a false positive does not yield a hedged answer — it yields NO answer, silently. A guardrail
that pushes the model toward vaguer prose has made the product worse without making it safer.
"""


@pytest.mark.parametrize("text", _KNOWN_FORBIDDEN_SHAPES)
def test_a_known_forbidden_shape_is_caught(text: str) -> None:
    assert "causal_attribution" in forbidden_matches(text), text


@pytest.mark.parametrize("text", _MUST_REMAIN_SAYABLE)
def test_legitimate_guidance_is_never_discarded(text: str) -> None:
    assert forbidden_matches(text) == (), text


def test_the_pattern_set_documents_its_own_incompleteness() -> None:
    # The honesty of the docstring is load-bearing, not decoration. If it ever reads as complete
    # enforcement, a later author will treat Req 30.2 as closed and drop the managed guardrail
    # that is actually the authority — so the disclaimer is asserted rather than trusted.
    from aqm_advisor.domain import forbidden

    source = pathlib.Path("src/aqm_advisor/domain/forbidden.py").read_text(encoding="utf-8")
    assert "NOT COMPLETE ENFORCEMENT" in source
    assert "34.5" in source, "the docstring must name the guardrail that IS the authority"
    assert forbidden.ATTRIBUTION_PATTERNS, "the set must not be empty"
