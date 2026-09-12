"""Requirement 33's prohibitions, enforced structurally.

WHY THESE ARE PROHIBITIONS RATHER THAN AN ADAPTER. Task 18 asks for an association trigger and
for a choice among three AWS integrations. Both of the requirements behind that are CONDITIONAL,
and each antecedent was checked against Service 2's code before any of this was written:

* Req 33.3 says "WHERE this service hosts the trigger for the association job". It does not.
  DD13 makes the derivation Service 2's, and Service 2's own Req 32.12 keeps it off the serving
  path and on its own schedule.
* Req 33.10 says "WHERE the association job is driven from a serverless pipeline rather than
  from within a turn". It is not. Service 2's `jobs/entrypoint.py` is a one-shot process whose
  docstring places the schedule "outside this code -- cron, an EventBridge rule, a Kubernetes
  CronJob", and Service 2 exposes seven HTTP routes, none of which triggers a derivation, with
  no queue, EventBridge or task-token consumer anywhere in it.

So building an adapter would have meant INVENTING a Service 2 trigger API that its spec does not
define. That is the Req 3.1a defect of task 16.3 in reverse -- there a cross-service contract
was DISCOVERED and the requirements corrected to match; here one would have been fabricated
unilaterally against a service that is complete and merged.

TWO CRITERIA ARE ALREADY SATISFIED ELSEWHERE, and the tests below pin them so a refactor cannot
quietly undo them:

* Req 33.8 (`add_async_task` / `complete_async_task` for work continuing past a response) is met
  by the entrypoint, which brackets every turn with them.
* Req 33.4's idempotency is met by SERVICE 2, whose `put_learned_thresholds` replaces rather
  than merges. Asserting it here would be asserting somebody else's guarantee.

TWO ARE UNCITED BY TASK 18 AND BIND ANYWAY -- Reqs 33.8 and 33.9. 33.9 is the interesting
one: it says asynchrony is an agent-side behaviour and there is no fire-and-forget job
endpoint, so an Advisory_Request needs no async variant. Nothing asserted that before this.

A NOTE ON A FALSE POSITIVE THIS FILE AVOIDS. `correlation_id` appears in the trigger's signature
and is an IDENTIFIER, not a computation. A textual scan for correlation vocabulary flags it and
reads as evidence the advisor computes an association. The scan below therefore reads defined
FUNCTION NAMES and imports out of the AST, never parameter names.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from aqm_advisor.agentcore.app import build_app
from aqm_advisor.config.loader import REGISTERED_ADAPTERS
from aqm_advisor.domain.models import AdvisoryRequest, AdvisoryResponse
from aqm_advisor.ports.clock import FixedClock

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "aqm_advisor"
_TURN_PATH = ("domain", "agent", "agentcore")

_TRIGGER_NAMES = ("AssociationTrigger", "association_trigger")
"""The port and its config key. A production adapter would have to name one of them."""

_ALLOWED_TRIGGER_REFERENCES = {
    "ports/protocols.py": "declares the port -- the design's DD13 seam",
    "adapters/local.py": "RecordingAssociationTrigger, the test double",
    "config/loader.py": "registers the port's adapter names, none of them production",
}
"""Where the trigger may be named. Anywhere else means somebody wired it.

