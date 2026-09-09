"""Fault-behavior property test (task 13.3).

Feature: sensor-simulator-service, Property 23 — stuck-value and dropout fault
behavior (Req 7.3, 7.4, 7.5).
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.signal.faults import FaultController, FaultKind, FaultWindow

_BASE = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)


@given(
    kind=st.sampled_from(list(FaultKind)),
    start_h=st.integers(min_value=0, max_value=40),
    dur_h=st.integers(min_value=1, max_value=20),
    probe_h=st.integers(min_value=0, max_value=71),
)
@settings(max_examples=100)
def test_property_23_fault_window_behavior(
    kind: FaultKind, start_h: int, dur_h: int, probe_h: int
) -> None:
    """Feature: sensor-simulator-service, Property 23."""
    start = _BASE + dt.timedelta(hours=start_h)
    end = start + dt.timedelta(hours=dur_h)
    ctrl = FaultController(windows=[FaultWindow(kind, start, end)])
    probe = _BASE + dt.timedelta(hours=probe_h)
    decision = ctrl.decide("CB0001", probe)

    inside = start <= probe < end
    if not inside:
        # outside the window: always resume normal emission
        assert decision.emit is True and decision.hold is False
    elif kind is FaultKind.DROPOUT:
        assert decision.emit is False  # omitted
    else:  # stuck value
        assert decision.emit is True and decision.hold is True
