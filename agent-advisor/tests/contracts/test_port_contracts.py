"""The fence around the port-contract suites (Req 26.8, task 16.1).

**A SKIP IS INDISTINGUISHABLE FROM A PASS.** `pytest` reports a skipped parameter as not-failed,
so a cloud adapter whose endpoint is never configured leaves the suite green while that adapter
has never executed once. Task 16.1 therefore requires the skip be PROVABLE: when an endpoint IS
present, nothing may skip.

**The fence and the suites share one predicate.** `would_skip` decides both. Two copies could
disagree, and the copy that drifted would be the one silently excusing a cloud adapter — the
same trap that let `AdviceRecord` exist twice with field sets matching by luck.

**The mechanism is proven on synthetic cases.** Every real adapter today is offline, so a fence
over the real registry alone would hold by having nothing to check. That is a vacuous guarantee,
which this codebase has been bitten by repeatedly, so the discriminating behaviour is asserted
on a fabricated pair instead.
"""

from __future__ import annotations

import pytest

from tests.contracts.registry import (
    ADAPTER_CASES,
    PORT_TYPES,
    AdapterCase,
    cases_for,
    declared_ports,
    protocol_count,
    synthetic_cases,
    would_skip,
)

pytestmark = pytest.mark.contract


# --- the predicate discriminates ---------------------------------------


def test_an_offline_case_is_never_skipped() -> None:
    # Req 26.5: the full suite passes with no credentials and no network. That guarantee is
    # worth
    # nothing if it is satisfied by skipping, so an offline case cannot skip for any
    # environment.
    offline = next(case for case in synthetic_cases() if case.is_offline)
    assert would_skip(offline, {}) is False
    assert would_skip(offline, {"AQM_ADVISOR_TEST_ENDPOINT": "https://x.invalid"}) is False


def test_a_cloud_case_skips_only_when_its_endpoint_is_absent() -> None:
    # Both directions, because a predicate that always returned True would satisfy "skips when
    # absent" while excusing the adapter forever.
    cloud = next(case for case in synthetic_cases() if not case.is_offline)
    assert would_skip(cloud, {}) is True
    assert would_skip(cloud, {"AQM_ADVISOR_TEST_ENDPOINT": "   "}) is True, (
        "a blank endpoint is not a configured endpoint"
    )
    assert would_skip(cloud, {"AQM_ADVISOR_TEST_ENDPOINT": "https://x.invalid"}) is False


# --- THE fence ---------------------------------------------------------


def test_no_case_skips_when_its_endpoint_is_present() -> None:
    # Task 16.1's named assertion, WITH ITS SCOPE STATED. A review pointed out this proves the
    # PREDICATE and not the FIXTURE: it never builds an adapter and never observes a run, so
    # every
    # other way a test can vanish — an exception in `build()`, a `pytest.skip` in a body, a
    # `skipif` marker — was invisible to it. Calling that "nothing may skip" oversold it.
    #
    # The session hook in `conftest.py` covers those, by recording what the runner ACTUALLY
    # skipped
    # and failing the session on any skip this environment cannot explain. This test stays as
    # the
    # cheap direct check on the predicate that hook defers to.
    supplied = {
        case.requires_endpoint: "https://endpoint.example.test"
        for case in (*ADAPTER_CASES, *synthetic_cases())
        if case.requires_endpoint is not None
    }
    for case in (*ADAPTER_CASES, *synthetic_cases()):
        assert would_skip(case, supplied) is False, (case.port, case.name)


def test_the_fence_would_catch_a_case_that_skipped_anyway() -> None:
    # Self-check. A fence built from a predicate that never returns True would pass the test
    # above
    # for free, so the predicate must be shown to skip when it should.
    cloud = next(case for case in synthetic_cases() if not case.is_offline)
    assert would_skip(cloud, {}) is True


# --- the registry covers every port ------------------------------------


def test_every_declared_port_has_at_least_one_adapter_case() -> None:
    # Req 26.8 is "one suite per port, executed against every adapter of that port". A port with
    # no
    # case would have a suite that runs zero parameters and still reports green.
    for port in declared_ports():
        assert cases_for(port), port


def test_the_registry_covers_every_protocol_the_ports_module_declares() -> None:
    # Held against `port_protocols()` rather than a hand-written count, so a port added later
    # fails
    # this test by DEFAULT instead of being silently uncovered. Same "covered by default" shape
    # the
    # leak sweeps use.
    assert len(PORT_TYPES) == protocol_count(), (
        f"{protocol_count()} ports declared, {len(PORT_TYPES)} in the contract registry"
    )


@pytest.mark.parametrize("case", ADAPTER_CASES, ids=lambda c: f"{c.port}:{c.name}")
def test_each_case_builds_something_that_satisfies_its_port(case: AdapterCase) -> None:
    # The registry claims a port; this checks the claim. A case wired to the wrong port would
    # otherwise run that port's whole behavioural suite against an object of another shape, and
    # the
    # failures would read as behavioural rather than as a registry mistake.
    #
    # Routed through `would_skip` because a review found it calling `build()` UNCONDITIONALLY —
    # the
    # one place bypassing the predicate. A cloud case with no endpoint configured would have had
    # its
    # constructor run anyway, erroring the offline suite for a case every other test correctly
    # skips.
    import os

    if would_skip(case, os.environ):
        pytest.skip(f"{case.name} needs {case.requires_endpoint}, which is not configured")
    built = case.build()
    assert isinstance(built, PORT_TYPES[case.port]), (case.port, case.name, type(built))


def test_no_two_cases_share_a_port_and_name() -> None:
    # A duplicate id makes one parameter shadow the other in the report, so an adapter could
    # look
    # covered while never running.
    identities = [(case.port, case.name) for case in ADAPTER_CASES]
    assert len(identities) == len(set(identities)), identities


def test_every_case_is_offline_today_and_says_so() -> None:
    # Pins the CURRENT state rather than asserting a permanent truth: every adapter today is
    # offline. When task 16.2 adds a cloud case this test fails, which is the intended prompt to
    # confirm the skip path is wired for it rather than discovering later that it never ran.
    non_offline = [
        (c.port, c.name, c.requires_endpoint) for c in ADAPTER_CASES if not c.is_offline
    ]
    assert non_offline == [], (
        "a cloud adapter joined the registry: confirm its endpoint key is honoured by "
        "would_skip "
        "and by the offline recipe, then update this test to expect it"
    )
