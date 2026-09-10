"""The audit writer: one record per served response.

Requirements 25.7, 25.8, 25.9.

WHAT THIS MODULE DELIBERATELY CANNOT DO. Requirement 25.10 forbids any autonomous action on a
Threshold_Crossing — no notification, no message, no external call — and this module is the one
that learns a crossing happened. So it has no client, transport or publisher in scope, and a
test
greps this module's own source for such names. That is a blunt check, but the requirement is a
blunt prohibition: the point is that adding a notifier here would have to be a visible act.

Requirement 25.8 is what makes the record safe to keep: it carries the identity, the instant,
the
route, and PROVENANCE — never a Condition, a Sensitivity_Level, a Personal_Threshold value, a
User_Location coordinate, or a forecast or pollen value. Everything but the identity is
non-identifying, which is exactly why Requirement 17.8's erasure can clear the identity and
retain
the rest as a de-identified count.
"""

from __future__ import annotations

import datetime as dt

from aqm_ingestion.ports.protocols import AuditIdentifiers
from aqm_ingestion.serving.basis import Basis

DEFAULT_AUDIT_RETENTION_DAYS = 365
"""Requirement 25.9's default retention period for an Audit_Record."""


def audit_record_for(
    user_id: str,
    served_at: dt.datetime,
    route: str,
    basis: Basis,
    threshold_crossed: bool,
) -> AuditIdentifiers:
    """Build the Audit_Record for one served response (Requirement 25.7).

    Derived from the BASIS rather than assembled separately, so the audit trail and the response
    cannot disagree about what a value rested on — two independent derivations would be free to
    drift, and an audit trail that disagrees with the response it audits is worse than none.

    ``threshold_crossed`` is a boolean, not the threshold: Requirement 25.7 wants whether a
    crossing was reported and Requirement 25.8 forbids the value.
    """
    return AuditIdentifiers(
        user_id=user_id,
        served_at=served_at,
        route=route,
        breakpoint_table=basis.breakpoint_table,
        # A SET, per Requirement 25.7: sorted and deduplicated, which also keeps the entry
        # byte-comparable across runs (§2).
        calibration_strategies=tuple(
            sorted({entry.calibration_strategy for entry in basis.species})
        ),
        threshold_crossed=threshold_crossed,
        record_references=tuple(
            reference.archive_id for reference in basis.records
        ),
    )


__all__ = ["DEFAULT_AUDIT_RETENTION_DAYS", "audit_record_for"]
