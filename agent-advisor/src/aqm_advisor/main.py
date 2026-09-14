"""Composition root for the AI Advisor — the module the container's CMD runs (task 21).

This module builds nothing new. It selects each port's adapter from the validated
configuration, constructs the process-wide collaborators, and hands them to
`composition.build_pipeline_factory`, whose per-turn closure owns the intricate
work (fresh recorder, ledger, tools and Agent per turn). The resulting `run_turn`
goes to `agentcore.app.build_app`.

Two boundaries are deliberate. The AgentCore SDK is imported ONLY inside the
function body (Req 32.2: no module-level SDK import outside `agentcore/`), so the
offline suite and every domain test stay free of it. And `build_from_config`
takes an already-resolved `AdvisorConfig` rather than the environment, matching
the loader's rule that validation completes before construction begins.

POC note (see the advisor POC-ADDENDUM): a swarm of one or two sensors and a
serving stand-in are enough to exercise the whole turn against real Bedrock. The
adapter names in `AQM_ADVISOR_ADAPTERS` decide what is real versus scripted, so
nothing here hard-codes the deploy target.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, cast

from aqm_advisor.adapters.model.bedrock import build_bedrock_model
from aqm_advisor.agent.advisory import build_turn_runner
from aqm_advisor.agent.prompt import load_system_prompt
from aqm_advisor.composition import (
    ADAPTER_FACTORIES,
    IdentityUnavailableError,
    build_pipeline_factory,
    identity_from_snapshot,
)
from aqm_advisor.config.loader import AdvisorConfig, resolve_and_validate
from aqm_advisor.domain.forbidden import DEFAULT_FORBIDDEN_PATTERNS
from aqm_advisor.domain.redflag import DEFAULT_RED_FLAG_RULES
from aqm_advisor.observability.logging import configure_logging, get_logger
from aqm_advisor.ports.clock import Clock
from aqm_advisor.ports.protocols import (
    AdviceAuditStore,
    GuardrailChecker,
    ServingClient,
)

if TYPE_CHECKING:  # annotation only; the SDK is never imported in this module (Req 32.2)
    from strands.models import Model


def _adapter(port: str, name: str) -> object:
    """Look up a port's factory by the configured adapter name."""
    return ADAPTER_FACTORIES[port][name]


def _build_serving(config: AdvisorConfig) -> ServingClient:
    name = config.adapters["serving_client"]
    factory = _adapter("serving_client", name)
    if name == "http":
        return cast(
            ServingClient,
            factory(  # type: ignore[operator]
                base_url=config.serving_base_url,
                timeout_seconds=config.request_timeout_seconds,
            ),
        )
    return cast(ServingClient, factory())  # type: ignore[operator]  # scripted stand-in


def _build_guardrail(config: AdvisorConfig) -> GuardrailChecker:
    name = config.adapters["guardrail_checker"]
    factory = _adapter("guardrail_checker", name)
    if name == "bedrock":
        import boto3

        return cast(
            GuardrailChecker,
            factory(  # type: ignore[operator]
                client=boto3.client("bedrock-runtime", region_name=config.model_region),
                guardrail_identifier=config.guardrail_identifier or "",
                guardrail_version=config.guardrail_version or "DRAFT",
            ),
        )
    return cast(GuardrailChecker, factory())  # type: ignore[operator]  # local checker


def _build_audit(config: AdvisorConfig) -> AdviceAuditStore:
    name = config.adapters["advice_audit_store"]
    factory = _adapter("advice_audit_store", name)
    if name == "dynamodb":
        import boto3

        table_name = os.environ.get("AQM_ADVISOR_AUDIT_TABLE", "")
        table = boto3.resource("dynamodb", region_name=config.model_region).Table(table_name)
        return cast(AdviceAuditStore, factory(table=table))  # type: ignore[operator]
    return cast(AdviceAuditStore, factory())  # type: ignore[operator]  # in-memory


def _build_model(config: AdvisorConfig) -> Model:
    name = config.adapters["model"]
    if name == "bedrock":
        return build_bedrock_model(
            model_id=config.model_id,
            model_region=config.model_region,
            model_temperature=config.model_temperature,
            model_max_output_tokens=config.model_max_output_tokens,
            request_timeout_seconds=config.request_timeout_seconds,
            model_credential_path=config.model_credential_path,
            model_prompt_caching=config.model_prompt_caching,
        )
    return cast("Model", _adapter("model", name)())  # type: ignore[operator]  # scripted


def _build_clock(config: AdvisorConfig) -> Clock:
    return cast(Clock, _adapter("clock", config.adapters["clock"])())  # type: ignore[operator]


def build_from_config(config: AdvisorConfig) -> object:
    """Assemble the servable app from a validated configuration.

    Import-time construction at module scope means a misconfiguration raises at
    container cold start, so the runtime never serves a half-built advisor.
    """
    # SDK imported here, not at module scope, so the Req 32.2 fence holds.
    from aqm_advisor.agentcore.app import build_app

    configure_logging(config.log_level)
    logger = get_logger("advisor.main")

    emergency = config.emergency_guidance_fallback or (
        "If you are struggling to breathe, have chest pain, or your reliever is not "
        "helping, call your local emergency number now."
    )
    forbidden = config.forbidden_patterns or DEFAULT_FORBIDDEN_PATTERNS
    system_prompt = load_system_prompt(_read_prompt(config.system_prompt_path))

    make_pipeline = build_pipeline_factory(
        identity_for=identity_from_snapshot,
        serving_client=_build_serving(config),
        guardrail=_build_guardrail(config),
        audit_store=_build_audit(config),
        model=_build_model(config),
        clock=_build_clock(config),
        logger=logger,
        red_flag_rules=DEFAULT_RED_FLAG_RULES,
        forbidden_patterns=forbidden,
        emergency_guidance=emergency,
        system_prompt=system_prompt,
        tools_concurrent=config.tools_concurrent,
    )
    run_turn = build_turn_runner(make_pipeline=make_pipeline)

    logger.info(
        "advisor_composed",
        model=config.adapters["model"],
        guardrail=config.adapters["guardrail_checker"],
        audit=config.adapters["advice_audit_store"],
        serving=config.adapters["serving_client"],
    )
    return build_app(
        run_turn=run_turn,
        emergency_guidance=emergency,
        turn_budget_seconds=config.turn_budget_seconds,
        clock=_build_clock(config),
        streaming_enabled=config.streaming_enabled,
    )


def _read_prompt(path: str | None) -> str | None:
    if not path:
        return None
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


__all__ = ["IdentityUnavailableError", "build_from_config"]


def main() -> None:  # pragma: no cover - the container entry, not the suite
    """Resolve the environment, build the app, and serve. Import stays side-effect-free."""
    build_from_config(resolve_and_validate(dict(os.environ))).run()  # type: ignore[attr-defined]


if __name__ == "__main__":  # pragma: no cover - the container runs the module
    main()
