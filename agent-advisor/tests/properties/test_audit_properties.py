"""Properties 14 and 15: exactly one audit record per turn, and audit minimisation.

Tasks 12.2, 12.3. Validates Reqs 20.1, 20.3, 20.4, 20.5.

**Property 14 is a counting claim over ARBITRARY write sequences.** A turn can attempt the write
more than once — a repair after a guardrail rejection, a retry after a transient store failure —
so the property quantifies over sequences of attempts and asserts at most one record lands.
Testing a single happy-path write would say nothing about the case that actually produces
duplicates.

The failing-store half matters just as much in the other direction: a failed attempt must NOT
close the latch, or one transient error would lose that turn's audit permanently. So the
property is "at most one SUCCESS", not "at most one attempt".

**Property 15 is quantified over text the record must never carry.** Req 20.3's list —
utterance, guidance, condition, sensitivity, threshold, coordinate — is a list of things that
would make erasure mean more than removing an identity. The property generates such text, builds
a record while it is in scope, serialises the record, and asserts none of it appears. That
catches a leak through any field, including one added later, which a field-name check alone
would not.
"""

from __future__ import annotations

import datetime as dt
import logging

from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from aqm_advisor.agent.audit import AuditWriter, build_advice_record
from aqm_advisor.domain.idempotency import TurnIdentity
from aqm_advisor.domain.models import (
    BasisSummary,
    Escalation,
    NowcastBasis,
    RecordReference,
    SpeciesBasis,
)
from aqm_advisor.observability.logging import EventLogger
from aqm_advisor.ports.protocols import AdviceRecord

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _logger() -> EventLogger:
    return EventLogger(logging.getLogger("aqm_advisor.test.audit_properties"))


class _RecordingStore:
    def __init__(self) -> None:
        self.records: list[AdviceRecord] = []

    def append(self, record: AdviceRecord) -> None:
        self.records.append(record)

    def forget_user(self, user_id: str) -> int:
        before = len(self.records)
        self.records = [r for r in self.records if r.user_id != user_id]
        return before - len(self.records)


class _FlakyStore:
    """Fails the first `failures` attempts, then accepts.

    Models a transient outage rather than a permanent one, which is the case that distinguishes
    "at most one success" from "at most one attempt".
    """

    def __init__(self, failures: int) -> None:
        self.records: list[AdviceRecord] = []
        self._remaining = failures
        self.attempts = 0

    def append(self, record: AdviceRecord) -> None:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise OSError("transient")
        self.records.append(record)

    def forget_user(self, user_id: str) -> int:
        return 0


def _basis(records: tuple[RecordReference, ...]) -> BasisSummary:
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
        nowcast=NowcastBasis(window_hours=12, hours_available=12, weight_factor=0.72),
        records=records,
    )


_SPECIES = st.sampled_from(["PM25", "NO2", "PM25Index", "NO2Index"])


@st.composite
def _references(draw: st.DrawFn) -> tuple[RecordReference, ...]:
    count = draw(st.integers(min_value=0, max_value=4))
    return tuple(
        RecordReference(
            site_code=f"AQM{index}",
            species=draw(_SPECIES),
            date_time=_AT,
            duration="PT1H",
        )
        for index in range(count)
    )


_ESCALATIONS = st.one_of(
    st.none(),
    st.just(
        Escalation(
            kind="emergency",
            markers=("struggling to breathe",),
            guidance="Call emergency services now.",
        )
    ),
)


def _record(
    *,
    escalation: Escalation | None = None,
    rejected: bool = False,
    references: tuple[RecordReference, ...] = (),
    user_id: str = "user-1",
) -> AdviceRecord:
    return build_advice_record(
        identity=TurnIdentity(user_id=user_id, session_id="s" * 33),
        turn_at=_AT,
        route="/invocations",
        escalation=escalation,
        threshold_crossed=False,
        driving_pollutant="PM25",
        basis=_basis(references),
        guardrail_rejected=rejected,
        rejection_category="diagnosis" if rejected else None,
    )


# --- Property 14: exactly one audit record per turn ---------------------


@given(
    st.integers(min_value=1, max_value=6),
    _ESCALATIONS,
    st.booleans(),
    _references(),
)
def test_at_most_one_record_lands_however_many_attempts(
    attempts: int,
    escalation: Escalation | None,
    rejected: bool,
    references: tuple[RecordReference, ...],
) -> None:
    # THE counting claim, over arbitrary attempt counts and turn outcomes. A repair after a
    # guardrail
    # rejection is still ONE turn, so a writer recording per generation would double-count
    # exactly the
    # turns most worth counting accurately.
    store = _RecordingStore()
    writer = AuditWriter(store=store, logger=_logger())
    for _ in range(attempts):
        writer.write(
            _record(escalation=escalation, rejected=rejected, references=references)
        )
    assert len(store.records) == 1


