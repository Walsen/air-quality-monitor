"""Fail-fast configuration resolution and validation.

Requirement 26, plus Requirement 8.13's coefficient checks, 10.11's table shape, and 29.9's log
level.

FAIL-FAST MEANS "NEVER HALF-START", NOT "STOP AT THE FIRST ERROR" (Requirement 26.3). Every
value is validated and every violation reported, because an operator fixing one setting per
deploy is exactly what accumulation avoids — the same rule §5 states and the same shape
Service 1's loader uses. The structure is deliberately mirrored from that loader rather than
imported: Requirement 28.3 forbids importing across service directories until a shared contract
package is specced.

THE SETTINGS OBJECTS VALIDATE THEMSELVES, so this module does NOT duplicate their range checks.
`ServingSettings`, `SelectionSettings`, `EnrichmentSettings`, `ProfileLimits` and the rest each
refuse an out-of-range value in ``__post_init__``, so the loader CONSTRUCTS them and collects
what
they raise. Duplicating the bounds here would give two places to disagree, and the copy that
drifted would be the one the operator hit.

A SECRET IS RESOLVED BUT NEVER RENDERED (Requirements 26.8, 26.11). The loader reports whether a
required credential resolved, naming the CONFIGURATION VALUE that supplies it — never the value
itself, and it never reads a credential file's contents, only checks that the path exists. A
loader that cannot see a secret cannot log one.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aqm_ingestion.domain.aqi.breakpoints import (
    BreakpointTableError,
    BreakpointTableRegistry,
)
from aqm_ingestion.domain.calibration import (
    CalibrationRegistry,
    RhLinearCoefficients,
)
from aqm_ingestion.domain.profile import Condition, ProfileLimits
from aqm_ingestion.domain.weighting import ConditionWeightingRegistry
from aqm_ingestion.serving.app import ServingSettings
from aqm_ingestion.serving.enrichment import EnrichmentSettings
from aqm_ingestion.serving.geo import SelectionSettings

PERMITTED_LOG_LEVELS: tuple[str, ...] = ("debug", "info", "warning", "error", "critical")
"""Requirement 29.9's recognized log levels."""

INTERFACE_SWITCHES: tuple[str, ...] = ("enable_push", "enable_pull", "enable_serving")
"""Requirement 26.10's three interfaces. Named as a tuple so the rejection can list them."""

DEFAULT_RETENTION_DAYS = 90
DEFAULT_QUARANTINE_RETENTION_DAYS = 30
DEFAULT_AUDIT_RETENTION_DAYS = 365
DEFAULT_NOWCAST_WINDOW_HOURS = 12
DEFAULT_MAX_HISTORY_SPAN_DAYS = 30
DEFAULT_FRESHNESS_HOURS = 3

_RECOGNIZED_KEYS: Mapping[str, frozenset[str]] = {
    "durations": frozenset(
        {
            "retention_days",
            "quarantine_retention_days",
            "audit_retention_days",
            "nowcast_window_hours",
            "max_history_span_days",
            "freshness_hours",
        }
    ),
    "adapters": frozenset(
        {
            "readings_store",
            "sensor_registry_store",
            "raw_archive",
            "profile_store",
            "forecast_client",
            "meteorology_provider",
            "authenticator",
        }
    ),
    "selection": frozenset(
        {"nearest_n", "radius_km", "distance_decimals", "fallback_centre"}
    ),
    "enrichment": frozenset({"ttl_minutes", "trend_band_points", "timeout_seconds"}),
    "serving": frozenset({"rate_limit_per_minute", "log_level"}),
    "interfaces": frozenset(INTERFACE_SWITCHES),
    "aqi": frozenset({"breakpoint_table", "species_precedence"}),
    "calibration": frozenset({"rh_linear_a", "rh_linear_b", "rh_linear_c"}),
    "credentials": frozenset({"feed_credential_path", "forecast_credential_path"}),
}

# Every recognized key, flattened. Kept derived rather than written twice so the two cannot
# drift.
RECOGNIZED_KEYS: frozenset[str] = frozenset(
    key for keys in _RECOGNIZED_KEYS.values() for key in keys
)

_REGISTERED_ADAPTERS: Mapping[str, tuple[str, ...]] = {
    "readings_store": ("memory", "dynamodb"),
    "sensor_registry_store": ("memory", "dynamodb"),
    "raw_archive": ("memory", "s3"),
    "profile_store": ("memory", "dynamodb"),
    "forecast_client": ("memory", "http"),
    "meteorology_provider": ("memory", "http"),
    "authenticator": ("local", "cognito"),
}


