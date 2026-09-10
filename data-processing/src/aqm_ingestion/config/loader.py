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
from aqm_ingestion.domain.association import (
    DEFAULT_ASSOCIATION_LAGS,
    DEFAULT_ELEVATED_SEVERITY,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_MIN_STRENGTH,
    DEFAULT_THRESHOLD_FLOOR,
    AssociationLimits,
)
from aqm_ingestion.domain.calibration import (
    CalibrationRegistry,
    RhLinearCoefficients,
)
from aqm_ingestion.domain.profile import (
    DEFAULT_LOCATION_LIMIT,
    DEFAULT_LOCATION_PRECISION,
    DEFAULT_MEDICATION_LIMIT,
    DEFAULT_ROUTINE_LIMIT,
    Condition,
    ProfileLimits,
)
from aqm_ingestion.domain.symptoms import (
    DEFAULT_NOTE_MAX_LENGTH,
    MAX_SEVERITY,
    MIN_SEVERITY,
    SymptomLogLimits,
)
from aqm_ingestion.domain.symptoms import (
    DEFAULT_SYMPTOM_RETENTION_DAYS as _DEFAULT_SYMPTOM_RETENTION_DAYS,
)
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

DEFAULT_SYMPTOM_NOTE_MAX_LENGTH = DEFAULT_NOTE_MAX_LENGTH
"""Requirement 31.5's default Symptom_Note bound, re-exported from its owning module."""

DEFAULT_SYMPTOM_RETENTION_DAYS = _DEFAULT_SYMPTOM_RETENTION_DAYS
"""Requirement 31.8's default retention window, re-exported from its owning module."""

DEFAULT_ASSOCIATION_MIN_OBSERVATIONS = DEFAULT_MIN_OBSERVATIONS
DEFAULT_ASSOCIATION_MIN_STRENGTH = DEFAULT_MIN_STRENGTH
DEFAULT_ASSOCIATION_THRESHOLD_FLOOR = DEFAULT_THRESHOLD_FLOOR
DEFAULT_ASSOCIATION_ELEVATED_SEVERITY = DEFAULT_ELEVATED_SEVERITY
"""Requirement 32's defaults, re-exported so the loader's own surface names them.

Re-exported rather than restated: each value has ONE owner in the domain, and a second literal
here would be a copy free to drift from the module that actually uses it.
"""

_MIN_ASSOCIATION_OBSERVATIONS = 2
"""A correlation over fewer than two pairs is undefined, not weak.

So a configured minimum of 1 could never produce a usable association and would silently disable
every learned threshold while looking like a deliberate loosening.
"""