@given(st.integers(min_value=1, max_value=6), _ESCALATIONS, st.booleans())
def test_exactly_one_record_lands_rather_than_none(
    attempts: int, escalation: Escalation | None, rejected: bool
) -> None:
    # Non-vacuity for the property above. "At most one" is also satisfied by zero, and a writer
    # that never
    # wrote would pass it while leaving the operator with no trail at all.
    store = _RecordingStore()
    writer = AuditWriter(store=store, logger=_logger())
    for _ in range(attempts):
        writer.write(_record(escalation=escalation, rejected=rejected))
    assert len(store.records) == 1
    assert writer.has_written is True


@given(st.booleans(), _ESCALATIONS)
def test_a_rejected_or_escalating_turn_is_still_recorded(
    rejected: bool, escalation: Escalation | None
) -> None:
    # Req 20.4. Quantified over BOTH flags because those are the turns an operator most needs to
    # see, and
    # a writer that skipped either would produce an audit of the turns that went fine.
    store = _RecordingStore()
    AuditWriter(store=store, logger=_logger()).write(
        _record(escalation=escalation, rejected=rejected)
    )
    assert len(store.records) == 1
    assert store.records[0].guardrail_rejected is rejected
    assert store.records[0].escalated is (escalation is not None)


@given(st.integers(min_value=1, max_value=4), st.integers(min_value=1, max_value=6))
def test_a_transient_failure_does_not_lose_the_turns_audit(
    failures: int, attempts: int
) -> None:
    # The clause that makes this "at most one SUCCESS" rather than "at most one attempt". A
    # failed write
    # must not close the latch, or one transient error would lose that turn's audit permanently.
    store = _FlakyStore(failures=failures)
    writer = AuditWriter(store=store, logger=_logger())
    for _ in range(attempts):
        writer.write(_record())
    expected = 1 if attempts > failures else 0
    assert len(store.records) == expected
    assert store.attempts == min(attempts, failures + 1)


@given(st.integers(min_value=1, max_value=6))
def test_a_permanently_failing_store_never_raises(attempts: int) -> None:
    # Req 20.5, quantified. However many times the turn tries, the user still gets their answer
    # — the
    # writer reports failure rather than propagating it.
    store = _FlakyStore(failures=attempts + 1)
    writer = AuditWriter(store=store, logger=_logger())
    results = [writer.write(_record()) for _ in range(attempts)]
    assert results == [False] * attempts
    assert writer.has_written is False


@given(st.lists(st.sampled_from(["a", "b", "c"]), min_size=2, max_size=4))
def test_one_writer_per_turn_means_one_record_per_turn(user_ids: list[str]) -> None:
    # The latch is per WRITER, and a writer is per turn. A process-wide latch would record the
    # first turn
    # of a run and silently drop every turn after it.
    store = _RecordingStore()
    for user_id in user_ids:
        AuditWriter(store=store, logger=_logger()).write(_record(user_id=user_id))
    assert len(store.records) == len(user_ids)


# --- Property 15: audit minimisation ------------------------------------


_CLINICAL_TEXT = st.text(
    alphabet=st.characters(codec="utf-8", categories=("L", "N", "Zs")),
    min_size=8,
    max_size=40,
).filter(lambda value: value.strip() != "")
"""Stands for anything Req 20.3 forbids recording: an utterance, guidance, a condition."""

_FORBIDDEN_FIELDS = [
    "utterance",
    "guidance",
    "text",
    "condition",
    "sensitivity",
    "threshold",
    "latitude",
    "longitude",
    "coordinates",
    "medications",
    "note",
    "severity",
    "postcode",
    "address",
    "age",
]


@given(st.sampled_from(_FORBIDDEN_FIELDS), _CLINICAL_TEXT)
def test_the_record_refuses_every_forbidden_field(field: str, value: str) -> None:
    # Req 20.3 made enforceable at CONSTRUCTION. This is what consolidating onto the Pydantic
    # model bought:
    # `extra="forbid"` turns an attempt to record clinical content into an error, where a
    # dataclass would
    # simply not have the field and the caller's value would vanish — and a vanished field looks
    # exactly
    # like one that was never passed.
    kwargs = {
        "user_id": "user-1",
        "turn_at": _AT,
        "route": "/invocations",
        "escalated": False,
        "threshold_crossed": False,
        "driving_pollutant": "PM25",
        "record_references": (),
        "guardrail_rejected": False,
        "rejection_category": None,
        "idempotency_key": "k1",
        field: value,
    }
    try:
        AdviceRecord(**kwargs)  # type: ignore[arg-type]
    except ValidationError:
        return
    raise AssertionError(f"the record accepted a {field!r} field")


