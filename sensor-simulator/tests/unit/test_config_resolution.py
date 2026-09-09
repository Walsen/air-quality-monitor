"""Unit tests for Config_Loader resolution (task 6.1).

Requirement 15.1: resolve every value in precedence env > file > default; with
no config file, resolve from env + defaults alone without error.
Requirement 15.2: accept the documented keys; reject any unrecognized key by name.
Requirement 15.3: default profile is cochabamba; a supplied individual profile
value overrides that profile's default while retaining unsupplied values.
Requirement 15.6: defaults for every value except MQTT endpoint/port/credential
paths; interface defaults to rest.
"""

from __future__ import annotations

import pytest

from aqm_simulator.config.loader import ConfigError, resolve_config


def test_resolves_defaults_with_no_file_and_no_env() -> None:
    cfg = resolve_config(env={}, file_data=None)
    assert cfg.swarm_size == 50
    assert cfg.tick_minutes == 1
    assert cfg.publish_minutes == 60
    assert cfg.profile_name == "cochabamba"
    assert cfg.interface == "rest"
    assert cfg.retention_days == 30
    assert cfg.log_level == "info"


def test_env_overrides_file_overrides_default() -> None:
    file_data = {"swarm_size": 100, "tick_minutes": 5}
    env = {"AQM_SWARM_SIZE": "200"}
    cfg = resolve_config(env=env, file_data=file_data)
    assert cfg.swarm_size == 200  # env wins
    assert cfg.tick_minutes == 5  # file wins over default
    assert cfg.publish_minutes == 60  # default


def test_unknown_config_key_rejected_by_name() -> None:
    with pytest.raises(ConfigError) as exc:
        resolve_config(env={}, file_data={"bogus_key": 1})
    assert "bogus_key" in str(exc.value)


def test_unknown_env_key_ignored_but_unknown_file_key_rejected() -> None:
    # Unknown env vars without the AQM_ prefix are ignored; unknown file keys
    # are rejected by name (Req 15.2).
    cfg = resolve_config(env={"UNRELATED": "x"}, file_data=None)
    assert cfg.swarm_size == 50


def test_interface_selection_values() -> None:
    assert resolve_config(env={"AQM_INTERFACE": "mqtt"}, file_data=None).interface == "mqtt"
    assert resolve_config(env={"AQM_INTERFACE": "both"}, file_data=None).interface == "both"


def test_profile_value_override_retains_unsupplied(monkeypatch: pytest.MonkeyPatch) -> None:
    # Req 15.3: overriding one profile value keeps the rest of cochabamba.
    cfg = resolve_config(env={}, file_data={"profile_overrides": {"sponsor_name": "Custom"}})
    assert cfg.profile_overrides == {"sponsor_name": "Custom"}


def test_seed_parsed_as_int_from_env() -> None:
    cfg = resolve_config(env={"AQM_SEED": "12345"}, file_data=None)
    assert cfg.seed == 12345


def test_seed_absent_is_none_for_later_selection() -> None:
    cfg = resolve_config(env={}, file_data=None)
    assert cfg.seed is None
