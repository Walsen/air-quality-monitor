"""The five retrieval tools over the Serving_Client (Reqs 2, 3, 4, 5, 6).

**The credential is captured in a CLOSURE, never taken as a tool parameter.** A tool's
`inputSchema` is part of the prompt the model sees, so a credential parameter would put it there
— where the model could be asked for it, could hallucinate one, or could echo it back. Req 5.1
forwards it only to the Serving_Client and Req 5.2 keeps it out of every prompt, and a closure
is how both hold by construction rather than by care. A test asserts no tool spec mentions it
and that the properties map is empty.

**Every retrieval records into the accumulator.** `RetrievalRecorder` captures the ordered
`ToolCall` trajectory Req 35.4 asserts over, and — the load-bearing part — every NUMERAL that
arrived in a response. That is the seam between retrieval and grounding: a generation quoting a
number that arrived is grounded, one inventing a number is not, and without the link the
grounding check would reject everything.

A FAILED call is recorded too. An attempt that failed is part of what happened, and omitting it
would make the trajectory a record of successes rather than of the turn.

**At most one air-quality retrieval per turn** (Req 2.6). A second would spend the budget and
could return a different snapshot mid-turn, leaving the guidance and the basis describing
different readings. The second call says so rather than failing silently, because a model given
no explanation keeps asking.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from strands import tool
from strands.tools.decorator import DecoratedFunctionTool

from aqm_advisor.domain.grounding import numerals
from aqm_advisor.domain.history import history_view
from aqm_advisor.domain.idempotency import TurnIdentity, profile_idempotency_key
from aqm_advisor.domain.records import RetrievedValues, ToolCall
from aqm_advisor.domain.snapshot import DEFAULT_PROFILE_TEXT, snapshot_sites
from aqm_advisor.ports.clock import Clock
from aqm_advisor.ports.protocols import ServingClient, ServingClientError

_MAX_HISTORY_DAYS = 30
"""Service 2's default maximum history span. A drift guard pins it against that
service's own constant, because it is configurable there."""


def _snapshot_notes(body: object) -> list[str]:
    """The framing Reqs 2.4 and 2.5 require, delivered WITH the data rather than hoped for.

    Req 2.5's real failure mode is the model not NOTICING an empty measurement set — an absent
    array is easy to read straight past, and the result is a site quietly dropped from the
    answer. Handing over explicit prose about each quiet site removes the noticing step, so the
    requirement no longer depends on the model's attention. The same reasoning applies to Req
    2.4's default-profile statement.
    """
    notes = [site.text for site in snapshot_sites(body) if not site.has_reading]
    if isinstance(body, dict):
        personalized = body.get("personalized")
        if isinstance(personalized, dict) and personalized.get("usedDefaultProfile"):
            notes.append(DEFAULT_PROFILE_TEXT)
    return notes


def _site_code_of(body: object) -> str | None:
    """The nearest sensor's `siteCode` from a snapshot, for Req 3.1a's history call.

    Takes the FIRST entry of `nearestSensors`, which is the nearest one: Service 2 orders that
    list
    by distance, and a history window is asked about where the user is. Returns None rather than
    a
    fallback when the body has no usable site, so the history tool reports the period
    unavailable
    instead of calling Service 2 with a site it guessed — which Req 3.1a forbids and which
    Service 2
    would answer 404 for, making a guess look like a service fault.
    """
    if not isinstance(body, dict):
        return None
    sensors = body.get("nearestSensors")
    if not isinstance(sensors, list) or not sensors:
        return None
    nearest = sensors[0]
    if not isinstance(nearest, dict):
        return None
    code = nearest.get("siteCode")
    return code if isinstance(code, str) and code.strip() else None


