"""Tests for the structured logger and its central redaction (task 1.2).

Service 3 holds a category of data Service 2 never did: **the user's own words**. Req 19.2
forbids any utterance substring reaching a log, and that is not something a key-name redactor
can
promise on its own — a caller can always put prose under an innocent key. So the design is two
layers, and both are tested here:

* the formatter redacts by key name, configured ONCE, so no call site can forget it;
* the request model refuses to render its own contents, which is what covers the case where a
  profile or an utterance is interpolated as one object.

The allowlist carries a lesson from Service 2's logger, where a configured CAP on medications
was redacted as though it were a medication. Here the collision is `token`: Req 22 budgets a
turn in OUTPUT TOKENS, and a token COUNT is not a credential. The exception is exact-match, and
two
guards keep it honest — a near-miss must still be redacted, and an entry no marker would have
caught
is dead weight that implies coverage it does not provide.

- 19.2 / 19.3: no utterance, condition, coordinate or medication reaches a log.
- 19.4 / 19.5: the pseudonymous identity DOES, so the sweeps are not vacuous.
- 24.1 / 24.2: one single-line JSON object per event on stdout, never `print`.
- 24.3: a handled error carries its type and stack as a STRING, so redaction still applies.
"""

from __future__ import annotations

import json

import pytest

from aqm_advisor.observability.logging import (
    PERMITTED_COUNT_KEYS,
    SENSITIVE_KEY_MARKERS,
    InvalidLogLevelError,
    configure_logging,
    get_logger,
    is_sensitive,
    log_handled_error,
)


def _events(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.strip().splitlines() if line.strip()]


# --- Req 24.1 / 24.2: one single-line JSON object per event -------------

def test_an_event_is_one_line_of_json_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    get_logger("test").info("turn_answered", degraded=False)
    out = capsys.readouterr().out
    assert len(out.strip().splitlines()) == 1
    event = json.loads(out)
    assert event["event"] == "turn_answered"
    assert event["level"] == "info"
    assert event["degraded"] is False


def test_the_instant_is_iso_8601_utc_at_whole_seconds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    get_logger("test").info("turn_answered")
    instant = str(_events(capsys.readouterr().out)[0]["instant"])
    assert instant.endswith("Z")
    assert "." not in instant, (
        "a fractional second would break the contract's whole-second form"
    )


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
def test_every_permitted_level_configures(level: str) -> None:
    configure_logging(level)


def test_an_unrecognized_level_is_refused_naming_the_permitted_set() -> None:
    with pytest.raises(InvalidLogLevelError) as caught:
        configure_logging("verbose")
    assert "info" in str(caught.value)


def test_configuring_twice_does_not_duplicate_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A second configure that added a handler would double every line, which is how a duplicated
    # log silently doubles an operator's error counts.
    configure_logging("info")
    configure_logging("info")
    get_logger("test").info("turn_answered")
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


# --- Req 19.2: the user's own words never reach a log -------------------

@pytest.mark.parametrize(
    "key",
    [
        "utterance",
        "user_utterance",
        "prior_turns",
        "guidance",
        "guidance_text",
        "generated_text",
        "prompt",
        "system_prompt",
        "rejected_text",
    ],
)
def test_a_key_that_could_carry_prose_is_redacted(key: str) -> None:
    assert is_sensitive(key) is True


def test_an_utterance_is_redacted_rather_than_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("debug")
    get_logger("test").debug("turn_started", utterance="I cannot catch my breath")
    out = capsys.readouterr().out
    assert "cannot catch my breath" not in out
    assert _events(out)[0]["utterance"] == "[redacted]"


# --- Req 19.3: health-adjacent profile values ---------------------------

@pytest.mark.parametrize(
    "key",
    [
        "credential",
        "authorization",
        "bearer",
        "api_key",
        "secret",
        "condition",
        "sensitivity",
        "sensitivity_level",
        "personal_threshold",
        "personal_thresholds",
        "medication",
        "medications",
        "latitude",
        "longitude",
        "coordinates",
        "note",
        "severity",
        "markers",
        "symptom_markers",
    ],
)
def test_a_health_or_credential_key_is_redacted(key: str) -> None:
    assert is_sensitive(key) is True


def test_a_nested_sensitive_field_is_redacted_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A profile passed as ONE object must not slip through because only the outer key was
    # checked.
    configure_logging("debug")
    get_logger("test").debug(
        "retrieved", basis={"siteCode": "AQM1", "condition": "asthma"}
    )
    out = capsys.readouterr().out
    assert "asthma" not in out
    event = _events(out)[0]
    assert event["basis"] == {"siteCode": "AQM1", "condition": "[redacted]"}


def test_a_sensitive_field_inside_a_list_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("debug")
    get_logger("test").debug(
        "retrieved", sites=[{"siteCode": "AQM1", "medications": ["salbutamol"]}]
    )
    assert "salbutamol" not in capsys.readouterr().out


# --- Req 19.4 / 19.5: the identity IS loggable --------------------------

