"""Local implementations of the three non-model ports (task 2.4).

These are NOT test doubles bolted on afterwards — they are what makes the offline suite possible
(Requirement 26.5), so they ship in the package rather than under ``tests/``. A property test at
100
examples needs them importable from the library, and so does a developer running the agent
locally
with ``app.run()`` and no AWS.

THE CANNED SERVING BODY IS TRANSCRIBED FROM SERVICE 2's REAL RESPONSE SHAPE, NOT INVENTED. That
is
the single biggest risk in this package: every one of the 20 correctness properties reads these
bodies, so a shape that drifts from what Service 2 actually serves would have the whole suite
proving
things about a response nobody sends.

It cannot be IMPORTED — the engineering practices forbid importing across service directories
until
a shared contract package is specced. So it is transcribed, and
``test_the_canned_body_matches_service_2s_real_response_model`` reads the sibling service's
model
definition from disk and compares the field names, skipping when that directory is absent so
this
service still builds alone. A filesystem check is not an import: nothing here depends on Service
2
at runtime.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from aqm_advisor.ports.protocols import (
    AdviceRecord,
    GuardrailResult,
    GuardrailVerdict,
    ServingClientError,
    ServingFailureKind,
)

_ADVISORY_SCOPE = "exposure-reduction"
"""Service 2's ``ADVISORY_SCOPE`` constant. Transcribed — see the module docstring."""

DEFAULT_DISCLAIMER = (
    "This information is for exposure reduction and general wellness. It is not medical "
    "advice, diagnosis, or treatment. Follow the plan agreed with your clinician."
)

DEFAULT_EMERGENCY_GUIDANCE = (
    "If you are severely breathless, your reliever inhaler is not helping, or your lips or "
    "face look blue, seek emergency care now."
)


def canned_air_quality(
    *,
    user: str = "user-1",
    generated_at: str = "2026-07-01T12:00:00Z",
    driving_pollutant: str = "PM25",
    sub_index: int = 68,
    band: str = "Moderate",
    threshold_crossed: bool = False,
    threshold_source: str | None = "sensitivity_level",
    escalation_sub_index: int | None = 101,
    window_hours: int = 12,
    hours_available: int | None = None,
    nowcast: bool = True,
) -> dict[str, object]:
    """One air-quality body in Service 2's real shape (Requirement 19.2's pinned members).

    Every member name here is Service 2's own. The guard named in the module docstring is what
    keeps
    that claim true rather than aspirational.

    ``hours_available`` defaults to a FULL window; pass fewer to script the partial-window case
    Req 9.3 makes traceable. ``nowcast=False`` scripts an index that was not nowcast-derived at
    all,
    which Req 9.3a calls a complete answer rather than a gap. Those are two DIFFERENT states and
    a
    caller must be able to tell them apart, so they are separate parameters rather than one
    nullable
    number — collapsing them is exactly the confusion Req 9.3a exists to forbid.
    """
    available = window_hours if hours_available is None else hours_available
    return {
        "user": user,
        "generatedAt": generated_at,
        "locations": [{"name": "home", "lat": 51.507, "lon": -0.128}],
        "nearestSensors": [
            {
                "siteCode": "AQM1",
                "siteName": "Test Site",
                "siteClassification": "Urban Background",
                "locationName": "home",
                "distanceKm": 1.2,
                "asOf": generated_at,
                "measurements": [
                    {
                        "species": "PM25",
                        "reportedValue": 24.1,
                        "correctedValue": 18.2,
                        "units": "ug.m-3",
                        "qualityFlag": "calibrated",
                        "confidence": "high",
                        "subIndex": sub_index,
                        "band": band,
                        "method": "nowcast",
                        "mixingRatioPpb": None,
                    }
                ],
                "overallAqi": sub_index,
                "band": band,
                "drivingPollutant": driving_pollutant,
                "confidence": "high",
            }
        ],
        "personalized": {
            "condition": "asthma",
            "sensitivity": "standard",
            "usedDefaultProfile": False,
            "weightedFocus": ["PM25", "O3"],
            "unavailableWeightedSpecies": ["O3"],
            "escalationSubIndex": escalation_sub_index,
            "thresholdCrossed": threshold_crossed,
            "thresholdSource": threshold_source,
            "crossings": [],
            "pollen": None,
            "inhaledDose": None,
            "doseBasis": "none",
            "inhaledDoseWindows": [],
            "unavailableDoseWindows": 0,
        },
        "forecast": {
            "tomorrowAqi": 72,
            "trend": "steady",
            "source": "test-provider",
            "retrievedAt": generated_at,
            "degraded": False,
        },
        "basis": {
            "breakpointTable": "epa-2024-05-06",
            "calibrationStrategies": {"PM25": "rh_linear"},
            "humiditySource": "provider",
            "conversionSource": None,
            "nowcast": (
                {
                    "windowHours": window_hours,
                    "hoursAvailable": available,
                    "weightFactor": 0.72,
                }
                if nowcast
                else None
            ),
            # Requirement 9.5's record references — the specific Readings a claim rests on. The
            # drift guard caught this block missing `records` and `nowcast` on its first run,
            # which
            # matters more than a shape nit: `records` is precisely what an Advice_Record's
            # ``record_references`` cites, so the grounding layer would have been built against
            # a
            # basis that could not supply its own provenance.
            "records": [
                {
                    "siteCode": "AQM1",
                    "species": "PM25",
                    "dateTime": generated_at,
                    "duration": "PT1H",
                }
            ],
        },
        "advisoryScope": _ADVISORY_SCOPE,
        "emergencyGuidance": DEFAULT_EMERGENCY_GUIDANCE,
        "disclaimer": DEFAULT_DISCLAIMER,
    }


