"""Health profile elicitation (Requirements 4.2 to 4.5, 27.1 to 27.6).

**Reqs 27.3 and 27.4 both end with "SHALL NOT echo the offered value", and that is made
structural here.** `decline_message` takes a KIND, not the offered text, so there is no
parameter through which a dose or a date of birth could reach the reply. A function that
received the text and promised not to use it would be a promise; one that cannot see it is a
guarantee.

**A `MedicationEntry` has nowhere to put a dose.** Req 27.3 says record only the name and the
role, so the model carries exactly those two fields with `extra="forbid"` — an attempt to record
a dose RAISES rather than being silently dropped, because a dropped field looks identical to one
that was never offered, and this service must be able to say truthfully that it did not record
it.

The name is checked for an embedded strength too. "salbutamol 100mcg" puts the dose IN the name,
which a field check alone would wave through, and a name with a strength in it is not only the
name.

**Req 27.2's confirmation is a gate, not a courtesy.** Mapping "a brown inhaler every morning"
onto a preventer role is an interpretation of someone's health, so a draft starts unconfirmed
and `write_body` RAISES — the same fail-closed shape as the verification ledger. The caller does
not get an unconfirmed body back; it gets a bug report.

**Req 27.5 keeps values for the turn only, so the draft is frozen.** A mutable draft is a place
to accumulate values across turns, which is how "for the turn" quietly becomes "for the
session". Confirmation therefore produces a NEW draft rather than mutating one.
"""

from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

_STRENGTH = re.compile(r"\d")


class DeclinedKind(Enum):
    """What kind of offered detail is being declined.

    A closed set rather than free text, because the message is chosen by kind and never built
    from the offered value — that is what makes "never echo" structural.
    """

    DOSE_OR_FREQUENCY = "dose_or_frequency"
    ROUTE_OR_SCHEDULE = "route_or_schedule"
    DIAGNOSIS_NARRATIVE = "diagnosis_narrative"
    IDENTITY_DETAIL = "identity_detail"
    OUTSIDE_ALLOWLIST = "outside_allowlist"


_DECLINE_MESSAGES: dict[DeclinedKind, str] = {
    DeclinedKind.DOSE_OR_FREQUENCY: (
        "I record only the name and the role of a medication, never a dose or how often you "
        "take it. The decision about how much to take is the one you agreed with your "
        "clinician."
    ),
    DeclinedKind.ROUTE_OR_SCHEDULE: (
        "I record only the name and the role of a medication, so I have not kept how or when "
        "you take it."
    ),
    DeclinedKind.DIAGNOSIS_NARRATIVE: (
        "I do not keep a diagnosis history. What I record is the condition category and how "
        "sensitive you are to poor air, which is what shapes the advice."
    ),
    DeclinedKind.IDENTITY_DETAIL: (
        "I do not keep names, dates of birth or contact details. What I record is your "
        "condition, your sensitivity, your medication names and roles, your routines and your "
        "locations."
    ),
    DeclinedKind.OUTSIDE_ALLOWLIST: (
        "That is not something I can keep. What I record is your condition, your sensitivity, "
        "your medication names and roles, your routines and your locations."
    ),
}
"""Fixed sentences chosen by kind.

Each says what the service DOES keep, because Reqs 27.3 and 27.4 both require it: "I cannot
record that" leaves the user unsure whether to try a different wording, which for Req 27.4's
values would elicit the very detail just declined. None contains a numeral, so the echo cannot
arrive that way either.
"""


def decline_message(kind: DeclinedKind) -> str:
    """The reply for a declined offer (Reqs 27.3, 27.4, 4.3).

    Takes a kind and nothing else. There is deliberately no parameter carrying the offered text.
    """
    return _DECLINE_MESSAGES[kind]


def limit_rejection_message(*, field: str, limit: int) -> str:
    """Report a limit rejection without reporting the change as applied (Reqs 27.6, 4.5).

    Names the field and the limit. Without the limit the user cannot tell a transient failure
    from a permanent one, and without the field they cannot tell which change to undo.

    The wording avoids every word that would read as success. Reporting an unapplied change as
    applied leaves someone believing their profile is something it is not, which then shapes
    advice they think was personalised to them.
    """
    return (
        f"That change was not made: Service 2 allows at most {limit} for {field}. "
        "Nothing has changed on your profile."
    )


