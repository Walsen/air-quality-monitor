"""Unit tests for the Symptom_Log (Requirement 31).

The governing decision here is Req 31.6: the Symptom_Note contributes to NOTHING. It is not read
by any computation, is not returned in the air-quality response body, and is not visible to the
Requirement 32 association. Prose is simultaneously a clinical narrative and an injection
vector,
and keeping it out of every derivation is what lets it exist at all.

That is enforced structurally rather than by discipline. The association does not consume
``SymptomEntry`` — it consumes ``SeverityObservation``, a projection with exactly a date and a
severity and no field a note or a marker could occupy. So "the note reaches no computation" is a
statement about a type, which a test can check, rather than a rule every future call site has to
remember.

- 31.2: the exact field set.
- 31.3: severity is 1-5 inclusive.
- 31.4: markers come from a closed configured set.
- 31.5: one optional bounded note and NO other free-text field.
- 31.6: the note reaches no computation.
- 31.7: one entry per user per date; a second write REPLACES.
- 31.8: retention applied at query time against the injected Clock.
- 31.9: erasure DELETES rather than de-identifies, and reports the count.
- 31.10: no clinical value reaches a log.
- 31.11: a future-dated entry is rejected.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from aqm_ingestion.adapters.memory.adapters import InMemorySymptomLogStore
from aqm_ingestion.domain.symptoms import (
    DEFAULT_NOTE_MAX_LENGTH,
    DEFAULT_SYMPTOM_RETENTION_DAYS,
    MARKER_ORDER,
    MAX_SEVERITY,
    MIN_SEVERITY,
    SeverityObservation,
    SymptomEntry,
    SymptomLogLimits,
    SymptomMarker,
    build_symptom_entry,
    severity_series,
)
from aqm_ingestion.ports.clock import FixedClock

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_TODAY = _NOW.date()


def _fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "user_id": "user-123",
        "entry_date": _TODAY,
        "severity": 3,
        "markers": ["wheeze", "cough"],
        "reliever_used": True,
    }
    return base | overrides


def _entry(**overrides: object) -> SymptomEntry:
    return build_symptom_entry(_fields(**overrides), now=_NOW)


def _store(**kwargs: object) -> InMemorySymptomLogStore:
    return InMemorySymptomLogStore(clock=FixedClock(_NOW), **kwargs)  # type: ignore[arg-type]


# --- Req 31.2 / 31.5: the exact shape -----------------------------------

def test_the_field_set_is_exactly_the_documented_one() -> None:
    assert set(SymptomEntry.model_fields) == {
        "user_id",
        "entry_date",
        "severity",
        "markers",
        "reliever_used",
        "note",
        "recorded_at",
    }


@pytest.mark.parametrize(
    "forbidden",
    ["diagnosis", "medication", "narrative", "description", "symptoms", "free_text", "cause"],
)
def test_there_is_no_second_free_text_field(forbidden: str) -> None:
    # Req 31.5: "SHALL NOT accept any other free-text field". Structural, like Req 30.3.
    assert forbidden not in SymptomEntry.model_fields


def test_an_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_symptom_entry(_fields(diagnosis="asthma exacerbation"), now=_NOW)


# --- Req 31.3: the severity range ---------------------------------------

def test_the_documented_severity_range() -> None:
    assert (MIN_SEVERITY, MAX_SEVERITY) == (1, 5)


@pytest.mark.parametrize("severity", [1, 2, 3, 4, 5])
def test_every_severity_in_range_is_accepted(severity: int) -> None:
    assert _entry(severity=severity).severity == severity


@pytest.mark.parametrize("severity", [0, 6, -1, 100])
def test_a_severity_outside_the_range_is_rejected_naming_the_range(severity: int) -> None:
    with pytest.raises(ValidationError) as caught:
        _entry(severity=severity)
    rendered = str(caught.value)
    assert "severity" in rendered
    assert "1" in rendered and "5" in rendered


def test_a_boolean_is_not_accepted_as_a_severity() -> None:
    # bool is a subclass of int in Python, so True would silently become severity 1 — a value
    # the user never reported.
    with pytest.raises(ValidationError):
        _entry(severity=True)


# --- Req 31.4: the closed marker set ------------------------------------

def test_the_default_marker_set_is_the_documented_one() -> None:
    assert {marker.value for marker in SymptomMarker} == {
        "cough",
        "wheeze",
        "breathlessness",
        "chest_tightness",
        "nasal_congestion",
        "sleep_disturbance",
    }


def test_an_unrecognized_marker_is_rejected_naming_the_recognized_set() -> None:
    with pytest.raises(ValidationError) as caught:
        _entry(markers=["fever"])
    rendered = str(caught.value)
    assert "cough" in rendered, "the rejection must name the recognized set (Req 31.4)"


def test_markers_are_ordered_deterministically() -> None:
    forward = _entry(markers=["wheeze", "cough"]).markers
    backward = _entry(markers=["cough", "wheeze"]).markers
    assert forward == backward == (SymptomMarker.COUGH, SymptomMarker.WHEEZE)


def test_the_marker_order_covers_every_marker() -> None:
    assert set(MARKER_ORDER) == set(SymptomMarker)
    assert len(MARKER_ORDER) == len(SymptomMarker)


def test_a_repeated_marker_collapses() -> None:
    assert _entry(markers=["cough", "cough"]).markers == (SymptomMarker.COUGH,)


def test_an_entry_with_no_markers_is_accepted() -> None:
    # A day with a severity but no specific marker is a real report — "I felt rough" — so an
    # empty set must not be an error, unlike a routine with no days.
    assert _entry(markers=[]).markers == ()


def test_the_marker_set_is_configurable() -> None:
    limits = SymptomLogLimits(markers=frozenset({SymptomMarker.COUGH}))
    build_symptom_entry(_fields(markers=["cough"]), now=_NOW, limits=limits)
    with pytest.raises(ValidationError):
        build_symptom_entry(_fields(markers=["wheeze"]), now=_NOW, limits=limits)


# --- Req 31.5: the bounded note -----------------------------------------

def test_the_default_note_bound() -> None:
    assert DEFAULT_NOTE_MAX_LENGTH == 280


def test_a_note_is_optional() -> None:
    assert _entry().note is None


def test_a_note_at_the_bound_is_accepted() -> None:
    assert _entry(note="x" * DEFAULT_NOTE_MAX_LENGTH).note is not None


def test_a_note_over_the_bound_is_rejected_naming_the_bound() -> None:
    with pytest.raises(ValidationError) as caught:
        _entry(note="x" * (DEFAULT_NOTE_MAX_LENGTH + 1))
    assert str(DEFAULT_NOTE_MAX_LENGTH) in str(caught.value)


def test_the_note_bound_is_configuration() -> None:
    limits = SymptomLogLimits(note_max_length=5)
    build_symptom_entry(_fields(note="12345"), now=_NOW, limits=limits)
    with pytest.raises(ValidationError):
        build_symptom_entry(_fields(note="123456"), now=_NOW, limits=limits)


# --- Req 31.6: the note reaches no computation --------------------------

def test_the_association_input_has_nowhere_to_put_a_note() -> None:
    # THE load-bearing test for Req 31.6. The association consumes SeverityObservation, not
    # SymptomEntry, so a note cannot reach a derivation even by mistake — there is no field for
    # it. A discipline-based reading of 31.6 would leave SymptomEntry as the input and rely on
    # every future call site remembering not to look.
    fields = set(SeverityObservation.__dataclass_fields__)
    assert fields == {"on", "severity"}
    for forbidden in ("note", "markers", "reliever_used", "user_id"):
        assert forbidden not in fields


def test_the_severity_series_carries_only_dates_and_severities() -> None:
    entries = [
        _entry(entry_date=dt.date(2026, 6, 29), severity=2, note="woke at three"),
        _entry(entry_date=dt.date(2026, 6, 30), severity=4, note="bad day"),
    ]
    series = severity_series(entries)
    assert series == (
        SeverityObservation(on=dt.date(2026, 6, 29), severity=2),
        SeverityObservation(on=dt.date(2026, 6, 30), severity=4),
    )
    assert "woke at three" not in repr(series)


def test_the_severity_series_is_ordered_by_date_ascending() -> None:
    # Req 32.11's ordering, established here because this is where the series is built.
    entries = [
        _entry(entry_date=dt.date(2026, 6, 30), severity=4),
        _entry(entry_date=dt.date(2026, 6, 28), severity=1),
        _entry(entry_date=dt.date(2026, 6, 29), severity=2),
    ]
    assert [o.on.day for o in severity_series(entries)] == [28, 29, 30]


# --- Req 31.11: no future dates -----------------------------------------

def test_a_future_dated_entry_is_rejected_naming_the_field() -> None:
    with pytest.raises(ValidationError) as caught:
        _entry(entry_date=_TODAY + dt.timedelta(days=1))
    assert "entry_date" in str(caught.value)


def test_todays_entry_is_accepted() -> None:
    # The boundary: "not in the future" includes today, and a user records the day they had.
    assert _entry(entry_date=_TODAY).entry_date == _TODAY


def test_a_past_entry_is_accepted() -> None:
    assert _entry(entry_date=_TODAY - dt.timedelta(days=30)).entry_date is not None


# --- Req 31.7: one entry per date, replaced not accumulated --------------

def test_a_second_write_for_a_date_replaces_the_first() -> None:
    store = _store()
    store.put(_entry(entry_date=_TODAY, severity=2))
    store.put(_entry(entry_date=_TODAY, severity=5))
    held = store.query_window("user-123", _TODAY, _TODAY)
    assert len(held) == 1, "two entries for one day would double-count it in the association"
    assert held[0].severity == 5


def test_a_replacing_write_is_idempotent() -> None:
    store = _store()
    entry = _entry(entry_date=_TODAY, severity=3)
    store.put(entry)
    store.put(entry)
    assert len(store.query_window("user-123", _TODAY, _TODAY)) == 1


def test_entries_for_different_dates_both_survive() -> None:
    store = _store()
    store.put(_entry(entry_date=_TODAY, severity=2))
    store.put(_entry(entry_date=_TODAY - dt.timedelta(days=1), severity=4))
    assert len(store.query_window("user-123", _TODAY - dt.timedelta(days=1), _TODAY)) == 2


def test_one_users_entry_never_reaches_another() -> None:
    store = _store()
    store.put(_entry(user_id="user-a", severity=5))
    assert store.query_window("user-b", _TODAY, _TODAY) == ()


# --- Req 31.8: retention at query time ----------------------------------

def test_the_default_retention_window() -> None:
    assert DEFAULT_SYMPTOM_RETENTION_DAYS == 365


def test_an_entry_older_than_retention_is_excluded_from_every_query() -> None:
    store = _store(retention_days=30)
    old = _TODAY - dt.timedelta(days=31)
    store.put(build_symptom_entry(_fields(entry_date=old), now=_NOW))
    assert store.query_window("user-123", old, _TODAY) == ()


def test_an_entry_inside_retention_is_returned() -> None:
    store = _store(retention_days=30)
    recent = _TODAY - dt.timedelta(days=29)
    store.put(build_symptom_entry(_fields(entry_date=recent), now=_NOW))
    assert len(store.query_window("user-123", recent, _TODAY)) == 1


def test_retention_moves_with_the_clock_rather_than_evicting_on_write() -> None:
    # Mirrors the readings store's reasoning: the exclusion moves with the injected clock, so
    # stored data ages out correctly without anything running on a timer.
    early = FixedClock(_NOW)
    store = InMemorySymptomLogStore(clock=early, retention_days=10)
    entry_date = _TODAY - dt.timedelta(days=5)
    store.put(build_symptom_entry(_fields(entry_date=entry_date), now=_NOW))
    assert len(store.query_window("user-123", entry_date, _TODAY)) == 1

    later = InMemorySymptomLogStore(
        clock=FixedClock(_NOW + dt.timedelta(days=20)), retention_days=10
    )
    later.put(build_symptom_entry(_fields(entry_date=entry_date), now=_NOW))
    assert later.query_window("user-123", entry_date, _TODAY) == ()


def test_an_inverted_window_is_an_error_rather_than_an_empty_result() -> None:
    # Same reasoning as the readings store: silently returning nothing hides a caller bug (§5).
    store = _store()
    with pytest.raises(ValueError):
        store.query_window("user-123", _TODAY, _TODAY - dt.timedelta(days=1))


# --- Req 31.9: erasure deletes ------------------------------------------

def test_erasure_removes_every_entry_and_reports_the_count() -> None:
    store = _store()
    for offset in range(3):
        store.put(_entry(entry_date=_TODAY - dt.timedelta(days=offset)))
    removed = store.forget_user("user-123")
    assert removed == 3
    assert store.query_window("user-123", _TODAY - dt.timedelta(days=3), _TODAY) == ()


def test_erasure_of_an_unknown_user_reports_zero_rather_than_failing() -> None:
    assert _store().forget_user("nobody") == 0


def test_erasure_leaves_another_users_entries_alone() -> None:
    store = _store()
    store.put(_entry(user_id="user-a"))
    store.put(_entry(user_id="user-b"))
    assert store.forget_user("user-a") == 1
    assert len(store.query_window("user-b", _TODAY, _TODAY)) == 1


def test_erasure_deletes_rather_than_de_identifying() -> None:
    # Req 31.9 draws the contrast with the Audit_Record explicitly: a Symptom_Entry carries real
    # clinical content, so nothing may survive erasure — not even a de-identified husk.
    store = _store()
    store.put(_entry(severity=5, note="worst night in months"))
    store.forget_user("user-123")
    assert "worst night in months" not in repr(store.__dict__)
    assert store.count_all() == 0
