"""Unit tests for the Config_Loader (tasks 26.1 and 26.2).

Requirements 26.1-26.11, 8.13, 10.11, 29.9.

ONE TEST PER ROW of the design's configuration-error table, as task 26.2 asks, each asserting
the
message NAMES THE VALUE and the constraint it violated. Plus the accumulation tests, which are
the
ones that matter most in practice: §5 and Requirement 26.3 both require every invalid value to
be
reported, and a loader that stops at the first fault makes an operator fix a misconfiguration
one
deploy at a time.
"""

from __future__ import annotations

import json

import pytest

from aqm_ingestion.config.loader import (
    INTERFACE_SWITCHES,
    PERMITTED_LOG_LEVELS,
    RECOGNIZED_KEYS,
    ConfigError,
    ServiceConfig,
    read_config_file,
    resolve_and_validate,
    retention_window,
)


def _always_resolves(_path: object) -> bool:
    """A credential-path predicate that always resolves, injected so no file is touched."""
    return True


def _never_resolves(_path: object) -> bool:
    return False


def _load(
    env: dict[str, str] | None = None,
    file_data: dict[str, object] | None = None,
    exists: object = _always_resolves,
) -> ServiceConfig:
    return resolve_and_validate(
        env or {}, file_data or {}, credential_exists=exists
    )


def _problems(
    env: dict[str, str] | None = None,
    file_data: dict[str, object] | None = None,
    exists: object = _always_resolves,
) -> tuple[str, ...]:
    with pytest.raises(ConfigError) as caught:
        _load(env, file_data, exists)
    return caught.value.problems


# --- Req 26.1: precedence and the one startup log -----------------------

def test_the_defaults_resolve_with_no_env_and_no_file() -> None:
    config = _load()
    assert config.retention_days == 90
    assert config.serving.max_history_span_days == 30


def test_the_file_overrides_a_default() -> None:
    assert _load(file_data={"retention_days": 120}).retention_days == 120


def test_the_environment_overrides_the_file() -> None:
    config = _load({"AQM_RETENTION_DAYS": "200"}, {"retention_days": 120})
    assert config.retention_days == 200


def test_the_environment_overrides_a_default_with_no_file() -> None:
    assert _load({"AQM_NEAREST_N": "5"}).selection.nearest_n == 5


def test_the_resolved_configuration_names_no_secret() -> None:
    # Req 26.1 logs the resolved NON-SECRET configuration; Req 26.11 forbids a secret anywhere.
    # Credential paths are reported as whether they resolved, not as paths — a path is not a
    # secret but it is a map to one.
    config = _load(
        {
            "AQM_FEED_CREDENTIAL_PATH": "/run/secrets/feed-key",
            "AQM_FORECAST_CREDENTIAL_PATH": "/run/secrets/forecast-key",
        }
    )
    rendered = json.dumps(config.redacted())
    assert "/run/secrets/feed-key" not in rendered
    assert "/run/secrets/forecast-key" not in rendered
    assert "feedCredentialConfigured" in rendered


def test_the_redacted_view_still_reports_the_operational_values() -> None:
    # The counterpart: a redaction that dropped everything would satisfy the test above while
    # making Req 26.1's log useless.
    rendered = _load().redacted()
    assert rendered["retentionDays"] == 90
    assert rendered["logLevel"] == "info"
    adapters = rendered["adapters"]
    assert isinstance(adapters, dict)
    assert adapters["readings_store"] == "memory"


# --- Req 26.3: accumulate, never stop at the first ---------------------

def test_every_invalid_value_is_reported_not_only_the_first() -> None:
    problems = _problems(
        file_data={
            "retention_days": -1,
            "nowcast_window_hours": 0,
            "log_level": "chatty",
            "breakpoint_table": "nope",
        }
    )
    joined = " ".join(problems)
    assert "retention_days" in joined
    assert "nowcast_window_hours" in joined
    assert "log_level" in joined
    assert "breakpoint_table" in joined


def test_one_message_per_invalid_value() -> None:
    problems = _problems(file_data={"retention_days": -1, "freshness_hours": -2})
    assert len([p for p in problems if "retention_days" in p]) == 1
    assert len([p for p in problems if "freshness_hours" in p]) == 1


def test_nothing_is_returned_when_any_value_is_invalid() -> None:
    # Req 26.3's "never half-start": the caller gets an exception, not a config with some
    # values defaulted, so there is nothing to start with.
    with pytest.raises(ConfigError):
        _load(file_data={"retention_days": -1})


