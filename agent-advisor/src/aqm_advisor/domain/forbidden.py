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

_MEDICAL_OBJECT = (
    r"(?:asthma|copd|attack|exacerbation|flare[- ]?up|infection|bronchitis|pneumonia|"
    r"allergy|allergies|condition|episode|emergency)"
)
"""What makes "you have X" a DIAGNOSIS rather than ordinary English.

Required because the first version of these patterns matched a bare `you have`, which fired on
"Since you have noticed a change over several days" — the required clinician-suggestion text of
Req 11.4. A pattern keyed on the WORDS rather than the CLAIM makes text this service is obliged
to emit unpublishable, which is exactly the failure task 6.3's self-consistency guard exists to
catch, and it caught this one.

The trade is narrower coverage for a diagnosis naming something outside this vocabulary. That is
the right direction: Req 34.5 keeps a managed guardrail as an independent second check, whereas
an over-broad pattern has no second chance — it simply blocks the response.
"""

DIAGNOSIS_PATTERNS: tuple[str, ...] = (
    # Req 8.3: never state or imply the user is, or is not, having a medical event.
    rf"\byou (?:have|are having|are suffering from|are experiencing)\b"
    rf"[^.?!]{{0,24}}?\b{_MEDICAL_OBJECT}\b",
    rf"\byou (?:do not|don't|are not|aren't) (?:have|having)\b"
    rf"[^.?!]{{0,24}}?\b{_MEDICAL_OBJECT}\b",
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

_SYMPTOM_OBJECT = (
    r"(?:symptoms?|cough|coughing|wheeze|wheezing|breathless|breathlessness|"
    r"chest|attack|flare[- ]?up|worse|ill|struggle)"
)

_CAUSAL_VERB = (
    r"(?:caus\w*|trigger\w*|aggravat\w*|worsen\w*|flar\w*|irritat\w*|provok\w*|"
    r"inflam\w*|exacerbat\w*)"
)

ATTRIBUTION_PATTERNS: tuple[str, ...] = (
    # A trigger, possessive or attributed, in either word order.
    r"\bis\s+(?:your|the)\s+(?:\w+\s+){0,2}?trigger\b",
    r"\b(?:your|the)\s+trigger\s+(?:for\s+your|is)\b",
    r"\btrigger(?:s|ed)\s+(?:you|your)\b",
    r"\b(?:are|is)\s+your\s+(?:\w+\s+){0,2}?trigger\b",
    r"\b(?:appears|seems)\s+to\s+be\s+your\s+(?:\w+\s+){0,2}?trigger\b",
    # A causal verb bound to the user, active voice. Open-ended by STEM, because a review found
    # a
    # closed list catching only `cause` and `trigger` while an LLM reaches just as readily for
    # `aggravates`, `worsens`, `irritates`, `provokes`, `flares`.
    rf"\b{_CAUSAL_VERB}\s+(?:your|you)\b",
    r"\b(?:sets?|set)\s+(?:off\s+(?:your|you)|you\s+off)\b",
    r"\bbrings?\s+on\s+(?:your|you)\b",
    r"\byou(?:r body)?\s+(?:react|reacts|reacting|respond|responds)\s+to\b",
    r"\bmakes?\s+you\s+(?:react|worse|wheeze|cough|ill|breathless|feel\s+worse)\b",
    # The same claim in PASSIVE voice, which the active anchors cannot see.
    rf"\byour\s+{_SYMPTOM_OBJECT}\b[^.?!]{{0,30}}?"
    r"\b(?:was|were|is|are|been|being)\s+"
    r"(?:caused|triggered|brought\s+on|set\s+off|driven|aggravated|worsened)\b",
    # Nominalised attribution: the cause OF your symptoms.
    r"\b(?:cause|reason|source|explanation)\s+(?:of|for)\s+your\b",
    r"\bis\s+(?:behind|driving|to\s+blame\s+for|the\s+reason\s+for)\s+your\b",
    rf"\bexplains?\s+(?:your|the)\s+{_SYMPTOM_OBJECT}\b",
    r"\bresponsible\s+for\s+your\b",
    r"\bbecause\s+of\s+the\s+\w+\b[^.?!]{0,20}?\byour\s+(?:symptoms?|cough|wheeze)\b",
    rf"\byour\s+{_SYMPTOM_OBJECT}\b[^.?!]{{0,20}}?"
    r"\b(?:are|is|were|was)\s+because\s+of\b",
    # A PREDICTION about the user's future symptoms. Every one is bound to a SYMPTOM OBJECT,
    # because Req 30.2's own wording is "a prediction about the user's future SYMPTOMS" — and an
    # unbound version rejected "you will get less exposure" and "you will have your reliever
    # with
    # you", which is the exposure framing the system prompt asks for and the preparedness
    # language
    # Req 8.4 permits. Req 8.2 DISCARDS a matching response, so that destroyed a good answer
    # silently, which is a worse failure than a vague answer.
    rf"\bwill\s+make\s+you\s+{_SYMPTOM_OBJECT}\b",
    rf"\byou\s+will\s+(?:have|get|experience|feel)\b[^.?!]{{0,24}}?\b{_SYMPTOM_OBJECT}\b",
    rf"\byou\s+(?:are|'re)\s+going\s+to\s+(?:\w+\s+){{0,3}}?{_SYMPTOM_OBJECT}\b",
    rf"\byou\s+(?:may|might|could|should|are\s+likely\s+to)\b[^.?!]{{0,24}}?"
    rf"\b{_SYMPTOM_OBJECT}\b[^.?!]{{0,24}}?\b(?:tomorrow|tonight|overnight|later)\b",
    r"\bexpect\s+(?:your\s+)?symptoms?\s+to\b",
    r"\bpredicts?\s+your\b",
)
"""Req 30.2's generated-output FAST PATH, and Req 21.5's allergen clause.

**THIS IS NOT COMPLETE ENFORCEMENT OF REQ 30.2 AND MUST NOT BE PRESENTED AS SUCH.** An
adversarial review composed 77 sentences an LLM would plausibly produce that make a forbidden
causal or predictive claim, and the first version of this set caught 6. The patterns above close
the highest-traffic holes it found, but the remaining ceiling is the METHOD's, not the tuning's:
Req 30.2 forbids a SEMANTIC ACT — attributing causation, or predicting the user's future
symptoms — and that act has unbounded surface forms. Any regex set can be paraphrased around,
e.g. "the two tend to move together for you", or "on days like today your chest often has a
harder time".

So this is a cheap deterministic pre-filter and Req 34.5's managed guardrail is the AUTHORITY
for the semantic claim. That is the architecture `KNOWN_MEDICATION_TOKENS` already documents for
a vocabulary it admits cannot be complete: a miss degrades one of several defences rather than
removing the only one. The corpus in `tests/unit/test_attribution_patterns.py` is a coverage
FLOOR of shapes known to be caught, never a claim that all are.

**IT MATCHES A CLAIM, NOT A WORD.** Both requirements offend on the ATTRIBUTION — the possessive
that turns a correlation in someone's diary into a statement about their body.
`domain/actions.py` ships a required template reading "Both irritant and allergic triggers can
matter on the same day, so it is worth watching how you respond rather than assuming one cause":
it uses both forbidden nouns to say the anti-causal thing the requirement wants said, so a word
ban would delete the text that does the right thing. And `domain/association.py` is obliged by
Req 30.1 to explain a learned threshold, so a service that could not put "pattern" near "your"
could not meet it.
"""

_CATEGORY_BY_PATTERN: dict[str, str] = {
    **{pattern: "diagnosis" for pattern in DIAGNOSIS_PATTERNS},
    **{pattern: "dosing" for pattern in DOSING_PATTERNS},
    **{pattern: "causal_attribution" for pattern in ATTRIBUTION_PATTERNS},
}

DEFAULT_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    *DIAGNOSIS_PATTERNS,
    *DOSING_PATTERNS,
    *ATTRIBUTION_PATTERNS,
)
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
    "ATTRIBUTION_PATTERNS",
    "DEFAULT_FORBIDDEN_PATTERNS",
    "DIAGNOSIS_PATTERNS",
    "DOSING_PATTERNS",
    "GENERIC_ROLE_WORDS",
    "KNOWN_MEDICATION_TOKENS",
    "administration_near_medication",
    "forbidden_matches",
    "unlisted_medications",
]
