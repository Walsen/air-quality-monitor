"""Deterministic red-flag recognition (Requirement 10).

This runs FIRST in the advisory turn and depends on nothing (DD7). Req 10.4 requires an
escalation even when the Serving_Client is unavailable, and the same reasoning applies to the
model: a check that needed either could not fire when both were down, which is exactly the
moment someone having a severe attack still needs to be told to get help.

That independence is enforced by the SIGNATURE rather than by care. `match_red_flags` takes an
utterance and a rule set. There is no parameter through which a reading, a client, a model or a
configuration object could reach it, so escalation cannot be made conditional on any of them by
a later change that forgets why.

**It errs toward escalating, deliberately.** A false positive costs one unnecessary sentence
directing someone to emergency care; a false negative could cost a life. Those are not
comparable, so the patterns are permissive and Req 10.7 makes the set configuration a deployment
can widen. The limit on permissiveness is that it must not fire on ordinary conversation — a
matcher that escalated on "breathe" or "blue" alone would train the user to ignore the
direction, which costs exactly what a false negative costs.

**It recognises; it does not diagnose** (Req 10.5). The return value is the names of the rules
that matched, never a determination about the person. No marker may name a condition, because a
marker reaches an `Escalation` and a metric label, and "asthma_attack" would be the clinical
judgement Req 10.5 forbids smuggled in as an identifier.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from aqm_advisor.domain.models import PriorTurn

_WHITESPACE = re.compile(r"\s+")
_APOSTROPHES = {"\u2018": "'", "\u2019": "'", "\u02bc": "'", "\u00b4": "'", "`": "'"}


@dataclass(frozen=True, slots=True)
class RedFlagRule:
    """One recognised red flag and the phrasings that indicate it.

    `marker` is an identifier, not prose: it is reported on an `Escalation` and counted as a
    metric label, so it must name the RECOGNITION and not a diagnosis.
    """

    marker: str
    patterns: tuple[str, ...]


DEFAULT_RED_FLAG_RULES: tuple[RedFlagRule, ...] = (
    RedFlagRule(
        marker="severe_breathlessness",
        patterns=(
            "can't breathe",
            "cannot breathe",
            "can't catch my breath",
            "cannot catch my breath",
            "struggling to breathe",
            "struggling for breath",
            "severely breathless",
            "very breathless",
            "too breathless",
            "fighting for breath",
            "gasping",
            "can't get any air",
            "can't speak in full sentences",
            "can't finish a sentence",
        ),
    ),
    RedFlagRule(
        marker="reliever_not_working",
        patterns=(
            "inhaler isn't working",
            "inhaler is not working",
            "inhaler isn't helping",
            "inhaler is not helping",
            "reliever isn't working",
            "reliever is not working",
            "reliever isn't helping",
            "reliever is not helping",
            "no relief from my inhaler",
            "inhaler has stopped working",
            "puffs aren't helping",
            "puffs are not helping",
        ),
    ),
    RedFlagRule(
        marker="blue_lips_or_face",
        patterns=(
            "lips are blue",
            "lips look blue",
            "lips are going blue",
            "blue lips",
            "face is blue",
            "face looks blue",
            "turning blue",
            "going blue",
            "blue around the mouth",
            "blue around the lips",
            "mouth is blue",
            "mouth looks blue",
            # Fragments, on purpose. Clinical guidance words this symptom with a COMPOUND
            # subject —
            # "your lips or face look blue" — which contains neither "lips look blue"
            # (interrupted by
            # "or face") nor "face looks blue" (the verb agrees with the plural subject, so it
            # is
            # "look"). This rule missed the single phrasing a clinician would use, and the
            # emergency
            # guidance Service 2 returns is written in exactly that form. Found by the test that
            # feeds
            # that text back through the matcher, which is now kept as a permanent check.
            "face look blue",
            "face are blue",
        ),
    ),
)
"""Requirement 10.7's three defaults, from the research.

Each pattern is a PHRASE rather than a keyword. That is what keeps the permissive direction from
spilling into ordinary conversation: "breathe" and "blue" occur in benign sentences constantly,
while "can't breathe" and "lips are blue" essentially do not.
"""


def normalise(text: str) -> str:
    """Fold a text to the form patterns are matched against.

    Unicode-normalises, maps the several apostrophe characters onto the ASCII one, casefolds,
    and collapses whitespace runs.

    The apostrophe mapping is not cosmetic. Phone keyboards and word processors emit U+2019, so
    "can't" reaches this service as "can\u2019t" most of the time it is typed by a person — and
    a matcher keyed on the ASCII form would miss the single most likely spelling of the most
    important phrase it has.

    Idempotent, so a caller that normalises twice is not punished for it.
    """
    folded = unicodedata.normalize("NFKC", text)
    for variant, plain in _APOSTROPHES.items():
        folded = folded.replace(variant, plain)
    return _WHITESPACE.sub(" ", folded.casefold()).strip()


def match_red_flags(utterance: str, rules: Sequence[RedFlagRule]) -> tuple[str, ...]:
    """Return the markers of every rule matching `utterance`, in RULE order.

    Rule order rather than match order, because practices §2 requires a defined iteration order
    anywhere it reaches output: match order would depend on where in the sentence each phrase
    happened to appear, and two orderings of the same escalation would be a needless difference
    in an audited record.

    Each marker appears at most once, however many of its patterns matched.

    An empty rule set matches nothing rather than raising. A misconfiguration must be caught by
    configuration validation, not by an exception on the one path that has to work when
    everything else is failing.
    """
    haystack = normalise(utterance)
    return tuple(
        rule.marker
        for rule in rules
        if any(normalise(pattern) in haystack for pattern in rule.patterns)
    )


def match_request_red_flags(
    utterance: str,
    prior_turns: Sequence[PriorTurn],
    rules: Sequence[RedFlagRule],
) -> tuple[str, ...]:
    """Match the current utterance and the user's PRIOR UTTERANCES (Req 10.7).

    Someone may describe the symptom in one message and ask the question in the next, so the
    recognition set applies across the request rather than to the latest message alone.

    **The agent's own prior guidance is deliberately NOT scanned.** The `emergencyGuidance`
    Service 2 returns contains all three default red flags verbatim — "severely breathless",
    "reliever inhaler is not helping", "lips or face look blue" — so scanning guidance would
    make every turn after an escalation re-escalate on the agent's own words, indefinitely,
    while looking like correct caution. A red flag is something the USER described, which is
    also the only reading of Req 10.7 consistent with Req 10.5's refusal to diagnose.
    """
    seen: list[str] = []
    texts = [utterance, *(turn.utterance.get_secret_value() for turn in prior_turns)]
    for text in texts:
        for marker in match_red_flags(text, rules):
            if marker not in seen:
                seen.append(marker)
    # Re-project onto rule order so the result does not depend on which text matched first.
    return tuple(rule.marker for rule in rules if rule.marker in seen)


__all__ = [
    "DEFAULT_RED_FLAG_RULES",
    "RedFlagRule",
    "match_red_flags",
    "match_request_red_flags",
    "normalise",
]
