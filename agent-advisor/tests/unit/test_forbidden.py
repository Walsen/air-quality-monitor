"""Tests for forbidden claims and medication closure (tasks 6.1, 6.2, 6.3).

`test_the_required_texts_are_not_rejected_by_the_pattern_set` is task 6.3 and the one most
likely to catch a real defect. The emergency guidance Req 8.4 REQUIRES must mention a reliever
inhaler — so a bare medication-word pattern makes the text the service is obliged to publish
unpublishable. That is a genuine risk, not a hypothetical: the same wording already exposed a
false negative in the red-flag matcher.

The direction of the asymmetry here is the opposite of grounding's. A false rejection costs a
repair attempt; a false acceptance ships a diagnosis or a dosing instruction. So the patterns
are broad — and the non-vacuity tests below exist because a pattern set that rejected ordinary
advice would be equally useless.
"""

from __future__ import annotations

import pytest

from aqm_advisor.adapters.local import DEFAULT_DISCLAIMER, DEFAULT_EMERGENCY_GUIDANCE
from aqm_advisor.domain.forbidden import (
    ATTRIBUTION_PATTERNS,
    DEFAULT_FORBIDDEN_PATTERNS,
    DIAGNOSIS_PATTERNS,
    DOSING_PATTERNS,
    KNOWN_MEDICATION_TOKENS,
    administration_near_medication,
    forbidden_matches,
    unlisted_medications,
)

# Imported, NOT re-typed. A review found these as hand-copied string literals, which meant the
# sweep asserted over a COPY: if the shipped constant changed, this file would keep checking the
# old text and the real emitted string would go unchecked. Asserting on a copy of the thing
# under
# test is the same defect class as the duplicate `AdviceRecord` — it passes by luck.
_EMERGENCY = DEFAULT_EMERGENCY_GUIDANCE
_DISCLAIMER = DEFAULT_DISCLAIMER


# --- Req 8.2, 8.3: diagnosis assertions ---------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "You have asthma.",
        "You are having an asthma attack.",
        "This is an exacerbation.",
        "You probably have a chest infection.",
        "You do not have an infection.",
        "You are fine.",
    ],
)
def test_a_diagnosis_assertion_is_rejected(text: str) -> None:
    assert "diagnosis" in forbidden_matches(text)


def test_a_negative_diagnosis_is_rejected_too() -> None:
    # Req 8.3 forbids stating the user is NOT having a medical event as much as stating they
    # are.
    # Reassurance is a clinical determination and the more dangerous direction of the two.
    assert "diagnosis" in forbidden_matches("You are not having an attack, you are fine.")


# --- Req 8.4, 29.2: dosing and administration ---------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Take two puffs of your reliever.",
        "Use 2 puffs every 4 hours.",
        "You should double your dose today.",
        "Take another inhalation now.",
        "You need to take your preventer.",
        "Stop your steroid until the air clears.",
    ],
)
def test_a_dosing_instruction_is_rejected(text: str) -> None:
    assert "dosing" in forbidden_matches(text)


# --- non-vacuity: ordinary advice must pass -----------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Air quality near you is Moderate, driven by PM2.5.",
        "Consider moving your run to the evening, when levels are usually lower.",
        "Keeping your reliever inhaler to hand is sensible on a day like this.",
        "Your nearest sensor reports a sub-index of 68.",
        "Closing windows on the roadside of your home can reduce what gets in.",
        "The decision to use it is governed by the plan you agreed with your clinician.",
    ],
)
def test_ordinary_exposure_advice_passes(text: str) -> None:
    # Without these, a pattern set rejecting everything would satisfy every test above while
    # making the
    # service incapable of answering anyone.
    assert forbidden_matches(text) == ()


def test_the_default_pattern_set_is_populated() -> None:
    assert len(DIAGNOSIS_PATTERNS) >= 4
    assert len(DOSING_PATTERNS) >= 4
    assert len(ATTRIBUTION_PATTERNS) >= 4


