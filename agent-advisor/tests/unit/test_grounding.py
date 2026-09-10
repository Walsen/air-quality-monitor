"""Tests for grounding of quantitative claims (task 5.1).

Req 7.1 requires every concentration, sub-index, band, threshold, dose and pollen category in
the guidance to be a value this turn actually retrieved, and Req 7.2 refuses to return a
generation that fails it. Req 34.8 forbids using a contextual grounding SCORE for this: the
comparison is decidable and this service performs it, because a similarity threshold cannot
decide whether a number was invented.

**The asymmetry runs the other way from red-flag matching.** There, a false positive costs one
unnecessary sentence. Here a false ACCEPTANCE ships an invented health-adjacent number, while a
false rejection costs one repair attempt. So the check is strict, and where strictness bites the
fix is to make the retrieval return the value — not to loosen the comparison.

The species tests are the ones that would otherwise bite immediately: `PM2.5` and `NO2` contain
digits, so a naive extractor would treat every mention of a pollutant as an invented number and
refuse all guidance.
"""

from __future__ import annotations

import pytest

from aqm_advisor.domain.grounding import (
    DEFAULT_STRUCTURAL_CONSTANTS,
    SPECIES_TOKENS,
    UNIT_TOKENS,
    normalise_numeral,
    numerals,
    permitted_values,
    ungrounded,
)
from aqm_advisor.domain.records import RetrievedValues, ToolCall


def _retrieved(*values: str) -> RetrievedValues:
    return RetrievedValues(
        numerals=frozenset(values),
        medications=frozenset(),
        pollen_categories=frozenset(),
        tool_calls=(ToolCall(name="air_quality"),),
    )


# --- extraction ---------------------------------------------------------

def test_a_plain_integer_is_extracted() -> None:
    assert numerals("the sub-index is 68 today") == ("68",)


def test_a_decimal_is_extracted() -> None:
    assert numerals("the corrected value is 18.2 ug/m3") == ("18.2",)


def test_several_numerals_come_back_in_order() -> None:
    # Order is defined so a rejection message and a log line are stable across runs.
    assert numerals("68 then 18.2 then 101") == ("68", "18.2", "101")


def test_a_percentage_is_extracted_without_its_sign() -> None:
    assert numerals("humidity is 72%") == ("72",)


def test_text_with_no_numerals_yields_nothing() -> None:
    assert numerals("air quality is moderate today") == ()


# --- the species trap ---------------------------------------------------

@pytest.mark.parametrize(
    "species",
    ["PM2.5", "PM25", "PM10", "NO2", "O3", "SO2", "CO2"],
)
def test_a_species_name_contributes_no_numeral(species: str) -> None:
    # THE test that keeps this check usable. Every one of these contains digits, so a naive
    # extractor
    # would read "PM2.5" as the invented number 2.5 and refuse guidance for mentioning a
    # pollutant.
    assert numerals(f"{species} is the driving pollutant") == ()


def test_a_real_claim_beside_a_species_name_is_still_extracted() -> None:
    # The masking must not swallow the number next to it — that would be the opposite failure,
    # and a
    # silent one, since the check would pass on an invented value.
    assert numerals("PM2.5 is 68 today") == ("68",)


@pytest.mark.parametrize("unit", ["ug/m3", "ug.m-3", "\u00b5g/m\u00b3", "m3"])
def test_a_unit_contributes_no_numeral(unit: str) -> None:
    # Found by a failing test rather than by inspection. Every concentration unit contains a 3,
    # and
    # because "3" is also a structural constant the bug would have been INTERMITTENT — grounded
    # when
    # the lag constant was configured, ungrounded when it was not.
    assert numerals(f"the value is high in {unit}") == ()


def test_a_concentration_with_its_unit_yields_only_the_value() -> None:
    assert numerals("PM2.5 corrected to 18.2 ug/m3") == ("18.2",)


def test_the_unit_token_set_is_not_empty() -> None:
    assert len(UNIT_TOKENS) >= 4


def test_the_species_token_set_is_not_empty() -> None:
    # Non-vacuity: an empty mask set would make every species test above pass for the wrong
    # reason.
    assert len(SPECIES_TOKENS) >= 5


def test_an_iso_instant_contributes_no_numeral() -> None:
    # Provenance, not a quantitative claim, and the basis carries it separately. Left unmasked
    # it would
    # yield six ungrounded numbers for every timestamp the guidance mentions.
    assert numerals("as of 2026-07-01T12:00:00Z") == ()


def test_a_claim_beside_an_instant_is_still_extracted() -> None:
    assert numerals("as of 2026-07-01T12:00:00Z the sub-index was 68") == ("68",)


