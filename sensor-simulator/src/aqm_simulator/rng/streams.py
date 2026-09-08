"""The seeded random-stream boundary.

Every pseudo-random draw in the simulator comes from a generator this factory
produces, so domain code never touches module-level ``random`` or
``numpy.random`` (engineering-practices §2). Each generator is independent and
derived from ``(seed, SiteCode, purpose)`` — never a positional index — so:

- the same key reproduces the same sequence across processes (Requirement 11.4);
- distinct SiteCodes and distinct purposes get independent streams;
- changing the swarm size leaves every retained SiteCode's stream unchanged,
  because the derivation depends on the SiteCode string, not its position.

Cross-process reproducibility rules out Python's built-in ``hash`` (salted per
process); a BLAKE2b digest of the key string is folded into a NumPy
``SeedSequence`` instead, which is stable and well-distributed.
"""

from __future__ import annotations

import enum
import hashlib

import numpy as np


class Purpose(enum.Enum):
    """The independent random-stream purposes a Virtual_Sensor draws from."""

    IDENTITY = "identity"
    SIGNAL = "signal"
    METEOROLOGY = "meteorology"
    ARTIFACTS = "artifacts"
    FAULTS = "faults"


def _entropy(seed: int, site_code: str, purpose: Purpose) -> int:
    """Derive stable 128-bit entropy for one (seed, SiteCode, purpose) key."""
    key = f"{seed}|{site_code}|{purpose.value}".encode()
    digest = hashlib.blake2b(key, digest_size=16).digest()
    return int.from_bytes(digest, "big")


class RandomStreamFactory:
    """Mint independent NumPy generators keyed by (SiteCode, purpose).

    Constructed once from the run Seed (Requirement 11.1). ``stream`` returns a
    fresh, deterministic generator for each key; two calls with the same key
    yield generators producing identical sequences.
    """

    def __init__(self, seed: int) -> None:
        if not (0 <= seed <= 4_294_967_295):
            raise ValueError(
                f"seed must be an integer in 0..4294967295, got {seed!r}"
            )
        self._seed = seed

    def stream(self, site_code: str, purpose: Purpose) -> np.random.Generator:
        """Return an independent generator for this SiteCode and purpose."""
        seed_seq = np.random.SeedSequence(_entropy(self._seed, site_code, purpose))
        return np.random.default_rng(seed_seq)