@dataclass
class RetrievalRecorder:
    """Accumulates what a turn retrieved, for grounding and for the trajectory.

    Mutable on purpose: it is the one piece of per-turn state the tools share, and the
    alternative — threading an immutable accumulator through closures the model calls in an
    order nobody controls — cannot work. `values()` produces the frozen `RetrievedValues` the
    verifiers consume.
    """

    calls: list[ToolCall] = field(default_factory=list)
    numeral_values: set[str] = field(default_factory=set)
    medications: set[str] = field(default_factory=set)
    pollen_categories: set[str] = field(default_factory=set)

    def record_call(self, name: str) -> None:
        """Append one call to the trajectory, whether or not it succeeded."""
        self.calls.append(ToolCall(name=name))

    def record_body(self, body: object) -> None:
        """Harvest every groundable value from a retrieved body.

        Numerals come from the SERIALISED body rather than from named fields, deliberately: a
        number the response carried anywhere is a number the model may legitimately quote, and
        enumerating fields would mean a new Service 2 field silently became ungroundable.
        """
        rendered = json.dumps(body, default=str, sort_keys=True)
        self.numeral_values.update(numerals(rendered))

        if not isinstance(body, dict):
            return
        for entry in body.get("medications", ()) or ():
            if isinstance(entry, dict) and entry.get("name"):
                self.medications.add(str(entry["name"]).casefold())
        personalized = body.get("personalized")
        if isinstance(personalized, dict):
            pollen = personalized.get("pollen")
            if isinstance(pollen, dict):
                self.pollen_categories.update(str(value) for value in pollen.values())

    def values(self) -> RetrievedValues:
        """The frozen permitted set and trajectory for this turn."""
        return RetrievedValues(
            numerals=frozenset(self.numeral_values),
            medications=frozenset(self.medications),
            pollen_categories=frozenset(self.pollen_categories),
            tool_calls=tuple(self.calls),
        )


def _failure_note(name: str, error: ServingClientError) -> str:
    """Report a failure as a named KIND and nothing else (Req 21.1, 21.4).

    Never a raw exception, a stack trace or Service 2's error body: Req 21.4 forbids returning
    any of those, and a kind is what a degraded response can honestly name.
    """
    return f"{name} unavailable: {error.kind.value}. Do not state any condition value for it."


