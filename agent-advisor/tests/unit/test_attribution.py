"""Tests for unavailable values and forecast attribution (task 5.2; Reqs 7.3, 7.4, 7.5).

Req 7.5 and Req 7.4's attribution clause are already covered by `test_reporting.py`, which
asserts that a degraded forecast yields no next-day value and still names its provider. Two
things were NOT covered, and this file adds them.

**Req 7.3 had no path at all.** A value the user asked about that was not retrieved must be
reported unavailable rather than estimated — and there was no text to say that with. The
interesting test is not that the sentence exists but that it CONTAINS NO NUMERAL: the one thing
an unavailable-value message must never do is carry a number, because a number there is the
estimate the requirement forbids, arriving inside the very sentence meant to refuse it.

**Req 7.4's first clause had no check.** "SHALL NOT present a forecast value as a measurement"
is about wording, and reading a forecast into a `ForecastView` does not stop a generation
describing it as a reading. So `presents_forecast_as_measurement` pairs forecast tense with
measurement verbs — and the tests quantify the near-misses, because the failure mode here is a
check so broad it rejects the correct wording, which is exactly what the `you have` defect did
to the clinician text.
"""

from __future__ import annotations

import pytest

from aqm_advisor.domain.attribution import (
    FORECAST_TENSE_TOKENS,
    MEASUREMENT_VERBS,
    forecast_attributed,
    presents_forecast_as_measurement,
    unavailable_text,
)
from aqm_advisor.domain.forbidden import forbidden_matches
from aqm_advisor.domain.grounding import numerals
from aqm_advisor.domain.reporting import forecast_view

# --- Req 7.3: unavailable, never estimated ------------------------------


def test_the_unavailable_text_names_the_subject() -> None:
    # "It is unavailable" leaves the user unsure which of the things they asked about is
    # missing.
    assert "ozone" in unavailable_text("ozone")


def test_the_unavailable_text_says_it_is_unavailable() -> None:
    text = unavailable_text("ozone").casefold()
    assert "unavailable" in text or "do not have" in text or "don't have" in text


@pytest.mark.parametrize(
    "subject",
    ["ozone", "PM2.5", "the pollen count", "nitrogen dioxide", "tomorrow's index"],
)
def test_the_unavailable_text_never_carries_a_numeral(subject: str) -> None:
    # THE test for Req 7.3. A number inside the sentence that refuses to estimate IS the
    # estimate the
    # requirement forbids — and it would also be an ungrounded claim, since nothing was
    # retrieved.
    assert numerals(unavailable_text(subject)) == ()


def test_a_subject_containing_a_numeral_is_not_echoed_into_the_text() -> None:
    # `PM2.5` and `PM25` contain digits. Echoing the subject verbatim would put a numeral in the
    # sentence
    # and defeat the test above — so the subject is described, not quoted.
    assert numerals(unavailable_text("PM2.5 at 40")) == ()


def test_the_unavailable_text_offers_no_estimate_language() -> None:
    # "roughly", "probably around" is estimating with hedging attached, which is the failure
    # wearing a
    # disclaimer.
    lowered = unavailable_text("ozone").casefold()
    for hedge in ("roughly", "approximately", "probably", "around", "estimate", "likely"):
        assert hedge not in lowered, hedge


def test_the_unavailable_text_is_not_rejected_by_the_guardrails() -> None:
    # The lesson from task 6.3: a required text the pattern set rejects is unpublishable, and
    # this
    # service is obliged to be able to say it.
    assert forbidden_matches(unavailable_text("ozone")) == ()


def test_a_blank_subject_is_refused() -> None:
    # A message refusing to state "" would be incomprehensible, and the caller has a bug worth
    # surfacing.
    with pytest.raises(ValueError, match="subject"):
        unavailable_text("   ")


# --- Req 7.4: never as a measurement ------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "tomorrow's PM2.5 was measured at 40",
        "the recorded value for tomorrow is high",
        "next day readings show a rise",
        "tomorrow the monitor recorded a higher index",
    ],
)
def test_forecast_tense_paired_with_a_measurement_verb_is_caught(text: str) -> None:
    # Req 7.4's first clause. A forecast described as a reading invites the user to trust it as
    # one, and
    # a forecast is the least reliable number in the response.
    assert presents_forecast_as_measurement(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "the forecast from the provider suggests tomorrow will be worse",
        "tomorrow is expected to be worse according to the forecast",
        "today's reading was measured at 40",
        "the monitor recorded 40 this morning",
        "conditions tomorrow are forecast to improve",
    ],
)
def test_correct_wording_is_not_caught(text: str) -> None:
    # THE non-vacuity half, and the one that matters most. A check broad enough to reject
    # "today's
    # reading was measured" would block a correct present-tense measurement, and one that
    # rejects
    # "tomorrow is forecast" would block the only correct way to word a forecast.
    assert presents_forecast_as_measurement(text) is False


