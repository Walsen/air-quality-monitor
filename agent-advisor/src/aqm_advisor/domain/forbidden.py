"""Forbidden claims and medication closure (Requirements 8 and 29).

These rules live in the DOMAIN, not in the guardrail adapter. They were briefly in
`adapters/guardrail`-shaped code, which made the adapter a second authority on what may be said
— and two authorities on a safety rule is how they come to disagree. The adapter now delegates
here, so there is one pattern set, one category mapping, and one closure check.

**A configured set REPLACES the defaults** (Req 8.7). Extend-only configuration is not
configuration: an operator who finds a pattern misfiring needs to remove it, not pile another on
top. The same reasoning already applies to the grounding constants and the red-flag rules.

**A rejection names the CATEGORY and never the text** (Req 8.6). The rejected generation is
precisely the thing that must not be recorded, so `forbidden_matches` returns categories.
Returning the matched substring would put the forbidden claim into the log that exists to prove
it was blocked.

**Medication closure is a closure, not a pattern** (Req 29). Any drug name in the text must be
in the retrieved `Medication_Entry` set. Req 29.5 is explicit that this TIGHTENS Req 8.4 rather
than relaxing it: naming a medication is permitted only in the preparedness construction, and an
administration instruction adjacent to any medication name is rejected whether or not the drug
is listed.

Recognising a drug name at all needs a vocabulary, and `KNOWN_MEDICATION_TOKENS` is that
vocabulary. Its limit is honest and worth stating: a drug outside it cannot be detected as
unlisted. The mitigations are the administration patterns below, which fire on the INSTRUCTION
regardless of the drug, and the managed guardrail as the second independent check (Req 34.5).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

DIAGNOSIS_PATTERNS: tuple[str, ...] = (
    # Req 8.3: never state or imply the user is, or is not, having a medical event.
    r"\byou (?:have|are having|are suffering from|are experiencing)\b",
    r"\byou (?:do not|don't|are not|aren't) (?:have|having)\b",
    r"\bthis is (?:an?|your) (?:asthma attack|attack|exacerbation|flare[- ]?up|infection)\b",
    r"\byou (?:probably|likely|may|might) have\b",
    r"\b(?:it|this) (?:sounds|looks) like (?:an?|your) \w+ (?:attack|exacerbation)\b",
    r"\byou are (?:fine|okay|ok|not in danger)\b",
)

DOSING_PATTERNS: tuple[str, ...] = (
    # Req 8.4 and 29.2: no dose, no frequency, no change to either.
    r"\btake (?:\d+|one|two|three|four|a|another|an extra)\b",
    r"\b\d+\s*(?:puffs?|mg|ml|mcg|doses?|inhalations?)\b",
    r"\b(?:increase|decrease|double|halve|reduce|stop|start|skip)\s+(?:your\s+)?"
    r"(?:dose|dosage|medication|inhaler|preventer|reliever|steroid)\b",
    r"\bevery\s+(?:\d+|two|three|four|six|eight|twelve)\s*(?:hours?|days?)\b",
    r"\b(?:use|puff|inhale|administer)\s+(?:your\s+)?"
    r"(?:inhaler|reliever|preventer)\s+(?:now|again|twice|before)\b",
    r"\byou (?:should|need to|ought to) (?:take|use|puff|inhale)\b",
)

_CATEGORY_BY_PATTERN: dict[str, str] = {
    **{pattern: "diagnosis" for pattern in DIAGNOSIS_PATTERNS},
    **{pattern: "dosing" for pattern in DOSING_PATTERNS},
}

DEFAULT_FORBIDDEN_PATTERNS: tuple[str, ...] = (*DIAGNOSIS_PATTERNS, *DOSING_PATTERNS)
"""The default Forbidden_Claim set. A configured set REPLACES this (Req 8.7)."""

KNOWN_MEDICATION_TOKENS: frozenset[str] = frozenset(
    {
        # Relievers.
        "salbutamol",
        "albuterol",
        "ventolin",
        "terbutaline",
        "bricanyl",
        # Preventers and combinations.
        "beclometasone",
        "beclomethasone",
        "budesonide",
        "fluticasone",
        "mometasone",
        "ciclesonide",
        "clenil",
        "qvar",
        "pulmicort",
        "flixotide",
        "seretide",
        "symbicort",
        "fostair",
        "relvar",
        "trelegy",
        "salmeterol",
        "formoterol",
        "vilanterol",
        "montelukast",
        "tiotropium",
        "spiriva",
        "prednisolone",
        "theophylline",
    }
)
"""The vocabulary that lets an UNLISTED drug name be recognised at all (Req 29.3).

