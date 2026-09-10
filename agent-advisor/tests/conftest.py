"""Shared test configuration: the hypothesis profiles and the offline guarantee.

Two profiles, because the two audiences want different things (Requirement 26.9):

* ``ci`` runs every property test at the 100-example floor the design mandates — enough to
  catch a counterexample on every push without making the suite slow enough that people stop
  running it.
* ``nightly`` runs at 1000, which is where the shallower counterexamples were actually found in
  Service 2.

The default is ``ci``, so a bare ``pytest`` gives the documented floor rather than hypothesis's
own default of 100-with-different-settings.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, Verbosity, settings

CI_EXAMPLES = 100
"""The design's per-property floor: "no fewer than 100 examples"."""

NIGHTLY_EXAMPLES = 1000

settings.register_profile(
    "ci",
    max_examples=CI_EXAMPLES,
    deadline=None,
    # A generated advisory turn walks a scripted model and several fakes, so one example can
    # be slow without anything being wrong. A deadline here would make the suite flaky rather
    # than informative.
    suppress_health_check=[HealthCheck.too_slow],
)

settings.register_profile(
    "nightly",
    max_examples=NIGHTLY_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    verbosity=Verbosity.normal,
)

settings.load_profile(os.environ.get("AQM_HYPOTHESIS_PROFILE", "ci"))
