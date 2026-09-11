"""Symptom diary capture (Requirements 28.1 to 28.8).

**Req 28.6 shapes this module, and it is enforced by control flow.** A diary description is an
utterance like any other, so the red-flag check applies to it — and where a red flag is
described, "SHALL NOT let recording the entry displace the escalation". `plan_diary_turn`
therefore returns an outcome carrying an escalation and NO confirmation request, with
`may_write` false: the write is UNREACHABLE on that path rather than something a caller must
remember to skip. Same short-circuit as `TurnPipeline.run`, for the same reason — a guarantee
every caller must remember is not a guarantee.

The urgency is not diminished by the framing. Someone filing "my lips looked blue today" as
history is describing the same emergency as someone asking about it; that they are writing it
down does not make it historical.

**Req 28.7 is enforced by the absence of arithmetic and of ordering comparisons.** Telling
someone their condition is deteriorating or improving requires comparing this entry against
earlier ones, so this module contains neither — AST tests assert both. A module that cannot
compare two severities cannot report a trend in them, and an ordering comparison is how such a
trend would arrive without any arithmetic operator appearing.

**Req 28.2 puts the user above the agent's own reading.** Inferring a severity from prose is a
judgement about someone's health, and they are the authority on it. `SymptomEntryDraft.correct`
already returns an UNCONFIRMED copy for that reason: being told you misread someone is not the
same as their agreeing to whatever you substituted.

**Req 28.8 keeps nothing.** The outcome carries no description field — a planner holding the
user's words would be storing them, and "for the turn only" survives only if there is nowhere
for them to persist.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from aqm_advisor.domain.models import Escalation
from aqm_advisor.domain.records import SymptomEntryDraft
from aqm_advisor.domain.redflag import RedFlagRule
from aqm_advisor.domain.turn import determine_escalation

NOTE_PURPOSE_TEXT = (
    "I will keep those words with the entry so you can read them back later. They are stored "
    "for your recall only and are not used to compute anything."
)
"""Req 28.4's explanation.

Both halves matter. Someone who believes their own words feed a calculation will word them for
the machine rather than for themselves, which makes the diary worse at the one thing it exists
for.
"""


def replacement_warning(date: dt.date) -> str:
    """Say that an existing entry will be replaced, and ask (Req 28.5).

    Names the DATE, because "an entry will be replaced" leaves the user unsure which day they
    are about to overwrite — and a diary's value is that yesterday's entry is still yesterday's.
    """
    return (
        f"You already have an entry for {date.isoformat()}. Saving this one will replace it. "
        "Shall I go ahead?"
    )


def restate_entry_for_confirmation(draft: SymptomEntryDraft) -> str:
    """Restate the inferred entry and ask for confirmation (Req 28.2).

    Names the severity, every marker and whether a reliever was used. A restatement that omitted
    one would obtain confirmation for an entry different from the one written, and the reliever
    flag is the field most easily inferred wrongly from prose.

    Deliberately describes and never assesses (Req 28.7). A diary is a record, and a user reads
    a clinical word in it as a finding whatever hedging surrounds it.
    """
    markers = ", ".join(draft.markers) if draft.markers else "no particular markers"
    reliever = "you used your reliever" if draft.reliever_used else "you did not use a reliever"
    return (
        f"For {draft.date.isoformat()} I have severity {draft.severity} out of 5, "
        f"with {markers}, and that {reliever}. Have I got that right?"
    )


@dataclass(frozen=True, slots=True)
class DiaryTurnOutcome:
    """What a diary turn should do next.

    There is deliberately nowhere to put the description (Req 28.8), and nowhere to put a trend,
    a comparison or an assessment (Req 28.7). A field able to hold one is a place a later author
    could put one.
    """

    escalation: Escalation | None
    may_write: bool
    confirmation_request: str | None = None
    replacement_warning: str | None = None
    note_purpose: str | None = None


def plan_diary_turn(
    *,
    description: str,
    draft: SymptomEntryDraft,
    existing_dates: frozenset[dt.date],
    rules: Sequence[RedFlagRule],
    emergency_guidance: str,
    note_requested: bool = False,
) -> DiaryTurnOutcome:
    """Decide what a diary turn does, escalation first (Reqs 28.1 to 28.6).

    The red-flag check runs on the DESCRIPTION before anything else, and when it fires the
    outcome carries no confirmation request and `may_write` false. Recording cannot displace the
    escalation because there is no path from here to a write.
    """
    escalation = determine_escalation(
        utterance=description,
        prior_turns=(),
        rules=rules,
        emergency_guidance=emergency_guidance,
    )
    if escalation is not None:
        return DiaryTurnOutcome(escalation=escalation, may_write=False)

    return DiaryTurnOutcome(
        escalation=None,
        may_write=True,
        confirmation_request=restate_entry_for_confirmation(draft),
        replacement_warning=(
            replacement_warning(draft.date) if draft.date in existing_dates else None
        ),
        note_purpose=NOTE_PURPOSE_TEXT if note_requested else None,
    )


__all__ = [
    "NOTE_PURPOSE_TEXT",
    "DiaryTurnOutcome",
    "plan_diary_turn",
    "replacement_warning",
    "restate_entry_for_confirmation",
]