Its limit is real: a drug absent from this set cannot be detected. That is why Req 29.5's
administration patterns fire on the INSTRUCTION rather than on the drug, and why Req 34.5 keeps
a managed guardrail as an independent second check. A vocabulary miss therefore degrades one of
three defences rather than removing the only one.
"""

GENERIC_ROLE_WORDS: frozenset[str] = frozenset({"reliever", "preventer", "inhaler"})
"""Req 29.7's permitted language when nothing was retrieved: roles, never names."""

_ADMINISTRATION_NEAR_MEDICATION = re.compile(
    r"\b(?:take|takes|taking|use|uses|using|puff|puffs|inhale|inhales|inhaling|administer|"
    r"increase|decrease|double|halve|reduce|stop|start|skip)\b[^.!?]{0,40}?\b("
    + "|".join(sorted(KNOWN_MEDICATION_TOKENS | GENERIC_ROLE_WORDS))
    + r")\b",
    flags=re.IGNORECASE,
)


def forbidden_matches(text: str, patterns: Sequence[str] | None = None) -> tuple[str, ...]:
    """The CATEGORIES of every Forbidden_Claim pattern matching `text` (Req 8.2, 8.6).

    Categories, never the matched text: the rejected generation is the thing Req 8.6 forbids
    recording, so a return value carrying the substring would defeat the requirement it exists
    to serve.

    Deduplicated and returned in a defined order, so a log line is stable across runs.

    `patterns=None` uses the defaults. A supplied set REPLACES them (Req 8.7).
    """
    active = DEFAULT_FORBIDDEN_PATTERNS if patterns is None else tuple(patterns)
    found: list[str] = []
    for pattern in active:
        if re.search(pattern, text, flags=re.IGNORECASE):
            category = _CATEGORY_BY_PATTERN.get(pattern, "other")
            if category not in found:
                found.append(category)
    return tuple(found)


def unlisted_medications(text: str, listed: Iterable[str]) -> tuple[str, ...]:
    """Medication names in `text` that are not in the retrieved set (Req 29.3, 29.6).

    A closure check: the vocabulary says what counts as a drug name, and the retrieved set says
    which are permitted. With an EMPTY retrieved set every recognised name is unlisted, which is
    Req 29.7 — only generic role language is allowed when nothing was retrieved.

    Comparison is case-insensitive on the whole token; a brand and its molecule are separate
    entries because they are separate words, and treating one as the other would let an unlisted
    brand through on the strength of a listed molecule.
    """
    permitted = {name.strip().casefold() for name in listed}
    found: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z-]+", text):
        folded = token.casefold()
        recognised = folded in KNOWN_MEDICATION_TOKENS
        if recognised and folded not in permitted and folded not in found:
            found.append(folded)
    return tuple(found)


def administration_near_medication(text: str) -> bool:
    """Whether an administration verb sits next to a medication name or role (Req 29.5).

    Rejected REGARDLESS of whether the drug is listed. Req 29.5 says this requirement tightens
    Req 8.4 rather than relaxing it, so having the drug on the user's own list buys permission
    to NAME it in a preparedness sentence — never permission to be told to use it.

    Bounded to 40 characters and stopped at sentence punctuation, so "keep your salbutamol to
    hand. Use the plan your clinician agreed" is not read as an instruction to use the
    salbutamol.
    """
    return _ADMINISTRATION_NEAR_MEDICATION.search(text) is not None


__all__ = [
    "DEFAULT_FORBIDDEN_PATTERNS",
    "DIAGNOSIS_PATTERNS",
    "DOSING_PATTERNS",
    "GENERIC_ROLE_WORDS",
    "KNOWN_MEDICATION_TOKENS",
    "administration_near_medication",
    "forbidden_matches",
    "unlisted_medications",
]
