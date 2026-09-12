"""The container entry point: `python -m aqm_advisor.main` (task 21.1).

This is the module the image's `CMD` names, and the only place that reads configuration, builds
adapters and starts the server. Everything below it takes its collaborators as parameters, which
is what keeps the whole advisory path executable in the offline suite.

STARTUP FAILS FAST AND LOUDLY, per the engineering practices' Config_Loader rule: every value is
validated before anything is constructed, and an invalid configuration exits non-zero with one
message per problem rather than half-starting.

THE PSEUDONYMOUS USER IDENTITY HAS NO COMPLIANT SOURCE YET, and this module is where that
becomes visible instead of being quietly invented. Req 20.2 requires the audit record to carry
it; Req 5.6 forbids this service from decoding or parsing the credential; the AgentCore SDK
forwards no verified claim; and Service 2 validates a `userId` it is given without ever
returning one. So `_identity_for` raises, the container builds and starts, and a turn that would
write an unattributable audit record fails rather than writing a subject `forget_user` could
never erase. Resolving it is a spec change on Service 2, recorded in tasks.md.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import boto3
from strands.models.model import Model

from aqm_advisor.adapters.guardrail.bedrock import ApplyGuardrailChecker
from aqm_advisor.adapters.local import (
    InMemoryAdviceAuditStore,
    LocalGuardrailChecker,
    ScriptedServingClient,
)
from aqm_advisor.adapters.model.bedrock import build_bedrock_model
from aqm_advisor.adapters.model.scripted import ScriptedModel
from aqm_advisor.adapters.serving.http import HttpServingClient
from aqm_advisor.agent.advisory import build_turn_runner
from aqm_advisor.agent.prompt import load_system_prompt
from aqm_advisor.agentcore.app import build_app
from aqm_advisor.composition import build_pipeline_factory, identity_from_snapshot
from aqm_advisor.config.loader import (
    REGISTERED_ADAPTERS,
    AdvisorConfig,
    ConfigError,
    resolve_and_validate,
)
from aqm_advisor.observability.logging import configure_logging, get_logger
from aqm_advisor.ports.clock import Clock, FixedClock, SystemClock
from aqm_advisor.ports.protocols import AdviceAuditStore, GuardrailChecker, ServingClient

_LOGGER_NAME = "aqm_advisor.main"
_FIXED_AT = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
"""Only reachable with `clock=fixed`, which exists for reproducing a turn, never for serving."""


class UnbuildableAdapterError(RuntimeError):
    """A configured adapter name has a factory but no way to supply its arguments."""


def _selected(config: AdvisorConfig, port: str) -> str:
    """The adapter name configuration chose for `port`, defaulting to the registry's first."""
    return config.adapters.get(port) or REGISTERED_ADAPTERS[port][0]


def _serving_client(config: AdvisorConfig) -> ServingClient:
    if _selected(config, "serving_client") == "http":
        return HttpServingClient(
            base_url=config.serving_base_url,
            timeout_seconds=config.request_timeout_seconds,
        )
    return ScriptedServingClient()


def _guardrail(config: AdvisorConfig) -> GuardrailChecker:
    if _selected(config, "guardrail_checker") == "bedrock":
        if not config.guardrail_identifier or not config.guardrail_version:
            raise UnbuildableAdapterError(
                "guardrail_checker=bedrock needs guardrail_identifier and guardrail_version"
            )
        return ApplyGuardrailChecker(
            client=boto3.client("bedrock-runtime", region_name=config.model_region),
            guardrail_identifier=config.guardrail_identifier,
            guardrail_version=config.guardrail_version,
        )
    return LocalGuardrailChecker(patterns=config.forbidden_patterns or None)


def _audit_store(config: AdvisorConfig) -> AdviceAuditStore:
    if _selected(config, "advice_audit_store") == "dynamodb":
        # A CONFIG GAP, recorded rather than papered over. `REGISTERED_ADAPTERS` offers
        # `dynamodb` and `ADAPTER_FACTORIES` has a factory, so the agreement test passes — but
        # `AdvisorConfig` carries no TABLE NAME, so the factory cannot be called. That test
        # proves a name maps to a CALLABLE, never that the callable can be called with what
        # configuration supplies. Adding the field is a loader change.
        raise UnbuildableAdapterError(
            "advice_audit_store=dynamodb needs a table name, and AdvisorConfig has no "
            "field for one. See tasks.md, task 21.1."
        )
    return InMemoryAdviceAuditStore()


def _model(config: AdvisorConfig) -> Model:
    if _selected(config, "model") == "bedrock":
        return build_bedrock_model(
            model_id=config.model_id,
            model_region=config.model_region,
            model_temperature=config.model_temperature,
            model_max_output_tokens=config.model_max_output_tokens,
            model_credential_path=config.model_credential_path,
            request_timeout_seconds=config.request_timeout_seconds,
        )
    return ScriptedModel()


def _clock(config: AdvisorConfig) -> Clock:
    return SystemClock() if _selected(config, "clock") == "system" else FixedClock(_FIXED_AT)


def build_from_config(config: AdvisorConfig) -> object:
    """Assemble the application from validated configuration.

    Separate from `main` so a test can build the whole graph without starting a server. Adapters
    are selected by NAME through `composition.ADAPTER_FACTORIES`, whose agreement with the
    loader's registry is asserted in `tests/unit/test_composition.py` — so a name the
    configuration permits always has something that builds it.
    """
    logger = get_logger(_LOGGER_NAME)
    make_pipeline = build_pipeline_factory(
        identity_for=identity_from_snapshot,
        serving_client=_serving_client(config),
        guardrail=_guardrail(config),
        audit_store=_audit_store(config),
        model=_model(config),
        clock=_clock(config),
        logger=logger,
        red_flag_rules=(),
        forbidden_patterns=config.forbidden_patterns,
        emergency_guidance=config.emergency_guidance_fallback or "",
        system_prompt=load_system_prompt(),
    )
    return build_app(
        run_turn=build_turn_runner(make_pipeline=make_pipeline),
        emergency_guidance=config.emergency_guidance_fallback or "",
        turn_budget_seconds=config.turn_budget_seconds,
        clock=_clock(config),
        logger=logger,
        streaming_enabled=config.streaming_enabled,
    )


def main(argv: list[str] | None = None) -> int:
    """Load configuration, build the app, and serve. Returns an exit status."""
    del argv
    try:
        config = resolve_and_validate(os.environ)
    except ConfigError as error:
        # One message per invalid value, then exit non-zero — never a half-started process.
        # `sys.stderr.write` rather than `print`: the logger emits single-line JSON on
        # stdout, so a stray print would corrupt it. A test asserts no module prints.
        sys.stderr.write(f"configuration is invalid: {error}\n")
        return 2

    configure_logging(config.log_level)
    logger = get_logger(_LOGGER_NAME)
    logger.info("advisor starting", extra={"logLevel": config.log_level})

    app = build_from_config(config)
    app.run()  # type: ignore[attr-defined]
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the container, not the suite
    raise SystemExit(main())