def test_the_default_set_is_exactly_the_union_of_the_category_sets() -> None:
    # Asserted as a UNION over the category mapping rather than a sum of named lengths, so a
    # fifth
    # category is covered the moment it is added. The arithmetic version had to be edited by
    # hand
    # when the attribution set landed, which is the shape of an assertion that stops being
    # checked
    # because updating it is indistinguishable from fixing it.
    from aqm_advisor.domain.forbidden import _CATEGORY_BY_PATTERN

    assert set(DEFAULT_FORBIDDEN_PATTERNS) == set(_CATEGORY_BY_PATTERN)
    assert len(DEFAULT_FORBIDDEN_PATTERNS) == len(set(DEFAULT_FORBIDDEN_PATTERNS)), (
        "a pattern appears in two categories, so its reported category depends on order"
    )


def test_every_default_pattern_has_a_category() -> None:
    # A pattern with no category is reported as "other", which tells an operator nothing about
    # what the generation did wrong (Req 8.6 wants the category named).
    from aqm_advisor.domain.forbidden import _CATEGORY_BY_PATTERN

    for pattern in DEFAULT_FORBIDDEN_PATTERNS:
        assert _CATEGORY_BY_PATTERN.get(pattern) not in (None, "other"), pattern


# --- Req 8.6: a rejection names the category, never the text -------------

def test_a_rejection_names_only_the_category() -> None:
    result = forbidden_matches("Take two puffs of salbutamol now.")
    assert result == ("dosing",)
    assert "puffs" not in str(result)
    assert "salbutamol" not in str(result)


def test_categories_are_deduplicated_and_ordered() -> None:
    # Several dosing patterns can fire on one sentence; the category is reported once, and the
    # order is
    # defined so a log line is stable across runs.
    result = forbidden_matches("Take two puffs every 4 hours and double your dose.")
    assert result == ("dosing",)


def test_both_categories_can_be_reported_together() -> None:
    result = forbidden_matches("You are having an attack, so take two puffs.")
    assert set(result) == {"diagnosis", "dosing"}


# --- Req 8.7: a configured set REPLACES the defaults --------------------

def test_a_configured_set_replaces_rather_than_extends() -> None:
    # Extend-only configuration is not configuration: an operator finding a pattern misfiring
    # must be
    # able to remove it.
    assert forbidden_matches("Take two puffs.", patterns=(r"\bnever say this\b",)) == ()
    assert forbidden_matches("never say this", patterns=(r"\bnever say this\b",)) == ("other",)


def test_an_empty_configured_set_disables_the_check_visibly() -> None:
    # Not a silent no-op: an operator who configures nothing gets nothing, which configuration
    # validation should catch rather than this function pretending a default was meant.
    assert forbidden_matches("Take two puffs.", patterns=()) == ()


# --- task 6.3: the required texts must not be self-rejecting ------------

