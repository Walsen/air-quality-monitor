"""Unavailable values, and keeping a forecast a forecast (Requirements 7.3, 7.4).

**Req 7.3's sentence must contain no numeral.** A number inside the message that refuses to
estimate IS the estimate the requirement forbids, arriving inside the very sentence meant to
refuse it — and it would also be an ungrounded claim, since by definition nothing was retrieved.
So the subject is DESCRIBED rather than quoted: `PM2.5` and `PM25` contain digits, and echoing
the user's words verbatim would put a numeral in the sentence.

Estimate-flavoured hedging is excluded for the same reason. "Probably around 40" is estimating
with a disclaimer attached, which is the failure wearing a costume.

**Req 7.4's first clause is about WORDING, and reading a forecast does not enforce it.**
`forecast_view` returns the retrieved forecast faithfully, and nothing there stops a generation
describing it as a reading. So `presents_forecast_as_measurement` looks for forecast tense
PAIRED with a measurement verb.

The pairing is the whole design. A measurement verb alone is fine — measurements are measured.
Forecast tense alone is fine — that is how a forecast should be worded. Only the combination
misrepresents one as the other, and a check that fired on either alone would reject the correct
wording for both. That is precisely what the `you have` defect did to the clinician text, so the
near-misses are tested explicitly here.

Matching is on TOKENS, not substrings — the third such defect in this service. `premeasured` is
not a measurement verb, and a substring sweep would say it was.
"""

from __future__ import annotations

import re

from aqm_advisor.domain.reporting import ForecastView

FORECAST_TENSE_TOKENS: frozenset[str] = frozenset(
    {"tomorrow", "overnight", "later"}
)
"""Words that place a claim in the future.

Deliberately short. A long list would start catching ordinary conditional wording, and the
pairing with a measurement verb is what makes the check specific rather than the breadth of this
set.
"""

MEASUREMENT_VERBS: frozenset[str] = frozenset(
    {"measured", "recorded", "observed", "monitored", "sampled"}
)
"""Verbs that assert something was actually observed.

`is` and `was` are excluded on purpose: "tomorrow's index is 5" is how a forecast is
legitimately stated, and including the copula would make every forecast sentence a violation.
"""

_NEXT_DAY_PHRASES: frozenset[str] = frozenset({"next day", "next-day"})
"""Multi-word forecast tense, which a token scan alone would miss."""

_UNAVAILABLE_TEMPLATE = (
    "I do not have a current {subject} value from the monitoring data, so I cannot "
    "tell you what it is."
)

_SUBJECT_DESCRIPTIONS: dict[str, str] = {
    "pm2.5": "fine particulate",
    "pm25": "fine particulate",
    "no2": "nitrogen dioxide",
    "pm10": "coarse particulate",
    "o3": "ozone",
}
"""Subjects whose usual names contain digits, mapped to a digit-free description.

Without this the sentence refusing to estimate would itself carry a numeral, which Req 7.3
forbids and grounding would flag.
"""

_DIGITS = re.compile(r"\d")


def describe_subject(subject: str) -> str:
    """Render a subject without digits.

    A known digit-bearing name maps to its written form; anything else has its digit-bearing
    tokens dropped, because a subject echoed from the user's utterance can contain arbitrary
    numbers.
    """
    key = subject.strip().casefold()
    described = _SUBJECT_DESCRIPTIONS.get(key)
    if described is not None:
        return described
    kept = [
        token
        for token in subject.strip().split()
        if not _DIGITS.search(token) and _SUBJECT_DESCRIPTIONS.get(token.casefold()) is None
    ]
    replaced = [
        _SUBJECT_DESCRIPTIONS[token.casefold()]
        for token in subject.strip().split()
        if _SUBJECT_DESCRIPTIONS.get(token.casefold()) is not None
    ]
    words = [*replaced, *kept]
    return " ".join(words) if words else "that"


def unavailable_text(subject: str) -> str:
    """Say a value is unavailable, without estimating it (Req 7.3).

    Raises:
        ValueError: for a blank subject. A message refusing to state nothing in particular
            would be incomprehensible, and the caller has a bug worth surfacing.
    """
    if not subject.strip():
        raise ValueError("a subject is required to say which value is unavailable")
    return _UNAVAILABLE_TEMPLATE.format(subject=describe_subject(subject))


def _tokens(text: str) -> set[str]:
    """Tokens, with possessive forms also yielding their base.

    `tomorrow's PM2.5 was measured` must match the token `tomorrow`. Dropping apostrophes from
    the pattern entirely would work here but would mangle contractions elsewhere ("don't" ->
    "don", "t"), so the possessive is stripped as an ADDITIONAL form instead of the apostrophe
    being excluded.
    """
    found = set(re.findall(r"[a-z0-9']+", text.casefold()))
    return found | {
        token.removesuffix("'s") for token in found if token.endswith("'s")
    }


def presents_forecast_as_measurement(text: str) -> bool:
    """True when forecast tense is paired with a measurement verb (Req 7.4).

    The PAIRING is the test. A measurement verb alone is correct wording for a measurement, and
    forecast tense alone is correct wording for a forecast; only together do they misrepresent
    one as the other.
    """
    lowered = text.casefold()
    tokens = _tokens(text)
    has_future = bool(tokens & FORECAST_TENSE_TOKENS) or any(
        phrase in lowered for phrase in _NEXT_DAY_PHRASES
    )
    if not has_future:
        return False
    if tokens & MEASUREMENT_VERBS:
        return True
    return "readings" in tokens or "reading" in tokens


def forecast_attributed(text: str, view: ForecastView) -> bool:
    """True when a stated next-day value names the provider Service 2 served (Req 7.4).

    Attribution is required only where a VALUE is stated. A degraded forecast states none (Req
    7.5), and a qualitative sentence states none either — demanding a provider for those would
    make the honest "the outlook was unavailable" wording unpublishable.

    A forecast with no served source can never satisfy this. That is the intended outcome: the
    honest response is to treat the value as unstatable rather than to invent a source for it.
    """
    if not view.available or view.tomorrow_aqi is None:
        return True
    if str(view.tomorrow_aqi) not in text:
        return True
    if view.source is None:
        return False
    return view.source.casefold() in text.casefold()


__all__ = [
    "FORECAST_TENSE_TOKENS",
    "MEASUREMENT_VERBS",
    "describe_subject",
    "forecast_attributed",
    "presents_forecast_as_measurement",
    "unavailable_text",
]
