"""The advisory turn's domain models (tasks 3.1 and 3.2).

Three structural decisions carry most of the weight here, and each replaces a rule someone has
to remember with a shape that cannot hold the mistake.

**The credential and the user's prose are `SecretStr`.** Req 5.2 forbids the credential reaching
any log entry, record, response or error message, and Req 19.2 forbids an utterance substring
reaching a log. A convention that every call site must remember is a convention that one call
site will forget, so the values refuse to render instead. `credential` is additionally excluded
from serialisation — not masked, absent, because a masked placeholder still advertises that a
credential was there.

**There is no `max_length` on the utterance.** Req 1.5 makes the maximum CONFIGURED, so pinning
one on the field would create a second authority that a configured value could disagree with.
That is the same fault Req 15.6 forbids for measurement weakness, and the same one that made
Service 2's profile limits "configured" in name only. Length is checked once, by
`validate_utterance_length`, against the value configuration supplies.

**Every basis field is copied, never computed.** Req 9.4 says read the basis and do not
re-derive any part of it, so these are plain carriers: no defaults that could invent a value, no
validators that could normalise one, and no method that derives a reading from another field.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from aqm_advisor.domain.instants import iso_z

DEFAULT_MAX_UTTERANCE_LENGTH = 4000
"""Req 1.5's default. The EFFECTIVE limit comes from configuration; this is the fallback."""


class TurnContractError(ValueError):
    """A request that must be refused before the Model_Port is invoked (Req 1.4, 1.5).

    A `ValueError` subclass so the FastAPI and AgentCore boundaries can map it to a 400
    alongside Pydantic's own failures, rather than needing a second handler for the same class
    of fault.
    """


class BlankUtteranceError(TurnContractError):
    """Req 1.4: the utterance was empty or only whitespace."""

    def __init__(self) -> None:
        """Build the refusal, naming only the field."""
        super().__init__("utterance must not be empty or only whitespace")


class UtteranceTooLongError(TurnContractError):
    """Req 1.5: the utterance exceeded the configured maximum.

    The message names the field and the limit and NOTHING ELSE. It must not quote the offending
    text: rejection messages are logged, and an echoed utterance would carry the user's own
    words into a log that Req 19.2 forbids them from reaching. Service 2 shipped exactly this
    defect — a message that named the remaining hours disclosed the start time it was meant to
    withhold.
    """

    def __init__(self, *, length: int, max_length: int) -> None:
        """Build the refusal, naming the field and the limit but never the text."""
        super().__init__(
            f"utterance exceeds the configured maximum of {max_length} characters "
            f"(received {length})"
        )
        self.length = length
        self.max_length = max_length


def validate_utterance_length(utterance: str, *, max_length: int) -> None:
    """Check an utterance against the CONFIGURED maximum (Req 1.5).

    Separate from the model because the limit is configuration, not contract. Called at the edge
    before the Model_Port is invoked, which is what Req 1.5 requires — a check that ran after
    the model call would have already paid for the thing it was meant to prevent.

    Raises:
        UtteranceTooLongError: naming the field and the limit, never the text.
    """
    if len(utterance) > max_length:
        raise UtteranceTooLongError(length=len(utterance), max_length=max_length)


class _StrictModel(BaseModel):
    """Rejects unknown fields, so "exactly these fields" holds at construction time."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _require_aware(instant: dt.datetime) -> dt.datetime:
    """Refuse a naive instant.

    Req 1.3 takes the turn instant from the injected Clock, which yields aware instants. A naive
    value means something read a wall clock instead, so this refuses rather than assuming a
    zone.
    """
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise ValueError(
            "instant must carry a timezone; a naive value means a wall clock was read"
        )
    return instant


class PriorTurn(_StrictModel):
    """One earlier exchange, carried so the agent can follow a conversation.

    Both fields are `SecretStr`. This is not a credential, but it is the user's own words and
    the guidance given back — the exact material Req 19.2 keeps out of logs. Making them refuse
    to render means a debug log of the request cannot leak the conversation, whatever the call
    site does.
    """

    utterance: SecretStr
    guidance: SecretStr


class AdvisoryRequest(_StrictModel):
    """Req 1.1's request: an utterance, a credential, optional prior turns and locale."""

    utterance: str
    credential: Annotated[SecretStr, Field(exclude=True)]
    prior_turns: tuple[PriorTurn, ...] = ()
    locale: str | None = None

    @field_validator("utterance")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Req 1.4: refuse an empty or whitespace-only utterance, and store one trimmed value.

        A `min_length=1` constraint would accept `" "`, which is the case Req 1.4 names
        explicitly. Trimming here rather than at a call site means the stored value and the
        value the configured length check sees are the same one — two differing lengths for the
        same request would make the limit ambiguous at the boundary.
        """
        trimmed = value.strip()
        if not trimmed:
            raise BlankUtteranceError
        return trimmed


class SpeciesBasis(_StrictModel):
    """One species' contribution to the reading, as Service 2 reported it."""

    species: str
    sub_index: int | None
    band: str | None
    confidence: str


