"""Tests for the local port implementations (task 2.4).

`test_the_canned_body_matches_service_2s_real_response_model` is the one with teeth. Every one
of the 20 correctness properties reads these canned bodies, so a shape that has drifted from
what Service 2 actually serves would have the entire offline suite proving things about a
response nobody sends — passing green the whole time.

The shape cannot be imported: the engineering practices forbid importing across service
directories until a shared contract package is specced. So the guard reads the sibling service's
model definition from disk and compares field names, skipping when that directory is absent so
this service still builds and tests alone. A filesystem read is not a runtime dependency.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib

import pytest

from aqm_advisor.adapters.local import (
    DEFAULT_EMERGENCY_GUIDANCE,
    InMemoryAdviceAuditStore,
    LocalGuardrailChecker,
    RecordingAssociationTrigger,
    ScriptedServingClient,
    canned_air_quality,
)
from aqm_advisor.domain.forbidden import DEFAULT_FORBIDDEN_PATTERNS
from aqm_advisor.ports.protocols import (
    AdviceAuditStore,
    AdviceRecord,
    AssociationTrigger,
    GuardrailChecker,
    GuardrailVerdict,
    ServingClient,
    ServingClientError,
    ServingFailureKind,
)

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_SERVICE_2_MODELS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "data-processing"
    / "src"
    / "aqm_ingestion"
    / "serving"
    / "models.py"
)


# --- every fake satisfies its port --------------------------------------

def test_each_local_implementation_satisfies_its_port() -> None:
    assert isinstance(ScriptedServingClient(), ServingClient)
    assert isinstance(LocalGuardrailChecker(), GuardrailChecker)
    assert isinstance(InMemoryAdviceAuditStore(), AdviceAuditStore)
    assert isinstance(RecordingAssociationTrigger(), AssociationTrigger)


# --- THE guard: the canned body matches what Service 2 really serves ----

def _model_fields(source: str, class_name: str) -> set[str]:
    """Field names declared on one class in a module's source, via the AST."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    raise AssertionError(f"{class_name} not found")


def test_the_canned_body_matches_service_2s_real_response_model() -> None:
    if not _SERVICE_2_MODELS.is_file():
        pytest.skip(
            "the sibling service is not present; this service builds and tests independently"
        )
    declared = _model_fields(_SERVICE_2_MODELS.read_text(encoding="utf-8"), "ServingResponse")
    canned = set(canned_air_quality())
    assert canned == declared, (
        f"the canned body has drifted from Service 2's ServingResponse. "
        f"missing: {sorted(declared - canned)}; extra: {sorted(canned - declared)}"
    )


def test_the_canned_personalized_block_matches_service_2s_model() -> None:
    if not _SERVICE_2_MODELS.is_file():
        pytest.skip("the sibling service is not present")
    declared = _model_fields(
        _SERVICE_2_MODELS.read_text(encoding="utf-8"), "PersonalizedOut"
    )
    personalized = canned_air_quality()["personalized"]
    assert isinstance(personalized, dict)
    canned = set(personalized)
    assert canned == declared, (
        f"the canned personalized block has drifted. "
        f"missing: {sorted(declared - canned)}; extra: {sorted(canned - declared)}"
    )


def test_the_canned_basis_block_matches_service_2s_model() -> None:
    if not _SERVICE_2_MODELS.is_file():
        pytest.skip("the sibling service is not present")
    declared = _model_fields(_SERVICE_2_MODELS.read_text(encoding="utf-8"), "BasisOut")
    basis = canned_air_quality()["basis"]
    assert isinstance(basis, dict)
    canned = set(basis)
    assert canned == declared


def test_this_package_never_imports_the_sibling_service() -> None:
    # The guard above reads a file; nothing here may IMPORT across service directories.
    import aqm_advisor

    root = pathlib.Path(next(iter(aqm_advisor.__path__)))
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n.startswith(("aqm_ingestion", "aqm_simulator")) for n in names):
                offenders.append(path.name)
    assert offenders == [], f"cross-service import: {offenders}"


# --- the scripted serving client ----------------------------------------