_RECOGNIZED_KEYS: Mapping[str, frozenset[str]] = {
    "durations": frozenset(
        {
            "retention_days",
            "quarantine_retention_days",
            "audit_retention_days",
            "nowcast_window_hours",
            "max_history_span_days",
            "freshness_hours",
            "symptom_retention_days",
        }
    ),
    "adapters": frozenset(
        {
            "readings_store",
            "sensor_registry_store",
            "raw_archive",
            "profile_store",
            "symptom_log_store",
            "forecast_client",
            "meteorology_provider",
            "authenticator",
        }
    ),
    # Requirements 17.5, 17.6, 23.6, 30.4 and 30.6 all say "configured". Before this category
    # existed the loader passed a bare ProfileLimits(), so every one of those words was true of
    # the model and false of the service.
    "profile": frozenset(
        {
            "location_precision",
            "location_limit",
            "medication_limit",
            "routine_limit",
            "max_activity_duration_hours",
        }
    ),
    "symptoms": frozenset({"symptom_note_max_length"}),
    "association": frozenset(
        {
            "association_lags",
            "association_min_observations",
            "association_min_strength",
            "association_threshold_floor",
            "association_elevated_severity",
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
    "symptom_log_store": ("memory", "dynamodb"),
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
    symptom_limits: SymptomLogLimits
    association: AssociationLimits
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
            "symptomRetentionDays": self.symptom_limits.retention_days,
            "symptomNoteMaxLength": self.symptom_limits.note_max_length,
            "locationLimit": self.profile_limits.location_limit,
            "medicationLimit": self.profile_limits.medication_limit,
            "routineLimit": self.profile_limits.routine_limit,
            "associationLags": list(self.association.lags),
            "associationMinObservations": self.association.min_observations,
            "associationMinStrength": self.association.min_strength,
            "associationThresholdFloor": self.association.threshold_floor,
            # Req 32.5's effective reach: the SHORTER of the two retention windows. Worth
            # logging because it is derived rather than configured, so an operator who
            # shortens readings retention can see the association's reach follow it.
            "associationReachDays": min(
                self.symptom_limits.retention_days, self.retention_days
            ),
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
    _validate_profile_and_symptom_limits(resolved, problems)
    _validate_association(resolved, problems)
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
    "location_precision": ("AQM_LOCATION_PRECISION", DEFAULT_LOCATION_PRECISION),
    "location_limit": ("AQM_LOCATION_LIMIT", DEFAULT_LOCATION_LIMIT),
    "medication_limit": ("AQM_MEDICATION_LIMIT", DEFAULT_MEDICATION_LIMIT),
    "routine_limit": ("AQM_ROUTINE_LIMIT", DEFAULT_ROUTINE_LIMIT),
    "max_activity_duration_hours": ("AQM_MAX_ACTIVITY_DURATION_HOURS", 24.0),
    "symptom_note_max_length": (
        "AQM_SYMPTOM_NOTE_MAX_LENGTH",
        DEFAULT_SYMPTOM_NOTE_MAX_LENGTH,
    ),
    "symptom_retention_days": (
        "AQM_SYMPTOM_RETENTION_DAYS",
        DEFAULT_SYMPTOM_RETENTION_DAYS,
    ),
    "association_min_observations": (
        "AQM_ASSOCIATION_MIN_OBSERVATIONS",
        DEFAULT_ASSOCIATION_MIN_OBSERVATIONS,
    ),
    "association_min_strength": (
        "AQM_ASSOCIATION_MIN_STRENGTH",
        DEFAULT_ASSOCIATION_MIN_STRENGTH,
    ),
    "association_threshold_floor": (
        "AQM_ASSOCIATION_THRESHOLD_FLOOR",
        DEFAULT_ASSOCIATION_THRESHOLD_FLOOR,
    ),
    "association_elevated_severity": (
        "AQM_ASSOCIATION_ELEVATED_SEVERITY",
        DEFAULT_ASSOCIATION_ELEVATED_SEVERITY,
    ),
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
    # A sequence comes from the FILE only, following species_precedence and fallback_centre: an
    # environment variable would need a separator convention this loader deliberately does not
    # have, and inventing one here would make two settings parse differently.
    lags = data.get("association_lags")
    if lags is None:
        resolved["association_lags"] = DEFAULT_ASSOCIATION_LAGS
    else:
        try:
            resolved["association_lags"] = tuple(int(item) for item in lags)
        except (TypeError, ValueError):
            problems.add("association_lags", "a list of whole numbers of days")
            resolved["association_lags"] = DEFAULT_ASSOCIATION_LAGS
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


def _validate_association(resolved: Mapping[str, Any], problems: _Problems) -> None:
    """Check the Requirement 32 settings that the limits object cannot check for itself.

    ``AssociationLimits`` is a plain frozen dataclass shared with a pure domain function, so it
    carries no ``__post_init__`` validation — a domain type that raised on construction would
    make the association's purity harder to reason about. The checks therefore live here, and
    each one rules out a value that would silently disable the feature rather than fail loudly.
    """
    lags = resolved.get("association_lags") or ()
    if not lags:
        problems.add("association_lags", "at least one lag in whole days")
    if any(int(lag) < 0 for lag in lags):
        # A negative lag would pair a symptom with a LATER exposure, which is not a lag.
        problems.add("association_lags", "every lag to be zero or more days")

    minimum = resolved.get("association_min_observations")
    if isinstance(minimum, int) and minimum < _MIN_ASSOCIATION_OBSERVATIONS:
        problems.add(
            f"association_min_observations={minimum}",
            f"at least {_MIN_ASSOCIATION_OBSERVATIONS}, since a correlation over fewer "
            "pairs is undefined rather than weak",
        )

    strength = resolved.get("association_min_strength")
    if isinstance(strength, (int, float)) and not 0.0 < float(strength) <= 1.0:
        problems.add(
            f"association_min_strength={strength}",
            "greater than 0 and at most 1, the range a correlation coefficient can occupy",
        )

    floor = resolved.get("association_threshold_floor")
    if isinstance(floor, int) and not 1 <= floor <= 500:
        problems.add(
            f"association_threshold_floor={floor}",
            "within the Sub_Index range 1 to 500, or no Learned_Threshold could ever meet it",
        )

    severity = resolved.get("association_elevated_severity")
    if isinstance(severity, int) and not MIN_SEVERITY <= severity <= MAX_SEVERITY:
        problems.add(
            f"association_elevated_severity={severity}",
            f"within the Symptom_Severity range {MIN_SEVERITY} to {MAX_SEVERITY}, or it would "
            "select no days and silently disable every learned threshold",
        )


def _validate_profile_and_symptom_limits(
    resolved: Mapping[str, Any], problems: _Problems
) -> None:
    """Check the positive-integer bounds Requirements 17, 30 and 31 configure.

    One message per invalid value (Requirement 26.3), and each names the field so an operator
    can act on it.
    """
    positive = (
        ("location_precision", 0),
        ("location_limit", 1),
        ("medication_limit", 1),
        ("routine_limit", 1),
        ("symptom_note_max_length", 1),
        ("symptom_retention_days", 1),
    )
    for key, lowest in positive:
        value = resolved.get(key)
        if isinstance(value, int) and value < lowest:
            problems.add(f"{key}={value}", f"at least {lowest}")

    duration = resolved.get("max_activity_duration_hours")
    if isinstance(duration, (int, float)) and float(duration) <= 0:
        problems.add(f"max_activity_duration_hours={duration}", "greater than 0")


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
        # Built from the RESOLVED values rather than defaulted. Requirements 17.5, 17.6, 23.6,
        # 30.4 and 30.6 each say "configured", and a bare ProfileLimits() made every one of
        # those words true of the model and false of the service.
        #
        # threshold_species is derived from the CONFIGURED table rather than left at its
        # default: otherwise configuring a different Breakpoint_Table would leave the profile
        # rejecting thresholds for species that table actually defines.
        profile_limits=ProfileLimits(
            location_precision=int(resolved["location_precision"]),
            location_limit=int(resolved["location_limit"]),
            threshold_species=frozenset(
                BreakpointTableRegistry.with_defaults().species_for(
                    str(resolved["breakpoint_table"])
                )
            ),
            max_activity_duration_hours=float(resolved["max_activity_duration_hours"]),
            medication_limit=int(resolved["medication_limit"]),
            routine_limit=int(resolved["routine_limit"]),
        ),
        symptom_limits=SymptomLogLimits(
            note_max_length=int(resolved["symptom_note_max_length"]),
            retention_days=int(resolved["symptom_retention_days"]),
        ),
        association=AssociationLimits(
            lags=tuple(resolved["association_lags"]),
            min_observations=int(resolved["association_min_observations"]),
            min_strength=float(resolved["association_min_strength"]),
            threshold_floor=int(resolved["association_threshold_floor"]),
            elevated_severity=int(resolved["association_elevated_severity"]),
            symptom_retention_days=int(resolved["symptom_retention_days"]),
            readings_retention_days=int(resolved["retention_days"]),
            species_precedence=tuple(resolved["species_precedence"]),
        ),
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
