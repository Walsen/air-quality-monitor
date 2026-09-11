"""Local implementations of the three non-model ports (task 2.4).

These are NOT test doubles bolted on afterwards — they are what makes the offline suite possible
(Requirement 26.5), so they ship in the package rather than under ``tests/``. A property test at
100 examples needs them importable from the library, and so does a developer running the agent
locally with ``app.run()`` and no AWS.

THE CANNED SERVING BODY IS TRANSCRIBED FROM SERVICE 2's REAL RESPONSE SHAPE, NOT INVENTED. That
is the single biggest risk in this package: every one of the 20 correctness properties reads
these bodies, so a shape that drifts from what Service 2 actually serves would have the whole
suite proving things about a response nobody sends.

It cannot be IMPORTED — the engineering practices forbid importing across service directories
until a shared contract package is specced. So it is transcribed, and
``test_the_canned_body_matches_service_2s_real_response_model`` reads the sibling service's
model definition from disk and compares the field names, skipping when that directory is absent
so this service still builds alone. A filesystem check is not an import: nothing here depends on
Service 2 at runtime.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from aqm_advisor.domain.forbidden import forbidden_matches
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
    keeps that claim true rather than aspirational.

    ``hours_available`` defaults to a FULL window; pass fewer to script the partial-window case
    Req 9.3 makes traceable. ``nowcast=False`` scripts an index that was not nowcast-derived at
    all, which Req 9.3a calls a complete answer rather than a gap. Those are two DIFFERENT
    states and a caller must be able to tell them apart, so they are separate parameters rather
    than one nullable number — collapsing them is exactly the confusion Req 9.3a exists to
    forbid.
    """
    available = window_hours if hours_available is None else hours_available
    # MIRRORS Service 2's `NOWCAST_COVERAGE_CAPS`, and its FOUR coverage states rather than
    # three.
    # Derived here rather than passed in because a caller must NOT be able to script a partial
    # window
    # that still reports high confidence: Service 2 never sends that, and a fake able to express
    # it
    # would let a test prove behaviour against a response that cannot occur. Req 15.6 makes
    # confidence
    # the single weakness authority, so the fake has to honour the coupling.
    #
    # nowcast=False -> not_applicable: no window species at all (e.g. NO2). NO cap.
    # available == 0 -> insufficient: too few hours to compute one. Capped at low.
    # 0 < available < window -> incomplete: computed from a short window. Capped at medium.
    #   available == window        -> complete:       no cap.
    #
    # not_applicable and insufficient are DIFFERENT and must not be collapsed — that is the very
    # confusion Req 9.3a forbids, and an absent nowcast is the benign one.
    if not nowcast:
        confidence = "high"
    elif available == 0:
        confidence = "low"
    elif available < window_hours:
        confidence = "medium"
    else:
        confidence = "high"
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
                        "confidence": confidence,
                        "subIndex": sub_index,
                        "band": band,
                        "method": "nowcast",
                        "mixingRatioPpb": None,
                    }
                ],
                "overallAqi": sub_index,
                "band": band,
                "drivingPollutant": driving_pollutant,
                "confidence": confidence,
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

    _applied_write_keys: set[str] = field(default_factory=set)
    """Keys already applied, so a redelivered write is collapsed rather than merely accepted.

    The call is still recorded in `calls` when it is collapsed — an offline test needs to see
    that the second delivery ARRIVED and was suppressed, which is different from it never
    happening.
    """

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
        site_code: str,
        start: dt.datetime,
        end: dt.datetime,
        species: frozenset[str] | None = None,
    ) -> Mapping[str, object]:
        """Return the canned history body, recording the site and window asked for."""
        return self._answer("history", self.history_body, site_code, start, end, species)

    def profile_get(self, credential: str) -> Mapping[str, object]:
        """Return the canned profile."""
        return self._answer("profile_get", self.profile_body)

    def profile_put(
        self,
        credential: str,
        patch: Mapping[str, object],
        idempotency_key: str,
    ) -> Mapping[str, object]:
        """Record a profile write and echo it back, collapsing a repeated key (Req 32.4c).

        The local adapter COLLAPSES rather than merely accepting the key, so an offline test can
        observe the deduplication instead of trusting that Service 2 will perform it. An adapter
        that took the key and ignored it would let every idempotency test pass while the real
        protection existed nowhere.
        """
        if idempotency_key in self._applied_write_keys:
            return self._answer("profile_put", self.profile_body)
        self._applied_write_keys.add(idempotency_key)
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


@dataclass
class LocalGuardrailChecker:
    """The offline Guardrail_Checker: pattern matching, no network.

    Requirement 34.5 puts the local check and the managed one at TWO INDEPENDENT points, neither
    conditional on the other — this is the one that runs offline and when Bedrock is
    unreachable.

    A verdict names the CATEGORY that fired and never the offending text (Requirement 8.6), so a
    rejection is diagnosable without storing what was rejected.
    """

    patterns: tuple[str, ...] | None = None
    """None uses the domain defaults. A supplied set REPLACES them (Req 8.7)."""
    unavailable: bool = False
    """Scripts Requirement 34.6's fail-closed path, where the check cannot run at all."""

    checks: list[int] = field(default_factory=list)
    """The LENGTH of each text checked. Never the text: this object is held in tests whose whole
    point is that generated prose does not get retained anywhere it need not be."""

    def check(self, text: str) -> GuardrailResult:
        """Return the verdict for one generation, delegating the RULES to the domain.

        The pattern set and the category mapping are not this adapter's to own. They were
        briefly duplicated here, which made the adapter a second authority on what may be said —
        and two authorities on a safety rule is how they come to disagree. This is a
        transport-shaped wrapper over `domain.forbidden`.
        """
        self.checks.append(len(text))
        if self.unavailable:
            return GuardrailResult(verdict=GuardrailVerdict.UNAVAILABLE)
        categories = forbidden_matches(text, self.patterns)
        if categories:
            return GuardrailResult(
                verdict=GuardrailVerdict.INTERVENED, categories=categories
            )
        return GuardrailResult(verdict=GuardrailVerdict.PASSED)


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
    wants is that the request HAPPENED and that the turn did not wait for it — which needs the
    request recorded and nothing else.
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
    "InMemoryAdviceAuditStore",
    "LocalGuardrailChecker",
    "RecordingAssociationTrigger",
    "ScriptedServingClient",
    "canned_air_quality",
]
