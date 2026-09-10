"""Configuration wiring for Requirements 30, 31 and 32.

Every limit those requirements call "configured" has to actually BE configured. Writing these
tests surfaced that no `ProfileLimits` field was reachable from configuration at all — the
loader passed a bare `ProfileLimits()`, so Requirement 17.5's "configured precision" and 17.6's
"configured number of User_Location entries" were constants wearing the word "configured". The
same trap was waiting for Requirement 30.4's medication cap and 30.6's routine cap.

- 30.4 / 30.6: the medication and routine caps resolve from configuration.
- 31.5 / 31.8: the note bound and the symptom retention window resolve from configuration.
- 32.1 / 32.4 / 32.6 / 32.8: the lags, minimum observation count, minimum strength, floor and
  elevated-severity level all resolve from configuration.
- 26.4: an unrecognised key in any new category is rejected NAMING that category's keys.
- 26.8: a symptom-log adapter is selectable by name and an unregistered name is rejected.
"""

from __future__ import annotations

import pytest

from aqm_ingestion.config.loader import (
    DEFAULT_ASSOCIATION_ELEVATED_SEVERITY,
    DEFAULT_ASSOCIATION_MIN_OBSERVATIONS,
    DEFAULT_ASSOCIATION_MIN_STRENGTH,
    DEFAULT_ASSOCIATION_THRESHOLD_FLOOR,
    DEFAULT_SYMPTOM_NOTE_MAX_LENGTH,
    DEFAULT_SYMPTOM_RETENTION_DAYS,
    RECOGNIZED_KEYS,
    ConfigError,
    ServiceConfig,
    resolve_and_validate,
)


def _config(env: dict[str, str] | None = None, **file_data: object) -> ServiceConfig:
    return resolve_and_validate(env=env or {}, file_data=file_data)


# --- Req 30.4 / 30.6: the caps are configuration ------------------------

def test_the_medication_and_routine_caps_default_to_the_documented_values() -> None:
    config = _config()
    assert config.profile_limits.medication_limit == 10
    assert config.profile_limits.routine_limit == 14


def test_the_caps_resolve_from_the_environment() -> None:
    config = _config({"AQM_MEDICATION_LIMIT": "3", "AQM_ROUTINE_LIMIT": "7"})
    assert config.profile_limits.medication_limit == 3
    assert config.profile_limits.routine_limit == 7


def test_the_caps_resolve_from_a_file() -> None:
    config = _config(medication_limit=2, routine_limit=4)
    assert config.profile_limits.medication_limit == 2
    assert config.profile_limits.routine_limit == 4


@pytest.mark.parametrize("key", ["AQM_MEDICATION_LIMIT", "AQM_ROUTINE_LIMIT"])
def test_a_non_positive_cap_is_rejected_naming_the_field(key: str) -> None:
    with pytest.raises(ConfigError) as caught:
        _config({key: "0"})
    assert any(key.removeprefix("AQM_").lower() in p.lower() for p in caught.value.problems)


# --- the pre-existing gap: ProfileLimits was never wired ----------------

def test_the_location_precision_and_limit_are_configuration_not_constants() -> None:
    # Req 17.5 and 17.6 both say "configured". Before this commit the loader passed a bare
    # ProfileLimits(), so neither was reachable — the words were true of the model and false of
    # the service.
    config = _config({"AQM_LOCATION_PRECISION": "2", "AQM_LOCATION_LIMIT": "9"})
    assert config.profile_limits.location_precision == 2
    assert config.profile_limits.location_limit == 9


def test_the_permitted_threshold_species_follow_the_configured_breakpoint_table() -> None:
    # A second half of the same gap: ProfileLimits.threshold_species defaulted to {PM25, NO2}
    # regardless of the configured table, so configuring a different table would have left the
    # profile rejecting thresholds the table actually defines.
    config = _config()
    assert config.profile_limits.threshold_species == frozenset({"PM25", "NO2"})


# --- Req 31.5 / 31.8: the symptom log's bounds --------------------------

def test_the_symptom_defaults_are_the_documented_ones() -> None:
    assert DEFAULT_SYMPTOM_NOTE_MAX_LENGTH == 280
    assert DEFAULT_SYMPTOM_RETENTION_DAYS == 365
    config = _config()
    assert config.symptom_limits.note_max_length == 280
    assert config.symptom_limits.retention_days == 365


def test_the_symptom_bounds_resolve_from_the_environment() -> None:
    config = _config(
        {"AQM_SYMPTOM_NOTE_MAX_LENGTH": "120", "AQM_SYMPTOM_RETENTION_DAYS": "30"}
    )
    assert config.symptom_limits.note_max_length == 120
    assert config.symptom_limits.retention_days == 30


def test_a_non_positive_note_bound_is_rejected() -> None:
    with pytest.raises(ConfigError):
        _config({"AQM_SYMPTOM_NOTE_MAX_LENGTH": "0"})


def test_a_non_positive_symptom_retention_is_rejected() -> None:
    with pytest.raises(ConfigError):
        _config({"AQM_SYMPTOM_RETENTION_DAYS": "-1"})


# --- Req 32: the association's configuration ----------------------------