class RecordReference(_StrictModel):
    """One retrieved Reading a claim rests on, as Service 2 names it in `basis.records`."""

    site_code: str
    species: str
    date_time: dt.datetime
    duration: str

    @field_validator("date_time")
    @classmethod
    def _aware(cls, value: dt.datetime) -> dt.datetime:
        return _require_aware(value)

    def identifier(self) -> str:
        """The stable composite identifier an Advice_Record stores (Req 20.2).

        All four parts are needed. A bare site code would collapse records that are genuinely
        distinct — one sensor reports several species, and one sensor and species report at
        successive instants — so an audit trail keyed on it would under-report provenance while
        looking complete.

        None of the parts is health-adjacent: a site code, a pollutant name, an instant and a
        duration are public sensor facts, so storing the composite does not breach Req 20.3.
        """
        return f"{self.site_code}:{self.species}:{iso_z(self.date_time)}:{self.duration}"


class NowcastBasis(_StrictModel):
    """The nowcast weighting behind the sub-index, as Service 2 names it in `basis.nowcast`.

    Part of how the index was DERIVED: the same readings under a different window length, a
    different count of hours available, or a different weight factor give a different sub-index.
    Req 9.3 requires that derivation to be traceable, so a basis naming only the breakpoint
    table left a step unaccounted for.

    This is provenance for review, NOT a second trigger for a disclosure. Req 15.6 makes the
    confidence Service 2 returned the single authority on how weak a measurement is, and Service
    2 has already capped it for an incomplete window.
    """

    window_hours: int
    hours_available: int
    weight_factor: float


class BasisSummary(_StrictModel):
    """Req 9's reviewable basis. Every field is copied from the retrieved response (Req 9.4)."""

    driving_pollutant: str | None
    site_code: str
    distance_km: float
    as_of: dt.datetime | None
    per_species: tuple[SpeciesBasis, ...]
    threshold: int | None
    threshold_source: str | None
    breakpoint_table: str | None
    calibration_strategies: Mapping[str, str]
    nowcast: NowcastBasis | None
    records: tuple[RecordReference, ...]

    @field_validator("as_of")
    @classmethod
    def _aware(cls, value: dt.datetime | None) -> dt.datetime | None:
        return None if value is None else _require_aware(value)

    def record_identifiers(self) -> tuple[str, ...]:
        """The identifiers an Advice_Record stores (Req 20.2).

        Lives here because Req 20.2 names "the retrieved records the Basis_Summary named" —
        deriving them anywhere else would let the audit trail claim provenance the response
        never cited.
        """
        return tuple(reference.identifier() for reference in self.records)

    @property
    def nowcast_window_is_complete(self) -> bool | None:
        """Whether the nowcast used its whole window; `None` when it was not nowcast-derived.

        Three-valued on purpose. `None` here means NOT nowcast-derived (Req 9.3a) — a complete,
        ordinary answer — and NOT that the window is unknown. Returning `True` for an absent
        nowcast would claim a completeness that was never measured; returning `False` would
        invent a weakness that does not exist. This is the same distinction as Service 2's
        correlation returning `None` on zero variance, where "nothing to compare" is not "no
        relationship".

        Read-only provenance. Req 15.6 forbids using it to gate a disclosure.
        """
        if self.nowcast is None:
            return None
        return self.nowcast.hours_available >= self.nowcast.window_hours


class GuardrailEnvelope(_StrictModel):
    """The three strings Service 2 returns, carried through unchanged.

    Never composed here. Req 8.5 requires the envelope on every response including a degraded
    one, and a locally-composed fallback would be this service inventing a disclaimer Service 2
    is responsible for.
    """

    advisory_scope: str
    emergency_guidance: str
    disclaimer: str


class Escalation(_StrictModel):
    """A determination that the turn must direct the user onward rather than merely advise."""

    kind: Literal["emergency", "clinician"]
    markers: tuple[str, ...]
    guidance: str

    @model_validator(mode="after")
    def _require_a_marker(self) -> Self:
        """An escalation must name what triggered it.

        Req 10 escalates on recognised red-flag markers, and Req 9's reviewability applies here
        most of all: an escalation with no marker could not be audited or explained to a
        clinician, and would be indistinguishable from a spurious one.
        """
        if not self.markers:
            raise ValueError("an escalation must name at least one marker that triggered it")
        return self


class AdvisoryResponse(_StrictModel):
    """Req 1.2's response: exactly these six fields.

    There is no field able to hold a credential (Req 1.6), which makes the prohibition
    structural rather than a rule a future field could break.
    """

    guidance: str | None
    basis: BasisSummary | None
    envelope: GuardrailEnvelope
    escalation: Escalation | None
    degraded: bool = False
    answered_at: dt.datetime

    @field_validator("answered_at")
    @classmethod
    def _aware(cls, value: dt.datetime) -> dt.datetime:
        return _require_aware(value)


__all__ = [
    "DEFAULT_MAX_UTTERANCE_LENGTH",
    "AdvisoryRequest",
    "AdvisoryResponse",
    "BasisSummary",
    "BlankUtteranceError",
    "Escalation",
    "GuardrailEnvelope",
    "NowcastBasis",
    "PriorTurn",
    "RecordReference",
    "SpeciesBasis",
    "TurnContractError",
    "UtteranceTooLongError",
    "validate_utterance_length",
]
