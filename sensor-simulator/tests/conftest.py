"""Pytest session configuration.

Registers Hypothesis profiles and selects the gating profile. The ``ci``
profile generates at least 100 examples per property, satisfying
Requirement 16.3 ("at least 100 examples for each of the named properties").
The active profile is chosen by the ``HYPOTHESIS_PROFILE`` environment
variable, defaulting to ``ci`` so the documented ``just test`` command gates
on the 100-example run.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile("dev", max_examples=20)
settings.register_profile(
    "ci",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("nightly", max_examples=1000, deadline=None)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