def test_the_association_defaults_are_the_documented_ones() -> None:
    assert DEFAULT_ASSOCIATION_MIN_OBSERVATIONS == 14
    assert DEFAULT_ASSOCIATION_THRESHOLD_FLOOR == 51
    assert DEFAULT_ASSOCIATION_ELEVATED_SEVERITY == 3
    assert 0.0 < DEFAULT_ASSOCIATION_MIN_STRENGTH < 1.0
    config = _config()
    assert config.association.lags == (0, 3)
    assert config.association.min_observations == 14
    assert config.association.threshold_floor == 51
    assert config.association.elevated_severity == 3


def test_the_association_scalars_resolve_from_the_environment() -> None:
    config = _config(
        {
            "AQM_ASSOCIATION_MIN_OBSERVATIONS": "21",
            "AQM_ASSOCIATION_MIN_STRENGTH": "0.5",
            "AQM_ASSOCIATION_THRESHOLD_FLOOR": "76",
            "AQM_ASSOCIATION_ELEVATED_SEVERITY": "4",
        }
    )
    assert config.association.min_observations == 21
    assert config.association.min_strength == 0.5
    assert config.association.threshold_floor == 76
    assert config.association.elevated_severity == 4


def test_the_lags_resolve_from_a_file_like_the_other_sequence_settings() -> None:
    # Follows species_precedence and fallback_centre: a sequence comes from the file, since an
    # environment variable would need a parsing convention this loader does not have.
    config = _config(association_lags=[0, 1, 3, 5])
    assert config.association.lags == (0, 1, 3, 5)


def test_a_negative_lag_is_rejected() -> None:
    # A negative lag would pair a symptom with a LATER exposure, which is not a lag at all.
    with pytest.raises(ConfigError) as caught:
        _config(association_lags=[0, -3])
    assert any("lag" in p.lower() for p in caught.value.problems)


def test_an_empty_lag_set_is_rejected() -> None:
    with pytest.raises(ConfigError):
        _config(association_lags=[])


def test_a_strength_outside_the_unit_interval_is_rejected() -> None:
    for value in ("0", "1.5", "-0.2"):
        with pytest.raises(ConfigError):
            _config({"AQM_ASSOCIATION_MIN_STRENGTH": value})


def test_a_floor_outside_the_sub_index_range_is_rejected() -> None:
    # Req 32.8 bounds a Learned_Threshold at 1-500, so an outside floor could never be met.
    for value in ("0", "501"):
        with pytest.raises(ConfigError):
            _config({"AQM_ASSOCIATION_THRESHOLD_FLOOR": value})


def test_an_elevated_severity_outside_the_symptom_range_is_rejected() -> None:
    # Req 31.3 bounds severity at 1-5, so a level of 6 would select no days ever and silently
    # disable every learned threshold.
    for value in ("0", "6"):
        with pytest.raises(ConfigError):
            _config({"AQM_ASSOCIATION_ELEVATED_SEVERITY": value})


def test_a_minimum_observation_count_below_two_is_rejected() -> None:
    # A correlation over fewer than two pairs is undefined rather than weak.
    with pytest.raises(ConfigError):
        _config({"AQM_ASSOCIATION_MIN_OBSERVATIONS": "1"})


# --- Req 26.8: the symptom-log adapter ----------------------------------

def test_the_symptom_log_store_defaults_to_the_in_memory_adapter() -> None:
    assert _config().adapters["symptom_log_store"] == "memory"


def test_the_symptom_log_store_is_selectable_by_name() -> None:
    assert _config(symptom_log_store="dynamodb").adapters["symptom_log_store"] == "dynamodb"


def test_an_unregistered_symptom_log_adapter_is_rejected_naming_the_registered_ones() -> None:
    with pytest.raises(ConfigError) as caught:
        _config(symptom_log_store="postgres")
    rendered = " ".join(caught.value.problems)
    assert "symptom_log_store" in rendered
    assert "memory" in rendered and "dynamodb" in rendered


# --- Req 26.4: unknown keys, and the recognized set ---------------------

@pytest.mark.parametrize(
    "key",
    [
        "medication_limit",
        "routine_limit",
        "location_limit",
        "location_precision",
        "symptom_note_max_length",
        "symptom_retention_days",
        "association_lags",
        "association_min_observations",
        "association_min_strength",
        "association_threshold_floor",
        "association_elevated_severity",
        "symptom_log_store",
    ],
)
def test_every_new_key_is_recognized(key: str) -> None:
    # Req 26.4 rejects an unrecognised key, so a key the loader RESOLVES but does not RECOGNIZE
    # would be refused by the very loader that reads it.
    assert key in RECOGNIZED_KEYS


def test_an_unknown_key_near_a_new_category_names_that_categorys_keys() -> None:
    with pytest.raises(ConfigError) as caught:
        _config(association_min_strenth=0.4)
    rendered = " ".join(caught.value.problems)
    assert "association_min_strength" in rendered


# --- Req 26.1: the startup log stays non-secret -------------------------

def test_the_new_values_appear_in_the_redacted_startup_log() -> None:
    redacted = _config().redacted()
    for expected in (
        "medicationLimit",
        "routineLimit",
        "symptomRetentionDays",
        "associationLags",
        "associationMinObservations",
    ):
        assert expected in redacted


def test_the_redacted_log_still_carries_no_credential_value() -> None:
    redacted = _config({"AQM_FORECAST_CREDENTIAL_PATH": "/tmp/secret-key.txt"}).redacted()
    assert "/tmp/secret-key.txt" not in str(redacted)