def build_retrieval_tools(
    *,
    client: ServingClient,
    credential: str,
    recorder: RetrievalRecorder,
    clock: Clock,
    identity: TurnIdentity,
) -> tuple[DecoratedFunctionTool[Any, Any], ...]:
    """Build the five tools for ONE turn, closed over that turn's credential and recorder.

    Built per turn rather than once per process, because the credential and the accumulator are
    per turn. A process-wide tool would need the credential as a parameter, which is exactly
    what must not happen.

    `identity` is closed over for the same reason the credential is, plus one of its own: a
    tool's `inputSchema` is part of the prompt, so an idempotency key exposed as a parameter
    would be authored by the MODEL — and a re-invoked entrypoint re-runs the model, which would
    invent a fresh key and defeat Req 32.4c entirely. The key must come from something the model
    cannot reach.
    """
    air_quality_calls = 0
    retrieved_site_code: str | None = None
    """The site the snapshot named, for Req 3.1a's history call.

    Held in the closure rather than asked of the model, because Req 3.1a forbids inventing or
    configuring a site: it must be the one Service 2 resolved for THIS user. A site the model
    supplied would be a site the model could get wrong, and Service 2 answers 404 for a site
    absent from its registry — so a wrong guess reads as a service fault rather than the mistake
    it is.
    """
    profile_write_key = profile_idempotency_key(identity=identity)

    @tool
    def air_quality() -> str:
        """Retrieve current air quality for the signed-in user's saved locations.

        Returns the nearest sensors, their per-species readings, the user's personalised
        interpretation, and the basis the values were derived from. Call this at most once per
        conversation turn.

        Returns:
            The air-quality snapshot as JSON, or a note naming why it was unavailable.
        """
        nonlocal air_quality_calls, retrieved_site_code
        recorder.record_call("air_quality")
        if air_quality_calls >= 1:
            return (
                "Air quality was already retrieved for this turn. Use the snapshot you have; "
                "retrieving again could return a different reading than the basis describes."
            )
        air_quality_calls += 1
        try:
            body = client.air_quality(credential)
        except ServingClientError as error:
            return _failure_note("air_quality", error)
        recorder.record_body(body)
        retrieved_site_code = _site_code_of(body)
        return json.dumps({"snapshot": body, "notes": _snapshot_notes(body)}, default=str)

    @tool
    def history(days: int = 7, species: str | None = None) -> str:  # noqa: D417
        """Retrieve the user's recent readings over a window of whole days ending today.

        Use this to describe how conditions have changed, never to compute a forecast.

        Args:
            days: How many days back to read, from 1 to 30. species: An optional single species
            to restrict the window to.

        Returns:
            The readings window as JSON, or a note naming why it was unavailable.
        """
        recorder.record_call("history")
        if days < 1 or days > _MAX_HISTORY_DAYS:
            return (
                f"history unavailable: a window of {days} days is outside the "
                f"permitted bound of 1 to {_MAX_HISTORY_DAYS} days. "
                "Report the period as unavailable."
            )
        end = clock.now()
        start = end - dt.timedelta(days=days)
        selected = frozenset({species}) if species else None
        if retrieved_site_code is None:
            # Req 3.1a: the site must come from a snapshot retrieved this turn. Reported as
            # unavailable rather than guessed, because Service 2 answers 404 for a site absent
            # from its registry — a guess would surface as a service fault instead of the
            # mistake it is.
            return (
                "history unavailable: current air quality has not been retrieved this turn, so "
                "the site to read history for is not known. Retrieve air quality first, then "
                "ask "
                "for history. Report the period as unavailable if that is not possible."
            )
        try:
            body = client.history(credential, retrieved_site_code, start, end, selected)
        except ServingClientError as error:
            return _failure_note("history", error)
        recorder.record_body(body)
        return json.dumps(
            {"history": body, "summary": history_view(body).text}, default=str
        )

    @tool
    def profile_get() -> str:
        """Retrieve the signed-in user's health profile, including any recorded medications.

        Naming a medication is permitted only from this retrieved list, and only as
        preparedness.

        Returns:
            The profile as JSON, or a note naming why it was unavailable.
        """
        recorder.record_call("profile_get")
        try:
            body = client.profile_get(credential)
        except ServingClientError as error:
            return _failure_note("profile_get", error)
        recorder.record_body(body)
        return json.dumps(body, default=str)

    @tool
    def profile_put(patch: str) -> str:
        """Update the signed-in user's health profile with fields the user has confirmed.

        Only write what the user explicitly asked to change. Never write a value you inferred.

        Args:
            patch: A JSON object of the profile fields to change.

        Returns:
            The updated profile as JSON, or a note naming why it was unavailable.
        """
        recorder.record_call("profile_put")
        try:
            body = client.profile_put(
                credential, json.loads(patch), profile_write_key
            )
        except ServingClientError as error:
            return _failure_note("profile_put", error)
        except json.JSONDecodeError:
            return "profile_put unavailable: the patch was not valid JSON."
        recorder.record_body(body)
        return json.dumps(body, default=str)

    @tool
    def symptom_entry_put(entry: str) -> str:
        """Record a symptom diary entry the user has explicitly confirmed.

        Restate the severity and markers you inferred and obtain confirmation BEFORE calling
        this. The user decides what their day was like, not you.

        Args:
            entry: A JSON object with the date, severity, markers and whether a reliever was
            used.

        Returns:
            The stored entry as JSON, or a note naming why it was unavailable.
        """
        recorder.record_call("symptom_entry_put")
        try:
            body = client.symptom_entry_put(credential, json.loads(entry))
        except ServingClientError as error:
            return _failure_note("symptom_entry_put", error)
        except json.JSONDecodeError:
            return "symptom_entry_put unavailable: the entry was not valid JSON."
        recorder.record_body(body)
        return json.dumps(body, default=str)

    return (air_quality, history, profile_get, profile_put, symptom_entry_put)


__all__ = ["RetrievalRecorder", "build_retrieval_tools"]
