"""Properties 1 and 2: grounding totality, and that ungrounded generations are never returned.

Tasks 5.3 and 5.4. Validates Reqs 7.1, 7.2, 22.2, 34.8.

**Property 1 is a totality claim, not a correctness claim.** The interesting failure is not
"grounding got one wrong" — it is a numeral that grounding never classified AT ALL, because such
a numeral reaches the user without any decision having been made about it. So the property is
that the numerals of a text partition exactly into permitted and reported: nothing in both,
nothing in neither.

That framing is what makes the masking behaviour testable. Species names and units contain
digits (`PM25`, `ug/m3`), and a masked digit must not be reported as an ungrounded claim; but a
real number ADJACENT to a mask must still be classified. Both directions are quantified here
rather than pinned by example.

**Property 2 is quantified over the pipeline, not the checker.** That an ungrounded numeral is
DETECTED is Property 1's business. Property 2 is the stronger and separate claim that a detected
one never reaches a response — which depends on the fail-closed ledger and on `run` raising
before assembly, not on the grounding function at all.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aqm_advisor.agent.pipeline import TurnPipeline
from aqm_advisor.agent.verification import VerificationVerdict
from aqm_advisor.domain.grounding import (
    SPECIES_TOKENS,
    UNIT_TOKENS,
    normalise_numeral,
    numerals,
    permitted_values,
    ungrounded,
)
from aqm_advisor.domain.models import AdvisoryRequest, Escalation
from aqm_advisor.domain.records import RetrievedValues

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _values(numeral_set: frozenset[str]) -> RetrievedValues:
    return RetrievedValues(
        numerals=numeral_set,
        medications=frozenset(),
        pollen_categories=frozenset(),
        tool_calls=(),
    )


_NUMBERS = st.one_of(
    st.integers(min_value=0, max_value=999).map(str),
    st.decimals(
        min_value=0, max_value=999, places=1, allow_nan=False, allow_infinity=False
    ).map(str),
)
"""Numerals as a reading actually carries them: whole indices, one-decimal concentrations."""

_WORDS = st.sampled_from(
    ["the", "index", "is", "today", "reading", "at", "site", "band", "moderate"]
)


@st.composite
def _sentences(draw: st.DrawFn) -> tuple[str, tuple[str, ...]]:
    """A sentence plus the numerals it contains, so the test knows the ground truth."""
    parts: list[str] = []
    stated: list[str] = []
    for _ in range(draw(st.integers(min_value=0, max_value=6))):
        if draw(st.booleans()):
            number = draw(_NUMBERS)
            stated.append(number)
            parts.append(number)
        else:
            parts.append(draw(_WORDS))
    return " ".join(parts), tuple(stated)


# --- Property 1: grounding totality -------------------------------------


@given(_sentences(), st.frozensets(_NUMBERS, max_size=6))
def test_every_numeral_is_either_permitted_or_reported(
    sentence: tuple[str, tuple[str, ...]], retrieved: frozenset[str]
) -> None:
    # THE totality claim. A numeral in neither set would reach a user with no decision made
    # about it.
    text, _stated = sentence
    permitted = permitted_values(_values(retrieved), constants=())
    reported = set(ungrounded(text, permitted))
    for numeral in numerals(text):
        normalised = normalise_numeral(numeral)
        assert (normalised in permitted) or (numeral in reported), numeral


@given(_sentences(), st.frozensets(_NUMBERS, max_size=6))
def test_no_numeral_is_both_permitted_and_reported(
    sentence: tuple[str, tuple[str, ...]], retrieved: frozenset[str]
) -> None:
    # The other half of the partition. Reporting a permitted value would withhold a correct
    # answer.
    text, _stated = sentence
    permitted = permitted_values(_values(retrieved), constants=())
    for numeral in ungrounded(text, permitted):
        assert normalise_numeral(numeral) not in permitted


@given(_sentences())
def test_a_generation_quoting_only_retrieved_values_is_fully_grounded(
    sentence: tuple[str, tuple[str, ...]],
) -> None:
    # Non-vacuity. If nothing were ever grounded the properties above would hold trivially, and
    # the
    # service would be unable to state any reading at all.
    text, stated = sentence
    permitted = permitted_values(_values(frozenset(stated)), constants=())
    assert ungrounded(text, permitted) == ()


_MASKED_TOKENS = sorted({*SPECIES_TOKENS, *UNIT_TOKENS})
"""Every token grounding masks.

