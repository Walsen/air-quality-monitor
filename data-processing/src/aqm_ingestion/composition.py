"""The composition root: the one place adapters are chosen and ports are injected.

Requirements 26.1, 26.5, 26.10, 27.1, 29.1.

THIS IS THE ONLY MODULE THAT CONSTRUCTS AN ADAPTER. Everything below it takes ports as
parameters,
which is what makes Requirement 27.2's determinism testable at all: the whole service can be
stood up
TWICE from the same configuration and the same Clock, and the two instances share nothing. No
amount
of per-module discipline would give that — a single component reaching for a default would break
it
silently, which is why the task-3.4 architecture check forbids a domain module from importing
``adapters/`` and why the Clock is a required argument everywhere rather than a defaulted one.

ADAPTERS ARE SELECTED FROM A REGISTRY, NEVER BY A CHAIN OF IFS (§1 open/closed).
``ADAPTER_FACTORIES``
maps each port to its named implementations, a test asserts it agrees EXACTLY with the loader's
own
registry — so a name the configuration permits but nothing builds fails in the offline suite
instead
of at the first deployment that selects it — and a second test AST-parses ``build_runtime`` to
prove
it compares no adapter-name literal. That check is what exposed the real gap this task closed:
the
loader had listed ``cognito`` as a registered authenticator since task 26 while no such adapter
existed, and PyJWT sat pinned and unused.

THE FAILURE MODE IS FAIL-FAST (§5). Configuration resolves and validates COMPLETELY
before anything
is constructed, so a rejected value cannot leave a half-built runtime holding an open listener
or a
subscription — Requirement 26.5's "before the Service opens a listener, subscribes to a topic,
or
issues a store call", which is only true if resolution happens first and raises.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from aqm_ingestion.config.loader import (
    ConfigError,
    ServiceConfig,
    read_config_file,
    resolve_and_validate,
)
from aqm_ingestion.observability.logging import configure_logging, get_logger
from aqm_ingestion.observability.metrics import MetricsRegistry
from aqm_ingestion.ports.clock import Clock, SystemClock

_logger = get_logger("composition")


class StartupError(RuntimeError):
    """The service cannot start.

    Raised only from the composition root, never from a component.
    """


def _memory_readings(config: ServiceConfig, clock: Clock) -> object:
    from aqm_ingestion.adapters.memory import InMemoryReadingsStore

    return InMemoryReadingsStore(
        clock=clock,
        retention_days=config.retention_days,
        species_precedence=config.species_precedence,
    )


def _dynamodb_readings(config: ServiceConfig, clock: Clock) -> object:
    from aqm_ingestion.adapters.dynamodb import DynamoDbReadingsStore

    return DynamoDbReadingsStore(
        table_name=_table("readings"),
        clock=clock,
        endpoint_url=_endpoint(),
        retention_days=config.retention_days,
        species_precedence=config.species_precedence,
    )


def _memory_registry(_config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.memory import InMemorySensorRegistryStore

    return InMemorySensorRegistryStore()


def _dynamodb_registry(_config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.dynamodb import DynamoDbSensorRegistryStore

    return DynamoDbSensorRegistryStore(
        table_name=_table("registry"), endpoint_url=_endpoint()
    )


def _memory_archive(_config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.memory import InMemoryRawArchive

    return InMemoryRawArchive()


def _s3_archive(_config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.s3 import S3RawArchive

    return S3RawArchive(bucket=_bucket(), endpoint_url=_endpoint())


def _memory_profiles(_config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.memory import InMemoryProfileStore

    return InMemoryProfileStore()


def _dynamodb_profiles(_config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.dynamodb import DynamoDbProfileStore

    return DynamoDbProfileStore(table_name=_table("profiles"), endpoint_url=_endpoint())


def _memory_forecast(_config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.memory import InMemoryForecastClient

    return InMemoryForecastClient()


def _http_forecast(config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    return HttpForecastClient(
        base_url=_required_setting("AQM_FORECAST_BASE_URL"),
        credential=_read_credential(config.forecast_credential_path, "AQM_FORECAST_API_KEY"),
        timeout_seconds=config.enrichment.timeout_seconds,
    )


def _memory_meteorology(_config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.memory import InMemoryMeteorologyProvider

    return InMemoryMeteorologyProvider()


def _http_meteorology(config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.forecast import HttpMeteorologyProvider

    return HttpMeteorologyProvider(
        base_url=_required_setting("AQM_METEOROLOGY_BASE_URL"),
        credential=_read_credential(config.forecast_credential_path, "AQM_FORECAST_API_KEY"),
        timeout_seconds=config.enrichment.timeout_seconds,
    )


def _local_authenticator(_config: ServiceConfig, _clock: Clock) -> object:
    """The development authenticator, with its credential map supplied at RUNTIME.

    THE DEFAULT IS AN EMPTY MAP, so an unconfigured local authenticator accepts NOTHING. The
    tempting alternative — a built-in development token — would be a committed credential
    (§7) and
    would silently become a way into a deployment that selected this adapter by mistake. Failing
    closed makes that mistake visible on the first request instead of never.

    The format is ``token=user`` pairs separated by commas, read from ``AQM_LOCAL_CREDENTIALS``;
    a malformed entry is skipped rather than guessed at, and the value is never logged.
    """
    from aqm_ingestion.adapters.memory import LocalAuthenticator

    raw = os.environ.get("AQM_LOCAL_CREDENTIALS", "")
    credentials = {
        token.strip(): user.strip()
        for token, _, user in (entry.partition("=") for entry in raw.split(",") if entry)
        if token.strip() and user.strip()
    }
    if not credentials:
        _logger.warning("local_authenticator_has_no_credentials", configured=0)
    return LocalAuthenticator(credentials)


def _cognito_authenticator(_config: ServiceConfig, _clock: Clock) -> object:
    from aqm_ingestion.adapters.auth import CognitoAuthenticator

    return CognitoAuthenticator(
        user_pool_id=_required_setting("AQM_COGNITO_USER_POOL_ID"),
        client_id=_required_setting("AQM_COGNITO_CLIENT_ID"),
        region=_required_setting("AQM_AWS_REGION"),
        key_resolver=_cognito_key_resolver(),
    )


ADAPTER_FACTORIES: Mapping[str, Mapping[str, Callable[[ServiceConfig, Clock], object]]] = {
    "readings_store": {"memory": _memory_readings, "dynamodb": _dynamodb_readings},
    "sensor_registry_store": {"memory": _memory_registry, "dynamodb": _dynamodb_registry},
    "raw_archive": {"memory": _memory_archive, "s3": _s3_archive},
    "profile_store": {"memory": _memory_profiles, "dynamodb": _dynamodb_profiles},
    "forecast_client": {"memory": _memory_forecast, "http": _http_forecast},
    "meteorology_provider": {"memory": _memory_meteorology, "http": _http_meteorology},
    "authenticator": {"local": _local_authenticator, "cognito": _cognito_authenticator},
}
"""Every port's named adapters.

