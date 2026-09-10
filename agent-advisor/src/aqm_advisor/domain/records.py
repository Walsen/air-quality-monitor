"""The grounding set, the symptom draft and the audit record (task 3.3).

**The audit record's minimisation is structural.** Req 20.3 keeps the utterance, the guidance
text, a
condition, a sensitivity, a personal threshold and a coordinate out of the trail, so that
erasure has only
an identity to remove. There is no field able to hold any of them — a convention would rely on
every future
call site remembering, whereas an absent field cannot be populated. `threshold_crossed` is a
`bool` for the
same reason: Req 20.2 records THAT a threshold was crossed, and the type refuses the value.

**The draft is immutable and starts unconfirmed.** Req 28.2 says inferring a severity from prose
is a
judgement about the user's health and the user is the authority on it, so `confirm()` and
`correct()` return
new drafts rather than mutating the one the user reviewed. A correction also comes back
UNCONFIRMED: the
user agreeing that the agent misread them is not the same as agreeing to the replacement.

**Nothing here reads a clock or generates randomness.** The idempotency key is a digest of the
turn's own
identity, so a retry of the same turn produces the same key — a random or time-based key would
defeat
Req 32.4c by making every delivery look new.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aqm_advisor.domain.instants import iso_z


class _StrictModel(BaseModel):
    """Rejects unknown fields and forbids mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolCall(_StrictModel):
    """One retrieval the agent performed, as an event in the turn's trajectory."""

    name: str


class RetrievedValues(_StrictModel):
    """What Service 2 actually served, as the permitted set for grounding.

    The value collections are `frozenset` because grounding asks a MEMBERSHIP question — was
    this numeral
    served? — and a set makes the order of retrieved values irrelevant to the answer, so
    grounding cannot
    accidentally depend on it.

    `tool_calls` is a tuple because Req 35.4 asserts which tools were called and IN WHAT ORDER.
    It is a
    sequence of events rather than a set of names: collapsing a repeated call would hide a retry
    loop,
    which is exactly what a trajectory assertion exists to reveal.
    """

    numerals: frozenset[str]
    medications: frozenset[str]
    pollen_categories: frozenset[str]
    tool_calls: tuple[ToolCall, ...]


class SymptomEntryDraft(_StrictModel):
    """A proposed diary entry, awaiting the user's agreement (Req 28.1-28.3)."""

    date: dt.date
    severity: int = Field(ge=1, le=5)
    markers: tuple[str, ...]
    reliever_used: bool
    note: str | None = Field(default=None, max_length=280)
    confirmed: bool = False

    def confirm(self) -> Self:
        """Return a confirmed copy. Req 28.3 refuses a write until this has happened."""
        return self.model_copy(update={"confirmed": True})

    def correct(
        self,
        *,
        severity: int | None = None,
        markers: tuple[str, ...] | None = None,
        reliever_used: bool | None = None,
        note: str | None = None,
    ) -> Self:
        """Apply the user's correction, returning an UNCONFIRMED copy (Req 28.2).

        Unconfirmed on purpose. The user telling the agent it misread them is not the same as
        the user
        agreeing to whatever the agent substituted, so the corrected draft goes back for
        confirmation
        rather than straight to a write.
        """
        update: dict[str, object] = {"confirmed": False}
        if severity is not None:
            update["severity"] = severity
        if markers is not None:
            update["markers"] = markers
        if reliever_used is not None:
            update["reliever_used"] = reliever_used
        if note is not None:
            update["note"] = note
        return self.model_copy(update=update)


def advice_idempotency_key(*, user_id: str, turn_at: dt.datetime, route: str) -> str:
    """Derive the key that collapses a duplicate Advice_Record write (Req 32.4c).

    A re-invoked entrypoint delivers the same turn twice, so the key is derived from the turn's
    own
    identity — never from a random value or the current time, either of which would make every
    delivery
    look new and defeat the deduplication this exists for.

    The instant is normalised through `iso_z` first, so the same instant expressed in another
    offset
    yields the same key: without that, a retry could be recorded twice.

    Returns a digest rather than a concatenation, because the key is stored and may be logged,
    and a key
    embedding the raw identity would put it somewhere Req 5.3 does not sanction.
    """
    material = f"{user_id}|{iso_z(turn_at)}|{route}".encode()
    return hashlib.sha256(material).hexdigest()


class AdviceRecord(_StrictModel):
    """Req 20's audit entry: what was advised, on what retrieved data, at what instant.

    Every field here is either an identity, a flag, a category, or a public sensor fact. There
    is
    deliberately nowhere to put an utterance, guidance text, a condition, a sensitivity, a
    personal
    threshold or a coordinate (Req 20.3).
    """

    user_id: str
    turn_at: dt.datetime
    route: str
    escalated: bool
    threshold_crossed: bool
    driving_pollutant: str | None
    record_references: tuple[str, ...]
    guardrail_rejected: bool
    rejection_category: str | None
    idempotency_key: str

    @field_validator("turn_at")
    @classmethod
    def _aware(cls, value: dt.datetime) -> dt.datetime:
        """Req 1.3's instant comes from the injected Clock, which yields aware instants."""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "turn_at must carry a timezone; a naive value means a wall clock was read"
            )
        return value


__all__ = [
    "AdviceRecord",
    "RetrievedValues",
    "SymptomEntryDraft",
    "ToolCall",
    "advice_idempotency_key",
]