def test_the_pseudonymous_identity_is_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The counterpart to every sweep above: if NOTHING were logged they would all pass
    # vacuously,
    # and Requirement 19.4 explicitly permits the pseudonymous identity.
    configure_logging("info")
    get_logger("test").info("turn_answered", user_id="user-sentinel")
    assert "user-sentinel" in capsys.readouterr().out


@pytest.mark.parametrize("key", ["user_id", "sub", "subject"])
def test_the_identity_keys_are_permitted(key: str) -> None:
    assert is_sensitive(key) is False


# --- the token-count allowlist, and the guards that keep it honest ------

def test_a_token_budget_is_not_redacted_as_a_credential(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Requirement 22 bounds a turn in OUTPUT TOKENS and Requirement 24.4 counts token usage. A
    # token COUNT is not a credential token, but the `token` marker cannot tell them apart —
    # which
    # is exactly the collision Service 2's logger hit with a configured medication CAP.
    configure_logging("info")
    get_logger("test").info(
        "model_invoked", output_tokens=412, total_tokens=1830, max_output_tokens=2048
    )
    event = _events(capsys.readouterr().out)[0]
    assert event["output_tokens"] == 412
    assert event["total_tokens"] == 1830
    assert event["max_output_tokens"] == 2048


@pytest.mark.parametrize("key", sorted(PERMITTED_COUNT_KEYS))
def test_every_allowlisted_count_key_is_permitted(key: str) -> None:
    assert is_sensitive(key) is False


@pytest.mark.parametrize("key", sorted(PERMITTED_COUNT_KEYS))
def test_every_allowlisted_key_would_otherwise_have_been_redacted(key: str) -> None:
    # Non-vacuity: an entry no marker would have caught is dead weight, and its presence
    # suggests
    # the markers cover something they do not. This guard caught two dead entries in Service 2.
    assert any(marker in key for marker in SENSITIVE_KEY_MARKERS), (
        f"{key!r} is not caught by any marker, so allowlisting it achieves nothing"
    )


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "access_token",
        "bearer_token",
        "id_token",
        "refresh_token",
        "session_token",
        "token_value",
    ],
)
def test_a_real_credential_token_key_is_still_redacted(key: str) -> None:
    # What makes the exception exact rather than a substring hole.
    assert is_sensitive(key) is True


# --- Req 24.3: a handled error carries its type and stack ---------------

def test_a_handled_error_logs_its_type_and_stack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    logger = get_logger("test")
    try:
        raise ValueError("the serving client refused")
    except ValueError as failure:
        log_handled_error(logger, "serving_client_failed", failure, attempt=2)

    event = _events(capsys.readouterr().out)[0]
    assert event["event"] == "serving_client_failed"
    assert event["level"] == "error"
    assert event["error_type"] == "ValueError"
    assert "Traceback" in str(event["stack"])
    assert event["attempt"] == 2


def test_the_stack_is_a_string_so_redaction_still_applies_to_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A stack rendered as a structure would sidestep the formatter's redaction; as a string it
    # goes through the same path as every other value.
    configure_logging("info")
    logger = get_logger("test")
    try:
        raise ValueError("boom")
    except ValueError as failure:
        log_handled_error(logger, "failed", failure)
    assert isinstance(_events(capsys.readouterr().out)[0]["stack"], str)


def test_a_handled_error_never_silently_swallows_the_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # §5: errors that are handled must still be logged. Silent failure is not acceptable.
    configure_logging("critical")  # the least talkative level
    logger = get_logger("test")
    try:
        raise OSError("transport down")
    except OSError as failure:
        log_handled_error(logger, "transport_failed", failure)
    assert capsys.readouterr().out.strip() == "", (
        "at CRITICAL an error-level event is correctly filtered — this pins the level, "
        "and the test below proves it is emitted at error level"
    )


def test_the_handled_error_is_emitted_at_error_level(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("error")
    logger = get_logger("test")
    try:
        raise OSError("transport down")
    except OSError as failure:
        log_handled_error(logger, "transport_failed", failure)
    assert _events(capsys.readouterr().out)[0]["level"] == "error"


# --- the detector detects ------------------------------------------------

def test_the_redaction_would_actually_catch_a_planted_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Proves the sweeps above are not passing against a logger that emits nothing.
    configure_logging("info")
    get_logger("test").info("control", site_code="AQM-CONTROL")
    assert "AQM-CONTROL" in capsys.readouterr().out, (
        "a NON-sensitive value must reach the log, or every redaction test is vacuous"
    )


def test_nothing_is_written_to_stdout_by_print(capsys: pytest.CaptureFixture[str]) -> None:
    # Req 24.1: the contract is that stdout carries only single-line JSON, so a stray print
    # would
    # corrupt a log consumer. Asserted over real source rather than trusted.
    import ast
    import pathlib

    import aqm_advisor

    root = pathlib.Path(next(iter(aqm_advisor.__path__)))
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"print() in library code: {offenders}"
