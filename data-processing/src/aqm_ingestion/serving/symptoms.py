"""The Symptom_Log on the serving path (Requirement 31).

Sits between the routes and the store so the routes stay thin and the two rules needing a
clock live in one place: Requirement 31.11's future-date refusal and 31.8's retention floor.

REQUIREMENT 31.10 SHAPES EVERY LOG LINE HERE. It permits "at most the pseudonymous identity and
the date of an entry that was recorded" — so a severity, a marker, a reliever flag and a note
may never be logged, and this module logs the identity and the date and nothing else. The
``SymptomEntry`` model's own ``__repr__`` is the backstop: even an accidental interpolation
renders only those two fields.

The wire shape is camelCase to match the Requirement 19.2 response body, and the translation
lives here rather than on the model: the domain model is what the store and the association
read, and naming its fields for a JSON convention would push a wire concern into the domain.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from aqm_ingestion.domain.association import LearnedThreshold
from aqm_ingestion.domain.symptoms import (
    DEFAULT_SYMPTOM_LIMITS,
    SymptomEntry,
    SymptomLogLimits,
    build_symptom_entry,
)
from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import SymptomLogStore, VerifiedIdentity

_logger = get_logger(__name__)

_WIRE_TO_MODEL: Mapping[str, str] = {
    "entryDate": "entry_date",
    "relieverUsed": "reliever_used",
}
"""The camelCase members whose model name differs. Others pass through unchanged."""


@dataclass(frozen=True, slots=True)
class SymptomWindow:
    """A validated inclusive date range over the diary."""

    start: dt.date
    end: dt.date


class SymptomLogService:
    """Diary access for the serving path."""

    def __init__(
        self,
        store: SymptomLogStore,
        clock: Clock,
        limits: SymptomLogLimits = DEFAULT_SYMPTOM_LIMITS,
    ) -> None:
        """Hold the store, the clock every date rule is measured from, and the bounds."""
        self._store = store
        self._clock = clock
        self._limits = limits

    def write(
        self, identity: VerifiedIdentity, submitted: Mapping[str, object]
    ) -> SymptomEntry:
        """Record one day's entry, replacing any existing one for that date.

        Args:
            identity: the VERIFIED identity. The user id comes from here and never from the
                body, so a caller cannot write into someone else's diary by naming them.
            submitted: the request body, in the wire's camelCase.

        Returns:
            The stored entry.

        Raises:
            pydantic.ValidationError: naming the offending field, never echoing its value.
        """
        fields = {
            _WIRE_TO_MODEL.get(key, key): value for key, value in submitted.items()
        }
        # The identity is IMPOSED, not merged: a body naming a different user must not be
        # able to redirect the write, and overwriting keeps the rule in one place.
        fields["user_id"] = identity.user_id
        entry = build_symptom_entry(
            fields, now=self._clock.now(), limits=self._limits
        )
        stored = self._store.put(entry)
        # Requirement 31.10: the identity and the date, and nothing else.
        _logger.info(
            "symptom_entry_recorded",
            user_id=stored.user_id,
            entry_date=stored.entry_date.isoformat(),
        )
        return stored

    def read(
        self, identity: VerifiedIdentity, window: SymptomWindow
    ) -> Sequence[SymptomEntry]:
        """Return this user's entries in the window, retention already applied by the store."""
        return self._store.query_window(identity.user_id, window.start, window.end)

    def default_window(self) -> SymptomWindow:
        """The window used when the caller names neither bound: the retention window itself.

        The retention window rather than an arbitrary span, because everything inside it is
        exactly what the service still holds — a shorter default would hide entries the user can
        legitimately still read, and a longer one would promise entries that have aged out.
        """
        today = self._clock.now().date()
        return SymptomWindow(
            start=today - dt.timedelta(days=self._limits.retention_days), end=today
        )

    def learned_for(
        self, identity: VerifiedIdentity
    ) -> Mapping[str, LearnedThreshold]:
        """This user's Learned_Thresholds, for Requirement 22.1's fourth precedence tier.

        A READ only. Requirement 32.12 forbids computing an association on the serving path: the
        derivation runs on its own schedule, so the serving path reads what it stored and never
        recomputes. There is deliberately no code path here that could derive one.
        """
        return self._store.learned_thresholds(identity.user_id)

    def forget(self, identity: VerifiedIdentity) -> int:
        """Erase this user's diary and return how many entries were removed."""
        removed = self._store.forget_user(identity.user_id)
        _logger.info(
            "symptom_log_erased", user_id=identity.user_id, entries_removed=removed
        )
        return removed


def entry_out(entry: SymptomEntry) -> dict[str, object]:
    """Render one entry for the response body.

    The note IS returned here and only here (Requirement 31.6): it exists for the user's own
    recall, so the route that serves it to its author is the one place it belongs. It reaches no
    computation and no other body.
    """
    return {
        "entryDate": entry.entry_date.isoformat(),
        "severity": entry.severity,
        "markers": [str(marker) for marker in entry.markers],
        "relieverUsed": entry.reliever_used,
        "note": entry.note,
        "recordedAt": entry.recorded_at.isoformat(),
    }


def parse_symptom_window(
    raw_start: str | None, raw_end: str | None, default: SymptomWindow
) -> tuple[SymptomWindow | None, list[str]]:
    """Parse the window parameters, accumulating EVERY problem (§5, Requirement 19.6).

    Returns the window and an empty problem list, or None and every problem found. Both bounds
    absent is the default window rather than an error, matching the history route's reading of
    Requirement 19.6: "one without the other" is the fault named, so neither is not one.
    """
    problems: list[str] = []
    start = _parse_date("start", raw_start, problems)
    end = _parse_date("end", raw_end, problems)

    if raw_start is None and raw_end is None:
        return default, problems
    if raw_start is None or raw_end is None:
        absent = "start" if raw_start is None else "end"
        problems.append(f"{absent}: required when the other bound is supplied")
    if problems or start is None or end is None:
        return None, problems
    if end < start:
        problems.append("end: must not precede start")
        return None, problems
    return SymptomWindow(start=start, end=end), problems


def _parse_date(name: str, raw: str | None, problems: list[str]) -> dt.date | None:
    """Parse one ISO date, naming the parameter and the expected form on failure."""
    if raw is None:
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        problems.append(f"{name}: expected an ISO-8601 calendar date, as YYYY-MM-DD")
        return None


__all__ = [
    "SymptomLogService",
    "SymptomWindow",
    "entry_out",
    "parse_symptom_window",
]
