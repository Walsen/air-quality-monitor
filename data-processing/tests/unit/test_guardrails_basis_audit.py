"""Unit tests for the Basis, the Guardrail_Enforcer, and the audit writer (tasks 24.1-24.3).

Requirements 25.1-25.12, 8.11, 9.7, 10.2, 11.9.

REQUIREMENT 25.11 DEMANDS A 500, WHICH §5 OTHERWISE FORBIDS — and the two do not conflict,
because their subjects differ. §5 says BAD INPUT must never produce a 500, and that stands: bad
input gets 400. Requirement 25.11 is about the service's own OUTPUT failing the service's own
guardrail check, which is precisely what a 500 means, and the requirement states the reasoning
outright: "emitting a guardrail-violating body is worse than emitting none". That is the fourth
time in this spec that two clauses look contradictory until you ask whose subject each names,
after Requirements 3.3/3.6, 15.9/17.9 and 23.5/23.6.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.ports.protocols import AuditIdentifiers
from aqm_ingestion.serving.audit import (
    DEFAULT_AUDIT_RETENTION_DAYS,
    audit_record_for,
)
from aqm_ingestion.serving.basis import (
    Basis,
    RecordReference,
    SpeciesBasis,
    assemble_basis,
)
from aqm_ingestion.serving.guardrails import (
    ADVISORY_SCOPE,
    DEFAULT_FORBIDDEN_PATTERNS,
    GuardrailEnvelope,
    GuardrailSettings,
    GuardrailViolationError,
    enforce_guardrails,
    guardrail_envelope,
)

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _reading(
    species: str = "PM25",
    *,
    strategy: str = "rh_linear",
    table: str = "epa-2024-05-06",
    humidity_source: str = "channel",
    conversion_source: str | None = None,
    nowcast_window: int | None = 12,
    site_code: str = "SITE1",
) -> CalibratedReading:
    return CalibratedReading(
        key=DedupKey(
            site_code=site_code, species=species, interval_start=_NOW, duration="PT1H"
        ),
        reported_value=20.0,
        corrected_value=18.0,
        units="ug.m-3" if species == "PM25" else "ppb",
        quality_flag=QualityFlag.CALIBRATED,
        confidence=Confidence.HIGH,
        calibration_strategy=strategy,
        breakpoint_table=table,
        ratification_status="R",
        ingested_at=_NOW,
        archive_id=f"archive-{site_code}-{species}",
        sub_index=55,
        band="Moderate",
        humidity_source=humidity_source,  # type: ignore[arg-type]
        conversion_source=conversion_source,  # type: ignore[arg-type]
        conversion_temperature_k=288.15 if conversion_source else None,
        conversion_pressure_pa=74000.0 if conversion_source else None,
        nowcast_window_hours=nowcast_window,
        nowcast_hours_available=9 if nowcast_window else None,
        nowcast_weight_factor=0.7 if nowcast_window else None,
    )


# --- 24.1 / Req 25.5: the basis is populated from stored provenance ------

def test_the_basis_names_the_breakpoint_table() -> None:
    basis = assemble_basis([_reading()])
    assert basis.breakpoint_table == "epa-2024-05-06"


def test_the_basis_names_the_calibration_strategy_per_species() -> None:
    # Req 8.11 and 25.5 both say PER SPECIES: NO2 defaults to identity while PM2.5 uses
    # rh_linear, so one shared strategy field would misreport one of them.
    basis = assemble_basis(
        [_reading("PM25", strategy="rh_linear"), _reading("NO2", strategy="identity")]
    )
    strategies = {entry.species: entry.calibration_strategy for entry in basis.species}
    assert strategies == {"PM25": "rh_linear", "NO2": "identity"}


def test_the_basis_names_the_rh_source() -> None:
    basis = assemble_basis([_reading(humidity_source="provider")])
    assert basis.species[0].humidity_source == "provider"


def test_the_basis_names_the_conversion_source_and_conditions() -> None:
    # Req 9.7 wants the temperature and pressure USED as well as their source, so a reviewer can
    # recompute the conversion rather than take it on trust.
    basis = assemble_basis([_reading("NO2", conversion_source="default")])
    entry = basis.species[0]
    assert entry.conversion_source == "default"
    assert entry.conversion_temperature_k == 288.15
    assert entry.conversion_pressure_pa == 74000.0


def test_the_basis_names_the_nowcast_window_and_coverage() -> None:
    # Req 11.9: hours available, window length, and the weight factor used.
    basis = assemble_basis([_reading(nowcast_window=12)])
    entry = basis.species[0]
    assert entry.nowcast_window_hours == 12
    assert entry.nowcast_hours_available == 9
    assert entry.nowcast_weight_factor == 0.7


def test_a_species_without_a_nowcast_reports_no_window() -> None:
    # NO2 has no NowCast window, and reporting a window it never used would be a fabrication.
    basis = assemble_basis([_reading("NO2", nowcast_window=None)])
    assert basis.species[0].nowcast_window_hours is None


def test_the_basis_carries_one_record_reference_per_reading() -> None:
    basis = assemble_basis(
        [_reading("PM25", site_code="A"), _reading("NO2", site_code="B")]
    )
    assert len(basis.records) == 2


def test_a_record_reference_identifies_its_reading() -> None:
    reference = assemble_basis([_reading()]).records[0]
    assert reference.site_code == "SITE1"
    assert reference.species == "PM25"
    assert reference.interval_start == _NOW
    assert reference.duration == "PT1H"
    assert reference.archive_id == "archive-SITE1-PM25"


def test_the_basis_entries_are_ordered() -> None:
    # §2: the basis reaches the response, so its order must be defined rather than incidental.
    forwards = assemble_basis([_reading("PM25"), _reading("NO2")])
    backwards = assemble_basis([_reading("NO2"), _reading("PM25")])
    assert [e.species for e in forwards.species] == [e.species for e in backwards.species]
    assert forwards.records == backwards.records


def test_an_empty_reading_set_yields_an_empty_basis() -> None:
    # Req 20.9 permits a site with no fresh Reading, so this is reachable rather than defensive.
    basis = assemble_basis([])
    assert basis.species == ()
    assert basis.records == ()
    assert basis.breakpoint_table is None


def test_disagreeing_breakpoint_tables_are_refused() -> None:
    # Req 25.5 says THE Breakpoint_Table identifier, singular. Two readings indexed against
    # different tables cannot be described by one identifier, and picking either would
    # misreport the other — so it is a caller bug, raised rather than papered over (§5).
    with pytest.raises(ValueError, match="one Breakpoint_Table"):
        assemble_basis(
            [_reading("PM25", table="epa-2024-05-06"), _reading("NO2", table="other")]
        )


def test_one_species_appears_once_in_the_basis() -> None:
    # Two sites both reporting PM2.5 share a strategy; listing it twice would imply otherwise.
    basis = assemble_basis(
        [_reading("PM25", site_code="A"), _reading("PM25", site_code="B")]
    )
    assert [entry.species for entry in basis.species] == ["PM25"]
    assert len(basis.records) == 2


def test_the_basis_shape_is_exactly_the_requirements_members() -> None:
    assert set(Basis.__dataclass_fields__) == {"breakpoint_table", "species", "records"}
    assert set(SpeciesBasis.__dataclass_fields__) == {
        "species",
        "calibration_strategy",
        "humidity_source",
        "conversion_source",
        "conversion_temperature_k",
        "conversion_pressure_pa",
        "nowcast_window_hours",
        "nowcast_hours_available",
        "nowcast_weight_factor",
    }
    assert set(RecordReference.__dataclass_fields__) == {
        "site_code",
        "species",
        "interval_start",
        "duration",
        "archive_id",
    }


# --- 24.2 / Req 25.1-25.4: the guardrail envelope ----------------------

def test_the_advisory_scope_is_exactly_exposure_reduction() -> None:
    assert ADVISORY_SCOPE == "exposure-reduction"
    assert guardrail_envelope().advisory_scope == "exposure-reduction"


def test_the_disclaimer_is_non_empty() -> None:
    assert guardrail_envelope().disclaimer.strip() != ""


def test_the_disclaimer_defers_to_the_clinicians_action_plan() -> None:
    # Req 25.3 names both halves: exposure guidance rather than medical advice, AND deference
    # to the action plan. A disclaimer with only the first half satisfies neither reading.
    disclaimer = guardrail_envelope().disclaimer.lower()
    assert "not medical advice" in disclaimer or "rather than medical advice" in disclaimer
    assert "action plan" in disclaimer


def test_the_emergency_guidance_names_the_red_flag_symptoms() -> None:
    # Req 25.2 lists three symptoms specifically, and generic "seek help" wording would not tell
    # a reader WHICH ones warrant emergency services. Asserted as the three CONCEPTS rather than
    # exact strings: the requirement names symptoms, not required phrasing, and the shipped
    # wording says "lips or face look blue", which is broader than "blue lips" and no less
    # clear.
    guidance = guardrail_envelope().emergency_guidance.lower()
    assert "breathless" in guidance
    assert "reliever" in guidance
    assert "lips" in guidance
    assert "blue" in guidance


def test_the_emergency_guidance_directs_to_emergency_services() -> None:
    guidance = guardrail_envelope().emergency_guidance.lower()
    assert "emergency" in guidance


def test_the_envelope_is_exactly_three_members() -> None:
    assert set(GuardrailEnvelope.__dataclass_fields__) == {
        "disclaimer",
        "advisory_scope",
        "emergency_guidance",
    }


def test_the_default_envelope_texts_pass_the_forbidden_phrase_check() -> None:
    # The guardrail text itself must not trip the guardrail. It mentions a reliever, which is
    # close to medication language, so this is a real risk rather than a formality.
    enforce_guardrails({"guardrails": _envelope_as_dict()})


def _envelope_as_dict() -> dict[str, str]:
    envelope = guardrail_envelope()
    return {
        "disclaimer": envelope.disclaimer,
        "advisoryScope": envelope.advisory_scope,
        "emergencyGuidance": envelope.emergency_guidance,
    }


# --- 24.2 / Req 25.4, 25.11: the forbidden-phrase check ----------------

def test_a_diagnosis_phrase_is_refused() -> None:
    with pytest.raises(GuardrailViolationError):
        enforce_guardrails({"advice": "You are having an asthma attack."})


def test_an_exacerbation_claim_is_refused() -> None:
    with pytest.raises(GuardrailViolationError):
        enforce_guardrails({"advice": "You are experiencing an exacerbation."})


def test_a_medication_name_is_refused() -> None:
    with pytest.raises(GuardrailViolationError):
        enforce_guardrails({"advice": "Take salbutamol now."})


def test_a_dosing_instruction_is_refused() -> None:
    with pytest.raises(GuardrailViolationError):
        enforce_guardrails({"advice": "Increase your dose to two puffs."})


def test_a_treatment_change_instruction_is_refused() -> None:
    with pytest.raises(GuardrailViolationError):
        enforce_guardrails({"advice": "Stop taking your preventer."})


def test_the_violation_names_the_pattern_it_matched() -> None:
    # §5: a documented failure names the offending thing, and an operator needs to know WHICH
    # pattern fired to fix the generator.
    with pytest.raises(GuardrailViolationError) as caught:
        enforce_guardrails({"advice": "Take salbutamol now."})
    assert caught.value.pattern


def test_the_check_is_case_insensitive() -> None:
    with pytest.raises(GuardrailViolationError):
        enforce_guardrails({"advice": "TAKE SALBUTAMOL NOW."})


def test_the_check_reaches_nested_values() -> None:
    # A violating phrase inside a nested member is still in the body a client receives.
    with pytest.raises(GuardrailViolationError):
        enforce_guardrails({"sites": [{"note": "you are having an attack"}]})


def test_a_clean_body_passes() -> None:
    # The counterpart, without which every refusal above could pass on a check that always
    # raises.
    enforce_guardrails(
        {"advice": "Air quality is moderate; consider a quieter route.", "aqi": 55}
    )


def test_a_non_string_value_does_not_break_the_check() -> None:
    enforce_guardrails({"aqi": 55, "trend": None, "fresh": True, "values": [1, 2.5]})


def test_the_forbidden_patterns_cover_diagnosis_and_dosing() -> None:
    # Req 25.11 says the DEFAULTS cover diagnosis and dosing language.
    joined = " ".join(DEFAULT_FORBIDDEN_PATTERNS).lower()
    assert "attack" in joined
    assert "exacerbation" in joined
    assert "dose" in joined


def test_the_patterns_are_configurable() -> None:
    with pytest.raises(GuardrailViolationError):
        enforce_guardrails(
            {"advice": "banned wording"},
            settings=GuardrailSettings(forbidden_patterns=("banned wording",)),
        )


def test_a_configured_pattern_set_replaces_the_defaults() -> None:
    # Otherwise "configurable" would only mean "extendable", and an operator could never
    # narrow a pattern that fired on legitimate text.
    enforce_guardrails(
        {"advice": "Take salbutamol now."},
        settings=GuardrailSettings(forbidden_patterns=("something-else",)),
    )


# --- 24.3 / Req 25.7, 25.8: the audit record --------------------------

def test_the_audit_record_names_the_required_fields() -> None:
    basis = assemble_basis([_reading("PM25"), _reading("NO2", strategy="identity")])
    record = audit_record_for(
        user_id="user-a",
        served_at=_NOW,
        route="/v1/advice",
        basis=basis,
        threshold_crossed=True,
    )
    assert record.user_id == "user-a"
    assert record.served_at == _NOW
    assert record.route == "/v1/advice"
    assert record.breakpoint_table == "epa-2024-05-06"
    assert record.calibration_strategies == ("identity", "rh_linear")
    assert record.threshold_crossed is True
    assert len(record.record_references) == 2


def test_the_strategy_set_is_sorted_and_deduplicated() -> None:
    # Req 25.7 says the strategy SET, so a repeat is not information — and sorting keeps the
    # audit entry byte-comparable across runs (§2).
    basis = assemble_basis(
        [_reading("PM25", site_code="A"), _reading("PM25", site_code="B")]
    )
    record = audit_record_for(
        user_id="user-a",
        served_at=_NOW,
        route="/v1/advice",
        basis=basis,
        threshold_crossed=False,
    )
    assert record.calibration_strategies == ("rh_linear",)


def test_the_audit_record_stores_no_health_adjacent_field() -> None:
    # Req 25.8 enumerates what must NOT be there: no Condition, Sensitivity_Level, threshold
    # value, User_Location coordinate, forecast or pollen value. Asserted on the field NAMES so
    # a later addition fails loudly rather than quietly making the audit a second copy.
    forbidden = (
        "condition",
        "sensitivity",
        "threshold_value",
        "latitude",
        "longitude",
        "coordinate",
        "location",
        "forecast",
        "pollen",
        "dose",
    )
    for field in AuditIdentifiers.__dataclass_fields__:
        assert not any(word in field.lower() for word in forbidden), field


def test_the_crossing_flag_is_a_boolean_not_the_threshold() -> None:
    # Req 25.7 wants WHETHER a crossing was reported; Req 25.8 forbids the threshold VALUE. A
    # field holding the threshold would satisfy the first and violate the second.
    annotation = AuditIdentifiers.__dataclass_fields__["threshold_crossed"].type
    assert "bool" in str(annotation)


def test_the_default_audit_retention_is_365_days() -> None:
    assert DEFAULT_AUDIT_RETENTION_DAYS == 365


def test_an_audit_record_is_deterministic() -> None:
    basis = assemble_basis([_reading()])
    first = audit_record_for(
        user_id="user-a",
        served_at=_NOW,
        route="/v1/advice",
        basis=basis,
        threshold_crossed=False,
    )
    second = audit_record_for(
        user_id="user-a",
        served_at=_NOW,
        route="/v1/advice",
        basis=basis,
        threshold_crossed=False,
    )
    assert first == second


# --- Req 25.10: no autonomous action on a crossing --------------------

def test_the_audit_writer_has_no_way_to_notify() -> None:
    # Req 25.10 forbids any notification, message or external call on a crossing, and this is
    # the module that learns a crossing happened.
    #
    # AST, NOT TEXT — the third time this trap has fired in this project. My first draft grepped
    # the source for "publish"/"notify"/"httpx" and failed on the module's own DOCSTRING, which
    # describes the prohibition. A rule about CODE must inspect code: this walks imports and
    # called names, so prose about the rule cannot trip the rule.
    import ast
    import inspect

    import aqm_ingestion.serving.audit as audit_module

    tree = ast.parse(inspect.getsource(audit_module))
    forbidden = {"notify", "publish", "send", "post", "request", "email"}

    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                called.add(target.id)
            elif isinstance(target, ast.Attribute):
                called.add(target.attr)

    assert not imported & {"httpx", "requests", "smtplib", "boto3", "urllib"}
    assert not {
        name for name in called if any(word in name.lower() for word in forbidden)
    }
    # The detector must be able to detect: this module DOES call something, so an empty `called`
    # would make the assertion above vacuous.
    assert called


def test_appending_an_audit_record_is_the_only_effect() -> None:
    import inspect

    assert set(inspect.signature(audit_record_for).parameters) == {
        "user_id",
        "served_at",
        "route",
        "basis",
        "threshold_crossed",
    }
