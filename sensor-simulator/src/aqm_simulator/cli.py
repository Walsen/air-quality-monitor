"""The entry point: configuration to a running interface.

Wires the whole service in one place (Requirement 15.6, 15.7):

    config -> logger -> profile registry -> random streams + clock -> swarm
           -> pipeline -> the SELECTED interfaces only

Only the selected interface is activated, defaulting to ``rest`` (Requirements
14.1, 13.1), so a REST-only run needs no MQTT settings and an MQTT-only run needs
no API key. Every startup fault is raised as :class:`StartupError` and turned into
a non-zero exit with one message per affected value, before any request is served
or any record generated (Requirements 14.9, 13.9, engineering-practices §5).

Splitting :func:`build_runtime` from :func:`main` keeps the wiring testable: the
tests assemble a runtime and drive the ASGI app in-process without binding a port.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from aqm_simulator.config.credentials import ApiKeyError, resolve_api_key
from aqm_simulator.config.loader import ConfigError, resolve_config, validate_config
from aqm_simulator.config.seed import select_seed
from aqm_simulator.geography.registry import ProfileRegistrationError, ProfileRegistry
from aqm_simulator.interfaces.mqtt.credentials import (
    SensorCredentials,
    resolve_sensor_credentials,
    validate_credentials,
)
from aqm_simulator.interfaces.rest import build_app
from aqm_simulator.observability.logging import configure_logging, get_logger
from aqm_simulator.pipeline.publish import PublishPipeline
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.scenarios.engine import ScenarioEngine
from aqm_simulator.signal.faults import FaultController
from aqm_simulator.swarm.factory import build_swarm
from aqm_simulator.time.clock import SystemClock

_PERMITTED_INTERFACES = ("rest", "mqtt", "both")
_DEFAULT_CERT_TEMPLATE = "certs/{SiteCode}/client.crt"
_DEFAULT_KEY_TEMPLATE = "certs/{SiteCode}/client.key"
_DEFAULT_CA_PATH = "certs/ca.crt"

NowFn = Any  # Callable[[], dt.datetime]; kept loose so a test can pass a lambda


class StartupError(RuntimeError):
    """A startup fault that must exit non-zero before serving (Req 14.9, 13.9)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Runtime:
    """Everything the process needs, with only the selected interfaces built."""

    config: Any
    pipeline: PublishPipeline
    app: FastAPI | None
    mqtt_credentials: dict[str, SensorCredentials] | None


def _system_now() -> dt.datetime:
    return SystemClock().now()


def build_runtime(
    env: dict[str, str],
    now: NowFn = _system_now,
    config_file: dict[str, Any] | None = None,
) -> Runtime:
    """Resolve configuration and build only the selected interfaces."""
    try:
        config = resolve_config(env=env, file_data=config_file or {})
    except ConfigError as error:
        raise StartupError([str(error)]) from error

    problems = validate_config(config)
    if problems:
        raise StartupError(problems)
    if config.interface not in _PERMITTED_INTERFACES:
        raise StartupError(
            [
                f"interface must be one of {', '.join(_PERMITTED_INTERFACES)}; "
                f"got {config.interface!r}"
            ]
        )

    configure_logging(config.log_level)
    logger = get_logger("startup")

    registry = ProfileRegistry.with_builtins()
    try:
        if config.profile_overrides:
            profile = registry.register_declared(config.profile_overrides)
        else:
            profile = registry.get(config.profile_name)
    except (ProfileRegistrationError, KeyError, ValueError) as error:
        raise StartupError([f"geography profile could not be resolved: {error}"]) from error

    seed, was_drawn = select_seed(config.seed)
    factory = RandomStreamFactory(seed=seed)
    swarm = build_swarm(size=config.swarm_size, profile=profile, factory=factory)
    pipeline = PublishPipeline(
        swarm=swarm,
        profile=profile,
        factory=factory,
        scenario_engine=ScenarioEngine(
            entries=[], swarm_site_codes={s.site_code for s in swarm}
        ),
        fault_controller=FaultController(windows=[]),
        publish_minutes=config.publish_minutes,
    )

    wants_rest = config.interface in ("rest", "both")
    wants_mqtt = config.interface in ("mqtt", "both")

    app = _build_rest(pipeline, env, now, config.retention_days) if wants_rest else None
    credentials = _build_mqtt(swarm, env) if wants_mqtt else None

    # The seed is disclosed so a run can be replayed; no secret is ever logged (§7).
    logger.info(
        "simulator_configured",
        interface=config.interface,
        swarm_size=config.swarm_size,
        profile=config.profile_name,
        publish_minutes=config.publish_minutes,
        seed=seed,
        seed_was_drawn=was_drawn,
    )
    return Runtime(config=config, pipeline=pipeline, app=app, mqtt_credentials=credentials)


def _build_rest(
    pipeline: PublishPipeline, env: dict[str, str], now: NowFn, retention_days: int
) -> FastAPI:
    """Activate the REST interface, requiring the API key secret (Req 14.9)."""
    try:
        api_key = resolve_api_key(env)
    except ApiKeyError as error:
        # the message names the configuration value, never the value itself (§7)
        raise StartupError([f"the REST API key secret is invalid: {error}"]) from error
    try:
        return build_app(
            pipeline=pipeline, api_key=api_key, now=now, retention_days=retention_days
        )
    except ValueError as error:
        raise StartupError([str(error)]) from error


def _build_mqtt(
    swarm: list[Any], env: dict[str, str]
) -> dict[str, SensorCredentials]:
    """Activate the MQTT interface, validating every credential path (Req 13.9)."""
    ca_path = Path(env.get("AQM_MQTT_CA_PATH", _DEFAULT_CA_PATH))
    cert_template = env.get("AQM_MQTT_CERT_TEMPLATE", _DEFAULT_CERT_TEMPLATE)
    key_template = env.get("AQM_MQTT_KEY_TEMPLATE", _DEFAULT_KEY_TEMPLATE)
    site_codes = [s.site_code for s in swarm]

    # A template without the placeholder resolves every sensor to the same file,
    # which would break per-sensor identity (Req 13.3), so accept a literal path
    # only when it is the same for all — otherwise report it.
    try:
        credentials = resolve_sensor_credentials(
            site_codes=site_codes,
            cert_template=cert_template,
            key_template=key_template,
        )
    except ValueError:
        credentials = {
            code: SensorCredentials(Path(cert_template), Path(key_template))
            for code in site_codes
        }

    problems = validate_credentials(
        ca_path=ca_path,
        credentials={c: (v.certificate, v.private_key) for c, v in credentials.items()},
    )
    if problems:
        raise StartupError([str(p) for p in problems])
    return credentials


def main(
    env: dict[str, str] | None = None,
    now: NowFn = _system_now,
    serve: bool = True,
) -> int:
    """Start the simulator, returning the process exit code.

    ``serve=False`` performs the whole startup path and returns without binding a
    port, which is what the tests and the container's health smoke check use.
    """
    import os

    environment = env if env is not None else dict(os.environ)
    try:
        runtime = build_runtime(env=environment, now=now)
    except StartupError as error:
        # Never a raw traceback at a top-level boundary (§5); one line per fault.
        configure_logging("info")
        logger = get_logger("startup")
        for problem in error.problems:
            logger.error("startup_failed", problem=problem)
        return 1

    if not serve:
        return 0

    if runtime.app is not None:
        import uvicorn

        host = environment.get("AQM_LISTEN_HOST", "127.0.0.1")
        port = int(environment.get("AQM_LISTEN_PORT", "8000"))
        uvicorn.run(runtime.app, host=host, port=port, log_config=None)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
