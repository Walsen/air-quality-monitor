"""Tests for the degradation paths and Req 21.8's drift warning (task 11.1).

Validates Reqs 21.1, 21.2, 21.3, 21.7, 21.8.

**The drift warning is the point of this task.** `emergency_guidance_drifted` already existed
and was tested, but nothing ever CALLED it — so A8a's narrow exception was bought with a guard
that was never armed. A detector nobody invokes is worse than no detector, because the design
cites it as the reason the exception is safe.

Three properties of that warning are load-bearing and each has a test:

* **Once per run, not once per turn.** A per-turn warning on a busy process is a flood, and an
  operator who learns to filter it out has the same protection as one with no detector at all.
* **The first SUCCESSFUL retrieval of the run decides.** Req 21.8 says exactly that, and it
  matters because a failed retrieval must not consume the comparison — the latch would then
  close having compared nothing.
* **The field name only, never either text.** These strings are not health data, but a warning
  quoting both puts clinical wording in a log for no diagnostic gain.

**Every degraded response still carries the envelope and any escalation** (Reqs 21.3, 21.9). A
degraded turn is exactly when a user most needs the emergency direction, so dropping it under
failure inverts the priority.
"""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from aqm_advisor.domain.degradation import (
    DriftWatcher,
    degraded_response,
    missing_data_note,
)
from aqm_advisor.domain.envelope import resolve_envelope
from aqm_advisor.domain.grounding import numerals
from aqm_advisor.domain.models import Escalation, GuardrailEnvelope
from aqm_advisor.observability.logging import EventLogger
from aqm_advisor.ports.protocols import ServingFailureKind

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_CONFIGURED = "If you are severely breathless, seek emergency care now."
_SERVED = "If you cannot speak in full sentences, call emergency services."


def _envelope() -> GuardrailEnvelope:
    return resolve_envelope(
        served=GuardrailEnvelope(
            emergency_guidance=_SERVED,
            advisory_scope="exposure-reduction",
            disclaimer="Not medical advice.",
        ),
        cached=None,
        configured_emergency_guidance=_CONFIGURED,
    ).envelope


def _fallback_envelope() -> GuardrailEnvelope:
    return resolve_envelope(
        served=None, cached=None, configured_emergency_guidance=_CONFIGURED
    ).envelope


@pytest.fixture
def captured(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.WARNING)
    return caplog


def _logger() -> EventLogger:
    return EventLogger(logging.getLogger("aqm_advisor.test.degradation"))


# --- Req 21.1: a serving failure states no condition value --------------


def test_a_serving_failure_states_no_condition_value() -> None:
    # Req 21.1's hardest clause. The safest way to state no value is to have nowhere to put one,
    # so
    # `guidance` and `basis` are both absent and the numeral check proves nothing leaked in.
    response = degraded_response(
        envelope=_envelope(), answered_at=_AT, missing=("current conditions",)
    )
    assert response.degraded is True
    assert response.basis is None
    assert numerals(response.guidance or "") == ()


def test_the_degraded_response_says_conditions_are_unavailable() -> None:
    response = degraded_response(
        envelope=_envelope(), answered_at=_AT, missing=("current conditions",)
    )
    assert "unavailable" in (response.guidance or "").casefold()


@pytest.mark.parametrize("kind", list(ServingFailureKind))
def test_every_serving_failure_kind_produces_a_degraded_response(
    kind: ServingFailureKind,
) -> None:
    # Quantified over the enum rather than a sample, so a new failure kind without a path is a
    # failing
    # test rather than an unhandled branch.
    response = degraded_response(
        envelope=_envelope(), answered_at=_AT, missing=(f"conditions ({kind.value})",)
    )
    assert response.degraded is True


# --- Req 21.3 / 21.9: the envelope and escalation always survive --------


