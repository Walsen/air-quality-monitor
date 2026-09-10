"""Tests for basis assembly and clinician deference (tasks 8.1, 8.2).

Two things here are decisions rather than mechanics, and both are asserted structurally.

**Which sensor is THE basis.** Service 2 returns `nearestSensors` as a list, one per location,
while a Basis_Summary names one site (Req 9.1, "the site the reading came from"). Choosing among
them by any criterion — highest sub-index, nearest, worst confidence — would be this service
DERIVING part of the basis, which Req 9.4 forbids. So the first as served is taken, and a test
asserts the assembly sorts nothing.

**Req 9.6, which is a prohibition rather than a value.** With no basis retrieved, assembly
returns `None` so there is nothing for a claim to rest on. Returning an empty `BasisSummary`
would satisfy a "basis present" check while carrying no provenance at all — the failure mode
that looks like success.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib

from aqm_advisor.domain.basis import assemble_basis
from aqm_advisor.domain.deference import (
    CLINICIAN_SUGGESTION_TEXT,
    DEFERENCE_TEXT,
    defers_to_plan,
    suggests_clinician,
)


def _body(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "nearestSensors": [
            {
                "siteCode": "AQM1",
                "siteName": "Test Site",
                "locationName": "home",
                "distanceKm": 1.2,
                "asOf": "2026-07-01T12:00:00Z",
                "drivingPollutant": "PM25",
                "overallAqi": 68,
                "band": "Moderate",
                "confidence": "high",
                "measurements": [
                    {
                        "species": "PM25",
                        "subIndex": 68,
                        "band": "Moderate",
                        "confidence": "high",
                    },
                    {
                        "species": "NO2",
                        "subIndex": 31,
                        "band": "Good",
                        "confidence": "medium",
                    },
                ],
            }
        ],
        "personalized": {
            "escalationSubIndex": 101,
            "thresholdSource": "sensitivity_level",
        },
        "basis": {
            "breakpointTable": "epa-2024-05-06",
            "calibrationStrategies": {"PM25": "rh_linear"},
            "humiditySource": "provider",
            "conversionSource": None,
            "nowcast": {"windowHours": 12, "hoursAvailable": 8, "weightFactor": 0.5},
            "records": [
                {
                    "siteCode": "AQM1",
                    "species": "PM25",
                    "dateTime": "2026-07-01T12:00:00Z",
                    "duration": "PT1H",
                }
            ],
        },
    }
    return {**base, **kwargs}


# --- Req 9.1, 9.3: every field is read ----------------------------------

def test_the_driving_pollutant_and_site_are_read() -> None:
    basis = assemble_basis(_body())
    assert basis is not None
    assert basis.driving_pollutant == "PM25"
    assert basis.site_code == "AQM1"
    assert basis.distance_km == 1.2


def test_the_reading_instant_is_read_as_an_aware_datetime() -> None:
    basis = assemble_basis(_body())
    assert basis is not None
    assert basis.as_of == dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def test_every_measurement_becomes_a_species_basis_in_served_order() -> None:
    # Order is Service 2's. Reordering would present a different pollutant as leading.
    basis = assemble_basis(_body())
    assert basis is not None
    assert [entry.species for entry in basis.per_species] == ["PM25", "NO2"]
    assert basis.per_species[1].confidence == "medium"


def test_the_threshold_and_its_source_are_named() -> None:
    basis = assemble_basis(_body())
    assert basis is not None
    assert basis.threshold == 101
    assert basis.threshold_source == "sensitivity_level"


def test_a_learned_threshold_source_is_carried_through() -> None:
    # Req 9.2 names the source AS Service 2 reported it, and `learned` is a value Service 2's
    # own
    # Req 32.7 introduced. Mapping it to something else would hide where the threshold came
    # from.
    body = _body(personalized={"escalationSubIndex": 84, "thresholdSource": "learned"})
    basis = assemble_basis(body)
    assert basis is not None
    assert basis.threshold_source == "learned"


def test_the_breakpoint_table_and_calibration_strategies_are_named() -> None:
    basis = assemble_basis(_body())
    assert basis is not None
    assert basis.breakpoint_table == "epa-2024-05-06"
    assert basis.calibration_strategies == {"PM25": "rh_linear"}


def test_the_nowcast_window_survives_unchanged() -> None:
    # Req 9.3/9.4: a partial window is carried as served, not normalised to a full one.
    basis = assemble_basis(_body())
    assert basis is not None
    assert basis.nowcast is not None
    assert basis.nowcast.window_hours == 12
    assert basis.nowcast.hours_available == 8
    assert basis.nowcast.weight_factor == 0.5


def test_the_records_become_reference_identifiers() -> None:
    # Req 20.2: the audit trail stores the identifiers of the records the basis named.
    basis = assemble_basis(_body())
    assert basis is not None
    assert basis.record_identifiers() == ("AQM1:PM25:2026-07-01T12:00:00Z:PT1H",)


def test_an_absent_nowcast_is_carried_as_absent() -> None:
    body = _body(
        basis={
            "breakpointTable": "epa-2024-05-06",
            "calibrationStrategies": {},
            "nowcast": None,
            "records": [],
        }
    )
    basis = assemble_basis(body)
    assert basis is not None
    assert basis.nowcast is None
    assert basis.nowcast_window_is_complete is None


# --- Req 9.6: no basis means no basis -----------------------------------

def test_a_body_with_no_basis_block_assembles_nothing() -> None:
    # Req 9.6: emit no claim that would require a basis. Returning an EMPTY BasisSummary would
    # pass a
    # "basis present" check while carrying no provenance — success-shaped failure.
    assert assemble_basis({"nearestSensors": [], "personalized": {}}) is None


def test_a_body_with_no_sensors_assembles_nothing() -> None:
    assert assemble_basis(_body(nearestSensors=[])) is None


def test_an_empty_body_assembles_nothing() -> None:
    assert assemble_basis({}) is None


def test_a_none_body_assembles_nothing() -> None:
    assert assemble_basis(None) is None


# --- Req 9.4: nothing is derived ----------------------------------------

def test_the_assembly_chooses_no_sensor_and_sorts_nothing() -> None:
    # Service 2 returns one sensor per location while a Basis_Summary names ONE site. Choosing
    # among
    # them by sub-index, distance or confidence would be deriving part of the basis, which Req
    # 9.4
    # forbids — so the first as served is taken and nothing is ordered here.
    from aqm_advisor.domain import basis as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for forbidden in ("sorted", "min", "max", "sum", "list.sort"):
        assert forbidden not in called, f"the assembly calls {forbidden}"


def test_the_assembly_performs_no_arithmetic() -> None:
    from aqm_advisor.domain import basis as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    operators = [
        type(node.op).__name__
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult | ast.Div | ast.Add | ast.Sub | ast.Pow | ast.FloorDiv)
    ]
    assert operators == [], f"the assembly computes: {operators}"


def test_the_first_served_sensor_is_the_one_used() -> None:
    # The behavioural half of the structural test above: a second sensor with a worse reading
    # must not
    # displace the first, because displacing it would mean a rule was applied.
    body = _body(
        nearestSensors=[
            {
                "siteCode": "FIRST",
                "distanceKm": 9.9,
                "asOf": "2026-07-01T12:00:00Z",
                "drivingPollutant": "PM25",
                "measurements": [],
            },
            {
                "siteCode": "WORSE",
                "distanceKm": 0.1,
                "asOf": "2026-07-01T12:00:00Z",
                "drivingPollutant": "NO2",
                "measurements": [],
            },
        ]
    )
    basis = assemble_basis(body)
    assert basis is not None
    assert basis.site_code == "FIRST"


# --- Req 11: clinician deference ----------------------------------------

def test_the_deference_text_points_at_the_plan_and_the_clinician() -> None:
    lowered = DEFERENCE_TEXT.casefold()
    assert "plan" in lowered
    assert "clinician" in lowered


def test_neither_text_asks_the_user_to_record_the_plan() -> None:
    # Req 11.3: never ask the user to record the contents of an action plan. It is the
    # highest-value
    # health data in the conversation and Req 19's minimisation says not to hold it — so the
    # sentence
    # that would invite it must not exist.
    for text in (DEFERENCE_TEXT, CLINICIAN_SUGGESTION_TEXT):
        lowered = text.casefold()
        for invitation in (
            "tell me what your plan",
            "share your plan",
            "upload your plan",
            "what does your plan say",
            "enter your plan",
            "record your plan",
        ):
            assert invitation not in lowered, text


def test_the_clinician_suggestion_does_not_characterise_the_trajectory() -> None:
    # Req 11.4: suggest contacting a clinician on reported worsening, WITHOUT characterising the
    # trajectory clinically. "Your asthma is deteriorating" is the determination Req 8.3
    # forbids,
    # arriving by way of a sympathetic sentence.
    lowered = CLINICIAN_SUGGESTION_TEXT.casefold()
    for clinical in (
        "deteriorating",
        "worsening asthma",
        "exacerbation",
        "progressive",
        "losing control",
        "uncontrolled",
    ):
        assert clinical not in lowered, CLINICIAN_SUGGESTION_TEXT


def test_both_texts_pass_the_forbidden_claim_check() -> None:
    from aqm_advisor.domain.forbidden import administration_near_medication, forbidden_matches

    for text in (DEFERENCE_TEXT, CLINICIAN_SUGGESTION_TEXT):
        assert forbidden_matches(text) == (), text
        assert not administration_near_medication(text), text


def test_neither_text_states_a_number() -> None:
    from aqm_advisor.domain.grounding import numerals

    for text in (DEFERENCE_TEXT, CLINICIAN_SUGGESTION_TEXT):
        assert numerals(text) == (), text


# --- Req 11.2: a plan-governed question is deferred, not answered --------

def test_a_plan_governed_question_is_recognised() -> None:
    for utterance in (
        "should I step up my treatment today?",
        "do I need to start my rescue pack?",
        "should I increase my preventer while the air is bad?",
        "is it time to follow my action plan?",
    ):
        assert defers_to_plan(utterance) is True, utterance


def test_an_ordinary_exposure_question_is_not_deferred() -> None:
    # Non-vacuity: a recogniser that deferred everything would refuse to answer the questions
    # this
    # service exists to answer.
    for utterance in (
        "is it safe to run this evening?",
        "how bad is the air near home?",
        "will tomorrow be better?",
        "which pollutant is driving it today?",
    ):
        assert defers_to_plan(utterance) is False, utterance


def test_reported_worsening_suggests_a_clinician() -> None:
    for utterance in (
        "my symptoms have been getting worse all week",
        "I have been needing my reliever more often than usual",
        "it has been worse each day for the last few days",
    ):
        assert suggests_clinician(utterance) is True, utterance


def test_a_single_bad_day_is_not_a_worsening_trajectory() -> None:
    # The distinction Req 11.4 draws is over TIME. Treating one bad day as a trajectory would
    # send
    # everyone to their clinician on the first poor-air day and make the suggestion meaningless.
    for utterance in (
        "today has been rough",
        "the air is bad and I noticed it",
        "I felt it on my run this morning",
    ):
        assert suggests_clinician(utterance) is False, utterance


def test_the_deference_module_reads_no_clock_and_no_network() -> None:
    from aqm_advisor.domain import deference as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("httpx", "boto3", "socket", "random", "datetime", "time"):
        assert forbidden not in imported