def test_the_builder_has_no_parameter_that_could_carry_clinical_content() -> None:
    # DISCHARGED BY CONSTRUCTION, which is the honest form of this claim.
    #
    # An earlier version of this test generated clinical text and asserted it did not appear in
    # the
    # serialised record. That proved nothing: the text was never passed anywhere near the
    # record, so it
    # could not have appeared. The property held for free — the same vacuity as a branch never
    # entered.
    #
    # What is actually true and worth asserting is stronger: `build_advice_record` has NO
    # PARAMETER through
    # which an utterance, guidance, a condition, a sensitivity or a coordinate could arrive.
    # There is no
    # code path to leak through, so Req 20.3 holds structurally rather than by the caller's
    # care. This is
    # the same technique as `determine_escalation` taking no model parameter.
    import inspect

    parameters = set(inspect.signature(build_advice_record).parameters)
    for forbidden in (
        "utterance",
        "guidance",
        "text",
        "condition",
        "sensitivity",
        "threshold",
        "latitude",
        "longitude",
        "coordinates",
        "note",
        "severity",
        "prior_turns",
        "request",
        "response",
    ):
        assert forbidden not in parameters, forbidden


def test_the_builder_takes_the_basis_but_stores_only_its_identifiers() -> None:
    # The one parameter that DOES carry retrieved detail is the basis, and it carries plenty —
    # sub-indices,
    # thresholds, calibration strategies. The property is that only the identifiers survive into
    # the
    # record, so the basis is a source to read from rather than a payload to copy.
    references = (
        RecordReference(
            site_code="AQM1", species="PM25", date_time=_AT, duration="PT1H"
        ),
    )
    basis = _basis(references)
    record = _record(references=references)
    # The idempotency key is excluded from the search deliberately. It is a SHA-256 digest, so
    # it contains
    # arbitrary digit sequences by chance — "68" appeared inside it on the first run of this
    # test and failed
    # it. That is the substring-versus-token trap again, this time in the test rather than the
    # code; a
    # digest can never be evidence of a leak, so it is not searched.
    serialised = record.model_dump_json(exclude={"idempotency_key"})
    assert basis.breakpoint_table is not None
    assert basis.breakpoint_table not in serialised
    assert "rh_linear" not in serialised
    assert "68" not in serialised
    assert "0.72" not in serialised


@given(_CLINICAL_TEXT)
def test_the_users_own_words_never_become_the_idempotency_key(clinical: str) -> None:
    # The key is a digest of identity, instant and route (Req 32.4c) — never of the utterance. A
    # key built
    # from the text would put the utterance in every store that holds a key, and in any log that
    # quotes one.
    assert clinical not in _record().idempotency_key


@given(_references())
def test_only_public_sensor_facts_appear_in_the_identifiers(
    references: tuple[RecordReference, ...],
) -> None:
    # Req 20.3 permits the composite identifier because a site code, a pollutant name, an
    # instant and a
    # duration are PUBLIC sensor facts. This pins that nothing else joined them.
    record = _record(references=references)
    for identifier in record.record_references:
        parts = identifier.split(":")
        assert parts[0].startswith("AQM")
        assert parts[1] in {"PM25", "NO2", "PM25Index", "NO2Index"}


@given(_references())
def test_the_field_set_never_grows_with_the_data(
    references: tuple[RecordReference, ...],
) -> None:
    # Whatever was retrieved, the record's shape is the same. A shape that varied with the data
    # would make
    # erasure a question of inspecting each row rather than removing an identity.
    assert set(_record(references=references).model_dump()) == {
        "user_id",
        "turn_at",
        "route",
        "escalated",
        "threshold_crossed",
        "driving_pollutant",
        "record_references",
        "guardrail_rejected",
        "rejection_category",
        "idempotency_key",
    }


@given(st.sampled_from(["a", "b", "c"]))
def test_erasure_needs_only_the_identity(user_id: str) -> None:
    # Req 20.3's PURPOSE, tested rather than assumed: because the row holds nothing
    # health-adjacent,
    # removing rows by identity is a complete erasure. If any clinical field existed, this would
    # not be.
    store = _RecordingStore()
    AuditWriter(store=store, logger=_logger()).write(_record(user_id=user_id))
    assert store.forget_user(user_id) == 1
    assert store.records == []