def test_a_degraded_response_still_carries_the_envelope() -> None:
    # Req 21.3. A degraded turn is exactly when the user most needs the emergency direction, so
    # dropping
    # it under failure inverts the priority.
    response = degraded_response(
        envelope=_envelope(), answered_at=_AT, missing=("current conditions",)
    )
    assert response.envelope.emergency_guidance == _SERVED


def test_a_degraded_response_still_carries_the_escalation() -> None:
    escalation = Escalation(
        kind="emergency", markers=("struggling to breathe",), guidance=_SERVED
    )
    response = degraded_response(
        envelope=_envelope(),
        answered_at=_AT,
        missing=("current conditions",),
        escalation=escalation,
    )
    assert response.escalation is escalation


def test_escalation_still_precedes_guidance_in_a_degraded_response() -> None:
    # Req 10.2's field order does not relax under failure.
    response = degraded_response(
        envelope=_envelope(),
        answered_at=_AT,
        missing=("current conditions",),
        escalation=Escalation(kind="emergency", markers=("blue lips",), guidance=_SERVED),
    )
    keys = list(response.model_dump().keys())
    assert keys.index("escalation") < keys.index("guidance")


def test_a_fallback_envelope_omits_scope_and_disclaimer() -> None:
    # Req 21.9: omit, never invent. A locally-substituted disclaimer would be this service
    # making a legal
    # claim on Service 2's behalf.
    response = degraded_response(
        envelope=_fallback_envelope(), answered_at=_AT, missing=("current conditions",)
    )
    assert response.envelope.advisory_scope is None
    assert response.envelope.disclaimer is None
    assert response.envelope.emergency_guidance == _CONFIGURED


# --- Req 21.7: partial retrieval advises on what it has -----------------


def test_the_missing_note_names_what_is_missing() -> None:
    # Req 21.7. "Some data was unavailable" leaves the user unable to tell whether the part they
    # asked
    # about is the part that is missing.
    note = missing_data_note(("the pollen count", "tomorrow's outlook"))
    assert "pollen" in note
    assert "outlook" in note


def test_the_missing_note_carries_no_numeral() -> None:
    # Same reasoning as Req 7.3's unavailable text: a number in the sentence that reports absent
    # data
    # would be an ungrounded claim, since by definition it was not retrieved.
    assert numerals(missing_data_note(("PM2.5", "the 24-hour mean"))) == ()


def test_an_empty_missing_list_is_refused() -> None:
    # A note reporting nothing missing would be a sentence with no content, and the caller has a
    # bug.
    with pytest.raises(ValueError, match="missing"):
        missing_data_note(())


def test_a_partial_turn_is_degraded_but_still_advises() -> None:
    # Req 21.7's substance: advise on what IS available rather than failing the whole turn. So
    # guidance
    # is present, and the note says what is absent.
    response = degraded_response(
        envelope=_envelope(),
        answered_at=_AT,
        missing=("tomorrow's outlook",),
        available_guidance="Conditions near you are moderate right now.",
    )
    assert response.degraded is True
    assert "moderate" in (response.guidance or "")
    assert "outlook" in (response.guidance or "")


# --- Req 21.8: the drift warning, once per run --------------------------


def test_the_drift_warning_fires_when_the_texts_differ(
    captured: pytest.LogCaptureFixture,
) -> None:
    # The guard A8a's exception was bought with. Until now it existed and was never called.
    watcher = DriftWatcher(configured=_CONFIGURED, logger=_logger())
    watcher.observe_served(_SERVED)
    assert len(captured.records) == 1