@dataclass
class ScriptedServingClient:
    """A Serving_Client whose answers are supplied. Performs no network call.

    A failure is scripted as a :class:`ServingFailureKind` rather than an arbitrary exception,
    because Requirement 21.1 requires a degraded response naming the KIND — so the fake can only
    produce failures the pipeline is obliged to handle.
    """

    air_quality_body: Mapping[str, object] | ServingFailureKind = field(
        default_factory=canned_air_quality
    )
    history_body: Mapping[str, object] | ServingFailureKind = field(
        default_factory=lambda: {"readings": [], "count": 0}
    )
    profile_body: Mapping[str, object] | ServingFailureKind = field(
        default_factory=lambda: {
            "condition": "asthma",
            "sensitivity_level": "standard",
            "medications": [{"name": "salbutamol", "role": "reliever"}],
            "routines": [],
            "usedDefaultProfile": False,
        }
    )
    calls: list[tuple[str, tuple[object, ...]]] = field(default_factory=list)
    """Every call in order — the trajectory Requirement 35.4 asserts over."""

    def _answer(
        self, name: str, body: Mapping[str, object] | ServingFailureKind, *args: object
    ) -> Mapping[str, object]:
        """Record the call, then answer or raise."""
        self.calls.append((name, args))
        if isinstance(body, ServingFailureKind):
            raise ServingClientError(body)
        return body

    def air_quality(self, credential: str) -> Mapping[str, object]:
        """Return the canned air-quality body."""
        return self._answer("air_quality", self.air_quality_body)

    def history(
        self,
        credential: str,
        start: dt.datetime,
        end: dt.datetime,
        species: frozenset[str] | None = None,
    ) -> Mapping[str, object]:
        """Return the canned history body, recording the window asked for."""
        return self._answer("history", self.history_body, start, end, species)

    def profile_get(self, credential: str) -> Mapping[str, object]:
        """Return the canned profile."""
        return self._answer("profile_get", self.profile_body)

    def profile_put(
        self, credential: str, patch: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Record a profile write and echo it back."""
        return self._answer("profile_put", self.profile_body, patch)

    def profile_delete(self, credential: str) -> Mapping[str, object]:
        """Record an erasure."""
        return self._answer("profile_delete", {"profileDeleted": True})

    def symptom_entry_put(
        self, credential: str, entry: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Record a diary write and echo it back."""
        return self._answer("symptom_entry_put", {"entry": dict(entry)}, entry)

    def tool_names(self) -> tuple[str, ...]:
        """The call names in order, for a trajectory assertion."""
        return tuple(name for name, _args in self.calls)


DEFAULT_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    # Diagnosis assertions (Requirement 8.1).
    r"\byou (?:have|are having|are suffering from)\b",
    r"\bthis is (?:an? )?(?:asthma attack|exacerbation|infection)\b",
    r"\byou (?:probably|likely) have\b",
    # Dosing and administration (Requirements 8.3, 29.3).
    r"\btake (?:\d|one|two|three|four|a|another)\b",
    r"\b\d+\s*(?:puffs?|mg|ml|mcg|doses?)\b",
    r"\b(?:increase|double|reduce|stop|start)\s+(?:your\s+)?(?:dose|medication|inhaler)\b",
    r"\bevery\s+\d+\s*(?:hours?|days?)\b",
)
"""The default Forbidden_Claim patterns.

A configured set REPLACES these rather than extending them (Requirement 8.7), because
"configurable" that only ever adds is not configurable — an operator who finds a pattern
misfiring
needs to be able to correct it, not only to pile another on top.
"""