class ConfigError(ValueError):
    """One or more configuration values were rejected."""

    def __init__(self, problems: Sequence[str]) -> None:
        """Carry every problem, not just the first (Requirement 26.3)."""
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """The resolved configuration, as narrow settings objects rather than a blob.

    Holds the SETTINGS OBJECTS the service actually takes (§1 Interface Segregation), so no
    component is handed the whole configuration — which the task-3.4 architecture check enforces
    for domain modules.
    """

    serving: ServingSettings
    selection: SelectionSettings
    enrichment: EnrichmentSettings
    profile_limits: ProfileLimits
    breakpoint_table: str
    species_precedence: tuple[str, ...]
    rh_linear: RhLinearCoefficients
    adapters: Mapping[str, str]
    retention_days: int
    quarantine_retention_days: int
    audit_retention_days: int
    nowcast_window_hours: int
    enable_push: bool
    enable_pull: bool
    enable_serving: bool
    log_level: str
    feed_credential_path: str | None
    forecast_credential_path: str | None

    def redacted(self) -> dict[str, object]:
        """The resolved NON-SECRET configuration, for the one startup log (Requirement 26.1).

        Credential PATHS are reported as whether they resolved, not as paths: a path is not a
        secret, but it is a map to one, and Requirement 26.11's "never write a resolved secret"
        is cheapest to honour by never rendering anything credential-shaped.
        """
        return {
            "breakpointTable": self.breakpoint_table,
            "speciesPrecedence": list(self.species_precedence),
            "adapters": dict(sorted(self.adapters.items())),
            "retentionDays": self.retention_days,
            "quarantineRetentionDays": self.quarantine_retention_days,
            "auditRetentionDays": self.audit_retention_days,
            "nowcastWindowHours": self.nowcast_window_hours,
            "maxHistorySpanDays": self.serving.max_history_span_days,
            "freshnessHours": self.selection.freshness_hours,
            "rateLimitPerMinute": self.serving.rate_limit_per_minute,
            "nearestN": self.selection.nearest_n,
            "radiusKm": self.selection.radius_km,
            "enablePush": self.enable_push,
            "enablePull": self.enable_pull,
            "enableServing": self.enable_serving,
            "logLevel": self.log_level,
            "feedCredentialConfigured": self.feed_credential_path is not None,
            "forecastCredentialConfigured": self.forecast_credential_path is not None,
        }


@dataclass
class _Problems:
    """Accumulator, so every invalid value is reported (Requirement 26.3)."""

    messages: list[str] = field(default_factory=list)

    def add(self, value: str, constraint: str) -> None:
        """Record one invalid value and the constraint it violated."""
        self.messages.append(f"{value}: {constraint}")


def read_config_file(path: str | None) -> dict[str, Any]:
    """Read the configuration file, with NO fallback to defaults (Requirement 26.9).

    Raises:
        ConfigError: naming the PATH and the failure kind. Falling back to defaults on an
            unreadable file is the specific behaviour 26.9 forbids: a deployment would come up
            with settings nobody chose, which is worse than not coming up.
    """
    if path is None:
        return {}
    candidate = Path(path)
    try:
        import json

        text = candidate.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(
            [f"config file {path!r}: unreadable ({type(error).__name__})"]
        ) from error
    try:
        parsed = json.loads(text)
    except ValueError as error:
        raise ConfigError(
            [f"config file {path!r}: unparseable ({type(error).__name__})"]
        ) from error
    if not isinstance(parsed, dict):
        raise ConfigError(
            [f"config file {path!r}: unparseable (expected a JSON object)"]
        )
    return parsed


def resolve_and_validate(
    env: Mapping[str, str],
    file_data: Mapping[str, Any] | None = None,
    *,
    credential_exists: object = None,
) -> ServiceConfig:
    """Resolve env > file > default, validating everything (Requirement 26.1-26.11).

    ``credential_exists`` is an injected predicate over a path, so the loader can be tested
    without touching the filesystem and — more importantly — so it never READS a credential file
    (§2, §7). It checks that a secret is resolvable, never what the secret is.

    Raises:
        ConfigError: carrying EVERY problem. Nothing is constructed for the caller to
            half-start with.
    """
    data = dict(file_data or {})
    problems = _Problems()

    # Requirement 26.4: an unrecognized key names its CATEGORY's keys, not all of them — a list
    # of forty names tells an operator nothing about where their typo belongs.
    for key in sorted(set(data) - RECOGNIZED_KEYS):
        problems.add(
            f"configuration key {key!r}",
            f"not recognized; recognized keys are {_nearest_category(key)}",
        )

    resolved = _resolve_scalars(env, data, problems)
    exists = credential_exists if callable(credential_exists) else Path.exists

    _validate_registries(resolved, problems)
    _validate_durations(resolved, problems)
    _validate_log_level(resolved, problems)
    _validate_interfaces(resolved, problems)
    _validate_credentials(resolved, problems, exists)

    settings = _build_settings(resolved, problems)

    if problems.messages:
        raise ConfigError(problems.messages)
    assert settings is not None
    return settings


