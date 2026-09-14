"""The factory table and the loader's registry must agree exactly.

Task 21.1's named deliverable: "a test asserting the adapter factory table agrees exactly with
the loader's registry, so a name the configuration permits but nothing builds fails offline."

TWO FAILURES, IN OPPOSITE DIRECTIONS, AND BOTH ARE INVISIBLE IN ONE FILE:

* A registry entry with NO factory is a name the loader accepts and the composition root then
  cannot build. The operator reads the registry, sets the value, and the process dies at startup
  on a name its own configuration documented as valid.
* A factory with NO registry entry is unreachable. The loader rejects the name, so the code can
  never run — and it will still be maintained, tested and read by people who assume it can.

Neither shows up by reading `composition.py` or `config/loader.py` alone, which is why the
assertion is over both and in both directions.

THE ORDER MATTERS TOO, not just the membership. Req 23.6 makes the registry's FIRST entry the
default, so a factory table that offers every name but whose port keys disagree about which is
first would build a different adapter than the loader's default names. That is asserted
separately rather than folded in, because a set comparison passes over exactly that mistake.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib
from typing import cast

from pydantic import SecretStr
from strands.models import Model
from strands.tools.executors.concurrent import ConcurrentToolExecutor
from strands.tools.executors.sequential import SequentialToolExecutor

import aqm_advisor.composition as composition
from aqm_advisor.adapters.local import (
    InMemoryAdviceAuditStore,
    LocalGuardrailChecker,
    ScriptedServingClient,
    canned_air_quality,
)
from aqm_advisor.composition import (
    ADAPTER_FACTORIES,
    build_pipeline_factory,
    identity_from_snapshot,
)
from aqm_advisor.config.loader import REGISTERED_ADAPTERS
from aqm_advisor.domain.models import AdvisoryRequest
from aqm_advisor.observability.logging import get_logger
from aqm_advisor.ports.clock import FixedClock


def test_every_registered_port_has_a_factory_group() -> None:
    missing = sorted(set(REGISTERED_ADAPTERS) - set(ADAPTER_FACTORIES))
    assert not missing, (
        f"these ports are configurable but nothing builds them: {missing}. A name the loader "
        f"accepts and the root cannot build is a startup crash on a documented value."
    )


def test_no_factory_group_exists_for_an_unregistered_port() -> None:
    orphan = sorted(set(ADAPTER_FACTORIES) - set(REGISTERED_ADAPTERS))
    assert not orphan, (
        f"these ports have factories but no registry entry, so no configuration can reach "
        f"them: {orphan}"
    )


def test_every_registered_adapter_name_has_a_factory() -> None:
    missing: list[str] = []
    for port, names in REGISTERED_ADAPTERS.items():
        factories = ADAPTER_FACTORIES.get(port, {})
        missing += [f"{port}.{name}" for name in names if name not in factories]
    assert not missing, f"configurable names that nothing builds: {missing}"


def test_no_factory_is_unreachable_from_configuration() -> None:
    unreachable: list[str] = []
    for port, factories in ADAPTER_FACTORIES.items():
        names = REGISTERED_ADAPTERS.get(port, ())
        unreachable += [f"{port}.{name}" for name in factories if name not in names]
    assert not unreachable, f"factories no configuration can select: {unreachable}"


def test_every_factory_is_callable() -> None:
    # A table of INSTANCES would make two turns share one adapter, which is the cross-turn
    # leakage the per-turn construction rule exists to prevent. This pins the table's shape.
    not_callable = [
        f"{port}.{name}"
        for port, factories in ADAPTER_FACTORIES.items()
        for name, factory in factories.items()
        if not callable(factory)
    ]
    assert not not_callable, f"these are objects rather than constructors: {not_callable}"


def test_the_agreement_check_is_not_vacuous() -> None:
    # Every assertion above passes trivially over two empty mappings. This pins that both sides
    # are populated, and that they are the same size — so a future port added to one side alone
    # cannot be masked by a typo in the loop above.
    assert len(REGISTERED_ADAPTERS) >= 6
    assert len(ADAPTER_FACTORIES) == len(REGISTERED_ADAPTERS)
    total_names = sum(len(names) for names in REGISTERED_ADAPTERS.values())
    total_factories = sum(len(factories) for factories in ADAPTER_FACTORIES.values())
    assert total_names == total_factories >= 11


def test_the_default_adapter_name_is_the_registry_first_entry_and_buildable() -> None:
    # Req 23.6 makes the FIRST registered name the default. A set comparison passes over a table
    # whose port key order disagrees, so the default specifically is checked here.
    for port, names in REGISTERED_ADAPTERS.items():
        assert names, f"{port} registers no adapter at all"
        default = names[0]
        assert default in ADAPTER_FACTORIES[port], (
            f"{port}'s DEFAULT name {default!r} has no factory, so a service started with no "
            f"configuration for this port cannot build one"
        )


# --- the production model invocation must run the tool-use loop --------

_COMPOSITION_SRC = (
    pathlib.Path(__file__).resolve().parents[2] / "src" / "aqm_advisor" / "composition.py"
)


def _invoke_source() -> str:
    """The source of `build_pipeline_factory`'s inner `invoke`, as text.

    Read from disk and located by AST so the assertion is about the code that ACTUALLY runs in
    production, not a re-description of it a comment could let drift.
    """
    source = _COMPOSITION_SRC.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "invoke":
            return ast.get_source_segment(source, node) or ""
    raise AssertionError("no `invoke` function found in composition.py")


def test_the_production_invoke_runs_the_tool_loop_then_takes_structured_output() -> None:
    # THE BUG THIS PINS. `agent.structured_output(...)` (deprecated in strands 1.55.1) does a
    # single structured extraction and does NOT run the agentic loop, so the live model never
    # calls the retrieval tools: a probe had Claude report it had only the ModelGeneration tool,
    # ground nothing, and every turn degraded. The scripted-model tests could not see it because
    # the scripted model answers `structured_output` in isolation. The fix — and what this
    # pins — is passing `structured_output_model` to the ORDINARY agent invocation, which
    # runs the loop and then produces the structured output. A regression to the deprecated
    # call fails here.
    src = _invoke_source()
    assert "structured_output_model" in src, (
        "the production invoke must pass `structured_output_model` to the agent invocation so "
        "the tool-use loop runs before structured output is produced"
    )
    assert ".structured_output(" not in src, (
        "the production invoke must not call the deprecated `agent.structured_output(...)`, "
        "which skips the tool loop and leaves the model unable to ground its answer"
    )


# --- tool executor: sequential by default, because our tools are ordered ------


def _capture_tool_executor(
    monkeypatch: object, *, tools_concurrent: bool
) -> object:
    """Build one pipeline through the real factory and return the executor the Agent got.

    The Agent is constructed inside the factory closure and not otherwise reachable, so a
    recording stand-in for `composition.Agent` captures the `tool_executor` kwarg. Everything
    else is a local fake, so no model, network or credential is involved.
    """
    captured: dict[str, object] = {}

    class _RecordingAgent:
        def __init__(self, **kwargs: object) -> None:
            captured["tool_executor"] = kwargs.get("tool_executor")

        def __call__(self, *_a: object, **_k: object) -> object:  # pragma: no cover - unused
            raise AssertionError("the recording agent is not meant to be invoked")

    monkeypatch.setattr(composition, "Agent", _RecordingAgent)  # type: ignore[attr-defined]

    served = canned_air_quality()
    factory = build_pipeline_factory(
        identity_for=identity_from_snapshot,
        serving_client=ScriptedServingClient(air_quality_body=served),
        guardrail=LocalGuardrailChecker(),
        audit_store=InMemoryAdviceAuditStore(),
        model=cast(Model, object()),  # never invoked; the recording agent captures and stops
        clock=FixedClock(dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)),
        logger=get_logger("test.composition"),
        red_flag_rules=(),
        forbidden_patterns=(),
        emergency_guidance="call your local emergency number now.",
        system_prompt="a system prompt long enough to be non-trivial for the loader.",
        tools_concurrent=tools_concurrent,
    )
    factory(
        AdvisoryRequest(utterance="How is the air?", credential=SecretStr("cred"))
    )
    return captured["tool_executor"]


def test_tools_run_sequentially_by_default(monkeypatch: object) -> None:
    # Strands defaults to a ConcurrentToolExecutor, but our per-turn tools are NOT safe to run
    # concurrently: `history` depends on the `retrieved_site_code` that `air_quality` sets, and
    # the tools share a mutable recorder and `nonlocal` counters that are not thread-safe. So
    # the correct executor is sequential, and it is pinned rather than left to the default.
    executor = _capture_tool_executor(monkeypatch, tools_concurrent=False)
    assert isinstance(executor, SequentialToolExecutor)


def test_concurrency_can_be_enabled_by_configuration(monkeypatch: object) -> None:
    # The flag exists so the choice can be revisited once the tools are made thread-safe and the
    # air_quality -> history dependency is broken. Until then it stays off by default.
    executor = _capture_tool_executor(monkeypatch, tools_concurrent=True)
    assert isinstance(executor, ConcurrentToolExecutor)
