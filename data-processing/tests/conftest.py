"""Shared test configuration.

Registers the hypothesis profiles. Requirement 28.11 requires every named
correctness property to run at least 100 examples, so the default profile used by
the documented test command is the ``ci`` one below; ``nightly`` raises the count
for stress runs, and ``dev`` lowers it for a fast inner loop only.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile("dev", max_examples=20)
settings.register_profile(
    "ci",
    max_examples=100,  # the floor Requirement 28.11 sets
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("nightly", max_examples=1000, deadline=None)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
