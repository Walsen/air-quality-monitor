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