def test_the_drift_warning_names_the_field_and_neither_text(
    captured: pytest.LogCaptureFixture,
) -> None:
    # Req 21.8 is explicit. These strings are not health data, but a warning quoting both would
    # put
    # clinical wording into a log for no diagnostic gain — the field name is all an operator
    # needs.
    #
    # Asserted on the FORMATTED line rather than on `getMessage()`, because context arrives as
    # `extra`
    # and the formatter is what actually reaches stdout. Checking the bare message would have
    # passed
    # while the field never appeared in a real log at all.
    from aqm_advisor.observability.logging import _JsonFormatter

    watcher = DriftWatcher(configured=_CONFIGURED, logger=_logger())
    watcher.observe_served(_SERVED)
    line = _JsonFormatter().format(captured.records[0])
    assert "emergencyGuidance" in line
    assert _CONFIGURED not in line
    assert _SERVED not in line


def test_the_drift_warning_carries_the_field_as_structured_context(
    captured: pytest.LogCaptureFixture,
) -> None:
    # The field must be a separate KEY, not interpolated into the event name — that is the whole
    # reason
    # `EventLogger` takes an event plus context, since the formatter can only redact what
    # arrives as a key.
    watcher = DriftWatcher(configured=_CONFIGURED, logger=_logger())
    watcher.observe_served(_SERVED)
    assert getattr(captured.records[0], "field", None) == "emergencyGuidance"


def test_no_warning_when_the_texts_agree(captured: pytest.LogCaptureFixture) -> None:
    # Non-vacuity. A watcher that warned unconditionally would be an alert on a non-event, which
    # costs
    # exactly what having no detector costs.
    watcher = DriftWatcher(configured=_CONFIGURED, logger=_logger())
    watcher.observe_served(_CONFIGURED)
    assert captured.records == []


def test_whitespace_alone_is_not_drift(captured: pytest.LogCaptureFixture) -> None:
    watcher = DriftWatcher(configured=_CONFIGURED, logger=_logger())
    watcher.observe_served(f"  {_CONFIGURED}\n  ")
    assert captured.records == []


def test_only_one_warning_per_run(captured: pytest.LogCaptureFixture) -> None:
    # THE cardinality clause. A per-turn warning on a busy process is a flood, and an operator
    # who
    # filters it out has the same protection as one with no detector at all.
    watcher = DriftWatcher(configured=_CONFIGURED, logger=_logger())
    for _ in range(5):
        watcher.observe_served(_SERVED)
    assert len(captured.records) == 1


def test_the_first_successful_retrieval_decides(
    captured: pytest.LogCaptureFixture,
) -> None:
    # Req 21.8 says the FIRST successfully retrieved guidance of each run. A later differing
    # text must
    # not fire a second warning, because the comparison has already been made and reported.
    watcher = DriftWatcher(configured=_CONFIGURED, logger=_logger())
    watcher.observe_served(_CONFIGURED)
    watcher.observe_served(_SERVED)
    assert captured.records == []


def test_a_failed_retrieval_does_not_consume_the_comparison(
    captured: pytest.LogCaptureFixture,
) -> None:
    # The subtle one. A failure must not close the latch, or the run would record having
    # compared
    # something it never saw — and the drift would go unreported for the whole process lifetime.
    watcher = DriftWatcher(configured=_CONFIGURED, logger=_logger())
    watcher.observe_failure()
    watcher.observe_served(_SERVED)
    assert len(captured.records) == 1


def test_a_blank_served_text_is_not_a_comparison(
    captured: pytest.LogCaptureFixture,
) -> None:
    # An empty guidance is an unusable body, not a successful retrieval. Treating it as one
    # would both
    # report spurious drift and close the latch against the real comparison.
    watcher = DriftWatcher(configured=_CONFIGURED, logger=_logger())
    watcher.observe_served("   ")
    assert captured.records == []
    watcher.observe_served(_SERVED)
    assert len(captured.records) == 1


def test_the_watcher_reports_whether_it_has_compared() -> None:
    # Observable so the audit can record that the check ran, rather than the absence of a
    # warning having
    # to stand for both "no drift" and "never checked".
    watcher = DriftWatcher(configured=_CONFIGURED, logger=_logger())
    assert watcher.has_compared is False
    watcher.observe_served(_SERVED)
    assert watcher.has_compared is True