def test_a_scripted_failure_raises_the_kind_the_pipeline_must_handle() -> None:
    client = ScriptedServingClient(air_quality_body=ServingFailureKind.TIMEOUT)
    with pytest.raises(ServingClientError) as caught:
        client.air_quality("opaque-credential")
    assert caught.value.kind is ServingFailureKind.TIMEOUT


def test_the_client_records_its_calls_in_order() -> None:
    # Requirement 35.4's trajectory: which retrievals happened, in what order.
    client = ScriptedServingClient()
    client.air_quality("c")
    client.profile_get("c")
    client.history("c", _NOW, _NOW)
    assert client.tool_names() == ("air_quality", "profile_get", "history")


def test_the_history_call_records_the_window_it_was_asked_for() -> None:
    client = ScriptedServingClient()
    client.history("c", _NOW, _NOW + dt.timedelta(days=1), frozenset({"PM25"}))
    _name, args = client.calls[0]
    assert args[0] == _NOW
    assert args[2] == frozenset({"PM25"})


def test_the_canned_body_carries_the_guardrail_envelope() -> None:
    # A8: the envelope is carried FROM Service 2, never re-composed here, so the canned body has
    # to supply it or every envelope-invariance test would be exercising a local literal.
    body = canned_air_quality()
    assert body["advisoryScope"] == "exposure-reduction"
    assert body["emergencyGuidance"] == DEFAULT_EMERGENCY_GUIDANCE
    assert "not medical advice" in str(body["disclaimer"]).lower()


def test_the_canned_body_is_configurable_where_a_test_needs_variation() -> None:
    crossed = canned_air_quality(threshold_crossed=True, threshold_source="learned")
    personalized = crossed["personalized"]
    assert isinstance(personalized, dict)
    assert personalized["thresholdCrossed"] is True
    assert personalized["thresholdSource"] == "learned"


def test_a_partial_nowcast_window_is_scriptable() -> None:
    # Req 9.3: the weighting is part of how the sub-index was derived, so a window with fewer
    # hours
    # available than its length has to reach the basis unchanged rather than being normalised
    # away.
    basis = canned_air_quality(window_hours=12, hours_available=8)["basis"]
    assert isinstance(basis, dict)
    assert basis["nowcast"] == {
        "windowHours": 12,
        "hoursAvailable": 8,
        "weightFactor": 0.72,
    }


def test_an_absent_nowcast_is_a_distinct_state_from_a_partial_window() -> None:
    # Req 9.3a: no nowcast means the index was NOT nowcast-derived — a complete answer, not a
    # gap.
    # These are two different things and a caller must be able to produce each, so this asserts
    # they
    # are actually distinguishable rather than both arriving as some flavour of missing.
    absent = canned_air_quality(nowcast=False)["basis"]
    partial = canned_air_quality(hours_available=8)["basis"]
    assert isinstance(absent, dict)
    assert isinstance(partial, dict)
    assert absent["nowcast"] is None
    assert partial["nowcast"] is not None


def test_an_absent_nowcast_still_matches_service_2s_basis_shape() -> None:
    # The drift guard must hold in BOTH nowcast states: Service 2 declares `nowcast` as
    # `NowcastOut | None`, so omitting the KEY entirely would be a different shape from sending
    # null.
    if not _SERVICE_2_MODELS.is_file():
        pytest.skip("the sibling service is not present")
    declared = _model_fields(_SERVICE_2_MODELS.read_text(encoding="utf-8"), "BasisOut")
    basis = canned_air_quality(nowcast=False)["basis"]
    assert isinstance(basis, dict)
    assert set(basis) == declared, "the key must be present and null, not dropped"


def _confidence(**kwargs: object) -> str:
    sensors = canned_air_quality(**kwargs)["nearestSensors"]  # type: ignore[arg-type]
    assert isinstance(sensors, list)
    return str(sensors[0]["confidence"])