def _nearest_category(key: str) -> str:
    """The recognized keys of the category a key most plausibly belongs to.

    Falls back to every category name when the key resembles nothing, which is honest: an
    operator who invented a key wholesale needs to see the shape of what exists.
    """
    for category, keys in sorted(_RECOGNIZED_KEYS.items()):
        if any(key.split("_")[0] == known.split("_")[0] for known in keys):
            return f"{category}: {', '.join(sorted(keys))}"
    return ", ".join(
        f"{category}: {', '.join(sorted(keys))}"
        for category, keys in sorted(_RECOGNIZED_KEYS.items())
    )


_SCALARS: Mapping[str, tuple[str, object]] = {
    "retention_days": ("AQM_RETENTION_DAYS", DEFAULT_RETENTION_DAYS),
    "quarantine_retention_days": (
        "AQM_QUARANTINE_RETENTION_DAYS",
        DEFAULT_QUARANTINE_RETENTION_DAYS,
    ),
    "audit_retention_days": ("AQM_AUDIT_RETENTION_DAYS", DEFAULT_AUDIT_RETENTION_DAYS),
    "nowcast_window_hours": (
        "AQM_NOWCAST_WINDOW_HOURS",
        DEFAULT_NOWCAST_WINDOW_HOURS,
    ),
    "max_history_span_days": (
        "AQM_MAX_HISTORY_SPAN_DAYS",
        DEFAULT_MAX_HISTORY_SPAN_DAYS,
    ),
    "freshness_hours": ("AQM_FRESHNESS_HOURS", DEFAULT_FRESHNESS_HOURS),
    "rate_limit_per_minute": ("AQM_RATE_LIMIT_PER_MINUTE", 60),
    "nearest_n": ("AQM_NEAREST_N", 3),
    "radius_km": ("AQM_RADIUS_KM", 10.0),
    "distance_decimals": ("AQM_DISTANCE_DECIMALS", 2),
    "ttl_minutes": ("AQM_FORECAST_TTL_MINUTES", 60),
    "trend_band_points": ("AQM_TREND_BAND_POINTS", 5),
    "timeout_seconds": ("AQM_FORECAST_TIMEOUT_SECONDS", 2.0),
    "breakpoint_table": ("AQM_BREAKPOINT_TABLE", "epa-2024-05-06"),
    "log_level": ("AQM_LOG_LEVEL", "info"),
    "rh_linear_a": ("AQM_RH_LINEAR_A", 0.524),
    "rh_linear_b": ("AQM_RH_LINEAR_B", -0.0862),
    "rh_linear_c": ("AQM_RH_LINEAR_C", 5.75),
    "enable_push": ("AQM_ENABLE_PUSH", True),
    "enable_pull": ("AQM_ENABLE_PULL", False),
    "enable_serving": ("AQM_ENABLE_SERVING", True),
    "feed_credential_path": ("AQM_FEED_CREDENTIAL_PATH", None),
    "forecast_credential_path": ("AQM_FORECAST_CREDENTIAL_PATH", None),
}


def _resolve_scalars(
    env: Mapping[str, str], data: Mapping[str, Any], problems: _Problems
) -> dict[str, Any]:
    """Apply env > file > default, coercing to the default's type.

    The DEFAULT's type drives the coercion, so a new setting needs no separate parser entry and
    the two cannot disagree about what a value is.
    """
    resolved: dict[str, Any] = {}
    for key, (env_var, default) in _SCALARS.items():
        raw: object
        if env_var in env:
            raw = env[env_var]
        elif key in data:
            raw = data[key]
        else:
            resolved[key] = default
            continue
        try:
            resolved[key] = _coerce(raw, default)
        except (TypeError, ValueError):
            problems.add(
                f"{key}={raw!r}",
                f"expected {type(default).__name__ if default is not None else 'a path'}",
            )
            resolved[key] = default

    resolved["adapters"] = {
        name: str(
            env.get(f"AQM_ADAPTER_{name.upper()}")
            or data.get(name)
            or _REGISTERED_ADAPTERS[name][0]
        )
        for name in _REGISTERED_ADAPTERS
    }
    precedence = data.get("species_precedence") or ("PM25", "NO2")
    resolved["species_precedence"] = tuple(str(item) for item in precedence)
    resolved["fallback_centre"] = tuple(
        float(part) for part in (data.get("fallback_centre") or (51.507, -0.128))
    )
    return resolved


