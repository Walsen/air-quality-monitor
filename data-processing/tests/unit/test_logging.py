"""Unit tests for the structured logger and its central redaction (tasks 1.2, 1.4).

- Req 29.1: one single-line JSON object per event to stdout, configured once at
  startup; never ``print`` and never non-JSON text on stdout.
- Req 29.2: every event carries the ISO-8601 UTC instant, level, and event name,
  plus SiteCode / Species / interval start / route where the event concerns one.
- Req 29.3: the five levels carry their documented meanings, and a configured
  level emits at that level and above while suppressing below it.
- Req 29.4: a resolved secret, a bearer credential, an Authenticator claim beyond
  the user identity, and any User_Profile field are NEVER logged. The rule lives
  in ONE place so no call site can bypass it.
- Req 29.8: a handled error is logged with its exception type and stack
  information, and neither is returned to a client.
- Req 29.9: an unrecognized level is rejected naming the value and the permitted set.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aqm_ingestion.observability.logging import (
    InvalidLogLevelError,
    configure_logging,
    get_logger,
    log_handled_error,
)


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().out.strip().splitlines()
    return [json.loads(line) for line in out if line]


# --- Req 29.1 / 29.2 shape -------------------------------------------------

def test_event_is_one_line_of_json(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    get_logger("ingest").info("batch_accepted")
    raw = capsys.readouterr().out.strip().splitlines()
    assert len(raw) == 1
    parsed = json.loads(raw[0])
    assert parsed["event"] == "batch_accepted"


def test_event_carries_instant_level_and_name(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    get_logger("ingest").info("batch_accepted")
    event = _events(capsys)[0]
    assert event["level"] == "info"
    assert event["event"] == "batch_accepted"
    assert event["instant"].endswith("Z")


def test_event_carries_optional_context(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    get_logger("ingest").info(
        "reading_accepted",
        SiteCode="CB0001",
        Species="PM25",
        interval_start="2026-07-01T11:00:00Z",
        route="/readings",
    )
    event = _events(capsys)[0]
    assert event["SiteCode"] == "CB0001"
    assert event["Species"] == "PM25"
    assert event["interval_start"] == "2026-07-01T11:00:00Z"
    assert event["route"] == "/readings"


def test_nothing_non_json_reaches_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("debug")
    logger = get_logger("ingest")
    logger.debug("a")
    logger.info("b")
    logger.warning("c")
    logger.error("d")
    for line in capsys.readouterr().out.strip().splitlines():
        json.loads(line)  # raises if any line is not JSON


# --- Req 29.3 / 29.9 levels ------------------------------------------------

def test_configured_level_emits_at_and_above(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("warning")
    logger = get_logger("ingest")
    logger.debug("suppressed_debug")
    logger.info("suppressed_info")
    logger.warning("kept_warning")
    logger.error("kept_error")
    names = [e["event"] for e in _events(capsys)]
    assert names == ["kept_warning", "kept_error"]


def test_critical_is_available(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    get_logger("ingest").critical("unrecoverable")
    assert _events(capsys)[0]["level"] == "critical"


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
def test_every_documented_level_is_accepted(level: str) -> None:
    configure_logging(level)  # must not raise


def test_invalid_level_is_rejected_naming_value_and_permitted() -> None:
    with pytest.raises(InvalidLogLevelError) as caught:
        configure_logging("verbose")
    message = str(caught.value)
    assert "verbose" in message
    for permitted in ("debug", "info", "warning", "error", "critical"):
        assert permitted in message


# --- Req 29.4 central redaction -------------------------------------------

def test_bearer_credential_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    get_logger("serving").info("auth_ok", authorization="Bearer super-secret-token")
    event = _events(capsys)[0]
    assert "super-secret-token" not in json.dumps(event)
    assert event["authorization"] == "[redacted]"


def test_resolved_secret_is_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    get_logger("config").info("secret_loaded", api_key="abcdef0123456789")
    assert "abcdef0123456789" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "field",
    ["asthma", "copd", "pregnancy", "age_band", "conditions", "home_location"],
)
def test_profile_fields_are_redacted(
    field: str, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("info")
    get_logger("serving").info("profile_read", **{field: "sensitive-value"})
    assert "sensitive-value" not in capsys.readouterr().out


def test_claims_beyond_user_identity_are_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    get_logger("serving").info(
        "token_verified", user_id="u-123", email="person@example.com", groups="admins"
    )
    event = _events(capsys)[0]
    assert event["user_id"] == "u-123"  # the identity itself is permitted
    assert "person@example.com" not in json.dumps(event)
    assert "admins" not in json.dumps(event)


def test_redaction_is_case_insensitive(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    get_logger("serving").info("odd_casing", Authorization="Bearer x", API_KEY="y")
    body = capsys.readouterr().out
    assert "Bearer x" not in body
    assert '"y"' not in body


def test_nested_values_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    get_logger("serving").info("nested", profile={"asthma": True, "user_id": "u-1"})
    body = capsys.readouterr().out
    assert "asthma" not in body or "true" not in body.lower()


# --- Req 29.8 handled errors ----------------------------------------------

def test_handled_error_logs_type_and_stack(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    logger = get_logger("ingest")
    try:
        raise ValueError("archive unavailable")
    except ValueError as error:
        log_handled_error(logger, "archive_write_failed", error)
    event = _events(capsys)[0]
    assert event["level"] == "error"
    assert event["error_type"] == "ValueError"
    assert event["stack"]  # stack information present for the operator


def test_handled_error_message_is_not_silent(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info")
    logger = get_logger("ingest")
    try:
        raise OSError("disk full")
    except OSError as error:
        log_handled_error(logger, "poll_failed", error)
    assert _events(capsys)[0]["event"] == "poll_failed"