If a real trigger is ever built, this test fails and the failure is the point: whoever builds it
must first establish a Service 2 trigger surface, which means a requirement change on THAT
service rather than an adapter invented over here.
"""

_STATISTICAL_VOCABULARY = (
    "correlat",
    "pearson",
    "spearman",
    "covarian",
    "regress",
    "associate",
)
_STATISTICAL_MODULES = {"statistics", "numpy", "scipy", "pandas"}


def _modules() -> list[tuple[str, ast.Module]]:
    return [
        (str(path.relative_to(_SRC)), ast.parse(path.read_text(encoding="utf-8")))
        for path in sorted(_SRC.rglob("*.py"))
    ]


def _never_runs(_request: AdvisoryRequest) -> AdvisoryResponse:
    """A turn runner the route tests must never invoke."""
    raise AssertionError("inspecting routes must not run a turn")


def _app() -> BedrockAgentCoreApp:
    return build_app(
        run_turn=_never_runs,
        emergency_guidance="If you are struggling to breathe, call 999.",
        turn_budget_seconds=5,
        clock=FixedClock(dt.datetime(2026, 9, 12, 12, 0, tzinfo=dt.UTC)),
    )


# --- Req 33.3 / 33.10: the trigger is not hosted here ------------------


def test_no_production_adapter_is_registered_for_the_trigger() -> None:
    # The config registry ALREADY encodes this finding, which is why the conclusion is a reading
    # of the service rather than a preference: every other port offers a real adapter, and this
    # one offers only a recording double.
    assert REGISTERED_ADAPTERS["association_trigger"] == ("recording",)


def test_every_other_port_does_offer_a_production_adapter() -> None:
    # Without this, the assertion above could be satisfied by a service that registers one
    # adapter for everything, and the trigger's singleness would mean nothing.
    others = {
        name: options
        for name, options in REGISTERED_ADAPTERS.items()
        if name != "association_trigger"
    }
    thin = sorted(name for name, options in others.items() if len(options) < 2)
    assert not thin, f"these ports offer no alternative either; the contrast is empty: {thin}"


def test_the_turn_path_never_names_the_association_trigger() -> None:
    # Req 33.1: not computed, not triggered synchronously, not awaited during a turn. A turn
    # that cannot NAME the trigger cannot wait for it, which is the strongest form this can
    # take offline -- no timing test proves the absence of a call that is not there.
    offenders = [
        name
        for name, _tree in _modules()
        if name.split("/")[0] in _TURN_PATH
        and any(word in (_SRC / name).read_text(encoding="utf-8") for word in _TRIGGER_NAMES)
    ]
    assert not offenders, f"the turn path references the association trigger: {offenders}"


def test_only_the_port_its_double_and_the_registry_name_the_trigger() -> None:
    found = {
        name
        for name, _tree in _modules()
        if any(word in (_SRC / name).read_text(encoding="utf-8") for word in _TRIGGER_NAMES)
    }
    unexpected = sorted(found - set(_ALLOWED_TRIGGER_REFERENCES))
    assert not unexpected, (
        f"something new references the association trigger: {unexpected}. If this is a real "
        f"adapter, Service 2 has no trigger surface to call -- establish one there first, as a "
        f"requirement change on that service."
    )


def test_each_allowed_trigger_reference_is_still_real() -> None:
    # The staleness direction. A file renamed or a reference removed must not leave a phantom
    # entry that quietly widens what the previous test permits.
    found = {
        name
        for name, _tree in _modules()
        if any(word in (_SRC / name).read_text(encoding="utf-8") for word in _TRIGGER_NAMES)
    }
    phantom = sorted(set(_ALLOWED_TRIGGER_REFERENCES) - found)
    assert not phantom, f"these no longer reference the trigger; delist them: {phantom}"


# --- Req 33.1 / 33.2: the advisor derives nothing ----------------------


def test_the_service_defines_no_statistical_function() -> None:
    # Req 33.1's "SHALL NOT compute". Reads FUNCTION NAMES from the AST, never parameter names,
    # so the trigger's `correlation_id` argument is not mistaken for a correlation computation.
    offenders: list[str] = []
    for name, tree in _modules():
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            lowered = node.name.lower()
            if any(word in lowered for word in _STATISTICAL_VOCABULARY):
                offenders.append(f"{name}:{node.name}")
    assert not offenders, f"these look like a second derivation of Service 2's job: {offenders}"


def test_the_service_imports_no_statistics_library() -> None:
    offenders: list[str] = []
    for name, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [
                    f"{name}:{a.name}"
                    for a in node.names
                    if a.name.split(".")[0] in _STATISTICAL_MODULES
                ]
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] in _STATISTICAL_MODULES
            ):
                offenders.append(f"{name}:{node.module}")
    assert not offenders, f"a statistics library reached the advisor: {offenders}"


def test_the_statistical_detector_is_not_vacuous() -> None:
    # Self-check: the vocabulary must match something when it IS present. Without this, a typo
    # in the word list would make both tests above pass over any code at all.
    planted = ast.parse("def compute_correlation(): pass\nimport numpy\n")
    names = [
        n.name
        for n in ast.walk(planted)
        if isinstance(n, ast.FunctionDef)
        and any(w in n.name.lower() for w in _STATISTICAL_VOCABULARY)
    ]
    imports = [
        a.name
        for n in ast.walk(planted)
        if isinstance(n, ast.Import)
        for a in n.names
        if a.name.split(".")[0] in _STATISTICAL_MODULES
    ]
    assert names == ["compute_correlation"]
    assert imports == ["numpy"]


# --- Req 33.9: asynchrony is agent-side, with no job endpoint ----------


def test_the_request_model_has_no_asynchronous_variant() -> None:
    # Req 33.9. A client cannot tell a synchronous turn from an asynchronous one, so there is
    # nothing for a caller to opt into and no token, job id or callback to carry.
    async_ish = ("async", "job", "callback", "task_token", "tasktoken", "defer", "webhook")
    fields = [f for f in AdvisoryRequest.model_fields if any(w in f.lower() for w in async_ish)]
    assert not fields, f"an async variant leaked into the request model: {fields}"


def test_the_app_exposes_only_the_invocation_and_health_routes() -> None:
    # Req 33.9's "there is no fire-and-forget job endpoint to call", asserted against the built
    # app rather than the source, so a route registered by any means is caught.
    paths = {str(getattr(route, "path", "")) for route in _app().routes}
    assert paths == {"/invocations", "/ping"}, f"unexpected route surface: {sorted(paths)}"


# --- Req 33.8: already met, and pinned so it stays met -----------------


def test_the_entrypoint_brackets_work_with_the_async_task_api() -> None:
    # Req 33.8 is absent from task 18's citation list but binds anyway. It is satisfied by the
    # entrypoint, and this pins it: the SDK's task API is also what keeps `/ping` truthful, so
    # dropping it would break Req 32.4b silently rather than loudly.
    source = (_SRC / "agentcore" / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"add_async_task", "complete_async_task"} <= called


# --- Req 33.7: nothing can notify the user ----------------------------


def test_nothing_in_the_service_can_notify_the_user() -> None:
    # Req 33.7, which defers to Req 20.6. Service-wide rather than per-class: the audit writer's
    # public surface is pinned elsewhere, but a notifier added ANYWHERE would satisfy that test
    # and still break this requirement.
    offenders: list[str] = []
    for name, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                "notify" in node.name.lower() or "send_email" in node.name.lower()
            ):
                offenders.append(f"{name}:{node.name}")
    assert not offenders, f"the service gained a way to notify a user: {offenders}"


# --- Req 33.6: nothing to degrade from, stated rather than implied -----


def test_no_learned_threshold_is_required_for_a_response() -> None:
    # Req 33.6 says a failed learning run degrades personalisation without breaking the advice.
    # With no trigger hosted here there is no run to fail, so the criterion reduces to: a
    # served payload with no learned block must yield a usable outcome rather than raise.
    from aqm_advisor.domain.association import learned_threshold_view

    assert learned_threshold_view({"thresholdSource": "default"}) is None


def test_the_response_model_carries_no_association_field() -> None:
    # Req 33.2: a Learned_Threshold is read from a retrieved response, never held as advisor
    # state. A field here would be somewhere for a derived value to live.
    names = list(AdvisoryResponse.model_fields)
    assert not [n for n in names if "associat" in n.lower() or "threshold" in n.lower()], names
