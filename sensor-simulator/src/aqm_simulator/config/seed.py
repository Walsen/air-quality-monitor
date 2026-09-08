"""Seed selection and disclosure.

When configuration supplies no Seed, one is selected within the permitted range
0..4,294,967,295 and disclosed in the diagnostic output before the first record,
so the run can be replayed (Requirement 11.7). A supplied seed is returned
unchanged (and left for the validation pass to range-check).

The entropy source is injected so selection is deterministic under test; the
default draws from the OS CSPRNG. This is the one place a seed may be *chosen*
rather than supplied — domain code always receives an explicit seed
(engineering-practices §2).
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

_SEED_MODULUS = 4_294_967_296  # 2**32; seeds are 0..4294967295 inclusive


def _os_entropy() -> int:
    return secrets.randbits(32)


def select_seed(
    configured_seed: int | None,
    entropy: Callable[[], int] = _os_entropy,
) -> tuple[int, bool]:
    """Return ``(seed, was_selected)``.

    ``was_selected`` is True when no seed was configured and one was drawn, so
    the caller knows to disclose it. A configured seed is returned as-is (range
    validation is the Config_Loader's job).
    """
    if configured_seed is not None:
        return configured_seed, False
    return entropy() % _SEED_MODULUS, True
