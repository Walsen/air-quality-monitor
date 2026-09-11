"""Tests for the advice audit writer (task 12.1). Validates Reqs 20.1, 20.2, 20.4, 20.5, 20.6.

**Req 20.5 is the clause that shapes the whole module: the write is never a condition of
answering.** So `AuditWriter.write` returns a boolean and never raises, and the tests prove that
against a store that throws. A user asking about the air they are breathing must not lose their
answer because an audit table was unavailable — the audit exists for the operator, and the
answer exists for the user.

**Req 20.4 is the clause most easily missed.** A guardrail-rejected turn is exactly the turn an
operator needs to see, so it gets a record too. Writing records only for successful turns would
make the audit a log of things that went fine, which is the opposite of useful.

**Exactly one record per turn, and the writer enforces it.** A repair attempt after a guardrail
rejection is still ONE turn, so a writer that recorded per generation would double-count the
turns most worth counting accurately.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from aqm_advisor.agent.audit import AuditWriter, build_advice_record
from aqm_advisor.domain.idempotency import TurnIdentity
from aqm_advisor.domain.models import (
    BasisSummary,
    Escalation,
    RecordReference,
    SpeciesBasis,
)
from aqm_advisor.observability.logging import EventLogger
from aqm_advisor.ports.protocols import AdviceRecord

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


class _RecordingStore:
    """Accepts records and counts them."""

    def __init__(self) -> None:
        self.records: list[AdviceRecord] = []

    def append(self, record: AdviceRecord) -> None:
        self.records.append(record)

    def forget_user(self, user_id: str) -> int:
        before = len(self.records)
        self.records = [r for r in self.records if r.user_id != user_id]
        return before - len(self.records)


class _FailingStore:
    """Refuses every write, the way an unavailable table would."""

    def __init__(self, error: Exception | None = None) -> None:
        self.attempts = 0
        self._error = error or OSError("the audit table is unavailable")

    def append(self, record: AdviceRecord) -> None:
        self.attempts += 1
        raise self._error

    def forget_user(self, user_id: str) -> int:
        raise self._error


def _logger() -> EventLogger:
    return EventLogger(logging.getLogger("aqm_advisor.test.audit"))


def _reference(**kwargs: object) -> RecordReference:
    defaults: dict[str, object] = {
        "site_code": "AQM1",
        "species": "PM25",
        "date_time": _AT,
        "duration": "PT1H",
    }
    return RecordReference(**{**defaults, **kwargs})  # type: ignore[arg-type]


def _basis() -> BasisSummary:
    return BasisSummary(
        driving_pollutant="PM25",
        site_code="AQM1",
        distance_km=1.2,
        as_of=_AT,
        per_species=(
            SpeciesBasis(species="PM25", sub_index=68, band="Moderate", confidence="high"),
        ),
        threshold=None,
        threshold_source=None,
        breakpoint_table="epa-2024-05-06",
        calibration_strategies={"PM25": "rh_linear"},
        nowcast=None,
        records=(_reference(), _reference(species="NO2")),
    )


def _record(**kwargs: object) -> AdviceRecord:
    return build_advice_record(
        identity=TurnIdentity(user_id="user-1", session_id="s" * 33),
        turn_at=_AT,
        route="/invocations",
        **{  # type: ignore[arg-type]
            "escalation": None,
            "threshold_crossed": False,
            "driving_pollutant": "PM25",
            "basis": _basis(),
            "guardrail_rejected": False,
            "rejection_category": None,
            **kwargs,
        },
    )


# --- Req 20.2: what the record carries ----------------------------------


def test_the_record_carries_the_retrieved_record_identifiers() -> None:
    # Req 20.2 names "the identifiers of the retrieved records the Basis_Summary named". Without
    # them the
    # trail cannot answer which reading a piece of advice rested on, which is its entire
    # purpose.
    #
    # The identifier is a COMPOSITE of site, species, instant and duration — not a bare id. A
    # site code
    # alone would fold together records that are genuinely distinct, because one sensor reports
    # several
    # species and one sensor reports at successive instants, so the trail would under-report
    # provenance
    # while looking complete.
    references = _record().record_references
    assert references == (
        "AQM1:PM25:2026-07-01T12:00:00Z:PT1H",
        "AQM1:NO2:2026-07-01T12:00:00Z:PT1H",
    )


def test_the_identifiers_come_from_the_basis_rather_than_being_rebuilt() -> None:
    # `BasisSummary.record_identifiers()` already existed for exactly this, and its docstring
    # says why it
    # lives there: deriving them anywhere else would let the audit claim provenance the response
    # never
    # cited. I started to re-derive them in the writer and a failing test caught it — the
    # reference type
    # has no bare id field at all.
    assert _record().record_references == _basis().record_identifiers()


def test_the_record_notes_whether_an_escalation_was_returned() -> None:
    escalation = Escalation(
        kind="emergency", markers=("blue lips",), guidance="Call emergency services."
    )
    assert _record(escalation=escalation).escalated is True
    assert _record().escalated is False


def test_the_record_carries_the_driving_pollutant() -> None:
    assert _record().driving_pollutant == "PM25"


def test_a_turn_with_no_basis_records_no_identifiers_rather_than_failing() -> None:
    # A degraded turn retrieved nothing, and it still gets a record. Refusing to build one would
    # lose the
    # audit for exactly the turns most worth auditing.
    assert _record(basis=None).record_references == ()


def test_the_record_derives_its_own_idempotency_key() -> None:
    # Req 32.4c. Derived from the turn's identity, so a re-delivered turn produces the same key.
    assert _record().idempotency_key == _record().idempotency_key


def test_two_different_turns_get_different_keys() -> None:
    other = build_advice_record(
        identity=TurnIdentity(user_id="user-2", session_id="s" * 33),
        turn_at=_AT,
        route="/invocations",
        escalation=None,
        threshold_crossed=False,
        driving_pollutant="PM25",
        basis=_basis(),
        guardrail_rejected=False,
        rejection_category=None,
    )
    assert _record().idempotency_key != other.idempotency_key


# --- Req 20.4: a rejected generation is recorded ------------------------


def test_a_guardrail_rejected_turn_is_recorded_with_the_rejection() -> None:
    # Req 20.4. A suppressed generation is exactly what an operator needs to see, so recording
    # only
    # successful turns would make the audit a log of things that went fine.
    record = _record(guardrail_rejected=True, rejection_category="diagnosis")
    assert record.guardrail_rejected is True
    assert record.rejection_category == "diagnosis"


def test_a_clean_turn_records_no_rejection_category() -> None:
    # Non-vacuity: a category on every turn would make the field meaningless to filter on.
    assert _record().rejection_category is None


# --- Req 20.1 / 20.4: exactly one record per turn -----------------------


def test_the_writer_writes_exactly_one_record() -> None:
    store = _RecordingStore()
    writer = AuditWriter(store=store, logger=_logger())
    assert writer.write(_record()) is True
    assert len(store.records) == 1


def test_a_second_write_for_the_same_turn_is_refused() -> None:
    # A repair attempt after a guardrail rejection is still ONE turn, so a writer recording per
    # generation would double-count exactly the turns most worth counting accurately.
    store = _RecordingStore()
    writer = AuditWriter(store=store, logger=_logger())
    writer.write(_record())
    assert writer.write(_record()) is False
    assert len(store.records) == 1


def test_the_writer_reports_whether_it_has_written() -> None:
    # Observable so the pipeline can assert one record per turn rather than trusting the call
    # site.
    writer = AuditWriter(store=_RecordingStore(), logger=_logger())
    assert writer.has_written is False
    writer.write(_record())
    assert writer.has_written is True


def test_a_fresh_writer_per_turn_writes_again() -> None:
    # The latch is per TURN, not per process. A process-wide latch would record the first turn
    # only.
    store = _RecordingStore()
    AuditWriter(store=store, logger=_logger()).write(_record())
    AuditWriter(store=store, logger=_logger()).write(_record())
    assert len(store.records) == 2


# --- Req 20.5: the write is never a condition of answering --------------


def test_a_failing_store_does_not_raise(caplog: pytest.LogCaptureFixture) -> None:
    # THE Req 20.5 test. A user asking about the air they are breathing must not lose their
    # answer
    # because an audit table was unavailable.
    caplog.set_level(logging.WARNING)
    writer = AuditWriter(store=_FailingStore(), logger=_logger())
    assert writer.write(_record()) is False


def test_a_failing_store_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    # Req 20.5's other half, and section 6 of the practices: errors handled must still be
    # logged. A
    # silently dropped audit is indistinguishable from a turn that never happened.
    caplog.set_level(logging.WARNING)
    AuditWriter(store=_FailingStore(), logger=_logger()).write(_record())
    assert len(caplog.records) == 1


def test_the_failure_log_names_the_error_type_not_its_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Same discipline as the boundary handler: a store's message can quote the row it was
    # writing, and
    # that row is the audit record.
    caplog.set_level(logging.WARNING)
    marker = "AUDIT-ROW-DETAIL-XYZZY"
    AuditWriter(store=_FailingStore(OSError(marker)), logger=_logger()).write(_record())
    from aqm_advisor.observability.logging import _JsonFormatter

    line = _JsonFormatter().format(caplog.records[0])
    assert "OSError" in line
    assert marker not in line


def test_a_failed_write_does_not_close_the_latch() -> None:
    # So a retry within the same turn can still record. Closing the latch on failure would mean
    # one
    # transient error lost the audit for that turn permanently.
    store = _FailingStore()
    writer = AuditWriter(store=store, logger=_logger())
    writer.write(_record())
    writer.write(_record())
    assert store.attempts == 2


def test_the_user_identity_is_never_written_to_the_failure_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Req 5.3 permits at most the pseudonymous identity, and a failure log does not need even
    # that to be
    # actionable — the error type and the fact of failure are enough.
    caplog.set_level(logging.WARNING)
    AuditWriter(store=_FailingStore(), logger=_logger()).write(_record())
    from aqm_advisor.observability.logging import _JsonFormatter

    assert "user-1" not in _JsonFormatter().format(caplog.records[0])


# --- Req 20.6: no action, no notification -------------------------------


def test_the_writer_exposes_no_way_to_notify() -> None:
    # Req 20.6, structural. The research's no-autonomous-action guardrail is easiest to honour
    # by having
    # no method that could act: a `notify` somebody adds later has to change this test to land.
    surface = {name for name in dir(AuditWriter) if not name.startswith("_")}
    for forbidden in ("notify", "send", "alert", "email", "publish", "dispatch", "act"):
        assert forbidden not in surface, forbidden


def test_the_writer_surface_is_exactly_what_it_needs() -> None:
    # Pins the surface, so the sweep above cannot be defeated by a differently-named action
    # method.
    surface = {name for name in dir(AuditWriter) if not name.startswith("_")}
    assert surface == {"write", "has_written"}