`SPECIES_TOKENS` and `UNIT_TOKENS` are TUPLES, so they are unpacked into a set here rather than
unioned: order matters for masking, and a tuple is the right shape there.
"""


@given(st.sampled_from(_MASKED_TOKENS), _NUMBERS)
def test_a_masked_token_never_reports_its_own_digits(token: str, number: str) -> None:
    # Species and units contain digits (`PM25`, `ug/m3`). Reporting those as ungrounded claims
    # would
    # make every correctly-worded sentence unpublishable.
    permitted = permitted_values(_values(frozenset({number})), constants=())
    assert ungrounded(f"{token} is {number}", permitted) == ()


@given(st.sampled_from(_MASKED_TOKENS))
def test_a_number_adjacent_to_a_mask_is_still_classified(token: str) -> None:
    # The dangerous direction: masking must not swallow a real claim beside it. `4242` was never
    # retrieved, so it must be reported even sitting next to a masked token.
    permitted = permitted_values(_values(frozenset()), constants=())
    assert "4242" in ungrounded(f"{token} is 4242", permitted)


@given(st.frozensets(_NUMBERS, max_size=6))
def test_a_text_with_no_numerals_is_never_ungrounded(retrieved: frozenset[str]) -> None:
    permitted = permitted_values(_values(retrieved), constants=())
    assert ungrounded("the air is worse than usual today", permitted) == ()


# --- Property 2: an ungrounded generation is never returned -------------


class _GroundingPipeline(TurnPipeline[frozenset[str], dict[str, object]]):
    """A pipeline that generates arbitrary text and verifies it by grounding alone."""

    def __init__(self, *, generation: str, retrieved: frozenset[str]) -> None:
        self._generation = generation
        self._retrieved = retrieved
        self.assembled = False
        self.verified = False

    def check_red_flags(self, request: AdvisoryRequest) -> Escalation | None:
        return None

    def retrieve(
        self, request: AdvisoryRequest, escalation: Escalation | None
    ) -> frozenset[str]:
        return self._retrieved

    def generate(
        self,
        request: AdvisoryRequest,
        escalation: Escalation | None,
        retrieved: frozenset[str],
    ) -> str | None:
        return self._generation

    def verify(
        self, generated: str | None, retrieved: frozenset[str]
    ) -> VerificationVerdict:
        self.verified = True
        permitted = permitted_values(_values(retrieved), constants=())
        failures = ungrounded(generated or "", permitted)
        return VerificationVerdict(
            passed=not failures, checks=("grounding",), failures=failures
        )

    def assemble(
        self,
        request: AdvisoryRequest,
        escalation: Escalation | None,
        retrieved: frozenset[str],
        generated: str | None,
        verdict: VerificationVerdict,
    ) -> dict[str, object]:
        self.assembled = True
        return {"guidance": generated}

    def audit(
        self,
        request: AdvisoryRequest,
        response: dict[str, object],
        retrieved: frozenset[str],
        verdict: VerificationVerdict,
    ) -> None:
        return None


def _request() -> AdvisoryRequest:
    return AdvisoryRequest(
        utterance="How is the air?",
        credential="a-credential",  # type: ignore[arg-type]
    )


@given(_sentences(), st.frozensets(_NUMBERS, max_size=4))
def test_an_ungrounded_generation_never_reaches_a_response(
    sentence: tuple[str, tuple[str, ...]], retrieved: frozenset[str]
) -> None:
    # Property 2, quantified over the PIPELINE. That the numeral was detected is Property 1's
    # business;
    # this is the separate claim that a detected one never gets published.
    text, _stated = sentence
    pipeline = _GroundingPipeline(generation=text, retrieved=retrieved)
    permitted = permitted_values(_values(retrieved), constants=())
    is_grounded = not ungrounded(text, permitted)

    if is_grounded:
        assert pipeline.run(_request())["guidance"] == text
        return
    with pytest.raises(RuntimeError, match="verif"):
        pipeline.run(_request())
    assert pipeline.assembled is False


@given(_sentences())
def test_a_grounded_generation_is_returned_unchanged(
    sentence: tuple[str, tuple[str, ...]],
) -> None:
    # Non-vacuity for the property above, and Req 7.2's other half: a grounded generation must
    # be
    # published AS WRITTEN. Silently editing it would be this service authoring guidance.
    text, stated = sentence
    pipeline = _GroundingPipeline(generation=text, retrieved=frozenset(stated))
    assert pipeline.run(_request())["guidance"] == text


@given(_sentences(), st.frozensets(_NUMBERS, max_size=4))
def test_verification_runs_whatever_the_generation_says(
    sentence: tuple[str, tuple[str, ...]], retrieved: frozenset[str]
) -> None:
    # Property 2 depends on verification actually being REACHED. A short-circuit that skipped
    # it for some texts would make the property unfalsifiable rather than true, so this observes
    # the step running instead of asserting something about the step's name.
    text, _stated = sentence
    pipeline = _GroundingPipeline(generation=text, retrieved=retrieved)
    with contextlib.suppress(RuntimeError):
        pipeline.run(_request())
    assert pipeline.verified is True