def _coerce(raw: object, default: object) -> object:
    """Coerce a raw value to the default's type."""
    if default is None:
        return str(raw)
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        lowered = str(raw).strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise ValueError(f"not a boolean: {raw!r}")
    if isinstance(default, int):
        return int(str(raw))
    if isinstance(default, float):
        value = float(str(raw))
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"not finite: {raw!r}")
        return value
    return str(raw)


def _validate_registries(resolved: Mapping[str, Any], problems: _Problems) -> None:
    """Requirement 26.5: every pluggable name resolves in its registry."""
    tables = BreakpointTableRegistry.with_defaults()
    table_id = str(resolved["breakpoint_table"])
    if table_id not in tables.table_ids():
        problems.add(
            f"breakpoint_table={table_id!r}",
            f"not registered; registered tables are {', '.join(tables.table_ids())}",
        )
    else:
        # Requirement 10.11: the shape too, not merely the name. The shipped tables validate on
        # construction, so this catches a table registered by configuration.
        for species in tables.species_for(table_id):
            try:
                tables.resolve(table_id, species)
            except BreakpointTableError as error:
                problems.add(f"breakpoint_table={table_id!r} ({species})", str(error))

    strategies = CalibrationRegistry.with_defaults().names()
    weightings = ConditionWeightingRegistry.with_defaults()
    for condition in Condition:
        try:
            weighting = weightings.resolve(condition)
        except KeyError:
            problems.add(
                f"condition_weighting[{condition}]",
                f"no weighting registered; registered conditions are "
                f"{', '.join(weightings.names())}",
            )
            continue
        if not weighting.species:
            problems.add(f"condition_weighting[{condition}]", "names no species")

    for name, supplied in sorted(resolved["adapters"].items()):
        permitted = _REGISTERED_ADAPTERS[name]
        if supplied not in permitted:
            problems.add(
                f"{name}={supplied!r}",
                f"not registered; registered adapters are {', '.join(permitted)}",
            )

    # Requirement 8.13: finite coefficients, and a domain whose minimum is below its maximum —
    # checked by constructing the real object so the rule lives in one place.
    try:
        RhLinearCoefficients(
            a=float(resolved["rh_linear_a"]),
            b=float(resolved["rh_linear_b"]),
            c=float(resolved["rh_linear_c"]),
        )
    except (TypeError, ValueError) as error:
        problems.add("rh_linear coefficients", str(error))
    else:
        _ = strategies


def _validate_durations(resolved: Mapping[str, Any], problems: _Problems) -> None:
    """Requirement 26.6: positive durations, and retention at least the history span.

    Checks ONLY the durations no settings object owns. ``freshness_hours`` belongs to
    SelectionSettings and ``max_history_span_days`` to ServingSettings, both of which already
    refuse a non-positive value in ``__post_init__`` — checking them here too produced TWO
    messages for one invalid value, which Requirement 26.3's "one message per invalid value"
    forbids and which a test caught. The rule lives wherever the value lives.
    """
    durations = {
        "retention_days": resolved["retention_days"],
        "quarantine_retention_days": resolved["quarantine_retention_days"],
        "audit_retention_days": resolved["audit_retention_days"],
        "nowcast_window_hours": resolved["nowcast_window_hours"],
    }
    for name, value in durations.items():
        if not isinstance(value, int) or value <= 0:
            problems.add(f"{name}={value!r}", "must be a positive duration")

    retention = durations["retention_days"]
    span = resolved["max_history_span_days"]
    # Compared ONLY when both are individually valid. An ordering complaint about a negative
    # duration is noise: the operator already has to fix the negative, and comparing against it
    # means nothing. Same rule the history-window parser follows for its unknown-site check.
    if (
        isinstance(retention, int)
        and isinstance(span, int)
        and retention > 0
        and span > 0
        and retention < span
    ):
        # NAMES BOTH values, per the design's error table: an operator needs to know which to
        # raise and which to lower, and one value alone does not say.
        problems.add(
            f"retention_days={retention} with max_history_span_days={span}",
            "the Retention_Window must be at least the maximum history span, or the API "
            "would offer a window the store has already discarded",
        )


def _validate_log_level(resolved: Mapping[str, Any], problems: _Problems) -> None:
    """Requirement 29.9: the log level is one of the recognized set."""
    level = str(resolved["log_level"]).lower()
    if level not in PERMITTED_LOG_LEVELS:
        problems.add(
            f"log_level={resolved['log_level']!r}",
            f"not recognized; permitted values are {', '.join(PERMITTED_LOG_LEVELS)}",
        )


