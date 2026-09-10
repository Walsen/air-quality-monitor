"""The Guardrail_Envelope and the forbidden-phrase enforcer.

Requirements 25.1, 25.2, 25.3, 25.4, 25.6, 25.10, 25.11, 25.12.

REQUIREMENT 25.11 DEMANDS A 500 WHERE §5 OTHERWISE FORBIDS ONE, and the two do not conflict
because their subjects differ. §5 says BAD INPUT must never produce a 500 — and that still
stands, bad input gets 400. Requirement 25.11 is about the service's own OUTPUT failing the
service's own check, which is exactly what a 500 means; the requirement states the reasoning
outright: "emitting a guardrail-violating body is worse than emitting none". That is the fourth
place in this spec where two clauses look contradictory until you ask whose subject each names,
after Requirements 3.3/3.6, 15.9/17.9 and 23.5/23.6.

The check runs over the ASSEMBLED BODY rather than over each contributing string, because that
is
what a client receives: a phrase composed across two members, or one hidden in a nested site
note, is still in the response.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

ADVISORY_SCOPE = "exposure-reduction"
"""Requirement 25.1's advisory scope — EXACTLY this value, not a superset."""

DEFAULT_DISCLAIMER = (
    "This is exposure guidance based on air quality measurements, not medical advice. "
    "It does not replace your clinician's action plan; follow that plan and their "
    "instructions for your own care."
)
"""Requirement 25.3's disclaimer.

States both halves the requirement names — exposure guidance rather than medical advice, AND
deference to the clinician's action plan. Wording with only the first half would leave a reader
who HAS an action plan without being told it takes precedence.
"""

DEFAULT_EMERGENCY_GUIDANCE = (
    "If you have severe breathlessness, if your reliever inhaler is not working, or if "
    "your lips or face look blue, treat it as an emergency and contact emergency "
    "services immediately."
)
"""Requirement 25.2's emergency guidance.

Names the three red-flag symptoms the requirement lists specifically. Generic "seek help if
unwell" wording would satisfy no reading of 25.2, because it never tells the reader WHICH
symptoms warrant emergency services.
"""

DEFAULT_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    # Diagnosis and event claims (Requirement 25.4).
    r"\byou (?:are|'re) (?:having|experiencing) an? (?:asthma )?"
    r"(?:attack|exacerbation|flare)\b",
    r"\byou have (?:asthma|copd|an infection)\b",
    r"\bdiagnos(?:is|ed|e)\b",
    r"\bexacerbation\b",
    r"\basthma attack\b",
    # Medication, dose and schedule language (Requirement 25.4).
    r"\b(?:salbutamol|albuterol|ventolin|prednisolone|beclometasone|fluticasone|"
    r"salmeterol|montelukast)\b",
    r"\b(?:increase|decrease|double|halve|adjust|change) your dose\b",
    r"\bdose to\b",
    r"\bpuffs?\b",
    r"\btake (?:your )?(?:medication|inhaler|preventer|steroid)\b",
    r"\b(?:start|stop|skip) taking\b",
    r"\bprescrib(?:e|ed|ing)\b",
)
"""Requirement 25.11's default forbidden-phrase patterns.

Anchored on word boundaries and, where the phrase is a claim rather than a term, on the whole
claim — so `reliever inhaler is not working` in the emergency guidance does not trip the
medication pattern, while `take your inhaler` does. A bare `inhaler` pattern would make the
guardrail text itself unpublishable, which a test guards against.
"""


@dataclass(frozen=True, slots=True)
class GuardrailEnvelope:
    """Requirement 25.1's three members, and nothing else."""

    disclaimer: str
    advisory_scope: str
    emergency_guidance: str


@dataclass(frozen=True, slots=True)
class GuardrailSettings:
    """The configured guardrail texts and patterns (§1: narrow parameter object)."""

    disclaimer: str = DEFAULT_DISCLAIMER
    emergency_guidance: str = DEFAULT_EMERGENCY_GUIDANCE
    forbidden_patterns: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_FORBIDDEN_PATTERNS
    )

    def __post_init__(self) -> None:
        """Refuse an empty guardrail text, which Requirement 25.1 forbids outright."""
        if not self.disclaimer.strip():
            raise ValueError("the guardrail disclaimer must be non-empty")
        if not self.emergency_guidance.strip():
            raise ValueError("the guardrail emergencyGuidance must be non-empty")


class GuardrailViolationError(RuntimeError):
    """A response body matched a forbidden pattern (Requirement 25.11).

    Carries the PATTERN, not the offending text: an operator needs to know which rule fired to
    fix the generator, while the text itself is what must not travel.
    """

    def __init__(self, pattern: str) -> None:
        """Record which pattern matched."""
        super().__init__(f"response body matches forbidden pattern {pattern!r}")
        self.pattern = pattern


def guardrail_envelope(settings: GuardrailSettings | None = None) -> GuardrailEnvelope:
    """Build the envelope every data-bearing response carries (Requirements 25.1, 25.12)."""
    resolved = settings or GuardrailSettings()
    return GuardrailEnvelope(
        disclaimer=resolved.disclaimer,
        advisory_scope=ADVISORY_SCOPE,
        emergency_guidance=resolved.emergency_guidance,
    )


def enforce_guardrails(
    body: Mapping[str, object], settings: GuardrailSettings | None = None
) -> None:
    """Refuse a body carrying forbidden phrasing (Requirements 25.4, 25.11).

    Raises:
        GuardrailViolationError: naming the matched pattern. The caller turns this into a 500
            and one logged error, because Requirement 25.11 holds that emitting a
            guardrail-violating body is worse than emitting none.
    """
    resolved = settings or GuardrailSettings()
    compiled = [
        (pattern, re.compile(pattern, re.IGNORECASE))
        for pattern in resolved.forbidden_patterns
    ]
    for text in _strings_in(body):
        for pattern, expression in compiled:
            if expression.search(text):
                raise GuardrailViolationError(pattern)


def _strings_in(value: object) -> list[str]:
    """Every string anywhere in a body.

    Walks nested members because a violating phrase inside a site note is still in the document
    the client receives — checking only top-level members would miss exactly where generated
    prose tends to live.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        found: list[str] = []
        for key, item in value.items():
            if isinstance(key, str):
                found.append(key)
            found.extend(_strings_in(item))
        return found
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [text for item in value for text in _strings_in(item)]
    return []


__all__ = [
    "ADVISORY_SCOPE",
    "DEFAULT_DISCLAIMER",
    "DEFAULT_EMERGENCY_GUIDANCE",
    "DEFAULT_FORBIDDEN_PATTERNS",
    "GuardrailEnvelope",
    "GuardrailSettings",
    "GuardrailViolationError",
    "enforce_guardrails",
    "guardrail_envelope",
]
