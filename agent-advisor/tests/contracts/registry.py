"""The adapter registry every port-contract suite is parameterised over (Req 26.8, task 16.1).

**ONE PREDICATE DECIDES A SKIP, AND THE FENCE USES THE SAME ONE.** `would_skip` is called by the
suites to skip a cloud parameter and by `test_port_contracts.py`'s fence to assert no parameter
skips when its endpoint IS present. A second copy of that logic could disagree with the first,
and the copy that drifted would be the one letting a cloud adapter go untested — the same
two-definitions trap that let `AdviceRecord` exist twice.

**A SKIP IS INDISTINGUISHABLE FROM A PASS in a test report, which is the whole problem.** A
cloud parameter that skips forever leaves the suite green while the real adapter has never run
once. So the skip is not merely conditional, it is PROVABLE: the fence asserts that whenever an
endpoint is configured, nothing skips.

**THE MECHANISM IS PROVEN WITH SYNTHETIC CASES, because today it would otherwise be vacuous.**
Every real case here is offline — the cloud adapters arrive in tasks 16.2 to 16.5 — so a fence
over the real registry alone would pass by having nothing to check. `_SYNTHETIC_CASES` exercises
both branches now, so the mechanism is known to work before the first cloud adapter depends on
it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from aqm_advisor.adapters.local import (
    InMemoryAdviceAuditStore,
    LocalGuardrailChecker,
    RecordingAssociationTrigger,
    ScriptedServingClient,
)
from aqm_advisor.ports.protocols import (
    AdviceAuditStore,
    AssociationTrigger,
    GuardrailChecker,
    ServingClient,
    ServingFailureKind,
    port_protocols,
)


@dataclass(frozen=True, slots=True)
class AdapterCase:
    """One adapter of one port, and what it needs in order to run.

    `requires_endpoint` is the CONFIGURATION KEY that would name a live endpoint, not the
    endpoint itself. The registry therefore names a dependency without embedding a URL, and a
    test can decide whether the case can run by asking whether that key is set — no network
    probe, so the offline suite stays offline.

    `build_failing` is how THIS adapter is made to fail, and `build_unavailable` how it is made
    unreachable. Both live on the case rather than in a shared helper because a review found the
    helper keying on `hasattr(client, "air_quality_body")` — a scripted-adapter implementation
    detail — and returning None for anything else, which made the two most important tests in
    the
    serving suite SKIP for exactly the adapter that can leak a credential over a network. Each
    case now declares its own seam, so adding an adapter is one entry rather than another arm in
    an if/elif chain that the practices' Open/Closed rule warns about.

    A case with no seam cannot have that contract checked at all, and the suites treat the
    absence
    as a FAILURE rather than a skip: "we could not test the failure path" is not a pass.
    """

    port: str
    name: str
    build: Callable[[], Any]
    requires_endpoint: str | None = None
    build_failing: Callable[[], Any] | None = None
    build_unavailable: Callable[[], Any] | None = None

    @property
    def is_offline(self) -> bool:
        """True when this adapter needs nothing beyond the process."""
        return self.requires_endpoint is None


def would_skip(case: AdapterCase, env: Mapping[str, str]) -> bool:
    """Whether `case` must be skipped in `env` (Req 26.6's fence).

    THE single decision. The suites call it to skip; the fence calls it to assert that a
    configured endpoint leaves nothing skipped. One function, so the two cannot disagree.

    A case is skipped only when it names an endpoint that `env` does not supply. An offline case
    is never skipped, which is what keeps Req 26.5's guarantee — the full suite passing with no
    credentials and no network — from quietly becoming "the suite that skipped everything".
    """
    if case.requires_endpoint is None:
        return False
    return not env.get(case.requires_endpoint, "").strip()


PORT_TYPES: Mapping[str, type] = {
    "serving_client": ServingClient,
    "guardrail_checker": GuardrailChecker,
    "advice_audit_store": AdviceAuditStore,
    "association_trigger": AssociationTrigger,
}
"""Port name to its Protocol, so a case can be checked against what it claims to implement.

**THE MODEL PORT IS DELIBERATELY ABSENT, AND THIS IS THE SCOPED EXCEPTION.** A literal reading
of Req 26.8 — "one shared behavioral test suite per port, executed against every adapter of that
port" — is not satisfied for the Model_Port, which will have two adapters (the scripted one, and
`BedrockModel` in task 16.2). Recorded here rather than left silent, because a reviewer who
spots the gap would otherwise re-derive this argument or "fix" it by adding a suite.

The exception rests on two facts. First, design decision DD2 makes the Strands `Model` abstract
class ITSELF the port, so there is no first-party Protocol to register, and its four abstract
methods are enforced by construction: a subclass missing one cannot be instantiated. Second, and
decisively, the Model port has no adapter-independent behavioural contract that is checkable
offline. Every other suite here asserts observable behaviour — raises with a kind, returns a
verdict, reports a count on erase. The Model's behaviour is WHICH TOKENS IT STREAMS, which is
exactly what differs per adapter and per prompt, and which for Bedrock needs a live account.

So a shared Model suite would be either vacuous or integration-only, and a vacuous suite is
worse than none because
it certifies. No proposed Model assertion survives the test every other suite in this file
passes: true of every
adapter AND checkable offline. If someone later finds one that does, it belongs here and this
paragraph is wrong.
"""


ADAPTER_CASES: tuple[AdapterCase, ...] = (
    AdapterCase(
        "serving_client",
        "scripted",
        ScriptedServingClient,
        build_failing=lambda: ScriptedServingClient(
            air_quality_body=ServingFailureKind.UNREACHABLE
        ),
    ),
    AdapterCase(
        "guardrail_checker",
        "local",
        LocalGuardrailChecker,
        build_unavailable=lambda: LocalGuardrailChecker(unavailable=True),
    ),
    AdapterCase("advice_audit_store", "memory", InMemoryAdviceAuditStore),
    AdapterCase("association_trigger", "recording", RecordingAssociationTrigger),
)
"""Every adapter of every port.

ONLY OFFLINE CASES EXIST TODAY. Tasks 16.2 to 16.5 add the cloud ones, each as one entry
carrying its `requires_endpoint` AND its failure seams — the same one-entry extension the
configuration registry asks for.

An HTTP serving client's `build_failing` will not resemble the scripted one's: pointing it at a
closed local port, or handing it a stub transport that raises, both stay offline-safe. That the
seam DIFFERS per adapter is exactly why it belongs on the case rather than in a shared helper.
"""


def cases_for(port: str) -> tuple[AdapterCase, ...]:
    """Every case of one port, in registry order."""
    return tuple(case for case in ADAPTER_CASES if case.port == port)


_SYNTHETIC_CASES: tuple[AdapterCase, ...] = (
    AdapterCase("synthetic", "offline", dict),
    AdapterCase("synthetic", "cloud", dict, requires_endpoint="AQM_ADVISOR_TEST_ENDPOINT"),
)
"""Cases that exist ONLY to prove `would_skip` discriminates.

Without these the fence would be vacuous today: every real case is offline, so "no parameter
skips when an endpoint is present" holds by there being no such parameter. A mechanism whose
first real use is also its first exercise is a mechanism nobody has tested.
"""


def synthetic_cases() -> tuple[AdapterCase, ...]:
    """The synthetic pair, for the predicate's own tests."""
    return _SYNTHETIC_CASES


def declared_ports() -> tuple[str, ...]:
    """Port names in a defined order."""
    return tuple(sorted(PORT_TYPES))


def protocol_count() -> int:
    """How many ports `ports.protocols` declares, so the registry can be held to it."""
    return len(port_protocols())