def test_a_measurement_verb_alone_is_not_a_violation() -> None:
    # Measurements ARE measured. The violation is the PAIRING with forecast tense.
    assert presents_forecast_as_measurement("the value was measured at 40") is False


def test_forecast_tense_alone_is_not_a_violation() -> None:
    assert presents_forecast_as_measurement("tomorrow will be worse") is False


@pytest.mark.parametrize("verb", sorted(MEASUREMENT_VERBS))
def test_every_measurement_verb_is_caught_beside_forecast_tense(verb: str) -> None:
    # Quantified over the vocabulary rather than a sample, so adding a verb without covering it
    # is a
    # failing test.
    assert presents_forecast_as_measurement(f"tomorrow the value {verb} 40") is True


@pytest.mark.parametrize("token", sorted(FORECAST_TENSE_TOKENS))
def test_every_forecast_token_is_caught_beside_a_measurement_verb(token: str) -> None:
    assert presents_forecast_as_measurement(f"{token} the value was measured at 40") is True


def test_the_check_matches_tokens_not_substrings() -> None:
    # The third-instance lesson: `measured` inside another word, or `tomorrow` inside a longer
    # token,
    # must not fire. "premeasured" is not a measurement verb.
    assert presents_forecast_as_measurement("tomorrow the premeasuredness rose") is False


# --- Req 7.4: attributed to the provider Service 2 named ----------------


def test_a_stated_next_day_value_must_name_the_provider() -> None:
    # Req 7.4's second clause. An unattributed forecast reads as this service's own prediction,
    # which
    # Req 13.5 says it never makes.
    view = forecast_view({"source": "MetOffice", "trend": "worsening", "tomorrowAqi": 5})
    assert forecast_attributed("tomorrow's index is 5", view) is False


def test_naming_the_provider_satisfies_attribution() -> None:
    view = forecast_view({"source": "MetOffice", "trend": "worsening", "tomorrowAqi": 5})
    assert (
        forecast_attributed("the MetOffice forecast puts tomorrow at 5", view) is True
    )


def test_attribution_is_case_insensitive() -> None:
    # A provider name is a proper noun the model may sentence-case differently; rejecting on
    # case would
    # withhold a correctly attributed response.
    view = forecast_view({"source": "MetOffice", "trend": "worsening", "tomorrowAqi": 5})
    assert forecast_attributed("the metoffice forecast puts tomorrow at 5", view) is True


def test_a_forecast_with_no_source_cannot_be_attributed() -> None:
    # Service 2 served no provider, so there is nothing to attribute to. Req 7.4 cannot be
    # satisfied,
    # and the honest answer is to treat the value as unstatable rather than to invent a source.
    view = forecast_view({"trend": "worsening", "tomorrowAqi": 5})
    assert forecast_attributed("tomorrow's index is 5", view) is False


def test_an_unavailable_forecast_needs_no_attribution() -> None:
    # A degraded forecast states no value (Req 7.5), so there is no claim needing a provider.
    # Requiring
    # one would make the honest "the outlook was unavailable" sentence unpublishable.
    view = forecast_view({"source": "MetOffice", "degraded": True})
    assert forecast_attributed("the forecast was unavailable", view) is True


def test_text_stating_no_value_needs_no_attribution() -> None:
    # Non-vacuity in the other direction: attribution is required only where a value is actually
    # stated.
    view = forecast_view({"source": "MetOffice", "trend": "worsening", "tomorrowAqi": 5})
    assert forecast_attributed("conditions may worsen tomorrow", view) is True


# --- a defect this file found, kept as a regression --------------------


@pytest.mark.parametrize(
    "text",
    [
        "tomorrow's value was measured at 40",
        "tomorrow was measured at 40",
        "TOMORROW'S VALUE WAS MEASURED",
    ],
)
def test_a_possessive_forecast_token_still_matches(text: str) -> None:
    # A real defect, caught by the first run of this file: the token scan yielded `tomorrow's`,
    # which is
    # not the token `tomorrow`, so the highest-value phrasing in the whole check slipped
    # through. Dropping
    # apostrophes from the pattern would have fixed it and mangled contractions, so the
    # possessive is
    # stripped as an additional form instead.
    assert presents_forecast_as_measurement(text) is True


def test_a_contraction_is_not_mangled_by_the_possessive_handling() -> None:
    # The other half: "don't" must not become a token that matches something else. Nothing here
    # is a
    # forecast claim, so the answer is False.
    assert presents_forecast_as_measurement("i don't have that value") is False


def test_the_unavailable_text_and_the_measurement_check_agree() -> None:
    # The two halves of task 5.2 meet here: the sentence Req 7.3 obliges the service to emit
    # must not be
    # flagged by Req 7.4's check. A required text its own sibling check rejects is
    # unpublishable.
    assert presents_forecast_as_measurement(unavailable_text("ozone")) is False
    assert presents_forecast_as_measurement(unavailable_text("tomorrow's index")) is False
