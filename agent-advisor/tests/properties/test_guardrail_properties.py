"""Properties 5 and 6: guardrail verdict totality, and medication naming closure.

Tasks 6.4 and 6.5. Validates Reqs 8.2, 29.1, 29.3, 29.6, 34.4, 34.7.

**Property 5 is about totality, and totality here means never raising.** A checker that throws
on some input is worse than one that rejects it: the exception propagates out of the
verification step, and a verifier that crashed has established nothing about the text. So the
property quantifies over arbitrary text — including the shapes that break naive regex code, like
a lone backslash or an unbalanced bracket — and asserts a verdict comes back every time.

**Property 6 is a closure claim.** The permitted set is exactly the medications retrieved THIS
turn, so the property is that nothing outside that set survives: for any text and any listed
set, every known medication token the text names which is not listed gets reported. Quantified
in both directions, because a reporter that flagged everything would satisfy the closure half
while making it impossible to mention a medication the user actually takes.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from aqm_advisor.adapters.local import LocalGuardrailChecker
from aqm_advisor.domain.forbidden import (
    KNOWN_MEDICATION_TOKENS,
    administration_near_medication,
    forbidden_matches,
    unlisted_medications,
)
from aqm_advisor.ports.protocols import GuardrailVerdict

_MEDICATIONS = sorted(KNOWN_MEDICATION_TOKENS)

_HOSTILE_TEXT = st.text(
    alphabet=st.characters(
        codec="utf-8",
        categories=("L", "N", "P", "S", "Zs"),
    ),
    max_size=120,
)
"""Arbitrary text including punctuation and symbols.

Deliberately includes the characters that break naive pattern code — a lone backslash, an
unbalanced bracket, a stray quantifier — because a checker that raises on one of those has
established nothing about the text, and the exception surfaces from the verification step rather
than as a rejection.
"""


# --- Property 5: guardrail verdict totality -----------------------------


@given(_HOSTILE_TEXT)
def test_the_checker_always_returns_a_verdict(text: str) -> None:
    # THE totality claim. Never raising matters more than never being wrong: an exception out of
    # the
    # verification step means no decision was made about the text at all.
    result = LocalGuardrailChecker().check(text)
    assert result.verdict in set(GuardrailVerdict)


_FORBIDDEN_TEXTS = [
    "you are having an asthma attack",
    "you have a chest infection",
    "take two puffs of your reliever",
    "increase your preventer dose to four puffs",
    "you are not having an exacerbation",
]
"""Texts the pattern set is meant to reject.

