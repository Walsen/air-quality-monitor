"""Learned-association reporting (Requirements 30.1 to 30.7).

**Req 30.2's trap is the word "trigger".** It is the most natural word in the whole asthma
vocabulary — people say "my triggers" — and it is exactly the word this requirement forbids,
because a trigger is a causal claim about someone's body derived from a correlation in their
diary. `CAUSAL_WORDS` is swept against every text this module produces, not only the one that
felt risky.

**Req 30.4 must name two numbers without subtracting them.** "You need 14 more observations" is
a computation, and Req 30.3 forbids computing anything about an association — so the text states
the count Service 2 reported and the minimum Service 2 requires, and leaves the reader to do the
arithmetic. An AST test asserts this module performs none, which is what makes that phrasing a
structural consequence rather than a stylistic choice.

**Nothing here derives an association.** Service 2's `LearnedThreshold` carries species, lag and
observation count with it, and its own docstring says why: "so Requirement 30's reporting
obligation can be met without a second lookup, and so a threshold can never be surfaced without
the basis it rests on". This module reads that and relays it.

**Req 30.6 exists because the user needs to know they were not overridden.** Service 2 already
ranks a declared threshold above a learned one, and its `ThresholdSource` enum says why —
inference does not overrule an instruction. This service only has to SAY so.

**Req 30.7 confines the consequence.** A correlation in a diary is the weakest evidence in the
system, and it is the last thing that should move a clinical behaviour, so no text here mentions
a medication or suggests seeing anybody.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CAUSAL_WORDS: frozenset[str] = frozenset(
    {
        "cause",
        "causes",
        "caused",
        "causing",
        "trigger",
        "triggers",
        "triggered",
        "diagnos",
        "predict",
        "prediction",
        "because of",
        "leads to",
        "results in",
        "responsible for",
    }
)
"""The vocabulary Req 30.2 forbids.

`trigger` is the important entry. It is the word a user would use themselves, which is exactly
why an agent reaching for natural phrasing would use it — and a trigger is a causal claim, where
the diary supports only an association.
"""

PRECEDENCE_TEXT = (
    "The alerting point you set yourself still applies. A pattern from your diary does not "
    "replace it, so what you told me takes precedence over what I noticed."
)
"""Req 30.6's statement.

Service 2 already ranks the two; this exists so the user can tell that their instruction
survived. A silently correct precedence is indistinguishable, from the outside, from one that
was overridden.
"""


@dataclass(frozen=True, slots=True)
class LearnedThresholdView:
    """A learned threshold as Service 2 reported it.

    There is deliberately nowhere to put a strength, a confidence or a p-value. Req 30.3 forbids
    computing an association, and a field able to hold a derived measure of one is a place to
    compute it into.
    """

    species: str
    sub_index: int
    lag_days: int
    observations: int


def learned_threshold_view(served: object) -> LearnedThresholdView | None:
    """Read the retrieved learned threshold, or None when there is none (Req 30.3).

    Returns None for an absent, empty or PARTIAL block. Reporting a partial association would be
    worse than reporting none: the user would be shown a pattern whose derivation this service
    could not state, and Req 30.1 requires all three parts precisely so a threshold is never
    surfaced without its basis.
    """
    if not isinstance(served, dict) or not served:
        return None
    species = served.get("species")
    sub_index = served.get("subIndex")
    lag_days = served.get("lagDays")
    observations = served.get("observations")
    if not isinstance(species, str):
        return None
    if not all(isinstance(value, int) for value in (sub_index, lag_days, observations)):
        return None
    return LearnedThresholdView(
        species=species,
        sub_index=int(sub_index),  # type: ignore[arg-type]
        lag_days=int(lag_days),  # type: ignore[arg-type]
        observations=int(observations),  # type: ignore[arg-type]
    )


def explain_learned_threshold(view: LearnedThresholdView | None) -> str:
    """Explain where the escalation point came from (Reqs 30.1, 30.2, 30.5).

    Names the species, the lag, the observation count AND the value. The count is what lets the
    user judge whether they agree with the pattern, which is the whole reason for telling them;
    the value is what makes a changed alerting point traceable (Req 30.5).

    Describes it as an association and never as a cause.

    Raises:
        ValueError: when there is no threshold. Explaining nothing would produce a sentence
        about an
            association the retrieved data never reported, which Req 30.3 forbids.
    """
    if view is None:
        raise ValueError("there is no learned threshold to explain")
    return (
        f"Your alerting point for {view.species} is now {view.sub_index}. That came from an "
        f"association in your own diary: across {view.observations} recorded days, your "
        f"entries line up with {view.species} readings about {view.lag_days} days earlier. "
        "It is a pattern in what you recorded, not a statement about why you felt that way."
    )


def insufficient_history_text(*, observations: int, minimum: int) -> str:
    """Say there is not yet enough diary history, naming what is missing (Req 30.4).

    States BOTH numbers and subtracts neither. "You need 14 more" would be a computation about
    an association, which Req 30.3 forbids — and the reader can take one number from the other
    perfectly well.

    Deliberately offers no hedged pattern. Req 30.4 says name what is missing RATHER THAN
    presenting a weak association, and a hedged pattern is still a pattern to the person reading
    it.

    Raises:
        ValueError: for a non-positive minimum, which would make the message nonsense.
    """
    if minimum < 1:
        raise ValueError("the minimum observation count must be a positive integer")
    return (
        f"There is not yet enough diary history to draw a pattern for you: you have "
        f"{observations} recorded days and at least {minimum} are needed. Until then your "
        "alerting point stays as it is."
    )


def basis_threshold_source(view: LearnedThresholdView | None) -> dict[str, Any] | None:
    """What the Basis_Summary should say about the threshold's source (Req 30.5).

    Returns the source label and the value together. Separating them would let a changed
    alerting point be shown without the reason it changed, which is the traceability this
    requirement asks for.
    """
    if view is None:
        return None
    return {
        "threshold_source": "learned",
        "threshold": view.sub_index,
        "species": view.species,
        "observations": view.observations,
        "lag_days": view.lag_days,
    }


__all__ = [
    "CAUSAL_WORDS",
    "PRECEDENCE_TEXT",
    "LearnedThresholdView",
    "basis_threshold_source",
    "explain_learned_threshold",
    "insufficient_history_text",
    "learned_threshold_view",
]
