"""Guards for the redaction allowlist (Requirements 26.1, 26.8, 17.9, 29.4).

The redactor works by KEY NAME, which cannot tell a user's medication from a configured cap on
how
many medications may be stored. Requirement 26.1 needs the resolved configuration visible in one
startup log; Requirement 17.9 needs profile VALUES invisible everywhere. Both hold only because
the
allowlist is EXACT-MATCH — so these tests exist to keep that true, since a substring exception
would
quietly reopen the hole the markers exist to close.

Two of the allowlisted keys fix a PRE-EXISTING defect rather than one the diary work introduced:
`feedCredentialConfigured` and `forecastCredentialConfigured` had always rendered as
`[redacted]`,
so Requirement 26.8's "report whether a credential resolves" was never visible in the one log
supposed to carry it.
"""

from __future__ import annotations

import json

import pytest

from aqm_ingestion.config.loader import resolve_and_validate
from aqm_ingestion.domain.profile import UserProfile
from aqm_ingestion.observability.logging import (
    _PERMITTED_CONFIG_KEYS,
    _SENSITIVE_KEY_MARKERS,
    _is_sensitive,
    configure_logging,
    get_logger,
)

# --- the allowlist is exact, and cannot shadow a real field --------------

def test_no_allowlisted_key_is_a_user_profile_field_name() -> None:
    # The one thing that would make this exception dangerous: permitting a key that a profile
    # field actually travels under.
    fields = {name.lower() for name in UserProfile.model_fields}
    assert _PERMITTED_CONFIG_KEYS & fields == set()


@pytest.mark.parametrize(
    "key",
    [
        "medication",
        "medications",
        "medication_name",
        "medicationName",
        "location",
        "locations",
        "home_location",
        "profile",
        "user_profile",
        "condition",
        "sensitivity_level",
        "personal_thresholds",
        "consent",
        "activity_level",
        "credential",
        "forecast_credential_path",
        "token",
        "authorization",
    ],
)
def test_a_near_miss_of_an_allowlisted_key_is_still_redacted(key: str) -> None:
    # This is the test that makes the exception exact rather than a substring hole.
    assert _is_sensitive(key) is True


@pytest.mark.parametrize("key", sorted(_PERMITTED_CONFIG_KEYS))
def test_every_allowlisted_key_is_permitted(key: str) -> None:
    assert _is_sensitive(key) is False


@pytest.mark.parametrize("key", sorted(_PERMITTED_CONFIG_KEYS))
def test_every_allowlisted_key_would_otherwise_have_been_redacted(key: str) -> None:
    # Non-vacuity: an entry that no marker would have caught is dead weight, and its presence
    # would suggest the markers cover something they do not.
    assert any(marker in key for marker in _SENSITIVE_KEY_MARKERS), (
        f"{key!r} is not caught by any marker, so allowlisting it achieves nothing"
    )


# --- Req 26.1: the startup log is actually readable ---------------------

def test_the_startup_log_renders_every_configured_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    config = resolve_and_validate(env={}, file_data={})
    get_logger("test.startup").info("config_resolved", **config.redacted())
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    for key in (
        "locationLimit",
        "medicationLimit",
        "routineLimit",
        "associationMinObservations",
        "symptomRetentionDays",
    ):
        assert event[key] != "[redacted]", (
            f"{key} is a configured setting, not user data — Req 26.1 needs it visible"
        )
    assert event["adapters"]["profile_store"] == "memory"
    assert event["adapters"]["symptom_log_store"] == "memory"


def test_the_credential_markers_report_resolution_rather_than_being_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 26.8 asks whether a credential RESOLVES. The answer is a boolean, and a boolean cannot
    # leak a secret — but it was being redacted along with everything credential-shaped.
    configure_logging("info")
    config = resolve_and_validate(env={}, file_data={})
    get_logger("test.startup").info("config_resolved", **config.redacted())
    event = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

    assert event["feedCredentialConfigured"] is False
    assert event["forecastCredentialConfigured"] is False


def test_the_startup_log_still_carries_no_credential_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The half that must NOT change: a path is not a secret but it is a map to one, so the
    # redacted view reports only whether it resolved.
    configure_logging("info")
    config = resolve_and_validate(
        env={"AQM_FORECAST_CREDENTIAL_PATH": "/tmp/sentinel-key.txt"}, file_data={}
    )
    get_logger("test.startup").info("config_resolved", **config.redacted())
    output = capsys.readouterr().out
    assert "/tmp/sentinel-key.txt" not in output
    assert "sentinel-key" not in output


# --- the redactor still redacts ----------------------------------------

def test_a_real_medication_name_never_reaches_a_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The detector-detects self-check: the allowlist must not have made the marker inert.
    configure_logging("info")
    get_logger("test.leak").info("deliberate", medications=["salbutamol"])
    assert "salbutamol" not in capsys.readouterr().out


def test_a_real_location_never_reaches_a_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    get_logger("test.leak").info(
        "deliberate", locations=[{"name": "commute", "latitude": 51.5074}]
    )
    output = capsys.readouterr().out
    assert "commute" not in output
    assert "51.5074" not in output