A test asserts this agrees exactly with the loader's registry.
"""


@dataclass(frozen=True, slots=True)
class Runtime:
    """Everything the process needs, with each interface present only if it is enabled.

    ``None`` rather than a disabled stub, so a caller cannot accidentally drive an interface the
    configuration switched off (Requirement 26.10).
    """

    config: ServiceConfig
    clock: Clock
    pipeline: object
    app: object | None
    mqtt_entry: object | None
    feed_entry: object | None
    metrics: MetricsRegistry
    ports: Mapping[str, object]
    """The adapters this runtime built, keyed by port name.

    Exposed because the composition root is the only place they exist as a set: a caller that
    needs to inspect the store — the determinism check comparing what two instances stored, an
    operator tool — would otherwise have to reach inside the pipeline for a collaborator the
    pipeline holds for its own use. This keeps that reaching out of the components.
    """


def build_runtime(config: ServiceConfig, clock: Clock) -> Runtime:
    """Construct the whole service from a VALIDATED configuration and an injected Clock.

    Takes an already-resolved ``ServiceConfig`` rather than an environment, because Requirement
    26.5 puts validation before construction: a function that did both could not guarantee
    nothing
    was built when a value was rejected.
    """
    _logger.info("config_resolved", **config.redacted())

    metrics = MetricsRegistry()
    built = {
        port: _select(port, config.adapters[port])(config, clock)
        for port in sorted(ADAPTER_FACTORIES)
    }

    from aqm_ingestion.ingest.pipeline import (
        IngestPipeline,
        PipelineDependencies,
        PipelineSettings,
    )

    pipeline = IngestPipeline(
        PipelineDependencies(
            archive=built["raw_archive"],  # type: ignore[arg-type]
            readings=built["readings_store"],  # type: ignore[arg-type]
            registry=built["sensor_registry_store"],  # type: ignore[arg-type]
            meteorology=built["meteorology_provider"],  # type: ignore[arg-type]
            clock=clock,
            metrics=metrics,
        ),
        PipelineSettings(
            table_id=config.breakpoint_table,
            nowcast_window_hours=config.nowcast_window_hours,
            species_precedence=config.species_precedence,
        ),
    )

    return Runtime(
        config=config,
        clock=clock,
        pipeline=pipeline,
        app=_build_app(config, clock, built, pipeline) if config.enable_serving else None,
        mqtt_entry=_build_mqtt(config, clock, pipeline) if config.enable_push else None,
        feed_entry=_build_feed(config, clock, built, pipeline) if config.enable_pull else None,
        metrics=metrics,
        ports=built,
    )


def _select(port: str, name: str) -> Callable[[ServiceConfig, Clock], object]:
    """Look a factory up BY NAME. No comparison against any literal (§1 open/closed).

    Raises:
        StartupError: when the configuration names an adapter with no factory. Unreachable while
            the registry-agreement test passes, and kept because a KeyError here would be a raw
            trace from a startup path (§5).
    """
    try:
        return ADAPTER_FACTORIES[port][name]
    except KeyError:
        raise StartupError(f"no factory registered for {port}={name!r}") from None


def _build_app(
    config: ServiceConfig, clock: Clock, built: Mapping[str, object], pipeline: object
) -> object:
    """The FastAPI application, with every port injected."""
    from aqm_ingestion.adapters.memory import InMemoryAuditStore
    from aqm_ingestion.domain.aqi.breakpoints import BreakpointTableRegistry
    from aqm_ingestion.domain.weighting import ConditionWeightingRegistry
    from aqm_ingestion.serving.app import build_app
    from aqm_ingestion.serving.assembler import AssemblySettings, ResponseAssembler
    from aqm_ingestion.serving.enrichment import Enricher
    from aqm_ingestion.serving.geo import GeoSelector
    from aqm_ingestion.serving.profiles import ProfileService

    tables = BreakpointTableRegistry.with_defaults()
    audit = InMemoryAuditStore()
    profiles = ProfileService(
        profiles=built["profile_store"],  # type: ignore[arg-type]
        audit=audit,
        limits=config.profile_limits,
    )
    selector = GeoSelector(
        registry=built["sensor_registry_store"],  # type: ignore[arg-type]
        readings=built["readings_store"],  # type: ignore[arg-type]
        clock=clock,
        settings=config.selection,
    )
    enricher = Enricher(
        client=built["forecast_client"],  # type: ignore[arg-type]
        clock=clock,
        settings=config.enrichment,
    )
    assembler = ResponseAssembler(
        profiles=profiles,
        selector=selector,
        enricher=enricher,
        weightings=ConditionWeightingRegistry.with_defaults(),
        breakpoints=tables,
        clock=clock,
        settings=AssemblySettings(
            table_id=config.breakpoint_table,
            species_precedence=config.species_precedence,
            guardrails=None,
        ),
    )
    return build_app(
        authenticator=built["authenticator"],  # type: ignore[arg-type]
        clock=clock,
        assembler=assembler,
        profiles=profiles,
        readings=built["readings_store"],  # type: ignore[arg-type]
        registry=built["sensor_registry_store"],  # type: ignore[arg-type]
        audit=audit,
        breakpoints=tables,
        guardrails=None,
        settings=config.serving,
    )


def _build_mqtt(config: ServiceConfig, clock: Clock, pipeline: object) -> object:
    """The push entry point. Constructed but NOT connected — connecting is the caller's step.

    THE TWO TRANSPORTS ARE NOT IN THE LOADER'S ADAPTER REGISTRY, deliberately. That registry
    covers
    the seven ports whose implementation is a genuine per-deployment CHOICE — a store can be
    memory
    or DynamoDB in the same topology. A transport is not a choice in that sense: enabling the
    push
    interface IS choosing MQTT, so a second name would only ever say "the one you already asked
    for". What does vary is whether a broker is reachable, so the real transport is used when
    one is
    configured and the scripted one stands in otherwise, which is what lets a local stack start
    with the push path enabled and nothing to connect to.
    """
    from aqm_ingestion.ingest.mqtt_entry import MqttSettings, MqttSubscriber

    transport: object
    host = os.environ.get("AQM_MQTT_HOST", "").strip()
    if host:
        from aqm_ingestion.adapters.mqtt import PahoMqttTransport

        transport = PahoMqttTransport(
            host=host,
            port=int(os.environ.get("AQM_MQTT_PORT", "8883")),
            client_id=os.environ.get("AQM_MQTT_CLIENT_ID", "aqm-ingestion"),
            ca_cert_path=_required_setting("AQM_MQTT_CA_CERT"),
            client_cert_path=_required_setting("AQM_MQTT_CLIENT_CERT"),
            client_key_path=_required_setting("AQM_MQTT_CLIENT_KEY"),
        )
    else:
        from aqm_ingestion.adapters.memory import ScriptedMqttTransport

        _logger.warning("mqtt_transport_is_scripted", reason="AQM_MQTT_HOST is not set")
        transport = ScriptedMqttTransport([])

    return MqttSubscriber(
        transport=transport,
        pipeline=pipeline,  # type: ignore[arg-type]
        clock=clock,
        settings=MqttSettings(enabled=config.enable_push),
    )


def _build_feed(
    config: ServiceConfig, clock: Clock, built: Mapping[str, object], pipeline: object
) -> object:
    """The pull entry point, with the same reachability rule as the transport above."""
    from aqm_ingestion.ingest.feed_entry import FeedPoller, FeedSettings

    client: object
    base_url = os.environ.get("AQM_FEED_BASE_URL", "").strip()
    if base_url:
        from aqm_ingestion.adapters.http_feed import HttpFeedClient

        client = HttpFeedClient(
            base_url=base_url,
            credential=_read_credential(config.feed_credential_path, "AQM_FEED_API_KEY"),
        )
    else:
        from aqm_ingestion.adapters.memory import ScriptedFeedClient

        _logger.warning("feed_client_is_scripted", reason="AQM_FEED_BASE_URL is not set")
        client = ScriptedFeedClient()

    return FeedPoller(
        client=client,
        pipeline=pipeline,  # type: ignore[arg-type]
        readings=built["readings_store"],  # type: ignore[arg-type]
        registry=built["sensor_registry_store"],  # type: ignore[arg-type]
        clock=clock,
        settings=FeedSettings(),
    )


def _endpoint() -> str | None:
    """The store endpoint override, for local emulation. Absent in a real deployment."""
    return os.environ.get("AQM_AWS_ENDPOINT_URL")


def _table(suffix: str) -> str:
    return os.environ.get(f"AQM_TABLE_{suffix.upper()}", f"aqm-{suffix}")


def _bucket() -> str:
    return os.environ.get("AQM_BUCKET_RAW", "aqm-raw")


def _required_setting(name: str) -> str:
    """A setting an adapter cannot work without.

    Raises:
        StartupError: naming the VARIABLE, never a value — the same rule the Config_Loader
        follows,
            since several of these name credential locations.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise StartupError(f"{name} is required by the selected adapter but is not set")
    return value