# --- normalisation ------------------------------------------------------

@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("18.20", "18.2"),
        ("18.200", "18.2"),
        ("68.0", "68"),
        ("68.00", "68"),
        ("1,234", "1234"),
        ("1,234.50", "1234.5"),
        ("068", "68"),
        ("18.2", "18.2"),
    ],
)
def test_equivalent_renderings_normalise_together(written: str, expected: str) -> None:
    assert normalise_numeral(written) == expected


def test_a_retrieved_value_in_a_different_rendering_is_accepted() -> None:
    # Req 7.1 asks whether the VALUE was retrieved, not whether the string matches. A model
    # writing
    # "18.20" for a retrieved 18.2 has invented nothing, and rejecting it would burn a repair
    # attempt
    # on a formatting difference.
    permitted = permitted_values(_retrieved("18.2"), constants=())
    assert ungrounded("the value is 18.20", permitted) == ()


def test_normalisation_is_idempotent() -> None:
    for value in ("18.20", "1,234.50", "068"):
        once = normalise_numeral(value)
        assert normalise_numeral(once) == once


# --- the permitted set --------------------------------------------------

def test_the_permitted_set_is_the_retrieved_values_and_the_constants() -> None:
    permitted = permitted_values(_retrieved("68", "18.2"), constants=("3",))
    assert {"68", "18.2", "3"} <= permitted


def test_the_permitted_set_normalises_what_it_stores() -> None:
    # Otherwise a retrieval returning "18.20" and a generation writing "18.2" would disagree,
    # which is
    # the same formatting problem from the other direction.
    assert "18.2" in permitted_values(_retrieved("18.20"), constants=())


def test_an_invented_number_is_caught() -> None:
    permitted = permitted_values(_retrieved("68"), constants=())
    assert ungrounded("the sub-index is 71", permitted) == ("71",)


def test_every_invented_number_is_reported_not_just_the_first() -> None:
    # A repair attempt needs the whole list; reporting one at a time would take several rounds
    # to
    # converge, and Req 22 bounds how many the turn gets.
    permitted = permitted_values(_retrieved("68"), constants=())
    assert ungrounded("71 and 99 and 68", permitted) == ("71", "99")


def test_a_grounded_generation_reports_nothing() -> None:
    # Non-vacuity for every rejection test: a checker that flagged everything would refuse all
    # guidance, which fails the user as completely as an invented number misleads them.
    permitted = permitted_values(_retrieved("68", "18.2"), constants=())
    assert ungrounded("PM2.5 is 68, corrected to 18.2", permitted) == ()


def test_the_three_day_lag_constant_is_permitted_as_prose() -> None:
    # The research's ~3-day particulate lag is structural, not a measurement, so it is a
    # configured
    # constant rather than something a retrieval would ever return.
    permitted = permitted_values(_retrieved("68"), constants=DEFAULT_STRUCTURAL_CONSTANTS)
    assert ungrounded("particulate effects can lag about 3 days", permitted) == ()


def test_the_default_constants_are_not_a_blanket_allowance() -> None:
    # The constants are an escape hatch, so they must stay small and specific. If they admitted
    # any
    # small integer, "the sub-index is 5" would pass ungrounded.
    assert len(DEFAULT_STRUCTURAL_CONSTANTS) <= 6
    permitted = permitted_values(_retrieved(), constants=DEFAULT_STRUCTURAL_CONSTANTS)
    assert ungrounded("the sub-index is 68", permitted) == ("68",)


def test_an_empty_retrieval_grounds_nothing() -> None:
    # Req 7.3: a value that was not retrieved is reported unavailable rather than estimated, so
    # with
    # nothing retrieved every number in the guidance is ungrounded.
    permitted = permitted_values(_retrieved(), constants=())
    assert ungrounded("the sub-index is 68", permitted) == ("68",)


def test_grounding_is_a_decidable_comparison_and_not_a_score() -> None:
    # Req 34.8. The return type carries the ANSWER — which numerals are ungrounded — and there
    # is no
    # threshold, ratio or confidence anywhere in the signature for a caller to tune.
    import inspect

    signature = inspect.signature(ungrounded)
    assert set(signature.parameters) == {"text", "permitted"}
    assert signature.return_annotation in ("tuple[str, ...]", tuple)


def test_the_grounding_module_reads_no_clock_and_no_network() -> None:
    import ast
    import pathlib

    from aqm_advisor.domain import grounding

    tree = ast.parse(pathlib.Path(grounding.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("httpx", "boto3", "socket", "random", "time", "datetime"):
        assert forbidden not in imported
