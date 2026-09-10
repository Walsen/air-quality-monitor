"""Tests for forecast, activity timing, confidence, dose and pollen (tasks 7.3, 7.4).

Four requirements here forbid the service from DERIVING something: Req 13.5 (compute no
forecast), Req 16 (explain the dose without recomputing it), Req 12.1 (compute no weighting),
Req 15.6 (do not derive a second weakness signal from the nowcast hour counts). They share one
enforceable shape, and `test_the_reporting_module_performs_no_arithmetic` is it — this module
reads and relays, so it should contain no arithmetic operator at all. That single assertion
covers all four, and it fails on the FIRST multiplication someone adds rather than after the
derived number has reached a user.

`test_no_code_path_gates_a_disclosure_on_the_nowcast_hours` is task 7.4's named check. It
carries a self-check because a detector aimed at the wrong node reports a guarantee it is not
providing.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from aqm_advisor.domain.reporting import (
    GASEOUS_SAME_DAY_TEXT,
    PARTICULATE_LAG_TEXT,
    TIMING_TEMPLATES,
    confidence_view,
    dose_view,
    forecast_view,
    pollen_view,
)


def _forecast(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tomorrowAqi": 72,
        "trend": "rising",
        "source": "test-provider",
        "retrievedAt": "2026-07-01T12:00:00Z",
        "degraded": False,
    }
    return {**base, **kwargs}


def _measurement(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "species": "PM25",
        "confidence": "high",
        "qualityFlag": "calibrated",
        "subIndex": 68,
        "band": "Moderate",
    }
    return {**base, **kwargs}


# --- Req 13.1, 13.5: the forecast is read, never computed ----------------

def test_the_trend_and_its_provider_are_reported() -> None:
    view = forecast_view(_forecast())
    assert view.trend == "rising"
    assert view.source == "test-provider"


def test_a_degraded_forecast_yields_no_next_day_value() -> None:
    # Req 7.5: with `degraded` true, state no next-day value and say the forecast was
    # unavailable.
    view = forecast_view(_forecast(degraded=True, tomorrowAqi=72))
    assert view.tomorrow_aqi is None
    assert view.available is False


def test_a_degraded_forecast_still_reports_its_provider() -> None:
    # Req 7.4's attribution survives degradation: saying "the forecast from X was unavailable"
    # is
    # honest, while an unattributed absence tells an operator nothing about what failed.
    assert forecast_view(_forecast(degraded=True)).source == "test-provider"


def test_a_healthy_forecast_reports_its_next_day_value() -> None:
    # Non-vacuity for the degraded tests: if the value were always suppressed, the forecast
    # would be
    # useless and every test above would still pass.
    view = forecast_view(_forecast())
    assert view.tomorrow_aqi == 72
    assert view.available is True


def test_a_missing_forecast_block_is_unavailable_rather_than_an_error() -> None:
    view = forecast_view(None)
    assert view.available is False
    assert view.tomorrow_aqi is None
    assert view.source is None


def test_a_forecast_with_no_trend_is_not_invented() -> None:
    assert forecast_view(_forecast(trend=None)).trend is None


# --- Req 13.2, 13.3, 13.4: the lag is a pattern, not a prediction --------

def test_the_lag_text_describes_a_general_pattern() -> None:
    # Req 13.4: present the lag as a general pattern from the evidence base, never as a
    # prediction
    # that THIS user will develop symptoms.
    lowered = PARTICULATE_LAG_TEXT.casefold()
    assert "can" in lowered or "often" in lowered or "typically" in lowered
    for predictive in ("you will", "you'll", "expect to", "you are going to"):
        assert predictive not in lowered


def test_the_lag_and_the_same_day_effect_are_distinguished() -> None:
    # Req 13.3: gaseous effects are same-day and must be told apart from the particulate lag.
    # Conflating them would have a user relate today's symptoms to the wrong day's exposure.
    assert PARTICULATE_LAG_TEXT != GASEOUS_SAME_DAY_TEXT
    assert "same day" in GASEOUS_SAME_DAY_TEXT.casefold()


def test_neither_explanation_states_a_number() -> None:
    # Req 7.1: every numeral in guidance must be a Retrieved_Value. The lag is "about three
    # days" in
    # words, so it needs no structural constant and cannot be an ungrounded 3.
    from aqm_advisor.domain.grounding import numerals

    assert numerals(PARTICULATE_LAG_TEXT) == ()
    assert numerals(GASEOUS_SAME_DAY_TEXT) == ()


# --- Req 14.2, 14.4: timing templates ------------------------------------

def test_no_timing_template_names_a_clock_hour() -> None:
    # Req 14.2: express timing as a comparison between periods the data supports, and never name
    # a
    # specific hour the data does not distinguish. "Go out at 7am" asserts a resolution the
    # retrieval
    # does not have.
    import re

    for template in TIMING_TEMPLATES:
        clock = re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", template, re.IGNORECASE)
        assert not clock, template
        assert not re.search(r"\b\d{1,2}:\d{2}\b", template), template


def test_no_timing_template_instructs_starting_or_stopping_exercise() -> None:
    # Req 14.4: never direct the user to stop or start exercising; frame guidance as reducing
    # exposure
    # while doing what they intend to do.
    for template in TIMING_TEMPLATES:
        lowered = template.casefold()
        for instruction in (
            "stop exercising",
            "do not exercise",
            "don't exercise",
            "avoid exercise",
            "you should exercise",
            "start exercising",
        ):
            assert instruction not in lowered, template


def test_timing_templates_pass_the_forbidden_claim_check() -> None:
    from aqm_advisor.domain.forbidden import administration_near_medication, forbidden_matches

    for template in TIMING_TEMPLATES:
        assert forbidden_matches(template) == (), template
        assert not administration_near_medication(template), template


def test_there_are_timing_templates_to_check() -> None:
    assert len(TIMING_TEMPLATES) >= 2


# --- Req 15.1-15.4: confidence -------------------------------------------

def test_the_confidence_is_read_from_the_measurement() -> None:
    assert confidence_view(_measurement(confidence="medium")).confidence == "medium"


def test_a_confidence_below_the_highest_must_be_disclosed() -> None:
    # Req 15.2: say so in the GUIDANCE rather than only in the basis.
    assert confidence_view(_measurement(confidence="medium")).disclose is True
    assert confidence_view(_measurement(confidence="low")).disclose is True


def test_the_highest_confidence_needs_no_disclosure() -> None:
    # Non-vacuity: a view that always disclosed would put a caveat on every response and train
    # the
    # user to ignore it — the same cost as never disclosing.
    assert confidence_view(_measurement(confidence="high")).disclose is False


def test_a_qualified_quality_flag_is_disclosed() -> None:
    # Req 15.4: a fault, a conflict or an uncalibrated value is disclosed and not presented as a
    # plain
    # measurement.
    for flag in ("suspect_fault", "suspect_conflict", "uncalibrated"):
        view = confidence_view(_measurement(qualityFlag=flag))
        assert view.qualified is True, flag
        assert view.disclose is True, flag


def test_a_calibrated_flag_is_not_qualified() -> None:
    assert confidence_view(_measurement(qualityFlag="calibrated")).qualified is False


def test_an_extrapolated_calibration_is_still_qualified() -> None:
    # It is a calibration that ran out of data, which Req 15.3's "never present a low-cost
    # reading as
    # reference-grade" is squarely about.
    view = confidence_view(_measurement(qualityFlag="calibrated_extrapolated"))
    assert view.qualified is True


def test_a_missing_measurement_discloses_rather_than_assuming_the_best() -> None:
    # Fail-safe direction: an absent confidence is not evidence of a good one.
    view = confidence_view(None)
    assert view.confidence is None
    assert view.disclose is True


# --- Req 15.6: exactly ONE weakness authority ----------------------------

def test_no_code_path_gates_a_disclosure_on_the_nowcast_hours() -> None:
    # TASK 7.4's named check. Req 15.6 makes the confidence Service 2 returned the single
    # authority on
    # measurement weakness, because Service 2 already capped it for an incomplete window. A
    # second
    # derivation here could disagree with Service 2 about the same reading, and deciding
    # weakness from
    # the hour counts is re-deriving part of the basis, which Req 9.4 forbids.
    from aqm_advisor.domain import reporting as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    assert _compares_nowcast_hours(ast.parse(source)) == [], (
        "a code path derives weakness from the nowcast hour counts"
    )


def _compares_nowcast_hours(tree: ast.AST) -> list[str]:
    """Comparisons that put the two nowcast hour counts on opposite sides."""
    names = {"hours_available", "hoursAvailable", "window_hours", "windowHours"}
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        rendered = ast.unparse(node)
        mentioned = {name for name in names if name in rendered}
        if {"hours_available", "hoursAvailable"} & mentioned and {
            "window_hours",
            "windowHours",
        } & mentioned:
            found.append(rendered)
    return found


def test_the_nowcast_comparison_detector_would_catch_one() -> None:
    # Self-check. Without it a detector aimed at the wrong node would pass on code that DID
    # derive a
    # second weakness signal, and report a guarantee it was not providing.
    source = "def f(n):\n    if n.hours_available < n.window_hours:\n        return True\n"
    planted = ast.parse(source)
    assert _compares_nowcast_hours(planted) != []


def test_the_detector_ignores_an_innocent_mention() -> None:
    # Near-miss guard: reading either count is fine, and reporting the window is Req 9.3's
    # traceability. Only a COMPARISON between the two derives a verdict.
    innocent = ast.parse("def f(n):\n    return n.hours_available, n.window_hours\n")
    assert _compares_nowcast_hours(innocent) == []


# --- Req 16: the dose is explained, never recomputed ---------------------

def test_the_dose_is_read_from_the_retrieved_block() -> None:
    view = dose_view({"inhaledDose": 42.5, "doseBasis": "routine_windows"})
    assert view.dose == 42.5
    assert view.basis == "routine_windows"


def test_a_dose_of_none_is_unavailable_rather_than_zero() -> None:
    # Zero is a measured dose; absent is no measurement. Collapsing them would report a real
    # value
    # the retrieval never produced.
    view = dose_view({"inhaledDose": None, "doseBasis": "none"})
    assert view.dose is None
    assert view.available is False


def test_a_zero_dose_is_available() -> None:
    assert dose_view({"inhaledDose": 0.0, "doseBasis": "whole_day"}).available is True


def test_unavailable_dose_windows_are_reported() -> None:
    # Service 2 reports truncated windows so the gap is explicit rather than invisible.
    view = dose_view(
        {"inhaledDose": 1.0, "doseBasis": "routine_windows", "unavailableDoseWindows": 2}
    )
    assert view.unavailable_windows == 2


def test_the_reporting_module_performs_no_arithmetic() -> None:
    # THE structural check, covering four requirements at once: Req 13.5 (compute no forecast),
    # Req 16 (explain the dose without recomputing it), Req 12.1 (compute no weighting) and Req
    # 15.6
    # (derive no second weakness signal). This module reads and relays, so any arithmetic
    # operator is
    # evidence it has started deriving — and it fails on the first multiplication someone adds
    # rather
    # than after the derived number has reached a user.
    from aqm_advisor.domain import reporting as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    operators = [
        type(node.op).__name__
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult | ast.Div | ast.Add | ast.Sub | ast.Pow | ast.FloorDiv)
    ]
    assert operators == [], f"the reporting module computes: {operators}"


def test_the_arithmetic_detector_would_catch_a_computation() -> None:
    # Self-check for the assertion above.
    planted = ast.parse("def dose(c, r, h):\n    return c * r * h\n")
    operators = [
        type(node.op).__name__ for node in ast.walk(planted) if isinstance(node, ast.BinOp)
    ]
    assert "Mult" in operators


# --- Req 17: pollen ------------------------------------------------------

def test_the_pollen_categories_are_reported_as_returned() -> None:
    view = pollen_view({"pollen": {"grass": "high", "tree": "low"}})
    assert view.categories == {"grass": "high", "tree": "low"}
    assert view.available is True


def test_a_missing_pollen_block_is_unavailable() -> None:
    # Req 17.5: say the outlook is unavailable where it matters, rather than staying silent
    # about it.
    view = pollen_view({"pollen": None})
    assert view.available is False
    assert view.categories == {}


def test_the_synergy_is_flagged_only_when_both_are_elevated() -> None:
    # Req 17.3: state the pollen-pollution synergy when BOTH are elevated. Flagging it on either
    # alone
    # would make the interaction claim whenever one factor was present, which is not what it
    # says.
    assert pollen_view({"pollen": {"grass": "high"}}, pollution_elevated=True).synergy is True
    assert pollen_view({"pollen": {"grass": "high"}}, pollution_elevated=False).synergy is False
    assert pollen_view({"pollen": {"grass": "low"}}, pollution_elevated=True).synergy is False


def test_the_synergy_is_false_with_no_pollen_data() -> None:
    assert pollen_view({"pollen": None}, pollution_elevated=True).synergy is False


def test_the_views_are_frozen() -> None:
    for view in (
        forecast_view(_forecast()),
        confidence_view(_measurement()),
        dose_view({"inhaledDose": 1.0}),
        pollen_view({"pollen": None}),
    ):
        with pytest.raises((AttributeError, TypeError)):
            view.available = True  # type: ignore[misc,union-attr]
