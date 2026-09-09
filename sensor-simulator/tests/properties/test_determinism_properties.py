"""Determinism property tests (tasks 16.10-16.11).

Feature: sensor-simulator-service
- Property 30: seeded determinism (Req 11.1) — same seed, byte-identical output.
- Property 31: seed sensitivity (Req 11.2) — different seed, output diverges.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.pipeline.determinism import build_pipeline, run_and_serialize

_START = dt.datetime(2026, 7, 1, tzinfo=dt.UTC)
_END = dt.datetime(2026, 7, 1, 4, tzinfo=dt.UTC)  # 4 hourly intervals
_REF = dt.datetime(2026, 7, 2, tzinfo=dt.UTC)


@given(seed=st.integers(min_value=0, max_value=100_000))
@settings(max_examples=100)
def test_property_30_seeded_determinism(seed: int) -> None:
    """Feature: sensor-simulator-service, Property 30."""
    # two independent instances from FRESH factories (mimic separate processes)
    a = run_and_serialize(build_pipeline(seed), _START, _END, _REF)
    b = run_and_serialize(build_pipeline(seed), _START, _END, _REF)
    assert a == b  # byte-identical serialized output
    assert a  # non-empty


@given(
    seed=st.integers(min_value=0, max_value=50_000),
    delta=st.integers(min_value=1, max_value=50_000),
)
@settings(max_examples=100)
def test_property_31_seed_sensitivity(seed: int, delta: int) -> None:
    """Feature: sensor-simulator-service, Property 31."""
    a = run_and_serialize(build_pipeline(seed), _START, _END, _REF)
    b = run_and_serialize(build_pipeline(seed + delta), _START, _END, _REF)
    # same structure (same count) but at least one record differs somewhere
    assert len(a) == len(b)
    assert a != b
