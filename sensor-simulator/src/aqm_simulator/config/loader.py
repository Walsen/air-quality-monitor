"""Config_Loader — resolution.

Resolves every accepted configuration value in the precedence order environment
variable > configuration file > documented default (Requirement 15.1). With no
configuration file, values resolve from environment and defaults alone
(Requirement 15.1). Unrecognized configuration-file keys are rejected by name
(Requirement 15.2). Validation of the resolved values is a separate fail-fast
pass (task 6.2) so this step only assembles the resolved config.

Environment variables use the ``AQM_`` prefix and upper-snake form of the file
key (``swarm_size`` -> ``AQM_SWARM_SIZE``); unrelated environment variables are
ignored, while an unknown *file* key is an error because the file is the
operator's own document (§5, fail loudly on bad config).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ConfigValue = int | str | float | bool | None


class ConfigError(ValueError):
    """Raised when configuration cannot be resolved (e.g. an unknown key)."""


@dataclass(frozen=True, slots=True)
class SimulatorConfig:
    """The resolved (not yet fully validated) configuration surface."""

    swarm_size: int
    profile_name: str
    tick_minutes: int
    publish_minutes: int
    seed: int | None
    time_mode: str
    interface: str
    retention_days: int
    log_level: str
    profile_overrides: dict[str, Any] = field(default_factory=dict)
    site_list: list[dict[str, Any]] | None = None
    classification_mix: dict[str, float] | None = None
    scenario_schedule: list[dict[str, Any]] = field(default_factory=list)


def _as_int(value: ConfigValue) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ConfigError(f"expected an integer, got {value!r}")
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"expected an integer, got {value!r}") from exc


def _as_str(value: ConfigValue) -> str:
    return str(value)


# key -> (env var, default, parser). None default means "no default; may be None".
_SCALARS: dict[str, tuple[str, ConfigValue, Callable[[ConfigValue], Any]]] = {
    "swarm_size": ("AQM_SWARM_SIZE", 50, _as_int),
    "profile_name": ("AQM_PROFILE", "cochabamba", _as_str),
    "tick_minutes": ("AQM_TICK_MINUTES", 1, _as_int),
    "publish_minutes": ("AQM_PUBLISH_MINUTES", 60, _as_int),
    "seed": ("AQM_SEED", None, _as_int),
    "time_mode": ("AQM_TIME_MODE", "realtime", _as_str),
    "interface": ("AQM_INTERFACE", "rest", _as_str),
    "retention_days": ("AQM_RETENTION_DAYS", 30, _as_int),
    "log_level": ("AQM_LOG_LEVEL", "info", _as_str),
}

# File-only structured keys (no env form), each defaulting to an empty container.
_STRUCTURED = {
    "profile_overrides",
    "site_list",
    "classification_mix",
    "scenario_schedule",
}

_ACCEPTED_FILE_KEYS = set(_SCALARS) | _STRUCTURED


def resolve_config(
    env: dict[str, str],
    file_data: dict[str, Any] | None,
) -> SimulatorConfig:
    """Resolve configuration in precedence order env > file > default."""
    file_data = file_data or {}

    unknown = set(file_data) - _ACCEPTED_FILE_KEYS
    if unknown:
        raise ConfigError(
            "unrecognized configuration key(s): " + ", ".join(sorted(unknown))
        )

    resolved: dict[str, Any] = {}
    for key, (env_var, default, parse) in _SCALARS.items():
        if env_var in env:
            resolved[key] = parse(env[env_var])
        elif key in file_data:
            resolved[key] = parse(file_data[key])
        elif default is None:
            resolved[key] = None
        else:
            resolved[key] = parse(default)

    return SimulatorConfig(
        swarm_size=resolved["swarm_size"],
        profile_name=resolved["profile_name"],
        tick_minutes=resolved["tick_minutes"],
        publish_minutes=resolved["publish_minutes"],
        seed=resolved["seed"],
        time_mode=resolved["time_mode"],
        interface=resolved["interface"],
        retention_days=resolved["retention_days"],
        log_level=resolved["log_level"],
        profile_overrides=dict(file_data.get("profile_overrides", {})),
        site_list=file_data.get("site_list"),
        classification_mix=file_data.get("classification_mix"),
        scenario_schedule=list(file_data.get("scenario_schedule", [])),
    )
