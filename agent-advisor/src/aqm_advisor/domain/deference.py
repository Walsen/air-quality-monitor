"""Clinician deference (Requirement 11).

The user's written action plan outranks this service everywhere it touches what to do about
their condition. Following the agent must never mean departing from their clinician (Req 11.1).

**Req 11.3 is enforced by an absence.** The service must never ask the user to record the
contents of an action plan — it is the most sensitive health data in the conversation, and Req
19's minimisation says not to hold it. So there is no sentence here that invites it, and a test
sweeps both texts for the invitations a well-meaning author would write ("what does your plan
say?", "share your plan").

**Req 11.4 draws its distinction over TIME.** Worsening ACROSS DAYS suggests contacting a
clinician; one bad day does not. Treating a single rough day as a trajectory would send everyone
to their clinician on the first poor-air day, which makes the suggestion mean nothing when it
matters.

And the suggestion must not characterise the trajectory clinically. "Your asthma is
deteriorating" is the determination Req 8.3 forbids, arriving by way of a sympathetic sentence —
which is exactly how it would get past review.
"""

from __future__ import annotations

import re

DEFERENCE_TEXT = (
    "The decision about what to do for your condition is the one you agreed with your "
    "clinician, so your action plan is what to follow here."
)
"""Req 11.1 and 29.4: said wherever guidance touches what to do about the condition,
and whenever a medication is named."""

CLINICIAN_SUGGESTION_TEXT = (
    "Since you have noticed a change over several days rather than just today, it is worth "
    "contacting your clinician so someone who knows your history can look at it with you."
)
"""Req 11.4: suggests contact on reported worsening, without characterising the
trajectory clinically.

Deliberately says "a change over several days" rather than naming what kind of change it is. The
naming is what would turn a suggestion into a determination.
"""

_PLAN_GOVERNED = (
    # Req 11.2: questions the action plan decides, which this service directs rather than
    # answers.
    r"\bstep (?:up|down)\b",
    r"\brescue pack\b",
    r"\baction plan\b",
    r"\b(?:increase|decrease|double|reduce|start|stop|change)\b[^.?!]{0,30}"
    r"\b(?:preventer|reliever|inhaler|steroid|dose|treatment|medication)\b",
    r"\b(?:should|do|must) I\b[^.?!]{0,30}\b(?:take|use|start|stop|increase|decrease)\b",
    r"\bfollow my (?:plan|action plan)\b",
)

_WORSENING_OVER_TIME = (
    # The trajectory Req 11.4 responds to: a change ACROSS DAYS, not a single bad day.
    r"\b(?:getting|got|been) worse\b[^.?!]{0,30}\b(?:week|days?|month|while|lately)\b",
    r"\bworse (?:each|every) day\b",
    r"\bfor the last (?:few |several )?(?:days?|weeks?)\b",
    r"\b(?:more often|more and more)\b[^.?!]{0,30}\b(?:than usual|lately|recently|this week)\b",
    r"\bover the (?:last|past) (?:few )?(?:days?|weeks?)\b",
    r"\ball week\b",
)


def defers_to_plan(utterance: str) -> bool:
    """Whether the utterance asks something the action plan governs (Req 11.2).

    A match means DIRECT the user to their plan and their clinician rather than answering.
    Recognition is permissive in the same direction as the red-flag matcher and for a weaker
    version of the same reason: a false positive costs one deflection to the plan the user
    already has, while a false negative has this service answering a clinical question.

    The limit is that it must not swallow ordinary exposure questions — those are what the
    service exists to answer, and a recogniser that deferred everything would refuse its own
    job.
    """
    lowered = utterance.casefold()
    return any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in _PLAN_GOVERNED)


def suggests_clinician(utterance: str) -> bool:
    """Whether the utterance reports worsening over time (Req 11.4).

    Keyed on a change ACROSS DAYS. One bad day is not a trajectory, and treating it as one would
    fire the suggestion on the first poor-air day for everybody — after which nobody reads it.
    """
    lowered = utterance.casefold()
    return any(
        re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in _WORSENING_OVER_TIME
    )


__all__ = [
    "CLINICIAN_SUGGESTION_TEXT",
    "DEFERENCE_TEXT",
    "defers_to_plan",
    "suggests_clinician",
]
