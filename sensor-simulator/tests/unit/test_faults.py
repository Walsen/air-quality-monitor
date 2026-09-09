"""Unit tests for stuck-value and dropout fault windows (task 13.2).

Requirement 7.3: during a dropout window, omit every record (all 4 Species) for
intervals starting in the window, with no placeholder; housekeeping continues.
Requirement 7.4: during a stuck-value window, emit the ScaledValue the Species
held in the last interval completed before the window started.
Requirement 7.5: after a window ends, resume Signal_Engine values.
Requirement 6.8: a suppressed concentration suppresses its index record.
"""

from __future__ import annotations

import datetime as dt

from aqm_simulator.signal.faults import FaultController, FaultKind, FaultWindow


def _ts(hour: int) -> dt.datetime:
    return dt.datetime(2026, 7, 1, hour, tzinfo=dt.UTC)


def test_no_fault_emits_normally() -> None:
    ctrl = FaultController(windows=[])
    decision = ctrl.decide("CB0001", _ts(5))
    assert decision.emit is True
    assert decision.hold is False


def test_dropout_window_suppresses_emit() -> None:
    ctrl = FaultController(windows=[FaultWindow(FaultKind.DROPOUT, _ts(3), _ts(6))])
    assert ctrl.decide("CB0001", _ts(4)).emit is False  # inside window
    assert ctrl.decide("CB0001", _ts(2)).emit is True   # before
    assert ctrl.decide("CB0001", _ts(6)).emit is True   # end-exclusive -> resume


def test_stuck_value_window_holds() -> None:
    ctrl = FaultController(windows=[FaultWindow(FaultKind.STUCK_VALUE, _ts(3), _ts(6))])
    d = ctrl.decide("CB0001", _ts(4))
    assert d.emit is True
    assert d.hold is True  # repeat the last pre-window value


def test_active_faults_reported() -> None:
    ctrl = FaultController(
        windows=[
            FaultWindow(FaultKind.DROPOUT, _ts(3), _ts(6)),
            FaultWindow(FaultKind.STUCK_VALUE, _ts(10), _ts(12)),
        ]
    )
    assert ctrl.active_faults(_ts(4)) == ["dropout"]
    assert ctrl.active_faults(_ts(11)) == ["stuck value"]
    assert ctrl.active_faults(_ts(8)) == []


def test_windows_only_apply_to_targeted_sitecode() -> None:
    ctrl = FaultController(
        windows=[FaultWindow(FaultKind.DROPOUT, _ts(3), _ts(6), site_code="CB0001")]
    )
    assert ctrl.decide("CB0001", _ts(4)).emit is False
    assert ctrl.decide("CB0002", _ts(4)).emit is True  # untargeted sensor unaffected


def test_resume_after_window() -> None:
    ctrl = FaultController(windows=[FaultWindow(FaultKind.STUCK_VALUE, _ts(3), _ts(6))])
    after = ctrl.decide("CB0001", _ts(7))
    assert after.emit is True and after.hold is False
