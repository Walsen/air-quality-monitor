"""Resolving two records that claim the same measurement interval.

Requirement 7's job is convergence: an at-least-once transport and an overlapping
poll window must not double-count, and the stored value must not depend on arrival
order (Requirement 7.7).

Resolution is a PURE function of the two candidates. That is what makes order
independence provable rather than hoped for, and it is what lets the DynamoDB adapter
express the outcome as a CONDITIONAL WRITE instead of a read-modify-write: the
decision needs no store round-trip of its own, so there is no window for two
concurrent writers to interleave.

The precedence, highest first:

1. Ratified beats provisional (Requirements 7.4, 7.5) — status outranks magnitude, so
   a ratified 3 replaces a provisional 10.
2. At equal status, a differing value keeps the GREATER and marks the reading
   ``suspect_conflict`` (Requirement 7.6). Greater rather than newer because for a
   health advisory the more conservative of two irreconcilable readings is the safer
   one to serve.
3. Identical records write nothing (Requirement 7.3).

Why this converges: the outcome is fully determined by the highest ratification status
present, the maximum value among the records AT that status, and whether two distinct
values exist at that status. None of those depends on order — max and "are there two
distinct values" are both commutative. Property 8 asserts it against that closed form.

The subtlety that makes it work: a ratified replacement CLEARS a prior
``suspect_conflict``. If it preserved the flag, the final state would depend on whether
a conflicting provisional pair arrived before or after the ratified record.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, replace
from enum import StrEnum, auto

from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.domain.models import (
    CalibratedReading,
    DedupKey,
    QualityFlag,
)

_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Ratification statuses ranked. A table rather than a comparison chain so a third
# status becomes one entry instead of another branch (§1 open/closed).
_STATUS_RANK: dict[str, int] = {"P": 0, "R": 1}
_RATIFIED = "R"


class DedupAction(StrEnum):
    """What the caller should do with the incoming record."""

    STORE = auto()
    """The key was absent: process and store (Requirement 7.2)."""

    REPLACE = auto()
    """The incoming record wins: overwrite the stored Reading."""

    NO_WRITE = auto()
    """The stored Reading stands: make no write (Requirements 7.3, 7.5)."""


@dataclass(frozen=True, slots=True)
class DedupResolution:
    """The decision about one pair of candidates.

    ``winner`` is always populated, even for ``NO_WRITE``, so a caller can report the
    prevailing value without having to work out which side won.
    """

    action: DedupAction
    winner: SensorDataRecord
    quality_flag: QualityFlag | None = None
    warning: str | None = None
    is_duplicate: bool = False


def dedup_key_for(record: SensorDataRecord) -> DedupKey:
    """Build the Dedup_Key identifying this record's measurement (Requirement 7.1).

    The four fields are the WHOLE identity: two records sharing them are the same
    measurement whatever else they carry, which is precisely why a differing
    ``ScaledValue`` is a conflict to resolve rather than a second reading to store.
    """
    return DedupKey(
        site_code=record.SiteCode,
        species=record.Species,
        interval_start=dt.datetime.strptime(record.DateTime, _UTC_FORMAT).replace(
            tzinfo=dt.UTC
        ),
        duration=record.Duration,
    )


def resolve_duplicate(
    *,
    stored: SensorDataRecord | None,
    incoming: SensorDataRecord,
    stored_flag: QualityFlag | None = None,
) -> DedupResolution:
    """Decide between a stored record and an incoming one sharing its key.

    Args:
        stored: the record the stored Reading originated from, or None if the key is
            absent from the store.
        incoming: the newly arrived record.
        stored_flag: the stored Reading's current quality flag, so an already-known
            conflict is not forgotten by a later arrival.

    Returns:
        The action to take, the prevailing record, any quality flag the resulting
        Reading must carry, and a warning message when Requirement 7.5 or 7.6 asks
        for one. The warning is RETURNED rather than logged so this stays pure and
        the caller owns the one-log-per-record rule.

    Raises:
        ValueError: if the two records do not share a Dedup_Key. That is a caller
            bug, not bad upstream data: two different measurements are not duplicate
            candidates, and silently picking one would corrupt a stored reading (§5).
    """
    if stored is None:
        return DedupResolution(action=DedupAction.STORE, winner=incoming)

    if dedup_key_for(stored) != dedup_key_for(incoming):
        raise ValueError(
            "resolve_duplicate requires both records to share the same Dedup_Key; "
            f"got {dedup_key_for(stored)} and {dedup_key_for(incoming)}"
        )

    # Requirement 7.3 — every contract field equal. Model equality IS field equality
    # for these frozen records, so this needs no field list to fall out of date.
    if stored == incoming:
        return DedupResolution(
            action=DedupAction.NO_WRITE,
            winner=stored,
            quality_flag=stored_flag,
            is_duplicate=True,
        )

    stored_rank = _STATUS_RANK.get(stored.RatificationStatus, -1)
    incoming_rank = _STATUS_RANK.get(incoming.RatificationStatus, -1)

    if incoming_rank > stored_rank:
        return _ratified_supersedes(incoming)
    if incoming_rank < stored_rank:
        return _provisional_rejected(stored, incoming, stored_flag)
    return _equal_status(stored, incoming, stored_flag)


def _ratified_supersedes(incoming: SensorDataRecord) -> DedupResolution:
    """Requirement 7.4: a ratified value supersedes a provisional one.

    The flag is deliberately NOT carried over from the stored Reading. A ratified
    value is authoritative, so an earlier dispute between two provisional values is
    settled rather than inherited — and preserving it would make the outcome depend
    on arrival order (Requirement 7.7).
    """
    return DedupResolution(action=DedupAction.REPLACE, winner=incoming)


def _provisional_rejected(
    stored: SensorDataRecord,
    incoming: SensorDataRecord,
    stored_flag: QualityFlag | None,
) -> DedupResolution:
    """Requirement 7.5: a provisional value never displaces a ratified one."""
    key = dedup_key_for(stored)
    return DedupResolution(
        action=DedupAction.NO_WRITE,
        winner=stored,
        quality_flag=stored_flag,
        warning=(
            f"provisional record for {_describe(key)} did not displace the stored "
            f"ratified reading (incoming {incoming.ScaledValue}, "
            f"stored {stored.ScaledValue})"
        ),
    )


def _equal_status(
    stored: SensorDataRecord,
    incoming: SensorDataRecord,
    stored_flag: QualityFlag | None,
) -> DedupResolution:
    """Requirement 7.6, and the equal-value case that is not a conflict."""
    key = dedup_key_for(stored)

    if stored.ScaledValue == incoming.ScaledValue:
        # Same status, same value, but some other contract field differs — Req 7.3's
        # "all fields equal" does not apply and Req 7.6's "value differs" does not
        # either. There is nothing to choose between, so the stored Reading stands;
        # writing would churn the store for no change in what is served.
        return DedupResolution(
            action=DedupAction.NO_WRITE,
            winner=stored,
            quality_flag=stored_flag,
            is_duplicate=True,
        )

    # Keep the GREATER value: for a health advisory the more conservative of two
    # irreconcilable readings is the safer one to serve (Requirement 7.6).
    incoming_wins = incoming.ScaledValue > stored.ScaledValue
    return DedupResolution(
        action=DedupAction.REPLACE if incoming_wins else DedupAction.NO_WRITE,
        winner=incoming if incoming_wins else stored,
        quality_flag=QualityFlag.SUSPECT_CONFLICT,
        warning=(
            f"value conflict for {_describe(key)} at RatificationStatus "
            f"{stored.RatificationStatus}: stored {stored.ScaledValue}, incoming "
            f"{incoming.ScaledValue}; retaining the greater"
        ),
    )


def _describe(key: DedupKey) -> str:
    """Render a Dedup_Key for a log line (Requirements 7.5, 7.6 both name it)."""
    return (
        f"{key.site_code}/{key.species}/"
        f"{key.interval_start.strftime(_UTC_FORMAT)}/{key.duration}"
    )


def resolve_stored_reading(
    stored: CalibratedReading, incoming: CalibratedReading
) -> CalibratedReading:
    """Resolve two stored Readings sharing a Dedup_Key (Requirement 14.2).

    Requirement 14.2 says a write for an existing Dedup_Key resolves per Requirement 7, so
    this applies the SAME status precedence as :func:`resolve_duplicate` — reusing
    ``_STATUS_RANK`` rather than restating it, because two copies that drifted apart would
    let the store and the pipeline disagree about which value wins.

    It is a separate function rather than the same one because the inputs differ in kind: a
    ``CalibratedReading`` no longer carries every contract field, so Requirement 7.3's
    "all fields equal" cannot be evaluated here. What IS expressible — a ratified value
    superseding a provisional one, and an equal-status value conflict keeping the greater
    and marking it suspect — is enforced, which is what stops a blind overwrite from losing
    a ratified reading.

    Args:
        stored: the Reading already under this key.
        incoming: the Reading being written.

    Returns:
        Whichever Reading prevails, with ``suspect_conflict`` set when two equal-status
        Readings disagreed on the reported value (Requirement 7.6).
    """
    stored_rank = _STATUS_RANK.get(stored.ratification_status, -1)
    incoming_rank = _STATUS_RANK.get(incoming.ratification_status, -1)

    if incoming_rank > stored_rank:
        # Requirement 7.4: a ratified value supersedes, and settles any earlier dispute —
        # so the flag is NOT carried over (see resolve_duplicate for why that matters to
        # order independence).
        return incoming
    if incoming_rank < stored_rank:
        return stored  # Requirement 7.5

    if stored.reported_value == incoming.reported_value:
        return stored  # nothing to choose between

    # Requirement 7.6: keep the greater value, and record that the reading is disputed.
    winner = incoming if incoming.reported_value > stored.reported_value else stored
    return replace(winner, quality_flag=QualityFlag.SUSPECT_CONFLICT)