@pytest.mark.parametrize(
    ("kwargs", "expected", "coverage"),
    [
        ({}, "high", "complete"),
        ({"hours_available": 12}, "high", "complete"),
        ({"hours_available": 8}, "medium", "incomplete"),
        ({"hours_available": 0}, "low", "insufficient"),
        ({"nowcast": False}, "high", "not_applicable"),
    ],
)
def test_the_fake_mirrors_service_2s_coverage_confidence_caps(
    kwargs: dict[str, object], expected: str, coverage: str
) -> None:
    # Req 15.6 makes the confidence Service 2 returned the single authority on measurement
    # weakness,
    # so an incomplete window reaches the user THROUGH it. If the fake let a partial window keep
    # high
    # confidence, every test of that disclosure would pass against a response Service 2 never
    # sends.
    assert _confidence(**kwargs) == expected, f"{coverage} should cap at {expected}"


def test_a_partial_window_can_never_be_scripted_with_the_highest_confidence() -> None:
    # The property behind the table above, stated so it cannot be satisfied by the four rows
    # alone.
    for hours in range(1, 12):
        assert _confidence(window_hours=12, hours_available=hours) != "high"


def test_an_absent_nowcast_is_not_treated_as_a_weak_measurement() -> None:
    # Req 9.3a: not_applicable and insufficient BOTH lack a usable nowcast, but only one is
    # weak.
    # Collapsing them would either invent a warning for NO2 or hide one for a starved PM2.5
    # window.
    assert _confidence(nowcast=False) == "high"
    assert _confidence(hours_available=0) == "low"


_SERVICE_2_QUALITY = (
    pathlib.Path(__file__).resolve().parents[3]
    / "data-processing"
    / "src"
    / "aqm_ingestion"
    / "domain"
    / "quality.py"
)


def test_the_mirrored_cap_table_has_not_drifted_from_service_2s() -> None:
    # The caps above duplicate a fact Service 2 OWNS, so they need a guard: if Service 2 stopped
    # capping an incomplete window at medium, this fake would keep asserting a coupling that no
    # longer exists and every confidence-disclosure test would be measuring the fake, not the
    # system.
    if not _SERVICE_2_QUALITY.is_file():
        pytest.skip("the sibling service is not present")
    source = _SERVICE_2_QUALITY.read_text(encoding="utf-8")
    for coverage, cap in (
        ("COMPLETE", "None"),
        ("INCOMPLETE", "Confidence.MEDIUM"),
        ("INSUFFICIENT", "Confidence.LOW"),
    ):
        assert f"NowCastCoverage.{coverage}: {cap}" in source, (
            f"Service 2's cap for {coverage} is no longer {cap}; the mirrored table in "
            "adapters/local.py must be updated to match"
        )


# --- the local guardrail checker ----------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "You have asthma.",
        "This is an asthma attack.",
        "Take two puffs of your reliever.",
        "Use 2 puffs every 4 hours.",
        "You should double your dose today.",
        "You probably have a chest infection.",
    ],
)
def test_a_forbidden_claim_is_intervened(text: str) -> None:
    assert LocalGuardrailChecker().check(text).verdict is GuardrailVerdict.INTERVENED


@pytest.mark.parametrize(
    "text",
    [
        "Air quality near you is Moderate, driven by PM2.5.",
        "Consider moving your run to the evening.",
        "Have your reliever inhaler to hand while you are out.",
        "Your nearest sensor reports a sub-index of 68.",
    ],
)
def test_permitted_exposure_guidance_passes(text: str) -> None:
    assert LocalGuardrailChecker().check(text).verdict is GuardrailVerdict.PASSED


def test_the_required_emergency_text_is_not_itself_rejected() -> None:
    # A REAL RISK, not a hypothetical: the emergency guidance must mention a reliever inhaler,
    # so a
    # bare medication-word pattern would make the text Requirement 8.4 REQUIRES unpublishable.
    verdict = LocalGuardrailChecker().check(DEFAULT_EMERGENCY_GUIDANCE).verdict
    assert verdict is GuardrailVerdict.PASSED


def test_a_verdict_names_a_category_and_never_the_text() -> None:
    result = LocalGuardrailChecker().check("Take two puffs now.")
    assert result.categories == ("dosing",)
    assert "puffs" not in str(result)