def _read_credential(path: str | None, env_var: str) -> str:
    """Resolve a credential from a runtime path or the environment, and nothing else (§7).

    Raises:
        StartupError: naming the SOURCE that was empty, never the value.
    """
    if path is not None:
        content = Path(path).read_text(encoding="utf-8").strip()
        if not content:
            raise StartupError(f"the credential file for {env_var} is empty")
        return content
    value = os.environ.get(env_var, "").strip()
    if not value:
        raise StartupError(
            f"{env_var} is not set and no runtime credential path was configured"
        )
    return value


def _cognito_key_resolver() -> Callable[[str], object]:
    """Build the JWKS-backed signing-key resolver.

    Deferred behind a callable so the verifier itself needs no HTTP client and its whole
    verification path stays testable offline (see the adapter's own docstring).
    """
    from jwt import PyJWKClient

    client = PyJWKClient(
        f"{_required_setting('AQM_COGNITO_ISSUER')}/.well-known/jwks.json"
    )

    def resolve(token: str) -> object:
        return client.get_signing_key_from_jwt(token).key

    return resolve


def main(
    env: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    serve: bool = True,
) -> int:
    """Resolve configuration, build the runtime, and optionally serve.

    ``serve`` is a parameter so the whole wiring path is testable WITHOUT binding a port — the
    split Service 1 settled on at its own task 20.6, and the reason the composition tests can
    assert on a fully built runtime.

    Returns:
        0 on success, 1 on any startup failure, with one logged message per fault (§5).
    """
    environment = dict(os.environ if env is None else env)
    configure_logging(environment.get("AQM_LOG_LEVEL", "info"))

    try:
        file_data = read_config_file(environment.get("AQM_CONFIG_FILE"))
        config = resolve_and_validate(
            environment, file_data, credential_exists=_credential_exists
        )
    except ConfigError as rejected:
        # One message per invalid value: the loader accumulated them all, so splitting here
        # keeps
        # Requirement 26.3's one-per-value promise visible in the log.
        for problem in str(rejected).split("; "):
            _logger.error("config_rejected", problem=problem)
        return 1

    try:
        runtime = build_runtime(config, clock or SystemClock())
    except StartupError as failure:
        _logger.error("startup_failed", problem=str(failure))
        return 1

    if serve and runtime.app is not None:
        return _serve(runtime)
    return 0


def _credential_exists(path: str) -> bool:
    """Whether a credential resolves, WITHOUT reading it (Requirement 26.8).

    The loader takes this as a predicate precisely so it never holds credential contents: a
    loader
    that cannot see a secret cannot log one.
    """
    return Path(path).is_file()


def _serve(runtime: Runtime) -> int:
    """Bind the listener. Separated so `main(serve=False)` exercises everything but this."""
    import uvicorn

    uvicorn.run(
        cast("Any", runtime.app),
        host=os.environ.get("AQM_LISTEN_HOST", "127.0.0.1"),
        port=int(os.environ.get("AQM_LISTEN_PORT", "8000")),
        log_config=None,
    )
    return 0


def _utc_now() -> dt.datetime:
    """The process-edge clock read, kept here so no domain module needs one (§2)."""
    return dt.datetime.now(dt.UTC)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
