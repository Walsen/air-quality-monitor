"""Unit tests for Config_Loader fail-fast validation (task 6.2).

Requirement 15.4: validate every resolved value against its bounds.
Requirement 15.5: if any value fails, complete validation of all remaining
values, write one message per invalid value naming the value and constraint,
and fail (non-zero) — never half-start.
Requirement 15.7: interface in {mqtt, rest, both}.
Requirement 17.8: log level in {debug, info, warn, error}.
"""

from __future__ import annotations

import pytest

from aqm_simulator.config.loader import (
    ConfigError,
    SimulatorConfig,
    resolve_config,
    validate_config,
)


def _cfg(**overrides: object) -> SimulatorConfig:
    base = resolve_config(env={}, file_data=None)
    from dataclasses import replace

    return replace(base, **overrides)  # type: ignore[arg-type]


def test_valid_default_config_passes() -> None:
    assert validate_config(_cfg()) == []


@pytest.mark.parametrize(
    ("field", "bad", "needle"),
    [
        ("swarm_size", 0, "swarm_size"),
        ("swarm_size", 501, "swarm_size"),
        ("tick_minutes", 0, "tick_minutes"),
        ("tick_minutes", 61, "tick_minutes"),
        ("publish_minutes", 0, "publish_minutes"),
        ("publish_minutes", 1441, "publish_minutes"),
        ("seed", -1, "seed"),
        ("seed", 4_294_967_296, "seed"),
        ("retention_days", 0, "retention_days"),
        ("retention_days", 366, "retention_days"),
        ("interface", "carrier-pigeon", "interface"),
        ("log_level", "trace", "log_level"),
    ],
)
def test_each_out_of_range_value_reported(field: str, bad: object, needle: str) -> None:
    problems = validate_config(_cfg(**{field: bad}))
    assert any(needle in p for p in problems), problems


def test_publish_must_be_multiple_of_tick() -> None:
    problems = validate_config(_cfg(tick_minutes=5, publish_minutes=12))
    assert any("publish" in p.lower() and "multiple" in p.lower() for p in problems)


def test_all_invalid_values_reported_not_just_first() -> None:
    # Req 15.5: complete validation of all remaining values.
    problems = validate_config(_cfg(swarm_size=0, tick_minutes=0, interface="nope"))
    assert len(problems) >= 3


def test_classification_mix_must_sum_to_100() -> None:
    problems = validate_config(
        _cfg(classification_mix={"Roadside": 30.0, "Urban Background": 30.0, "Suburban": 20.0})
    )
    assert any("classification" in p.lower() for p in problems)


def test_valid_classification_mix_passes() -> None:
    assert (
        validate_config(
            _cfg(
                classification_mix={
                    "Roadside": 30.0,
                    "Urban Background": 50.0,
                    "Suburban": 20.0,
                }
            )
        )
        == []
    )


def test_load_and_validate_raises_on_invalid_with_all_messages() -> None:
    # The one-call entry point raises ConfigError listing every problem.
    with pytest.raises(ConfigError) as exc:
        validate_config(_cfg(swarm_size=0, log_level="trace"), raise_on_error=True)
    msg = str(exc.value)
    assert "swarm_size" in msg
    assert "log_level" in msg
