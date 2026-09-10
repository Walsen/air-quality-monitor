"""Degradation paths and the drift watcher (Requirements 21.1, 21.2, 21.3, 21.7, 21.8).

**`DriftWatcher` is why this module exists.** `emergency_guidance_drifted` already detected
drift and was tested — but nothing ever CALLED it, so A8a's narrow exception was bought with a
guard that was never armed. A detector nobody invokes is worse than no detector at all, because
the design cites it as the reason the exception is safe.

Three properties of the warning are load-bearing:

* **Once per run, not once per turn.** A per-turn warning on a busy process is a flood, and an
  operator who learns to filter it out has exactly the protection of one with no detector.
* **The first SUCCESSFUL retrieval of the run decides** (Req 21.8's wording). A failed retrieval
  must not consume the comparison, or the run records having compared something it never saw and
  the drift goes unreported for the whole process lifetime.
* **The field name only, never either text.** These strings are not health data, but a warning
  quoting both puts clinical wording into a log for no diagnostic gain — the field name is all
  an operator needs to go and compare.

**Every degraded response keeps the envelope and any escalation** (Reqs 21.3, 21.9). A degraded
turn is exactly when a user most needs the emergency direction, so dropping it under failure
inverts the priority.

**No degraded response states a condition value** (Req 21.1). Enforced by having nowhere to put
one: `basis` stays `None`, and the guidance is assembled from fixed sentences plus the caller's
own already-verified text, so no retrieved number can arrive through this path.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from aqm_advisor.domain.attribution import describe_subject
from aqm_advisor.domain.envelope import emergency_guidance_drifted
from aqm_advisor.domain.models import AdvisoryResponse, Escalation, GuardrailEnvelope
from aqm_advisor.observability.logging import EventLogger

_UNAVAILABLE_SENTENCE = (
    "Current conditions are unavailable, so I cannot describe the air near you right now."
)

DRIFT_FIELD_NAME = "emergencyGuidance"
"""The only thing the drift warning names. Never either text (Req 21.8)."""


def missing_data_note(missing: Sequence[str]) -> str:
    """Say what was not retrieved, naming each item (Req 21.7).

    Naming them is the requirement's substance: "some data was unavailable" leaves the user
    unable to tell whether the part they asked about is the part that is missing.

    Each subject goes through `describe_subject`, so the note carries no numeral — for the same
    reason Req 7.3's unavailable text does not: a number describing absent data would be an
    ungrounded claim, since by definition it was not retrieved. The first version of this made
    that the CALLER's duty, and a test with `"PM2.5"` caught the digits going straight through.

    Raises:
        ValueError: for an empty list. A note reporting nothing missing has no content, and the
        caller has a
            bug worth surfacing.
    """
    if not missing:
        raise ValueError("missing must name at least one thing that was not retrieved")
    named = ", ".join(describe_subject(subject) for subject in missing)
    return f"I could not retrieve {named}, so this answer leaves that out."


def degraded_response(
    *,
    envelope: GuardrailEnvelope,
    answered_at: dt.datetime,
    missing: Sequence[str],
    escalation: Escalation | None = None,
    available_guidance: str | None = None,
) -> AdvisoryResponse:
    """Build an honest degraded response (Reqs 21.1, 21.2, 21.3, 21.7).

    `available_guidance` is the PARTIAL case (Req 21.7): advise on what was retrieved rather
    than failing the whole turn. It is the caller's already-verified text — this function never
    generates prose, and passing unverified text here would route around the pipeline's
    fail-closed rule.

    With nothing available, the response says conditions are unavailable and states no condition
    value. `basis` is always `None` on this path: a basis is provenance for values, and there
    are no values.
    """
    parts: list[str] = []
    if available_guidance and available_guidance.strip():
        parts.append(available_guidance.strip())
    else:
        parts.append(_UNAVAILABLE_SENTENCE)
    parts.append(missing_data_note(missing))

    return AdvisoryResponse(
        escalation=escalation,
        guidance=" ".join(parts),
        basis=None,
        envelope=envelope,
        degraded=True,
        answered_at=answered_at,
    )


class DriftWatcher:
    """Compares the configured A8a fallback against the run's first served guidance (Req 21.8).

    Per-process state, deliberately. Req 21.8 scopes the comparison to "each run", so the latch
    lives as long as the process and a new deployment re-arms it — which is the granularity that
    matters, since drift is introduced by a change to one side or the other, not by a change in
    traffic.
    """

    def __init__(self, *, configured: str, logger: EventLogger) -> None:
        """Take the configured fallback and where to warn."""
        self._configured = configured
        self._logger = logger
        self._compared = False

    @property
    def has_compared(self) -> bool:
        """Whether the comparison has been made this run.

        Observable so the audit can record that the check RAN. Without it, the absence of a
        warning would have to stand for both "no drift" and "never checked", which are very
        different states.
        """
        return self._compared

    def observe_failure(self) -> None:
        """Note a retrieval that did not yield an envelope.

        Deliberately does nothing. It exists so a caller can route every retrieval outcome
        through the watcher without a failure closing the latch — the latch must be closed only
        by a real comparison, or the run records having compared something it never saw.
        """
        return None

    def observe_served(self, served_emergency_guidance: str) -> None:
        """Compare the run's FIRST successfully served guidance against the configured fallback.

        A blank text is not a successful retrieval: it is an unusable body, and treating it as a
        comparison would both report spurious drift and close the latch against the real one.
        """
        if self._compared:
            return
        if not served_emergency_guidance.strip():
            return
        self._compared = True
        if emergency_guidance_drifted(
            served=served_emergency_guidance, configured=self._configured
        ):
            self._logger.warning(
                "guardrail_envelope_fallback_drift",
                field=DRIFT_FIELD_NAME,
            )


__all__ = [
    "DRIFT_FIELD_NAME",
    "DriftWatcher",
    "degraded_response",
    "missing_data_note",
]
