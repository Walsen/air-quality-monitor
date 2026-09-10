"""Tests for the metrics registry (task 1.3).

These assert over REAL recorded data points, through an SDK meter provider with an in-memory
reader, rather than over mock calls. A mock would confirm the registry called a method; it would
not catch a counter recorded under the wrong instrument, a label that never made it onto the
point, or a value added twice.

Req 24.5 is the load-bearing rule: no metric label may carry a condition, a coordinate, or any
part of an utterance. That cannot be a convention, because a metric label is exactly the place a
well-meaning caller adds context. So the registry accepts only recognised label values and
replaces anything else with a sentinel — and the sweep that proves it carries both a non-vacuity
guard and a near-miss guard, because a label allowlist that matched nothing would pass every
test here while protecting nothing.
"""

from __future__ import annotations

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader, NumberDataPoint

from aqm_advisor.observability.metrics import (
    OTHER,
    PERMITTED_GUARDRAIL_CATEGORIES,
    PERMITTED_MODEL_FAILURE_KINDS,
    PERMITTED_SERVING_FAILURE_KINDS,
    AdvisorMetrics,
    safe_label_value,
)
from aqm_advisor.ports.protocols import ServingFailureKind


@pytest.fixture
def reader() -> InMemoryMetricReader:
    return InMemoryMetricReader()


@pytest.fixture
def metrics(reader: InMemoryMetricReader) -> AdvisorMetrics:
    # A LOCAL provider, never installed globally: Req 32.10 forbids this service configuring its
    # own
    # telemetry, and a global install here would also leak between tests.
    return AdvisorMetrics(meter=MeterProvider(metric_readers=[reader]).get_meter("test"))


def _points(reader: InMemoryMetricReader, instrument: str) -> list[tuple[dict[str, str], int]]:
    """Every recorded (attributes, value) pair for one instrument name."""
    data = reader.get_metrics_data()
    found: list[tuple[dict[str, str], int]] = []
    if data is None:
        return found
    for resource in data.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != instrument:
                    continue
                for point in metric.data.data_points:
                    # Narrowed rather than cast: these instruments are all counters, so a
                    # histogram point here would mean the registry created the wrong kind.
                    assert isinstance(point, NumberDataPoint), f"{instrument} is not a counter"
                    labels = {str(k): str(v) for k, v in (point.attributes or {}).items()}
                    found.append((labels, int(point.value)))
    return found


# --- Req 24.4: the six counters exist and record -------------------------