def test_the_required_texts_are_not_rejected_by_the_pattern_set() -> None:
    # TASK 6.3, GENERALISED. Req 8.4 requires the emergency guidance and Req 8.5 the disclaimer
    # on
    # every response, so a pattern set that rejected either would stop the service publishing
    # text
    # it is obliged to publish.
    #
    # This now sweeps EVERY text the service must emit, because the narrower version missed one.
    # Req 11.4's clinician suggestion begins "Since you have noticed a change over several
    # days",
    # and a bare `you have` diagnosis pattern rejected it — the pattern was keyed on the WORDS
    # rather than the CLAIM, and only a guard over all required texts finds that.
    import datetime as dt

    from aqm_advisor.domain.actions import CONDITION_ACTIONS
    from aqm_advisor.domain.association import (
        PRECEDENCE_TEXT,
        explain_learned_threshold,
        insufficient_history_text,
        learned_threshold_view,
    )
    from aqm_advisor.domain.attribution import unavailable_text
    from aqm_advisor.domain.deference import CLINICIAN_SUGGESTION_TEXT, DEFERENCE_TEXT
    from aqm_advisor.domain.degradation import missing_data_note
    from aqm_advisor.domain.diary import (
        NOTE_PURPOSE_TEXT,
        replacement_warning,
        restate_entry_for_confirmation,
    )
    from aqm_advisor.domain.elicitation import (
        DeclinedKind,
        MedicationEntry,
        ProfileDraft,
        decline_message,
        limit_rejection_message,
        restate_for_confirmation,
    )
    from aqm_advisor.domain.records import SymptomEntryDraft
    from aqm_advisor.domain.reporting import (
        GASEOUS_SAME_DAY_TEXT,
        PARTICULATE_LAG_TEXT,
        TIMING_TEMPLATES,
    )

    required = (
        _EMERGENCY,
        _DISCLAIMER,
        DEFERENCE_TEXT,
        CLINICIAN_SUGGESTION_TEXT,
        PARTICULATE_LAG_TEXT,
        GASEOUS_SAME_DAY_TEXT,
        # Req 7.3's unavailable-value sentence, added when it was written rather than left for a
        # later sweep to find — which is the whole point of having generalised this guard.
        unavailable_text("ozone"),
        unavailable_text("PM2.5"),
        # Req 21.7's missing-data note, for the same reason: a degraded turn must be
        # publishable,
        # and a degraded turn is exactly when the service can least afford a withheld response.
        missing_data_note(("the pollen count",)),
        missing_data_note(("PM2.5", "tomorrow's outlook")),
        # Reqs 27.3, 27.4 and 27.6. The highest-risk additions to this sweep so far: the dose
        # decline TALKS ABOUT doses ("never a dose or how often you take it"), which is exactly
        # the shape the dosing patterns look for. They pass because those patterns require a
        # medication object nearby and a message about what is NOT recorded names none — but
        # that
        # is a property of the current patterns, so it is pinned here rather than assumed.
        *(decline_message(kind) for kind in DeclinedKind),
        limit_rejection_message(field="medications", limit=5),
        # Req 28.4 and 28.5. The restatement is the interesting one: it says "you used your
        # reliever", which NAMES a medication role, and it passes only because
        # `administration_near_medication` distinguishes reporting a past action from
        # instructing
        # one. Req 29.1 permits naming a medication as preparedness, and reporting what the user
        # already did is neither an instruction nor a dose — pinned here so that stays true.
        NOTE_PURPOSE_TEXT,
        replacement_warning(dt.date(2026, 7, 1)),
        # Reqs 30.1, 30.4 and 30.6. The learned-threshold explanation carries four numerals and
        # the word "association", so it is exactly the kind of text a tightened pattern set
        # could
        # start rejecting.
        explain_learned_threshold(
            learned_threshold_view(
                {"species": "PM25", "subIndex": 4, "lagDays": 3, "observations": 24}
            )
        ),
        insufficient_history_text(observations=6, minimum=20),
        PRECEDENCE_TEXT,
        restate_entry_for_confirmation(
            SymptomEntryDraft(
                date=dt.date(2026, 7, 1),
                severity=3,
                markers=("cough", "wheeze"),
                reliever_used=True,
            )
        ),
        *TIMING_TEMPLATES,
        # Reqs 12.4/12.5's action templates. These had NEVER been in this sweep, which is the
        # gap
        # that would have let Req 30.2's new attribution patterns silently reject a required
        # text:
        # one template reads "Both irritant and allergic triggers can matter on the same day, so
        # it
        # is worth watching how you respond rather than assuming one cause", using both
        # forbidden
        # nouns to say the anti-causal thing the requirement wants said.
        *(
            template.text
            for templates in CONDITION_ACTIONS.values()
            for template in templates
        ),
        # Req 27.2's PROFILE restatement. A review found only the DIARY one in this sweep, so
        # the
        # profile confirmation — which interpolates the user's own condition, medication role
        # and
        # routine text — was a required emitted text sitting outside the guard.
        restate_for_confirmation(
            ProfileDraft(
                condition="asthma",
                medications=(MedicationEntry(name="salbutamol", role="reliever"),),
                confirmed=True,
            )
        ),
    )
    for text in required:
        assert forbidden_matches(text) == (), text
        assert not administration_near_medication(text), text


def test_the_required_text_sweep_covers_more_than_a_couple_of_strings() -> None:
    # Non-vacuity: the guard's value is its BREADTH. A version sweeping two strings is what let
    # the
    # clinician-suggestion defect through in the first place.
    from aqm_advisor.domain.deference import CLINICIAN_SUGGESTION_TEXT, DEFERENCE_TEXT
    from aqm_advisor.domain.reporting import TIMING_TEMPLATES

    assert len({_EMERGENCY, _DISCLAIMER, DEFERENCE_TEXT, CLINICIAN_SUGGESTION_TEXT}) == 4
    assert len(TIMING_TEMPLATES) >= 2