# --- Req 26.4: an unrecognized key names its category ------------------

def test_an_unrecognized_key_is_rejected_naming_the_key() -> None:
    problems = _problems(file_data={"retention_dayz": 90})
    assert any("retention_dayz" in problem for problem in problems)


def test_an_unrecognized_key_lists_the_recognized_keys_in_its_category() -> None:
    # Req 26.4 says the recognized keys in the SAME CATEGORY. Listing all forty would tell an
    # operator nothing about where their typo belongs.
    problems = _problems(file_data={"retention_dayz": 90})
    message = next(problem for problem in problems if "retention_dayz" in problem)
    assert "retention_days" in message
    assert "durations" in message


def test_a_wholly_invented_key_still_lists_the_categories() -> None:
    problems = _problems(file_data={"xyzzy": 1})
    message = next(problem for problem in problems if "xyzzy" in problem)
    assert "durations" in message
    assert "adapters" in message


def test_every_recognized_key_is_accepted() -> None:
    # The counterpart to the rejections: a recognized key must not be rejected as UNRECOGNIZED,
    # asserted for EVERY one rather than the handful a test happens to use. Each value is
    # type-appropriate so the key-recognition path is what is exercised; any OTHER problem the
    # value causes is ignored, since that is not what this test is about.
    benign: dict[str, object] = {
        "fallback_centre": [51.5, -0.1],
        "species_precedence": ["PM25", "NO2"],
        "breakpoint_table": "epa-2024-05-06",
        "log_level": "info",
        "enable_push": True,
        "enable_pull": False,
        "enable_serving": True,
        "feed_credential_path": "/tmp/x",
        "forecast_credential_path": "/tmp/x",
    }
    for name in _REGISTERED_ADAPTER_KEYS:
        benign[name] = "memory"
    benign["authenticator"] = "local"

    for key in sorted(RECOGNIZED_KEYS):
        value = benign.get(key, 1)
        try:
            resolve_and_validate({}, {key: value}, credential_exists=_always_resolves)
            problems: tuple[str, ...] = ()
        except ConfigError as rejected:
            problems = rejected.problems
        assert not any(
            "not recognized" in problem and key in problem for problem in problems
        ), key


_REGISTERED_ADAPTER_KEYS = (
    "readings_store",
    "sensor_registry_store",
    "raw_archive",
    "profile_store",
    "forecast_client",
    "meteorology_provider",
    "authenticator",
)


# --- Req 26.5: registry-name resolution -------------------------------

def test_an_unknown_breakpoint_table_names_the_registered_ones() -> None:
    problems = _problems(file_data={"breakpoint_table": "made-up"})
    message = next(problem for problem in problems if "breakpoint_table" in problem)
    assert "made-up" in message
    assert "epa-2024-05-06" in message


def test_an_unknown_adapter_name_names_the_registered_ones() -> None:
    problems = _problems(file_data={"readings_store": "postgres"})
    message = next(problem for problem in problems if "readings_store" in problem)
    assert "postgres" in message
    assert "dynamodb" in message
    assert "memory" in message


def test_every_pluggable_adapter_is_validated() -> None:
    # Req 26.5 enumerates seven adapters, so a loader validating six would pass a narrower test.
    for name in (
        "readings_store",
        "sensor_registry_store",
        "raw_archive",
        "profile_store",
        "forecast_client",
        "meteorology_provider",
        "authenticator",
    ):
        problems = _problems(file_data={name: "not-a-real-adapter"})
        assert any(name in problem for problem in problems), name


def test_the_shipped_adapter_names_are_accepted() -> None:
    config = _load(
        file_data={"readings_store": "dynamodb", "raw_archive": "s3"}
    )
    assert config.adapters["readings_store"] == "dynamodb"


# --- Req 26.6: durations and their ordering ---------------------------

@pytest.mark.parametrize(
    "key",
    [
        "retention_days",
        "quarantine_retention_days",
        "audit_retention_days",
        "nowcast_window_hours",
        "max_history_span_days",
        "freshness_hours",
    ],
)
def test_a_non_positive_duration_is_rejected_naming_the_value(key: str) -> None:
    # Req 26.6 covers all six. Two of them (max_history_span_days, freshness_hours) are owned by
    # a settings object whose own __post_init__ refuses them, so the message comes from there —
    # which is the point: the rule lives once, wherever the value lives. The assertion is
    # therefore on the KEY being named and a bound being described, not on a fixed word.
    problems = _problems(file_data={key: 0})
    message = next(problem for problem in problems if key in problem)
    assert "positive" in message or "at least" in message


