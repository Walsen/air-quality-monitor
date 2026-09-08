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


_INTERFACES = ("mqtt", "rest", "both")
_LOG_LEVELS = ("debug", "info", "warn", "error")
_TIME_MODES = ("realtime", "backfill")


def validate_config(config: SimulatorConfig, *, raise_on_error: bool = False) -> list[str]:
    """Validate every resolved value, accumulating one message per problem.

    Fail-fast means "do not half-start", not "stop at the first error": every
    value is checked and every violation reported (Requirement 15.5). Returns
    the list of human-readable problems (empty when valid); with
    ``raise_on_error`` it raises :class:`ConfigError` joining all messages, which
    the entry point turns into a non-zero exit (§5).
    """
    problems: list[str] = []

    def check(ok: bool, message: str) -> None:
        if not ok:
            problems.append(message)

    check(
        1 <= config.swarm_size <= 500,
        f"swarm_size {config.swarm_size} is outside the permitted range 1..500",
    )
    check(
        1 <= config.tick_minutes <= 60,
        f"tick_minutes {config.tick_minutes} is outside the permitted range 1..60",
    )
    check(
        1 <= config.publish_minutes <= 1440,
        f"publish_minutes {config.publish_minutes} is outside the permitted range 1..1440",
    )
    if config.tick_minutes > 0 and config.publish_minutes % config.tick_minutes != 0:
        problems.append(
            f"publish_minutes {config.publish_minutes} is not an integer multiple of "
            f"tick_minutes {config.tick_minutes}"
        )
    if config.seed is not None:
        check(
            0 <= config.seed <= 4_294_967_295,
            f"seed {config.seed} is outside the permitted range 0..4294967295",
        )
    check(
        1 <= config.retention_days <= 365,
        f"retention_days {config.retention_days} is outside the permitted range 1..365",
    )
    check(
        config.interface in _INTERFACES,
        f"interface {config.interface!r} is not one of {', '.join(_INTERFACES)}",
    )
    check(
        config.log_level in _LOG_LEVELS,
        f"log_level {config.log_level!r} is not one of {', '.join(_LOG_LEVELS)}",
    )
    check(
        config.time_mode in _TIME_MODES,
        f"time_mode {config.time_mode!r} is not one of {', '.join(_TIME_MODES)}",
    )

    if config.classification_mix is not None:
        total = sum(config.classification_mix.values())
        for name, pct in config.classification_mix.items():
            check(
                0 <= pct <= 100,
                f"classification mix proportion for {name!r} ({pct}) is outside 0..100",
            )
        check(
            abs(total - 100) < 1e-9,
            f"classification mix proportions sum to {total}, not 100",
        )

    if config.site_list is not None:
        check(
            len(config.site_list) <= 500,
            f"site list has {len(config.site_list)} entries, exceeding the 500 maximum",
        )
    check(
        len(config.scenario_schedule) <= 100,
        f"scenario schedule has {len(config.scenario_schedule)} entries, exceeding the 100 maximum",
    )

    if raise_on_error and problems:
        raise ConfigError("; ".join(problems))
    return problems