@dataclass
class LocalGuardrailChecker:
    """The offline Guardrail_Checker: pattern matching, no network.

    Requirement 34.5 puts the local check and the managed one at TWO INDEPENDENT points, neither
    conditional on the other — this is the one that runs offline and when Bedrock is
    unreachable.

    A verdict names the CATEGORY that fired and never the offending text (Requirement 8.6), so a
    rejection is diagnosable without storing what was rejected.
    """

    patterns: tuple[str, ...] = DEFAULT_FORBIDDEN_PATTERNS
    unavailable: bool = False
    """Scripts Requirement 34.6's fail-closed path, where the check cannot run at all."""

    checks: list[int] = field(default_factory=list)
    """The LENGTH of each text checked. Never the text: this object is held in tests whose whole
    point is that generated prose does not get retained anywhere it need not be."""

    def check(self, text: str) -> GuardrailResult:
        """Return the verdict for one generation."""
        self.checks.append(len(text))
        if self.unavailable:
            return GuardrailResult(verdict=GuardrailVerdict.UNAVAILABLE)
        fired = tuple(
            pattern
            for pattern in self.patterns
            if re.search(pattern, text, flags=re.IGNORECASE)
        )
        if fired:
            return GuardrailResult(
                verdict=GuardrailVerdict.INTERVENED,
                categories=tuple(_category_for(pattern) for pattern in fired),
            )
        return GuardrailResult(verdict=GuardrailVerdict.PASSED)


def _category_for(pattern: str) -> str:
    """Name the KIND of rule a pattern belongs to, never the pattern's own text.

    A category is what Requirement 8.6 logs and what Requirement 20.2's ``rejection_category``
    stores, so the mapping lives here rather than at each call site.
    """
    if any(word in pattern for word in ("puffs", "dose", "mg", "take", "every")):
        return "dosing"
    return "diagnosis"


@dataclass
class InMemoryAdviceAuditStore:
    """Advice_Records held in a list, with a read accessor for tests."""

    records: list[AdviceRecord] = field(default_factory=list)
    fail_on_append: bool = False
    """Scripts Requirement 20.5, where an audit failure must NOT prevent the response."""

    def append(self, record: AdviceRecord) -> None:
        """Record one turn."""
        if self.fail_on_append:
            raise OSError("the audit store is unavailable")
        self.records.append(record)

    def forget_user(self, user_id: str) -> int:
        """Erase this user's records and return the count."""
        doomed = [r for r in self.records if r.user_id == user_id]
        self.records = [r for r in self.records if r.user_id != user_id]
        return len(doomed)

    def for_user(self, user_id: str) -> Sequence[AdviceRecord]:
        """Every record for one user, for a test to assert over."""
        return tuple(r for r in self.records if r.user_id == user_id)


@dataclass
class RecordingAssociationTrigger:
    """Records requests instead of making them.

    Requirement 33.1 forbids awaiting the derivation inside a turn, so the assertion a test
    wants
    is that the request HAPPENED and that the turn did not wait for it — which needs the request
    recorded and nothing else.
    """

    requests: list[tuple[str, str]] = field(default_factory=list)
    fail: bool = False
    """Scripts Requirement 33.6, where a trigger failure is logged and turns continue."""

    def request(self, user_id: str, correlation_id: str) -> None:
        """Record the request."""
        if self.fail:
            raise OSError("the trigger is unavailable")
        self.requests.append((user_id, correlation_id))


__all__ = [
    "DEFAULT_DISCLAIMER",
    "DEFAULT_EMERGENCY_GUIDANCE",
    "DEFAULT_FORBIDDEN_PATTERNS",
    "InMemoryAdviceAuditStore",
    "LocalGuardrailChecker",
    "RecordingAssociationTrigger",
    "ScriptedServingClient",
    "canned_air_quality",
]