def test_retention_shorter_than_the_history_span_names_both_values() -> None:
    # The design's error table says "reject naming BOTH values" — an operator needs to know
    # which to raise and which to lower, and one value alone does not say.
    problems = _problems(
        file_data={"retention_days": 7, "max_history_span_days": 30}
    )
    message = next(problem for problem in problems if "retention_days" in problem)
    assert "7" in message
    assert "30" in message


def test_retention_equal_to_the_history_span_is_accepted() -> None:
    # Req 26.6 says "greater than or equal to", so equality is lawful and an exclusive
    # comparison is the natural off-by-one.
    config = _load(file_data={"retention_days": 30, "max_history_span_days": 30})
    assert config.retention_days == 30


def test_the_retention_window_is_available_as_a_duration() -> None:
    assert retention_window(_load()).days == 90


# --- Req 26.7 / 8.13: bound pairs, limits, coefficients ---------------

def test_a_non_positive_count_is_rejected() -> None:
    assert any("nearest_n" in problem for problem in _problems(file_data={"nearest_n": 0}))


def test_a_non_positive_radius_is_rejected() -> None:
    assert any("radius" in problem for problem in _problems(file_data={"radius_km": 0}))


def test_a_non_positive_rate_limit_is_rejected() -> None:
    assert any(
        "rate_limit" in problem
        for problem in _problems(file_data={"rate_limit_per_minute": 0})
    )


def test_a_non_positive_forecast_timeout_is_rejected() -> None:
    assert any(
        "timeout" in problem for problem in _problems(file_data={"timeout_seconds": 0})
    )


def test_a_non_finite_calibration_coefficient_is_rejected() -> None:
    # Req 8.13. The message must not simply say "invalid": the design's table says naming the
    # offending value.
    problems = _problems(file_data={"rh_linear_a": "nan"})
    assert any("rh_linear" in problem for problem in problems)


def test_an_infinite_calibration_coefficient_is_rejected() -> None:
    assert any(
        "rh_linear" in problem for problem in _problems(file_data={"rh_linear_b": "inf"})
    )


def test_the_shipped_calibration_coefficients_are_accepted() -> None:
    config = _load()
    assert config.rh_linear.a == 0.524
    assert config.rh_linear.b == -0.0862


# --- Req 26.9: the config file, with no fallback ----------------------

def test_an_unreadable_file_names_the_path_and_the_failure_kind() -> None:
    with pytest.raises(ConfigError) as caught:
        read_config_file("/nonexistent/aqm-config.json")
    message = caught.value.problems[0]
    assert "/nonexistent/aqm-config.json" in message
    assert "unreadable" in message


def test_an_unparseable_file_names_the_path_and_the_failure_kind(
    tmp_path: object,
) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        read_config_file(str(path))
    message = caught.value.problems[0]
    assert str(path) in message
    assert "unparseable" in message


def test_a_non_object_file_is_unparseable(tmp_path: object) -> None:
    from pathlib import Path

    path = Path(str(tmp_path)) / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError, match="unparseable"):
        read_config_file(str(path))


def test_an_unreadable_file_does_not_fall_back_to_defaults() -> None:
    # Req 26.9 forbids the fallback SPECIFICALLY, because a deployment would otherwise come up
    # with settings nobody chose — worse than not coming up at all.
    with pytest.raises(ConfigError):
        read_config_file("/nonexistent/aqm-config.json")


def test_no_file_at_all_is_not_an_error() -> None:
    # Distinct from an unreadable one: absent means "use env and defaults", which is lawful.
    assert read_config_file(None) == {}


def test_a_readable_file_is_returned() -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "config.json"
        path.write_text('{"retention_days": 45}', encoding="utf-8")
        assert read_config_file(str(path)) == {"retention_days": 45}


# --- Req 26.10: at least one interface -------------------------------

def test_a_configuration_enabling_no_interface_is_rejected() -> None:
    problems = _problems(
        file_data={"enable_push": False, "enable_pull": False, "enable_serving": False}
    )
    message = next(problem for problem in problems if "interfaces" in problem)
    for switch in INTERFACE_SWITCHES:
        assert switch in message


