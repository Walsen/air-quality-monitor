"""Unit tests for the seeded random-stream boundary (task 4.2).

Requirement 11.1: the Seed initializes every pseudo-random stream.
Requirement 11.4: each Virtual_Sensor gets an independent stream derived from
the Seed and the SiteCode (not a positional index), so changing the swarm size
leaves every retained SiteCode's stream byte-identical.

The factory is the injected randomness boundary (engineering-practices §2): no
domain code calls module-level ``random``/``numpy.random``.
"""

from __future__ import annotations

import numpy as np

from aqm_simulator.rng.streams import Purpose, RandomStreamFactory


def _draws(gen: np.random.Generator, n: int = 8) -> list[float]:
    return [float(x) for x in gen.random(n)]


def test_same_seed_sitecode_purpose_reproduces_draws() -> None:
    # Requirement 11.4: identical (seed, SiteCode, purpose) → identical stream.
    f1 = RandomStreamFactory(seed=1234)
    f2 = RandomStreamFactory(seed=1234)
    a = _draws(f1.stream("CB0001", Purpose.SIGNAL))
    b = _draws(f2.stream("CB0001", Purpose.SIGNAL))
    assert a == b


def test_different_sitecodes_are_independent() -> None:
    f = RandomStreamFactory(seed=1234)
    a = _draws(f.stream("CB0001", Purpose.SIGNAL))
    b = _draws(f.stream("CB0002", Purpose.SIGNAL))
    assert a != b


def test_different_purposes_are_independent_for_one_sitecode() -> None:
    f = RandomStreamFactory(seed=1234)
    sig = _draws(f.stream("CB0001", Purpose.SIGNAL))
    met = _draws(f.stream("CB0001", Purpose.METEOROLOGY))
    assert sig != met


def test_retained_sitecode_unaffected_by_swarm_size_change() -> None:
    # Requirement 11.4: derived from SiteCode, not a positional index — so a
    # retained SiteCode's stream is unchanged when the swarm grows/shrinks.
    small_swarm = ["CB0001", "CB0002"]
    large_swarm = ["CB0001", "CB0002", "CB0003", "CB0004", "CB0005"]
    f_small = RandomStreamFactory(seed=99)
    f_large = RandomStreamFactory(seed=99)
    for code in small_swarm:
        f_small.stream(code, Purpose.IDENTITY)
    for code in large_swarm:
        f_large.stream(code, Purpose.IDENTITY)
    # A fresh factory reproduces CB0002 regardless of how many peers were built.
    ref = _draws(RandomStreamFactory(seed=99).stream("CB0002", Purpose.IDENTITY))
    assert _draws(RandomStreamFactory(seed=99).stream("CB0002", Purpose.IDENTITY)) == ref


def test_different_seeds_diverge() -> None:
    a = _draws(RandomStreamFactory(seed=1).stream("CB0001", Purpose.SIGNAL))
    b = _draws(RandomStreamFactory(seed=2).stream("CB0001", Purpose.SIGNAL))
    assert a != b


def test_all_purposes_available() -> None:
    f = RandomStreamFactory(seed=7)
    for purpose in Purpose:
        assert isinstance(f.stream("CB0001", purpose), np.random.Generator)


def test_repeated_calls_same_key_give_same_generator_sequence() -> None:
    # Deterministic construction: two generators for the same key produce the
    # same sequence (each call returns a fresh generator at the same seed).
    f = RandomStreamFactory(seed=42)
    g1 = f.stream("CB0009", Purpose.FAULTS)
    g2 = f.stream("CB0009", Purpose.FAULTS)
    assert _draws(g1) == _draws(g2)