def test_the_required_emergency_text_survives_the_administration_check_too() -> None:
    # The same risk one layer over: "your reliever inhaler is not helping" puts a role word near
    # words
    # a naive adjacency rule would treat as administration.
    assert administration_near_medication(_EMERGENCY) is False


def test_the_disclaimers_mention_of_treatment_is_not_a_diagnosis() -> None:
    # The disclaimer contains the words "diagnosis" and "treatment" while denying them. A
    # pattern
    # keyed on the WORD rather than the claim would reject the sentence that exists to disclaim.
    assert "diagnosis" not in forbidden_matches(_DISCLAIMER)


# --- Req 29: medication closure -----------------------------------------

def test_an_unlisted_medication_is_caught() -> None:
    assert unlisted_medications("Keep your Symbicort to hand.", listed=("salbutamol",)) == (
        "symbicort",
    )


def test_a_listed_medication_is_permitted() -> None:
    assert unlisted_medications("Keep your salbutamol to hand.", listed=("salbutamol",)) == ()


def test_the_comparison_is_case_insensitive() -> None:
    assert unlisted_medications("Keep your Salbutamol to hand.", listed=("SALBUTAMOL",)) == ()


def test_a_brand_is_not_satisfied_by_a_listed_molecule() -> None:
    # Ventolin is salbutamol, but they are different words and the user recorded one of them.
    # Treating
    # them as interchangeable would be this service making a substitution judgement it must not
    # make.
    assert unlisted_medications("Keep your Ventolin to hand.", listed=("salbutamol",)) == (
        "ventolin",
    )


def test_with_no_retrieved_set_every_name_is_unlisted() -> None:
    # Req 29.7: with nothing retrieved, only generic role language is permitted.
    assert unlisted_medications("Keep your salbutamol to hand.", listed=()) == ("salbutamol",)


def test_generic_role_language_is_permitted_with_no_retrieved_set() -> None:
    # The other half of Req 29.7, and the non-vacuity guard for the test above.
    assert unlisted_medications("Keep your reliever inhaler to hand.", listed=()) == ()


def test_every_name_is_reported_not_just_the_first() -> None:
    found = unlisted_medications("Keep Symbicort and Ventolin to hand.", listed=())
    assert set(found) == {"symbicort", "ventolin"}


def test_the_vocabulary_is_populated() -> None:
    # Non-vacuity: an empty vocabulary would make every closure test above pass while
    # recognising
    # nothing at all.
    assert len(KNOWN_MEDICATION_TOKENS) >= 20
    assert "salbutamol" in KNOWN_MEDICATION_TOKENS


# --- Req 29.5: administration is rejected regardless of listing ----------

@pytest.mark.parametrize(
    "text",
    [
        "Take your salbutamol before you go out.",
        "Use your Ventolin if it gets worse.",
        "Increase your Symbicort today.",
        "Inhale your reliever before exercise.",
    ],
)
def test_administration_beside_a_medication_is_rejected(text: str) -> None:
    assert administration_near_medication(text) is True


def test_administration_is_rejected_even_for_a_listed_medication() -> None:
    # Req 29.5 TIGHTENS Req 8.4. Having the drug on the user's own list permits NAMING it in a
    # preparedness sentence; it never permits being told to use it.
    text = "Take your salbutamol now."
    assert unlisted_medications(text, listed=("salbutamol",)) == ()
    assert administration_near_medication(text) is True


def test_the_preparedness_construction_is_permitted() -> None:
    # The one construction Req 29.1/29.2 allows, and the non-vacuity guard for every rejection
    # above.
    text = "Having your salbutamol with you is sensible on a day like this."
    assert unlisted_medications(text, listed=("salbutamol",)) == ()
    assert administration_near_medication(text) is False
    assert forbidden_matches(text) == ()


def test_a_sentence_boundary_stops_the_adjacency_window() -> None:
    # Otherwise "keep your salbutamol to hand. Use the plan your clinician agreed" reads as an
    # instruction to use the salbutamol, and the required clinician-deference sentence (Req
    # 29.4)
    # could not be said in the same breath as naming the medication.
    text = "Keep your salbutamol to hand. Use the plan you agreed with your clinician."
    assert administration_near_medication(text) is False
