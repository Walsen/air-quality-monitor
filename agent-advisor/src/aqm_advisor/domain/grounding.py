"""Grounding of quantitative claims (Requirement 7).

Every concentration, sub-index, band, threshold, dose and pollen category in the guidance must
be a value this turn actually retrieved (Req 7.1), and a generation failing that is not returned
(Req 7.2).

**This is a decidable comparison, not a score** (Req 34.8). A contextual grounding score cannot
decide whether a number was invented — it can only say the text resembles the source — so the
check extracts the numerals, normalises them, and asks set membership. There is no threshold in
the signature for a caller to tune, because there is nothing to tune.

**The asymmetry runs the opposite way from red-flag matching.** A false rejection here costs one
repair attempt; a false acceptance ships an invented health-adjacent number. So the check is
strict, and where the strictness bites the fix is to make the retrieval return the value rather
than to loosen the comparison.

**Two things are masked before extraction, and both are load-bearing.** Species names contain
digits — `PM2.5`, `PM25`, `NO2`, `O3` — so a naive extractor reads every mention of a pollutant
as an invented number and refuses all guidance. ISO instants likewise yield six numbers apiece
while being provenance rather than a quantitative claim. Masking is narrow and closed: only
these two shapes, so it cannot quietly swallow a real claim. Tests assert both that the mask
works and that a number ADJACENT to a masked token is still extracted, which is the failure that
would otherwise be silent.

**Known limitation, recorded rather than hidden.** Only digit forms are extracted. A generation
writing "sixty-eight" states a quantitative claim this check cannot see. Mapping number words to
values reliably ("a couple", "several", "one or two") is its own problem, and the mitigation
belongs in the system prompt (task 9.4), which requires numerals in digits. Worth knowing before
trusting this check as total.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from aqm_advisor.domain.records import RetrievedValues

SPECIES_TOKENS: tuple[str, ...] = (
    # Longest first: "PM2.5" must be masked before a shorter pattern can bite into it.
    "PM2.5",
    "PM10",
    "PM25Index",
    "NO2Index",
    "PM25",
    "NO2",
    "SO2",
    "CO2",
    "O3",
)
"""Pollutant tokens whose digits are part of a NAME, not a measurement.

Both renderings of particulate matter appear: Service 2's wire name is `PM25`, while a model
writing prose says `PM2.5`. Masking only one of them would leave the other reading as an
invented number.
"""

UNIT_TOKENS: tuple[str, ...] = (
    # Longest first, for the same reason as the species tokens.
    "ug.m-3",
    "ug/m3",
    "ug m-3",
    "\u00b5g/m\u00b3",
    "\u00b5g/m3",
    "m\u00b3",
    "m-3",
    "m3",
)
"""Concentration units whose digits are part of the UNIT, not a measurement.

Found by a test, not by inspection: Service 2 serves `ug.m-3` and a model writing prose says
`ug/m3` or `\u00b5g/m\u00b3` — every one of which contains a 3. Left unmasked, stating any
concentration in its units emits a spurious numeral, and since `3` happens to BE a structural
constant the failure would have been intermittent: grounded whenever the lag constant was
configured, ungrounded whenever it was not. That is the worst kind of bug to ship, so the unit
is masked like the species name.
"""

DEFAULT_STRUCTURAL_CONSTANTS: tuple[str, ...] = ("3",)
"""Numerals that are structural rather than measured, so no retrieval would ever return them.

Currently just the research's ~3-day particulate lag. Deliberately tiny: this is an escape
hatch, and a constants set that admitted any small integer would let "the sub-index is 5" pass
ungrounded. A test pins the size for that reason.
"""

_NUMERAL = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_ISO_INSTANT = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?Z?)?")


def normalise_numeral(value: str) -> str:
    """Reduce a numeral to a canonical form, so equivalent renderings compare equal.

    Strips thousands separators, leading zeros, and trailing zeros in a fractional part,
    dropping the decimal point when nothing remains after it. `18.20`, `18.200` and `18.2` all
    become `18.2`; `68.0` and `068` both become `68`.

    Req 7.1 asks whether the VALUE was retrieved, not whether the string matches. A model
    writing `18.20` for a retrieved `18.2` has invented nothing, and rejecting it would spend a
    repair attempt on formatting. Idempotent, so normalising twice is harmless.
    """
    text = value.replace(",", "").strip()
    if "." in text:
        whole, _, fraction = text.partition(".")
        fraction = fraction.rstrip("0")
        text = f"{whole}.{fraction}" if fraction else whole
    whole_part, dot, rest = text.partition(".")
    whole_part = whole_part.lstrip("0") or "0"
    return f"{whole_part}{dot}{rest}"


def _mask(text: str) -> str:
    """Blank out tokens whose digits are not quantitative claims.

    Replaces with spaces rather than deleting, so two numbers either side of a masked token
    cannot be concatenated into a third that appears in neither.
    """
    masked = _ISO_INSTANT.sub(lambda m: " " * len(m.group()), text)
    for token in (*SPECIES_TOKENS, *UNIT_TOKENS):
        masked = re.sub(re.escape(token), " " * len(token), masked, flags=re.IGNORECASE)
    return masked


def numerals(text: str) -> tuple[str, ...]:
    """Every quantitative numeral in `text`, in order of appearance, normalised.

    Order is defined because it reaches a rejection message and a log line, and practices §2
    requires a defined order anywhere it reaches output.
    """
    return tuple(normalise_numeral(match.group()) for match in _NUMERAL.finditer(_mask(text)))


def permitted_values(
    retrieved: RetrievedValues, *, constants: Iterable[str]
) -> frozenset[str]:
    """The values a generation may state: this turn's retrievals plus the constants.

    Both sides are normalised on the way in, so a retrieval returning `18.20` and a generation
    writing `18.2` agree. Without that the comparison would fail on formatting from either
    direction.
    """
    return frozenset(
        normalise_numeral(value) for value in (*retrieved.numerals, *constants)
    )


def ungrounded(text: str, permitted: frozenset[str]) -> tuple[str, ...]:
    """The numerals in `text` that match no permitted value (Req 7.2).

    Returns EVERY offender rather than the first: a repair attempt needs the whole list, and Req
    22 bounds how many attempts a turn gets, so converging one number at a time could exhaust
    the budget.

    An empty result means the generation is grounded. There is no score and no threshold — Req
    34.8 is explicit that a similarity measure cannot decide this.
    """
    seen: list[str] = []
    for value in numerals(text):
        if value not in permitted and value not in seen:
            seen.append(value)
    return tuple(seen)


def structural_constants(configured: Sequence[str] | None = None) -> tuple[str, ...]:
    """The constants set, defaulting to the research's own.

    A configured set REPLACES the default rather than extending it, matching how Req 8.7 treats
    the Forbidden_Claim patterns: a deployment that finds a constant wrong needs to be able to
    remove it, and an extend-only mechanism cannot.
    """
    return DEFAULT_STRUCTURAL_CONSTANTS if configured is None else tuple(configured)


__all__ = [
    "DEFAULT_STRUCTURAL_CONSTANTS",
    "SPECIES_TOKENS",
    "UNIT_TOKENS",
    "normalise_numeral",
    "numerals",
    "permitted_values",
    "structural_constants",
    "ungrounded",
]
