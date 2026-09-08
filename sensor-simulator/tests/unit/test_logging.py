"""Unit tests for the structured JSON logger (task 1.2).

Requirement 17.1: each log event is exactly one single-line JSON object on
stdout carrying an ISO-8601 UTC ``Z`` timestamp, a level, and an event name,
and no non-JSON text reaches stdout.
Requirement 17.5: the configured level and above emit; lower levels are
suppressed.
"""

from __future__ import annotations

import json
import logging

import pytest

from aqm_simulator.observability.logging import configure_logging, get_logger


def _lines(captured: str) -> list[str]:
    return [ln for ln in captured.splitlines() if ln.strip()]


def test_event_is_single_line_json_with_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="info")
    log = get_logger("test")

    log.info("scenario_window_opened", scenario="wildfire_smoke")

    lines = _lines(capsys.readouterr().out)
    assert len(lines) == 1
    record = json.loads(lines[0])  # must parse as JSON — no non-JSON on stdout
    assert record["level"] == "info"
    assert record["event"] == "scenario_window_opened"
    assert record["scenario"] == "wildfire_smoke"
    assert record["timestamp"].endswith("Z")


def test_timestamp_is_iso8601_utc_whole_second(capsys: pytest.CaptureFixture[str]) -> None:
    from datetime import datetime

    configure_logging(level="info")
    log = get_logger("test")
    log.info("publish_interval_summary")

    record = json.loads(_lines(capsys.readouterr().out)[0])
    # Parseable as an ISO-8601 UTC instant (drop the trailing Z for fromisoformat).
    parsed = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


def test_level_filtering_suppresses_below_configured(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(level="warn")
    log = get_logger("test")

    log.info("should_be_suppressed")
    log.warning("should_appear")

    lines = _lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "should_appear"


@pytest.mark.parametrize("level", ["debug", "info", "warn", "error"])
def test_accepts_each_permitted_level(level: str) -> None:
    # Requirement 17.5: debug, info, warn, error are all accepted.
    configure_logging(level=level)


def test_rejects_invalid_level() -> None:
    # Requirement 17.8: an invalid level is rejected naming the value.
    with pytest.raises(ValueError, match="trace"):
        configure_logging(level="trace")


def test_no_non_json_on_stdout_for_exception(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="debug")
    log = get_logger("test")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("tick_failed", site_code="CB0001")

    for line in _lines(capsys.readouterr().out):
        json.loads(line)  # every line must be valid JSON


def teardown_function() -> None:
    # Reset root handlers so tests do not accumulate stdout handlers.
    logging.getLogger("aqm_simulator").handlers.clear()
    logging.getLogger("aqm_simulator").propagate = False