def test_a_turn_answered_is_counted(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    metrics.turn_answered()
    metrics.turn_answered()
    assert _points(reader, "advisor.turns.answered") == [({}, 2)]


def test_a_degraded_turn_is_counted_separately(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    # Separate instruments, because "how many turns" and "how many were degraded" are different
    # questions and an operator alarms on the ratio.
    metrics.turn_answered()
    metrics.turn_degraded()
    assert _points(reader, "advisor.turns.answered") == [({}, 1)]
    assert _points(reader, "advisor.turns.degraded") == [({}, 1)]


def test_a_guardrail_rejection_is_counted_by_category(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    metrics.guardrail_rejection("dosing")
    metrics.guardrail_rejection("dosing")
    metrics.guardrail_rejection("diagnosis")
    points = dict(
        (attributes["category"], value)
        for attributes, value in _points(reader, "advisor.guardrail.rejections")
    )
    assert points == {"dosing": 2, "diagnosis": 1}


def test_an_escalation_is_counted_by_kind(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    metrics.escalation_returned("emergency")
    points = dict(
        (attributes["kind"], value)
        for attributes, value in _points(reader, "advisor.escalations.returned")
    )
    assert points == {"emergency": 1}


def test_a_serving_failure_is_counted_by_kind(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    metrics.serving_failure(ServingFailureKind.TIMEOUT)
    metrics.serving_failure(ServingFailureKind.UNREACHABLE)
    points = dict(
        (attributes["kind"], value)
        for attributes, value in _points(reader, "advisor.serving.failures")
    )
    assert points == {"timeout": 1, "unreachable": 1}


def test_a_model_failure_is_counted_by_kind(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    metrics.model_failure("throttled")
    points = dict(
        (attributes["kind"], value)
        for attributes, value in _points(reader, "advisor.model.failures")
    )
    assert points == {"throttled": 1}


# --- Req 22.5: invocations and token usage ------------------------------

def test_model_invocations_and_token_usage_are_recorded(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    metrics.model_invocation(input_tokens=120, output_tokens=45)
    metrics.model_invocation(input_tokens=80, output_tokens=20)
    assert _points(reader, "advisor.model.invocations") == [({}, 2)]
    assert _points(reader, "advisor.model.tokens.input") == [({}, 200)]
    assert _points(reader, "advisor.model.tokens.output") == [({}, 65)]


def test_token_counts_are_separate_instruments_not_one_labelled_by_direction(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    # Req 22's budgets are per-direction (max output tokens, max total tokens). One instrument
    # labelled `direction` would make an output-token alarm depend on filtering, which is a
    # place to
    # get it wrong; two instruments make each budget directly queryable.
    metrics.model_invocation(input_tokens=1, output_tokens=2)
    assert _points(reader, "advisor.model.tokens.input") == [({}, 1)]
    assert _points(reader, "advisor.model.tokens.output") == [({}, 2)]


def test_a_negative_token_count_is_refused(metrics: AdvisorMetrics) -> None:
    # A counter must not go backwards. A negative value here means the caller misread the usage
    # block, and silently adding it would corrupt every downstream rate.
    with pytest.raises(ValueError, match="negative"):
        metrics.model_invocation(input_tokens=-1, output_tokens=0)


def test_a_zero_token_invocation_is_still_counted(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    # A guardrail-stopped generation can legitimately report zero output tokens, and the
    # invocation
    # still happened. Dropping it would understate model usage.
    metrics.model_invocation(input_tokens=0, output_tokens=0)
    assert _points(reader, "advisor.model.invocations") == [({}, 1)]


# --- Req 24.5: no label may carry health-adjacent content ---------------

def test_an_unrecognised_label_value_is_replaced_not_passed_through(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    # THE Req 24.5 test. A category the registry does not recognise could be anything —
    # including
    # prose assembled from an utterance — so it never reaches the label.
    metrics.guardrail_rejection("my chest has been tight since tuesday")
    attributes, value = _points(reader, "advisor.guardrail.rejections")[0]
    assert attributes == {"category": OTHER}
    assert value == 1


def test_the_count_survives_an_unrecognised_value(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    # Replaced, NOT dropped, and not raised either. Dropping would lose the rejection an
    # operator
    # most needs to see, and raising would let a metrics call break a turn.
    metrics.guardrail_rejection("something new")
    metrics.guardrail_rejection("dosing")
    total = sum(value for _attributes, value in _points(reader, "advisor.guardrail.rejections"))
    assert total == 2


@pytest.mark.parametrize(
    "leak",
    [
        "asthma",
        "copd",
        "51.507,-0.128",
        "my chest was tight",
        "latitude=51.5",
        "user-1",
    ],
)
def test_a_health_adjacent_value_never_reaches_a_label(leak: str) -> None:
    assert safe_label_value(leak, permitted=PERMITTED_GUARDRAIL_CATEGORIES) == OTHER


def test_every_recognised_value_passes_through_unchanged() -> None:
    # Non-vacuity for the sweep above. If `safe_label_value` returned OTHER for everything, each
    # leak test would pass while the metrics carried no useful granularity at all.
    for category in PERMITTED_GUARDRAIL_CATEGORIES:
        assert safe_label_value(category, permitted=PERMITTED_GUARDRAIL_CATEGORIES) == category
    for kind in PERMITTED_SERVING_FAILURE_KINDS:
        assert safe_label_value(kind, permitted=PERMITTED_SERVING_FAILURE_KINDS) == kind
    for kind in PERMITTED_MODEL_FAILURE_KINDS:
        assert safe_label_value(kind, permitted=PERMITTED_MODEL_FAILURE_KINDS) == kind


def test_the_permitted_sets_are_not_empty() -> None:
    # The other half of non-vacuity: an empty allowlist would make every value OTHER, which is
    # "safe" and useless.
    assert len(PERMITTED_GUARDRAIL_CATEGORIES) >= 2
    assert len(PERMITTED_SERVING_FAILURE_KINDS) >= 5
    assert len(PERMITTED_MODEL_FAILURE_KINDS) >= 3


def test_the_match_is_exact_not_a_prefix_or_substring() -> None:
    # The near-miss guard. A substring or prefix match would let "dosing instructions for
    # asthma"
    # through on the strength of containing "dosing" — carrying the rest of the string with it.
    for near_miss in ("dosing ", " dosing", "dosingx", "xdosing", "DOSING", "dosing:asthma"):
        assert safe_label_value(near_miss, permitted=PERMITTED_GUARDRAIL_CATEGORIES) == OTHER


def test_the_serving_failure_labels_cover_every_kind_the_port_defines() -> None:
    # A drift guard. A new ServingFailureKind that nobody added here would be silently recorded
    # as
    # OTHER, and the failure an operator was trying to diagnose would be invisible.
    assert {kind.value for kind in ServingFailureKind} == set(PERMITTED_SERVING_FAILURE_KINDS)


def test_no_instrument_name_carries_health_adjacent_wording(
    metrics: AdvisorMetrics, reader: InMemoryMetricReader
) -> None:
    # Instrument NAMES are as visible as labels and are chosen by this service.
    metrics.turn_answered()
    metrics.guardrail_rejection("dosing")
    metrics.serving_failure(ServingFailureKind.TIMEOUT)
    metrics.model_invocation(input_tokens=1, output_tokens=1)
    data = reader.get_metrics_data()
    assert data is not None
    names = [
        metric.name
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
    ]
    assert names, "no instruments were recorded, so this assertion proves nothing"
    for name in names:
        for forbidden in ("condition", "asthma", "utterance", "symptom", "lat", "lon", "user"):
            assert forbidden not in name, f"{name} carries {forbidden!r}"


# --- Req 32.10: this service installs no telemetry ----------------------

def test_the_registry_defaults_to_the_ambient_meter() -> None:
    # With no provider installed the ambient meter is a proxy: recording costs nothing and binds
    # later if AgentCore installs a provider. Constructing without a meter must therefore work
    # and
    # must not raise.
    AdvisorMetrics().turn_answered()


def test_the_module_installs_no_provider_or_exporter() -> None:
    import ast
    import pathlib

    from aqm_advisor.observability import metrics as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in (
        "opentelemetry.sdk.metrics",
        "opentelemetry.sdk.trace",
        "opentelemetry.exporter.otlp.proto.http.metric_exporter",
        "strands.telemetry",
    ):
        assert forbidden not in imported, f"{forbidden} would install or export telemetry"

    called = {ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for forbidden_call in ("metrics.set_meter_provider", "set_meter_provider", "MeterProvider"):
        assert forbidden_call not in called, (
            f"{forbidden_call} would displace the platform's provider"
        )
