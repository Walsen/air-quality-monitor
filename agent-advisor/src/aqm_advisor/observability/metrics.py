"""The metrics registry (task 1.3).

**This module installs no telemetry** (Req 32.10). It reads the AMBIENT OpenTelemetry meter and
never creates a provider, a reader or an exporter. With nothing configured the API returns proxy
instruments that record nothing and cost nothing; when AgentCore installs its own provider those
same proxies bind to it. A provider installed here would risk displacing the platform's
automatic instrumentation and losing every metric silently — a failure that would look exactly
like a quiet service.

That is also why `strands.telemetry.StrandsTelemetry` is NOT constructed anywhere in the service
path, even though Req 32.10 names the Strands `otel` extra as the instrumentor. The extra is a
DEPENDENCY, pinned in the manifest, and it is what lets the Strands agent loop emit its own
spans; `StrandsTelemetry()` is a SETUP call that installs a global tracer provider on
construction and `setup_meter()` installs a global meter provider. Calling either would be this
service configuring a collector, which the same criterion forbids where it runs on AgentCore. A
local developer wanting console output can opt in at the local entry point, which is not the
deployed path.

**Label values are restricted structurally** (Req 24.5). No metric label may carry a condition,
a coordinate, or any part of an utterance, and a label is precisely where a well-meaning caller
adds context. So a value must be one this registry RECOGNISES, matched exactly; anything else
becomes `OTHER`.

Replacing rather than dropping, and never raising:

- dropping would lose the count an operator most needs: an unrecognised guardrail category
  is still a rejection that happened;
- raising would let a metrics call break a turn, inverting the priority between answering
  the user and observing that we did.

The exact match matters. A substring test would admit `"dosing instructions for asthma"` on the
strength of containing `"dosing"`, carrying the condition along with it. That is the same
collision that made Service 2's logger redact a configured medication CAP as though it were a
medication, and that made this service's own audit sweep condemn `escalated` for containing
`lat`.
"""

from __future__ import annotations

from collections.abc import Iterable

from opentelemetry import metrics as otel_metrics

from aqm_advisor.domain.forbidden import _CATEGORY_BY_PATTERN
from aqm_advisor.ports.protocols import ServingFailureKind

OTHER = "other"
"""The label value an unrecognised one collapses to. Never a truncation of the original."""

MANAGED_GUARDRAIL_CATEGORY_PREFIXES: tuple[str, ...] = (
    "content_",
    "word_",
    "pii_",
    "regex_",
    "grounding_",
)
"""Prefixes the Bedrock guardrail adapter puts on a managed-policy category.

Prefixes rather than an exhaustive list, because the values behind them are AWS enums — content
filter types, PII entity types, managed word lists — that AWS extends without asking. An exact-
match
allowlist over those would collapse each newly added type to `other` the day it first fired,
which
is the same silent-miscount this set exists to prevent.
"""

PERMITTED_GUARDRAIL_CATEGORIES: frozenset[str] = (
    frozenset(_CATEGORY_BY_PATTERN.values())
    | {OTHER}
    | {"medication_administration", "uncategorised_intervention", "guardrail_unavailable"}
)
"""Req 8's Forbidden_Claim families, which are what Req 20.2 stores and Req 24.4 counts.

WIDENED FOR THE MANAGED GUARDRAIL. A review found this set derived only from the LOCAL regex
families, while task 16.4's Bedrock adapter emits `medication_administration` (one of Req 34.3's
own
denied topics), plus `uncategorised_intervention` and `guardrail_unavailable`. Because
`safe_label_value` is exact-match, all three collapsed to `other`: the Req 34.4 total survived,
but an
operator could not see how often the dosing topic fired or how often the guardrail was down.
Latent
rather than live only because nothing calls the metric until task 21 wires it.
"""


def is_permitted_guardrail_category(category: str) -> bool:
    """Whether a category may be published as a metric label.

    Exact match against the set above, OR a known managed-policy prefix. Two mechanisms because
    the
    two vocabularies differ in kind: this service's own families are a closed set it can
    enumerate,
    while AWS's are an open set it can only recognise by shape.
    """
    return category in PERMITTED_GUARDRAIL_CATEGORIES or any(
        category.startswith(prefix) and len(category) > len(prefix)
        for prefix in MANAGED_GUARDRAIL_CATEGORY_PREFIXES
    )

PERMITTED_SERVING_FAILURE_KINDS: frozenset[str] = frozenset(
    kind.value for kind in ServingFailureKind
)
"""Derived from the port's own enum, so a new kind is countable the moment it is defined.

Deliberately not hand-listed. Service 2 shipped a fenced-test selector built from a hand-written
table and a fifth table went uncovered; a derived set cannot fall behind the thing it describes.
A test asserts the two agree, so the derivation is visible rather than implied.
"""

PERMITTED_MODEL_FAILURE_KINDS: frozenset[str] = frozenset(
    {
        "throttled",
        "unavailable",
        "timeout",
        "content_filtered",
        "guardrail_intervened",
        "truncated",
        "unusable_response",
    }
)
"""Model_Port failure families (Req 24.4).

`guardrail_intervened` is the SDK's real stop reason. The design first said
`guardrail_intervention`, which is not a stop reason at all — so a counter keyed on the wrong
name would have stayed at zero while guardrail stops were being recorded as some other failure.
"""

PERMITTED_ESCALATION_KINDS: frozenset[str] = frozenset({"emergency", "clinician"})
"""Req 10's two determinations, matching the `Escalation.kind` literal."""