These exist because the hostile-text strategy alone made the intervention property VACUOUS:
random punctuation never produces a forbidden clinical claim, so the `INTERVENED` branch was
never entered and the property held without testing anything. Quantifying over known-bad texts
is what gives that branch inputs.
"""


@given(st.sampled_from(_FORBIDDEN_TEXTS))
def test_an_intervention_always_names_a_category(text: str) -> None:
    # Req 34.4's audit needs the category. An intervention with none could not be explained, and
    # the
    # metrics label would collapse to a single bucket.
    result = LocalGuardrailChecker().check(text)
    assert result.verdict is GuardrailVerdict.INTERVENED
    assert result.categories != ()


@given(_HOSTILE_TEXT)
def test_a_verdict_carries_categories_only_when_it_intervened(text: str) -> None:
    # The invariant across ALL text: categories and the verdict cannot disagree. A passing
    # verdict with a
    # category would read, to the audit, exactly like an intervention.
    result = LocalGuardrailChecker().check(text)
    if result.verdict is GuardrailVerdict.PASSED:
        assert result.categories == ()


def test_a_pass_never_carries_a_category() -> None:
    # A fixed example alongside the property above, pinning one known-clean text end to end.
    result = LocalGuardrailChecker().check("the air quality is moderate today")
    assert result.verdict is GuardrailVerdict.PASSED
    assert result.categories == ()


@given(_HOSTILE_TEXT)
def test_the_pattern_matcher_never_raises(text: str) -> None:
    # The layer beneath the checker, quantified separately: a pattern set is configuration (Req
    # 8.7), so
    # a text that breaks it would be a configuration change breaking the safety check.
    assert isinstance(forbidden_matches(text), tuple)


@given(_HOSTILE_TEXT)
def test_the_verdict_agrees_with_the_underlying_matcher(text: str) -> None:
    # The checker delegates to the domain (asserted structurally elsewhere); this quantifies
    # that the
    # delegation is faithful, so the two cannot drift into disagreeing about the same text.
    result = LocalGuardrailChecker().check(text)
    expected = (
        GuardrailVerdict.INTERVENED if forbidden_matches(text) else GuardrailVerdict.PASSED
    )
    assert result.verdict is expected


def test_a_forbidden_claim_is_actually_intervened_on() -> None:
    # Non-vacuity for every property above. If the checker passed everything, all of them would
    # hold.
    result = LocalGuardrailChecker().check("you are having an asthma attack right now")
    assert result.verdict is GuardrailVerdict.INTERVENED


# --- Property 6: medication naming closure ------------------------------


@given(
    st.lists(st.sampled_from(_MEDICATIONS), max_size=4),
    st.frozensets(st.sampled_from(_MEDICATIONS), max_size=4),
)
def test_every_named_medication_outside_the_listed_set_is_reported(
    named: list[str], listed: frozenset[str]
) -> None:
    # THE closure claim (Req 29.3). The permitted set is exactly what was retrieved this turn,
    # so
    # anything else the text names must come back.
    text = "keep your " + " and your ".join(named) + " with you" if named else "no names"
    reported = {token.casefold() for token in unlisted_medications(text, listed)}
    for token in named:
        if token.casefold() not in {entry.casefold() for entry in listed}:
            assert token.casefold() in reported, token


@given(st.frozensets(st.sampled_from(_MEDICATIONS), min_size=1, max_size=4))
def test_a_medication_from_the_retrieved_list_is_permitted(
    listed: frozenset[str],
) -> None:
    # The other direction, and the one that makes closure useful rather than merely safe. A
    # reporter
    # flagging everything would satisfy the property above while making Req 29.1's permitted
    # mention
    # impossible.
    text = "keep your " + " and your ".join(sorted(listed)) + " with you"
    assert unlisted_medications(text, listed) == ()


@given(st.lists(st.sampled_from(_MEDICATIONS), max_size=3))
def test_an_empty_listed_set_permits_no_medication_at_all(named: list[str]) -> None:
    # Req 29.6's case: no profile retrieved means no medication may be named. The failure mode
    # is naming
    # one from an earlier turn's memory, which this closes by having nothing permitted.
    text = " ".join(named) if named else ""
    reported = unlisted_medications(text, frozenset())
    assert len(reported) == len({token.casefold() for token in named})


@given(_HOSTILE_TEXT, st.frozensets(st.sampled_from(_MEDICATIONS), max_size=3))
def test_the_reporter_never_raises_on_arbitrary_text(
    text: str, listed: frozenset[str]
) -> None:
    # Same reasoning as Property 5: a crash here means no closure decision was made.
    assert isinstance(unlisted_medications(text, listed), tuple)


@given(st.sampled_from(_MEDICATIONS))
def test_an_administration_instruction_is_detected_for_any_medication(
    medication: str,
) -> None:
    # Req 29.1 permits naming a medication as PREPAREDNESS only. "Take your X" is an instruction
    # whatever
    # X is, so the detector must not depend on which medication was named.
    assert administration_near_medication(f"take your {medication} now") is True


@given(st.sampled_from(_MEDICATIONS))
def test_preparedness_wording_is_not_an_administration_instruction(
    medication: str,
) -> None:
    # Non-vacuity: a detector that fired on every mention would make Req 29.1's permitted form
    # unpublishable, which is the same failure the `you have` defect caused for the clinician
    # text.
    assert administration_near_medication(f"keep your {medication} with you") is False
