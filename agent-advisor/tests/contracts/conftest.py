"""The session-level skip fence for the port-contract suites (Req 26.8, task 16.1).

**WHY A HOOK AND NOT A TEST.** Task 16.1 asks that a skip be PROVABLE. A review showed the
in-suite fence proved only the PREDICATE: it asserted `would_skip` returns False when an
endpoint is configured, and never observed whether a parameter actually ran. So every other way
a test can vanish was invisible to it — an exception inside `build()`, a `pytest.skip` in a
body, a `pytest.importorskip`, a `skipif` marker. A cloud adapter could be fully configured,
skip
for one of those reasons, and leave the report green: the exact failure this is meant to close.

Only the runner knows what actually got skipped, so the fence lives where the runner reports.
`pytest_runtest_logreport` records every skipped contract test and `pytest_sessionfinish` fails
the
session unless each one is EXPLAINED.

**An explained skip is one whose adapter needs an endpoint this environment does not supply.**
That
is the single sanctioned reason, recognised by matching against `would_skip` — the same
predicate
the suites use, so a skip cannot be excused here by logic the suites do not share.

Everything else fails the session, with the ids named. That includes a missing failure seam,
though the suites now `pytest.fail` rather than skip in that case, so this is the backstop and
not
the only guard.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from tests.contracts.registry import ADAPTER_CASES, would_skip

if TYPE_CHECKING:  # pragma: no cover - typing only
    import pytest

_ENDPOINT_GATED_IDS: frozenset[str] = frozenset(
    case.name for case in ADAPTER_CASES if would_skip(case, os.environ)
)
"""Adapter names that legitimately skip in THIS environment.

Computed once, from the same predicate the suites call. An empty set — which is the state today,
every adapter
being offline — means NO skip is explainable, so any skip at all fails the session.
"""

_skipped_contract_tests: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Record every skipped test under `tests/contracts/`."""
    if report.when != "setup" and report.when != "call":
        return
    if not report.skipped:
        return
    if "tests/contracts/" not in str(getattr(report, "nodeid", "")):
        return
    _skipped_contract_tests.append(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session on any contract skip this environment does not explain.

    Deliberately does not care whether the rest of the run passed: a green suite that skipped a
    contract
    parameter is the precise thing task 16.1 calls out, so it must not be reported as success.
    """
    unexplained = [
        nodeid
        for nodeid in _skipped_contract_tests
        if not any(f"[{name}]" in nodeid for name in _ENDPOINT_GATED_IDS)
    ]
    if not unexplained:
        return
    session.exitstatus = 1
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep("=", "UNEXPLAINED CONTRACT SKIPS", red=True)
        reporter.write_line(
            "A port-contract test skipped for a reason this environment does not explain. "
            "A skip is indistinguishable from a pass, so the suite is not green:"
        )
        for nodeid in unexplained:
            reporter.write_line(f"  {nodeid}")
        if _ENDPOINT_GATED_IDS:
            reporter.write_line(
                f"  (endpoint-gated adapters that MAY skip here: "
                f"{', '.join(sorted(_ENDPOINT_GATED_IDS))})"
            )
        else:
            reporter.write_line(
                "  (no adapter is endpoint-gated here, so no skip is explainable)"
            )