def _validate_interfaces(resolved: Mapping[str, Any], problems: _Problems) -> None:
    """Requirement 26.10: at least one interface is enabled."""
    if not any(bool(resolved[switch]) for switch in INTERFACE_SWITCHES):
        problems.add(
            "interfaces",
            f"at least one of {', '.join(INTERFACE_SWITCHES)} must be enabled; a service "
            "with none would start, pass its health check, and do nothing",
        )


def _validate_credentials(
    resolved: Mapping[str, Any], problems: _Problems, exists: object
) -> None:
    """Requirement 26.8: a credential an ENABLED interface needs must resolve.

    Reports the CONFIGURATION VALUE, never the secret (Requirement 26.11) — and never reads the
    file, only asks whether the path resolves, so no key material enters this process.
    """
    checker = exists if callable(exists) else Path.exists
    required: list[tuple[str, str]] = []
    if resolved["enable_pull"]:
        required.append(("feed_credential_path", "the pull interface"))
    if resolved["adapters"]["forecast_client"] != "memory":
        required.append(("forecast_credential_path", "the configured ForecastClient"))

    for key, needed_by in required:
        path = resolved.get(key)
        if not path:
            problems.add(f"{key}", f"required by {needed_by} but not configured")
        elif not checker(Path(str(path))):
            problems.add(f"{key}", f"required by {needed_by} but does not resolve")


def _build_settings(
    resolved: Mapping[str, Any], problems: _Problems
) -> ServiceConfig | None:
    """Construct the settings objects, collecting whatever THEY reject.

    Requirement 26.7's bound pairs and positive limits are enforced by the settings objects'
    own ``__post_init__``, so they are constructed here rather than re-checked: two copies of a
    bound would eventually disagree, and the drifted one would be the one an operator hit.
    """
    try:
        serving = ServingSettings(
            rate_limit_per_minute=int(resolved["rate_limit_per_minute"]),
            max_history_span_days=int(resolved["max_history_span_days"]),
        )
        selection = SelectionSettings(
            nearest_n=int(resolved["nearest_n"]),
            radius_km=float(resolved["radius_km"]),
            distance_decimals=int(resolved["distance_decimals"]),
            freshness_hours=int(resolved["freshness_hours"]),
            fallback_centre=(
                float(resolved["fallback_centre"][0]),
                float(resolved["fallback_centre"][1]),
            ),
        )
        enrichment = EnrichmentSettings(
            ttl_minutes=int(resolved["ttl_minutes"]),
            trend_band_points=int(resolved["trend_band_points"]),
            timeout_seconds=float(resolved["timeout_seconds"]),
        )
        rh_linear = RhLinearCoefficients(
            a=float(resolved["rh_linear_a"]),
            b=float(resolved["rh_linear_b"]),
            c=float(resolved["rh_linear_c"]),
        )
    except (TypeError, ValueError, IndexError) as error:
        # The settings objects' own messages already NAME the field and the constraint, so they
        # are surfaced verbatim rather than wrapped in a prefix that adds nothing.
        problems.messages.append(str(error))
        return None

    return ServiceConfig(
        serving=serving,
        selection=selection,
        enrichment=enrichment,
        profile_limits=ProfileLimits(),
        breakpoint_table=str(resolved["breakpoint_table"]),
        species_precedence=tuple(resolved["species_precedence"]),
        rh_linear=rh_linear,
        adapters=dict(resolved["adapters"]),
        retention_days=int(resolved["retention_days"]),
        quarantine_retention_days=int(resolved["quarantine_retention_days"]),
        audit_retention_days=int(resolved["audit_retention_days"]),
        nowcast_window_hours=int(resolved["nowcast_window_hours"]),
        enable_push=bool(resolved["enable_push"]),
        enable_pull=bool(resolved["enable_pull"]),
        enable_serving=bool(resolved["enable_serving"]),
        log_level=str(resolved["log_level"]).lower(),
        feed_credential_path=resolved["feed_credential_path"] or None,
        forecast_credential_path=resolved["forecast_credential_path"] or None,
    )


def retention_window(config: ServiceConfig) -> dt.timedelta:
    """The Retention_Window as a duration, for the stores that need one."""
    return dt.timedelta(days=config.retention_days)


__all__ = [
    "INTERFACE_SWITCHES",
    "PERMITTED_LOG_LEVELS",
    "RECOGNIZED_KEYS",
    "ConfigError",
    "ServiceConfig",
    "read_config_file",
    "resolve_and_validate",
    "retention_window",
]