def test_the_checker_retains_the_length_not_the_text() -> None:
    checker = LocalGuardrailChecker()
    checker.check("Take two puffs of salbutamol now.")
    assert checker.checks == [33]
    assert "salbutamol" not in repr(checker)


def test_the_unavailable_verdict_is_scriptable_for_the_fail_closed_path() -> None:
    # Req 34.6: when the managed check cannot run, anything the local checks cannot clear is not
    # returned. That branch needs to be drivable.
    checker = LocalGuardrailChecker(unavailable=True)
    assert checker.check("anything").verdict is GuardrailVerdict.UNAVAILABLE


def test_a_configured_pattern_set_replaces_the_defaults() -> None:
    # Req 8.7: "configurable" that only ever ADDS is not configurable — an operator who finds a
    # pattern misfiring must be able to correct it.
    checker = LocalGuardrailChecker(patterns=(r"\bnever say this\b",))
    assert checker.check("Take two puffs.").verdict is GuardrailVerdict.PASSED
    assert checker.check("never say this").verdict is GuardrailVerdict.INTERVENED


def test_the_checker_delegates_its_rules_to_the_domain() -> None:
    # The pattern set and the category mapping are the DOMAIN's. They were briefly duplicated in
    # this
    # adapter, which made it a second authority on what may be said. Asserted by importing the
    # defaults
    # from the domain above and checking the adapter honours them with no set of its own.
    import ast as _ast
    import pathlib as _pathlib

    import aqm_advisor.adapters.local as module

    source = _pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = _ast.parse(source)
    assigned = {
        target.id
        for node in _ast.walk(tree)
        if isinstance(node, _ast.AnnAssign | _ast.Assign)
        for target in ([node.target] if isinstance(node, _ast.AnnAssign) else node.targets)
        if isinstance(target, _ast.Name)
    }
    assert "DEFAULT_FORBIDDEN_PATTERNS" not in assigned, (
        "the adapter has its own pattern set again"
    )


def test_the_default_patterns_are_not_empty() -> None:
    # Non-vacuity: an empty default set would make every permitted-text test above pass
    # trivially.
    assert len(DEFAULT_FORBIDDEN_PATTERNS) >= 6


# --- the audit store ----------------------------------------------------

def _record(user_id: str = "user-1") -> AdviceRecord:
    return AdviceRecord(
        user_id=user_id,
        turn_at=_NOW,
        route="/invocations",
        escalated=False,
        threshold_crossed=False,
        driving_pollutant="PM25",
        record_references=("AQM1",),
        guardrail_rejected=False,
        rejection_category=None,
        idempotency_key="k1",
    )


def test_an_appended_record_is_readable() -> None:
    store = InMemoryAdviceAuditStore()
    store.append(_record())
    assert store.for_user("user-1") == (store.records[0],)


def test_erasure_removes_only_that_users_records() -> None:
    store = InMemoryAdviceAuditStore()
    store.append(_record("user-a"))
    store.append(_record("user-b"))
    assert store.forget_user("user-a") == 1
    assert [r.user_id for r in store.records] == ["user-b"]


def test_an_append_failure_is_scriptable_for_requirement_20_5() -> None:
    # Req 20.5: an audit failure must not prevent the response, so the pipeline needs to be able
    # to
    # meet one.
    with pytest.raises(OSError):
        InMemoryAdviceAuditStore(fail_on_append=True).append(_record())


# --- the association trigger --------------------------------------------

def test_a_request_is_recorded_rather_than_made() -> None:
    trigger = RecordingAssociationTrigger()
    trigger.request("user-1", "corr-1")
    assert trigger.requests == [("user-1", "corr-1")]


def test_a_trigger_failure_is_scriptable() -> None:
    # Req 33.6: a failure is logged and turns continue against stored thresholds.
    with pytest.raises(OSError):
        RecordingAssociationTrigger(fail=True).request("user-1", "corr-1")


# --- no network anywhere in the local adapters --------------------------

def test_the_local_adapters_import_no_transport() -> None:
    import aqm_advisor.adapters.local as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("httpx", "boto3", "botocore", "urllib", "socket", "requests"):
        assert forbidden not in imported