class MedicationEntry(BaseModel):
    """A medication as recorded here: a name and a role, and nothing else (Req 27.3)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    role: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _reject_embedded_strength(cls, value: str) -> str:
        """Refuse a name carrying a dose or strength.

        The smuggling route a field check alone would miss: "salbutamol 100mcg" puts the dose IN
        the name. Req 27.3 says record only the name, and a name with a strength in it is not
        only the name.
        """
        if _STRENGTH.search(value):
            raise ValueError(
                "a medication name must not contain a dose or strength; record the name alone"
            )
        return value.strip()


class RoutineEntry(BaseModel):
    """A recurring activity, as a name and the days it happens on."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    days: tuple[str, ...] = ()


class ProfileDraft(BaseModel):
    """What a profile write would contain, pending the user's confirmation.

    There is deliberately nowhere to put a name, a date of birth, a contact detail or a
    diagnosis narrative (Req 27.4). Declining them at the boundary is necessary but not
    sufficient — a field able to hold one is a place a later author could put it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    condition: str | None = None
    sensitivity_level: str | None = None
    medications: tuple[MedicationEntry, ...] = ()
    routines: tuple[RoutineEntry, ...] = ()
    locations: tuple[str, ...] = ()
    confirmed: bool = False

    def _stated(self) -> dict[str, object]:
        """The fields this draft would actually write."""
        body: dict[str, object] = {}
        if self.condition is not None:
            body["condition"] = self.condition
        if self.sensitivity_level is not None:
            body["sensitivity_level"] = self.sensitivity_level
        if self.medications:
            body["medications"] = [
                {"name": entry.name, "role": entry.role} for entry in self.medications
            ]
        if self.routines:
            body["routines"] = [
                {"name": entry.name, "days": list(entry.days)} for entry in self.routines
            ]
        if self.locations:
            body["locations"] = list(self.locations)
        return body

    def confirm(self) -> ProfileDraft:
        """Return a CONFIRMED copy.

        A new value rather than a mutation, because the model is frozen for Req 27.5: nothing
        accumulates in place across turns.
        """
        return self.model_copy(update={"confirmed": True})

    def write_body(self) -> dict[str, object]:
        """The body to send, or raise if the user has not confirmed it (Reqs 27.2, 4.4).

        Raises:
            RuntimeError: when unconfirmed. The caller does not get an unconfirmed body back —
            it gets an
                exception, which is a bug report rather than a silent write to someone's health
                profile.
        """
        if not self.confirmed:
            raise RuntimeError(
                "refusing to build a profile write the user has not confirmed"
            )
        return self._stated()


def restate_for_confirmation(draft: ProfileDraft) -> str:
    """Restate the structured interpretation and ask for confirmation (Req 27.2).

    Names every field the write would apply. A restatement that omitted one would obtain
    confirmation for less than the write actually does, which is consent in form only.

    Raises:
        ValueError: for an empty draft. Asking someone to confirm nothing obtains a confirmation
        that
            authorises nothing, and the caller has a bug.
    """
    stated = draft._stated()
    if not stated:
        raise ValueError("there is nothing to confirm: the draft states no field")

    parts: list[str] = []
    if "condition" in stated:
        parts.append(f"your condition as {stated['condition']}")
    if "sensitivity_level" in stated:
        parts.append(f"your sensitivity as {stated['sensitivity_level']}")
    for entry in draft.medications:
        parts.append(f"{entry.name} as your {entry.role}")
    for routine in draft.routines:
        days = ", ".join(routine.days) if routine.days else "no particular days"
        parts.append(f"{routine.name} on {days}")
    for location in draft.locations:
        parts.append(f"{location} as one of your locations")

    listed = "; ".join(parts)
    return (
        f"Before I save anything, let me check I have this right: {listed}. "
        "Is that correct?"
    )


__all__ = [
    "DeclinedKind",
    "MedicationEntry",
    "ProfileDraft",
    "RoutineEntry",
    "decline_message",
    "limit_rejection_message",
    "restate_for_confirmation",
]
