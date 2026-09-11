"""The advice audit writer (Requirements 20.1, 20.2, 20.4, 20.5, 20.6).

**Req 20.5 shapes this whole module: the write is never a condition of answering.** `write`
returns a boolean and never raises. A user asking about the air they are breathing must not lose
their answer because an audit table was unavailable — the audit exists for the operator, and the
answer exists for the user.

**Req 20.4 is the clause most easily missed.** A guardrail-rejected turn gets a record too,
because a suppressed generation is exactly what an operator needs to see. Recording only
successful turns would make the audit a log of things that went fine, which is the opposite of
useful.

**Exactly one record per turn, enforced here rather than trusted of the caller.** A repair
attempt after a guardrail rejection is still ONE turn, so a writer recording per generation
would double-count precisely the turns most worth counting accurately. The latch is per WRITER,
and a writer is per turn — a process-wide latch would record the first turn of a run and nothing
after it.

A FAILED write does not close the latch, so a retry within the same turn can still record.
Closing it on failure would mean one transient error lost that turn's audit permanently.

**Req 20.6 is honoured by absence.** There is no method here that could notify or act on the
user's behalf, and a test pins the public surface to exactly `write` and `has_written` — so a
`notify` somebody adds later has to change that test to land, which makes it a reviewable act
rather than a quiet one.
"""

from __future__ import annotations

import datetime as dt

from aqm_advisor.domain.idempotency import TurnIdentity, advice_idempotency_key
from aqm_advisor.domain.models import BasisSummary, Escalation
from aqm_advisor.observability.logging import EventLogger
from aqm_advisor.ports.protocols import AdviceAuditStore, AdviceRecord


def build_advice_record(
    *,
    identity: TurnIdentity,
    turn_at: dt.datetime,
    route: str,
    escalation: Escalation | None,
    threshold_crossed: bool,
    driving_pollutant: str | None,
    basis: BasisSummary | None,
    guardrail_rejected: bool,
    rejection_category: str | None,
) -> AdviceRecord:
    """Assemble the record for one turn (Req 20.2).

    The record identifiers come from `BasisSummary.record_identifiers()`, which already existed
    for exactly this purpose. Its own docstring says why it lives there: Req 20.2 names "the
    retrieved records the Basis_Summary named", so deriving them anywhere else would let the
    audit trail claim provenance the response never cited. I had started to re-derive them here
    and a failing test caught it — the reference type has no `record_id` field at all.

    A turn with NO basis still produces a record, carrying no identifiers. Refusing to build one
    would lose the audit for exactly the degraded turns most worth auditing.

    The idempotency key is derived from the turn's STABLE identity (Req 32.4c), so a
    re-delivered turn yields the same key rather than a second row. `turn_at` is deliberately
    NOT part of it: an earlier version keyed on it and could not survive the redelivery it
    existed for, because a re-invoked entrypoint reads a later clock. When a turn happened is an
    audit fact; it is not what identifies the turn.
    """
    references = basis.record_identifiers() if basis is not None else ()
    return AdviceRecord(
        user_id=identity.user_id,
        turn_at=turn_at,
        route=route,
        escalated=escalation is not None,
        threshold_crossed=threshold_crossed,
        driving_pollutant=driving_pollutant,
        record_references=references,
        guardrail_rejected=guardrail_rejected,
        rejection_category=rejection_category,
        idempotency_key=advice_idempotency_key(identity=identity),
    )


class AuditWriter:
    """Writes exactly one Advice_Record per turn, and never fails the turn doing it.

    One instance per turn. The latch lives here rather than in the pipeline so the guarantee
    does not depend on the call site remembering it — the same reasoning that moved the
    escalation short-circuit into `TurnPipeline.run`.
    """

    def __init__(self, *, store: AdviceAuditStore, logger: EventLogger) -> None:
        """Take the injected store and where to log a failure."""
        self._store = store
        self._logger = logger
        self._written = False

    @property
    def has_written(self) -> bool:
        """Whether this turn's record has been written.

        Observable so the pipeline can assert one record per turn rather than trusting the call
        site.
        """
        return self._written

    def write(self, record: AdviceRecord) -> bool:
        """Attempt the write. Returns whether a record was appended; never raises (Req 20.5).

        Catches the expected store failures only — `OSError` for a transport or filesystem
        problem, `ValueError` for a body the store refuses. A broader catch belongs at the
        entrypoint boundary (Req 21.5), and swallowing everything here would hide a programming
        error in the record assembly as though it were an unavailable table.

        The failure log names the error TYPE, never its message: a store's message can quote the
        row it was writing, and that row IS the audit record. It also carries no user identity —
        Req 5.3 permits at most the pseudonymous one, and a failure log does not need even that
        to be actionable.
        """
        if self._written:
            return False
        try:
            self._store.append(record)
        except (OSError, ValueError) as error:
            self._logger.warning(
                "advice_audit_write_failed", error_type=type(error).__name__
            )
            return False
        self._written = True
        return True


__all__ = ["AuditWriter", "build_advice_record"]
