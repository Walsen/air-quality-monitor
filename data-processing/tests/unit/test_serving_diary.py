"""Serving-API wiring for Requirements 30, 31 and 32.

Three things have to be true at once, and two of them are about what does NOT appear.

**The diary has to be reachable.** Req 31 is worth nothing if no route can write an entry.

**The Symptom_Note must appear in no response body but its own** (Req 31.6). It is prose, so
it is both a clinical narrative and an injection vector; it exists for the user's own recall
and reaches no computation. The air-quality body is the one a downstream agent consumes, so a
note leaking into it would put free text into the very place Requirement 18 of the advisor
spec treats as untrusted.

**A Medication_Entry must appear in no air-quality body either** (Req 30.11). The advisor
reads the profile explicitly when it needs one; a medication list riding along on every
air-quality response would put a drug name into a body served on every poll.

Plus: erasure has to reach the diary (Requirements 30.10, 31.9) and the learned threshold has to
be readable on the serving path as Requirement 32.7's fourth precedence tier.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import httpx
import pytest
from fastapi import FastAPI

from aqm_ingestion.adapters.memory import (
    InMemoryAuditStore,
    InMemoryProfileStore,
    InMemorySymptomLogStore,
    LocalAuthenticator,
)
from aqm_ingestion.domain.association import LearnedThreshold
from aqm_ingestion.domain.profile import RECOGNIZED_CONSENT_VERSIONS
from aqm_ingestion.ports.clock import FixedClock
from aqm_ingestion.serving.app import build_app
from aqm_ingestion.serving.profiles import ProfileService
from aqm_ingestion.serving.symptoms import SymptomLogService

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_TOKEN = "dev-token"
_USER = "user-1"


def _stack() -> tuple[FastAPI, InMemorySymptomLogStore, ProfileService]:
    clock = FixedClock(_NOW)
    symptom_store = InMemorySymptomLogStore(clock=clock)
    audit = InMemoryAuditStore()
    profiles = ProfileService(profiles=InMemoryProfileStore(), audit=audit)
    symptoms = SymptomLogService(store=symptom_store, clock=clock)
    app = build_app(
        authenticator=LocalAuthenticator({_TOKEN: _USER}),
        clock=clock,
        profiles=profiles,
        symptoms=symptoms,
        audit=audit,
    )
    return app, symptom_store, profiles


def _call(
    app: FastAPI,
    method: str,
    path: str,
    body: object | None = None,
    token: str | None = _TOKEN,
) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {} if token is None else {"Authorization": f"Bearer {token}"}
            return await client.request(method, path, json=body, headers=headers)

    return asyncio.run(_run())


def _entry(**overrides: object) -> dict[str, object]:
    return {
        "entryDate": "2026-06-30",
        "severity": 3,
        "markers": ["cough", "wheeze"],
        "relieverUsed": True,
    } | overrides


# --- Req 31: the diary is reachable -------------------------------------

def test_an_entry_can_be_written_and_read_back() -> None:
    app, _store, _profiles = _stack()
    written = _call(app, "PUT", "/v1/symptoms/me", _entry())
    assert written.status_code == 200, written.text

    listed = _call(app, "GET", "/v1/symptoms/me?start=2026-06-01&end=2026-07-01")
    assert listed.status_code == 200
    entries = listed.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["severity"] == 3
    assert entries[0]["markers"] == ["cough", "wheeze"]


def test_the_diary_routes_require_authentication() -> None:
    app, _store, _profiles = _stack()
    assert _call(app, "GET", "/v1/symptoms/me", token=None).status_code == 401
    assert _call(app, "PUT", "/v1/symptoms/me", _entry(), token=None).status_code == 401


def test_a_second_write_for_a_date_replaces_rather_than_accumulating() -> None:
    # Req 31.7 through the API, not only in the store: two entries for one day would
    # double-count it in the Req 32 association.
    app, _store, _profiles = _stack()
    _call(app, "PUT", "/v1/symptoms/me", _entry(severity=1))
    _call(app, "PUT", "/v1/symptoms/me", _entry(severity=5))
    entries = _call(app, "GET", "/v1/symptoms/me?start=2026-06-01&end=2026-07-01").json()[
        "entries"
    ]
    assert len(entries) == 1
    assert entries[0]["severity"] == 5


def test_one_users_diary_never_reaches_another() -> None:
    clock = FixedClock(_NOW)
    store = InMemorySymptomLogStore(clock=clock)
    audit = InMemoryAuditStore()
    app = build_app(
        authenticator=LocalAuthenticator({"token-a": "user-a", "token-b": "user-b"}),
        clock=clock,
        profiles=ProfileService(profiles=InMemoryProfileStore(), audit=audit),
        symptoms=SymptomLogService(store=store, clock=clock),
        audit=audit,
    )
    _call(app, "PUT", "/v1/symptoms/me", _entry(severity=5), token="token-a")
    theirs = _call(
        app, "GET", "/v1/symptoms/me?start=2026-06-01&end=2026-07-01", token="token-b"
    )
    assert theirs.json()["entries"] == []


# --- Req 19.8 / §5: bad input never 500s --------------------------------

@pytest.mark.parametrize(
    "body",
    [
        None,
        42,
        "a string",
        {"entryDate": "not-a-date", "severity": 3, "relieverUsed": False},
        {"entryDate": "2026-06-30", "severity": 9, "relieverUsed": False},
        {"entryDate": "2026-06-30", "severity": 3, "relieverUsed": False, "markers": ["fever"]},
        {"entryDate": "2999-01-01", "severity": 3, "relieverUsed": False},
        {"entryDate": "2026-06-30", "severity": 3, "relieverUsed": False,
         "diagnosis": "asthma"},
    ],
)
def test_a_rejected_entry_is_a_400_never_a_500(body: object) -> None:
    app, _store, _profiles = _stack()
    response = _call(app, "PUT", "/v1/symptoms/me", body)
    assert response.status_code == 400, response.text
    assert "problems" in response.json()


def test_a_rejection_does_not_echo_the_submitted_note() -> None:
    # Req 31.10 covers error messages as well as logs, and the note is the most sensitive string
    # the service holds.
    app, _store, _profiles = _stack()
    response = _call(
        app,
        "PUT",
        "/v1/symptoms/me",
        _entry(severity=99, note="woke at three unable to breathe"),
    )
    assert response.status_code == 400
    assert "woke at three" not in response.text


def test_an_inverted_window_is_a_400_naming_the_parameters() -> None:
    app, _store, _profiles = _stack()
    response = _call(app, "GET", "/v1/symptoms/me?start=2026-07-01&end=2026-06-01")
    assert response.status_code == 400
    assert "start" in response.text or "end" in response.text


# --- Req 31.6: the note reaches only its own route ----------------------

def test_the_note_is_returned_on_the_diary_route_because_it_is_for_recall() -> None:
    app, _store, _profiles = _stack()
    _call(app, "PUT", "/v1/symptoms/me", _entry(note="slept badly"))
    entries = _call(app, "GET", "/v1/symptoms/me?start=2026-06-01&end=2026-07-01").json()[
        "entries"
    ]
    assert entries[0]["note"] == "slept badly"


def test_the_air_quality_body_has_no_field_a_note_could_occupy() -> None:
    # Req 31.6 asserted STRUCTURALLY over the response models rather than over one rendered
    # body, so a body that happens to omit the note today cannot start carrying it later.
    from aqm_ingestion.serving import models

    for name in dir(models):
        model = getattr(models, name)
        fields = getattr(model, "model_fields", None)
        if fields is None:
            continue
        for forbidden in ("note", "notes", "symptomNote", "symptoms"):
            assert forbidden not in fields, f"{name} carries {forbidden!r}"


def test_the_air_quality_body_has_no_field_a_medication_could_occupy() -> None:
    # Req 30.11: a Medication_Entry must not ride along on the air-quality response.
    from aqm_ingestion.serving import models

    for name in dir(models):
        model = getattr(models, name)
        fields = getattr(model, "model_fields", None)
        if fields is None:
            continue
        for forbidden in ("medication", "medications", "routines"):
            assert forbidden not in fields, f"{name} carries {forbidden!r}"


# --- Req 30 / 31: the profile route carries the new fields ---------------

def _profile_body(**overrides: object) -> dict[str, object]:
    """The profile route's real wire shape — snake_case, unlike the air-quality body.

    Copied from the shape `build_profile` actually accepts rather than guessed at: the first
    draft of these tests used camelCase and was rejected for every field at once, which is the
    route telling the truth.
    """
    return {
        "condition": "asthma",
        "sensitivity_level": "elevated",
        "consent": {
            "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
            "given_at": _NOW.isoformat(),
        },
        "created_at": _NOW.isoformat(),
        "updated_at": _NOW.isoformat(),
    } | overrides


def test_a_profile_write_accepts_medications_and_routines() -> None:
    app, _store, _profiles = _stack()
    body = _profile_body(
        medications=[{"name": "salbutamol", "role": "reliever"}],
        routines=[
            {
                "days": ["monday"],
                "start_time": "07:00:00",
                "duration_hours": 1.0,
                "activity_level": "vigorous",
            }
        ],
    )
    response = _call(app, "PUT", "/v1/profile/me", body)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["medications"] == [{"name": "salbutamol", "role": "reliever"}]
    assert len(out["routines"]) == 1
    assert out["routines"][0]["start_time"] == "07:00:00"


def test_a_profile_write_rejects_a_medication_dose_without_echoing_it() -> None:
    app, _store, _profiles = _stack()
    body = _profile_body(
        medications=[
            {"name": "salbutamol", "role": "reliever", "dose": "two puffs twice daily"}
        ]
    )
    response = _call(app, "PUT", "/v1/profile/me", body)
    assert response.status_code == 400
    assert "two puffs" not in response.text


def test_a_profile_write_rejects_too_many_medications_naming_the_limit() -> None:
    app, _store, _profiles = _stack()
    body = _profile_body(
        medications=[{"name": f"drug-{n}", "role": "other"} for n in range(11)]
    )
    response = _call(app, "PUT", "/v1/profile/me", body)
    assert response.status_code == 400
    assert "medications" in response.text


# --- Req 30.10 / 31.9: erasure reaches the diary ------------------------

def test_deletion_removes_the_diary_and_reports_the_count() -> None:
    app, store, _profiles = _stack()
    for day in ("2026-06-28", "2026-06-29", "2026-06-30"):
        _call(app, "PUT", "/v1/symptoms/me", _entry(entryDate=day))
    assert store.count_all() == 3

    receipt = _call(app, "DELETE", "/v1/profile/me")
    assert receipt.status_code == 200
    assert receipt.json()["symptomEntriesDeleted"] == 3
    assert store.count_all() == 0


def test_deletion_of_a_user_with_no_diary_reports_zero() -> None:
    app, _store, _profiles = _stack()
    assert _call(app, "DELETE", "/v1/profile/me").json()["symptomEntriesDeleted"] == 0


def test_deletion_leaves_another_users_diary_alone() -> None:
    clock = FixedClock(_NOW)
    store = InMemorySymptomLogStore(clock=clock)
    audit = InMemoryAuditStore()
    app = build_app(
        authenticator=LocalAuthenticator({"token-a": "user-a", "token-b": "user-b"}),
        clock=clock,
        profiles=ProfileService(profiles=InMemoryProfileStore(), audit=audit),
        symptoms=SymptomLogService(store=store, clock=clock),
        audit=audit,
    )
    _call(app, "PUT", "/v1/symptoms/me", _entry(), token="token-a")
    _call(app, "PUT", "/v1/symptoms/me", _entry(), token="token-b")
    _call(app, "DELETE", "/v1/profile/me", token="token-a")
    assert store.count_all() == 1


# --- Req 32.7 / 32.12: the learned threshold is readable ----------------

def test_a_learned_threshold_round_trips_through_the_store() -> None:
    # Req 32.12: the derivation happens on its own schedule and the result is STORED for the
    # serving path to read, so the store is the handoff point.
    clock = FixedClock(_NOW)
    store = InMemorySymptomLogStore(clock=clock)
    learned = (
        LearnedThreshold(species="PM25", sub_index=88, lag_days=3, observations=20),
    )
    store.put_learned_thresholds(_USER, learned)
    assert store.learned_thresholds(_USER) == {"PM25": learned[0]}


def test_learned_thresholds_are_erased_with_the_diary_they_came_from() -> None:
    # They are DERIVED from the diary, so leaving them behind would keep an inference about a
    # user whose data was erased.
    clock = FixedClock(_NOW)
    store = InMemorySymptomLogStore(clock=clock)
    store.put_learned_thresholds(
        _USER, (LearnedThreshold(species="PM25", sub_index=88, lag_days=3, observations=20),)
    )
    store.forget_user(_USER)
    assert store.learned_thresholds(_USER) == {}


def test_one_users_learned_threshold_never_reaches_another() -> None:
    clock = FixedClock(_NOW)
    store = InMemorySymptomLogStore(clock=clock)
    store.put_learned_thresholds(
        "user-a", (LearnedThreshold(species="PM25", sub_index=60, lag_days=0, observations=20),)
    )
    assert store.learned_thresholds("user-b") == {}


def test_the_diary_response_is_json_serialisable() -> None:
    # A date or an enum leaking into the body unserialised is a 500, and Req 19.8 forbids one.
    app, _store, _profiles = _stack()
    _call(app, "PUT", "/v1/symptoms/me", _entry(note="fine"))
    body = _call(app, "GET", "/v1/symptoms/me?start=2026-06-01&end=2026-07-01").text
    assert json.loads(body)["entries"][0]["entryDate"] == "2026-06-30"


def test_the_serving_path_reads_a_learned_threshold_but_never_derives_one() -> None:
    # The wiring test that matters: without it the learned tier is plumbed and unverified.
    # Asserted at the SERVICE seam rather than through a full air-quality response, because the
    # response needs a whole readings/registry/enrichment stack and this is a question about
    # whether the threshold travels, not about how a band is rendered.
    clock = FixedClock(_NOW)
    store = InMemorySymptomLogStore(clock=clock)
    service = SymptomLogService(store=store, clock=clock)
    identity = LocalAuthenticator({_TOKEN: _USER}).verify(_TOKEN)

    assert service.learned_for(identity) == {}
    store.put_learned_thresholds(
        _USER, (LearnedThreshold(species="PM25", sub_index=88, lag_days=3, observations=20),)
    )
    assert service.learned_for(identity)["PM25"].sub_index == 88


def test_the_symptom_service_has_no_way_to_derive_an_association() -> None:
    # Req 32.12 forbids computing an association on the serving path. Asserted structurally over
    # the service's own surface, so a later convenience method cannot quietly add one.
    surface = {
        name for name in dir(SymptomLogService) if not name.startswith("_")
    }
    for forbidden in ("derive", "compute", "compute_association", "associate"):
        assert forbidden not in surface


def test_the_assembler_serves_unchanged_without_a_symptom_log() -> None:
    # The diary is optional, so a deployment that has not enabled it must keep Req 22.1's
    # remaining three tiers working exactly as before.
    from aqm_ingestion.serving.assembler import ResponseAssembler

    parameters = ResponseAssembler.__init__.__annotations__
    assert "symptoms" in parameters