def safe_label_value(value: str, *, permitted: Iterable[str]) -> str:
    """Return `value` when it is a recognised label, else `OTHER` (Req 24.5).

    Exact match, deliberately: no casefolding, no stripping, no prefix or substring test. Each
    of those would admit a longer string on the strength of containing a permitted one, and the
    remainder is exactly the health-adjacent content this exists to keep out of a label.
    """
    return value if value in set(permitted) else OTHER


class AdvisorMetrics:
    """The service's counters (Req 24.4, Req 22.5).

    Instruments are created once per registry and reused, which is what the OpenTelemetry API
    expects: creating a counter per call would produce duplicate instrument registrations.

    The meter is INJECTED so a test can supply a real SDK meter with an in-memory reader and
    assert over recorded data points rather than over mock calls. It defaults to the ambient
    meter, which is a proxy when nothing is configured — so ordinary construction neither
    requires nor installs telemetry.
    """

    def __init__(self, meter: otel_metrics.Meter | None = None) -> None:
        """Create the instruments against `meter`, or the ambient meter when none is given."""
        self._meter = meter if meter is not None else otel_metrics.get_meter(__name__)

        self._turns_answered = self._meter.create_counter(
            "advisor.turns.answered", unit="1", description="Advisory turns answered."
        )
        self._turns_degraded = self._meter.create_counter(
            "advisor.turns.degraded", unit="1", description="Advisory turns answered degraded."
        )
        self._guardrail_rejections = self._meter.create_counter(
            "advisor.guardrail.rejections",
            unit="1",
            description="Generations withheld by a guardrail check, by category.",
        )
        self._escalations = self._meter.create_counter(
            "advisor.escalations.returned",
            unit="1",
            description="Turns that returned an escalation, by kind.",
        )
        self._serving_failures = self._meter.create_counter(
            "advisor.serving.failures",
            unit="1",
            description="Serving_Client calls that did not yield a usable body, by kind.",
        )
        self._model_failures = self._meter.create_counter(
            "advisor.model.failures", unit="1", description="Model_Port failures, by kind."
        )
        self._model_invocations = self._meter.create_counter(
            "advisor.model.invocations", unit="1", description="Model invocations."
        )
        # Two instruments rather than one labelled by direction: Req 22's budgets are
        # per-direction
        # (max output tokens, max total tokens), and one instrument would make an output-token
        # alarm
        # depend on filtering correctly — a place to get it wrong at the moment it matters.
        self._input_tokens = self._meter.create_counter(
            "advisor.model.tokens.input", unit="1", description="Input tokens consumed."
        )
        self._output_tokens = self._meter.create_counter(
            "advisor.model.tokens.output", unit="1", description="Output tokens produced."
        )

    def turn_answered(self) -> None:
        """Count one answered turn."""
        self._turns_answered.add(1)

    def turn_degraded(self) -> None:
        """Count one degraded turn.

        Additional to `turn_answered`, not instead of it: a degraded turn WAS answered, and an
        operator alarms on the ratio of the two. Counting it only here would make degradation
        invisible in the answered total.
        """
        self._turns_degraded.add(1)

    def guardrail_rejection(self, category: str) -> None:
        """Count one withheld generation, by category (Req 24.4, Req 8.6).

        Routed through `is_permitted_guardrail_category` rather than a bare exact-match set, so
        a
        managed-policy category from the Bedrock adapter keeps its identity instead of
        collapsing to
        `other` and hiding which policy fired.
        """
        label = category if is_permitted_guardrail_category(category) else OTHER
        self._guardrail_rejections.add(1, {"category": label})

    def escalation_returned(self, kind: str) -> None:
        """Count one escalating turn, by kind."""
        self._escalations.add(
            1, {"kind": safe_label_value(kind, permitted=PERMITTED_ESCALATION_KINDS)}
        )

    def serving_failure(self, kind: ServingFailureKind | str) -> None:
        """Count one Serving_Client failure, by kind."""
        value = kind.value if isinstance(kind, ServingFailureKind) else kind
        self._serving_failures.add(
            1, {"kind": safe_label_value(value, permitted=PERMITTED_SERVING_FAILURE_KINDS)}
        )

    def model_failure(self, kind: str) -> None:
        """Count one Model_Port failure, by kind."""
        self._model_failures.add(
            1, {"kind": safe_label_value(kind, permitted=PERMITTED_MODEL_FAILURE_KINDS)}
        )

    def model_invocation(self, *, input_tokens: int, output_tokens: int) -> None:
        """Count one model invocation and its token usage (Req 22.5).

        A zero-token invocation is still counted: a guardrail-stopped generation can
        legitimately report no output tokens, and the invocation happened, so dropping it would
        understate model usage.

        Raises:
            ValueError: if either count is negative. A counter must not go backwards, and a
            negative value
                means the caller misread the usage block — adding it would corrupt every rate
                derived from the instrument, quietly and permanently.
        """
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must not be negative")
        self._model_invocations.add(1)
        self._input_tokens.add(input_tokens)
        self._output_tokens.add(output_tokens)


__all__ = [
    "OTHER",
    "PERMITTED_ESCALATION_KINDS",
    "PERMITTED_GUARDRAIL_CATEGORIES",
    "PERMITTED_MODEL_FAILURE_KINDS",
    "PERMITTED_SERVING_FAILURE_KINDS",
    "AdvisorMetrics",
    "safe_label_value",
]