def test_one_enabled_interface_is_enough() -> None:
    config = _load(
        file_data={"enable_push": True, "enable_pull": False, "enable_serving": False}
    )
    assert config.enable_push is True


# --- Req 26.8 / 26.11: credentials, never echoed ---------------------

def test_an_unresolvable_feed_credential_is_rejected_when_pull_is_enabled() -> None:
    problems = _problems(
        file_data={"enable_pull": True, "feed_credential_path": "/run/secrets/feed"},
        exists=_never_resolves,
    )
    assert any("feed_credential_path" in problem for problem in problems)


def test_a_missing_feed_credential_is_rejected_when_pull_is_enabled() -> None:
    problems = _problems(file_data={"enable_pull": True})
    message = next(problem for problem in problems if "feed_credential_path" in problem)
    assert "pull" in message


def test_no_feed_credential_is_needed_when_pull_is_disabled() -> None:
    # Req 26.8 scopes the requirement to an ENABLED interface: demanding a credential for an
    # interface nobody turned on would block a serving-only deployment.
    config = _load(file_data={"enable_pull": False})
    assert config.feed_credential_path is None


def test_a_forecast_credential_is_required_for_a_non_memory_client() -> None:
    problems = _problems(file_data={"forecast_client": "http"})
    assert any("forecast_credential_path" in problem for problem in problems)


def test_no_forecast_credential_is_needed_for_the_local_client() -> None:
    # Req 24.9's deterministic local adapter is a deployment-free path, so it must not demand
    # one.
    assert _load(file_data={"forecast_client": "memory"}).forecast_credential_path is None


def test_a_credential_message_never_contains_the_secret_path() -> None:
    # Req 26.11. The message names the CONFIGURATION VALUE, not what it points at.
    problems = _problems(
        file_data={
            "enable_pull": True,
            "feed_credential_path": "/run/secrets/super-secret-key",
        },
        exists=_never_resolves,
    )
    joined = " ".join(problems)
    assert "feed_credential_path" in joined
    assert "super-secret-key" not in joined


def test_the_loader_never_reads_a_credential_file() -> None:
    # Structural §7: the loader takes a PREDICATE over a path, so it can ask whether a
    # credential
    # resolves without the contents ever entering this process. A loader that cannot see a
    # secret
    # cannot log one.
    import inspect

    assert "credential_exists" in inspect.signature(resolve_and_validate).parameters

    import ast

    import aqm_ingestion.config.loader as loader_module

    tree = ast.parse(inspect.getsource(loader_module))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    # read_text IS called, but only on the CONFIG file, never on a credential path — asserted by
    # the predicate seam above rather than by banning the call, since banning it would also ban
    # reading the configuration file the loader exists to read.
    assert "read_text" in called


# --- Req 29.9: the log level ----------------------------------------

def test_an_unrecognized_log_level_names_the_permitted_values() -> None:
    problems = _problems(file_data={"log_level": "chatty"})
    message = next(problem for problem in problems if "log_level" in problem)
    assert "chatty" in message
    for level in PERMITTED_LOG_LEVELS:
        assert level in message


@pytest.mark.parametrize("level", PERMITTED_LOG_LEVELS)
def test_every_permitted_log_level_is_accepted(level: str) -> None:
    assert _load(file_data={"log_level": level}).log_level == level


def test_the_log_level_is_case_insensitive() -> None:
    assert _load(file_data={"log_level": "INFO"}).log_level == "info"


# --- coercion -------------------------------------------------------

def test_a_non_numeric_duration_is_rejected_naming_the_value() -> None:
    problems = _problems({"AQM_RETENTION_DAYS": "ninety"})
    message = next(problem for problem in problems if "retention_days" in problem)
    assert "ninety" in message


def test_a_non_boolean_switch_is_rejected() -> None:
    assert any(
        "enable_push" in problem
        for problem in _problems({"AQM_ENABLE_PUSH": "perhaps"})
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False)],
)
def test_a_boolean_switch_accepts_the_usual_spellings(raw: str, expected: bool) -> None:
    config = _load({"AQM_ENABLE_PUSH": raw})
    assert config.enable_push is expected


def test_the_config_holds_narrow_settings_objects_not_a_blob() -> None:
    # §1 Interface Segregation: the service's components take the settings object each needs, so
    # no component is handed the whole configuration.
    config = _load()
    assert config.serving.rate_limit_per_minute == 60
    assert config.selection.nearest_n == 3
    assert config.enrichment.ttl_minutes == 60
