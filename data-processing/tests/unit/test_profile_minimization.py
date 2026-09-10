"""Minimization and isolation tests for the User_Profile (task 17.3).

Requirements 17.9 and 17.10.

The leak sweep here is deliberately DATA-DRIVEN rather than a list of hand-picked strings.
Tasks 17.1 and 17.2 each asserted a handful of literals ("asthma", "51.507") do not reach the
logs, which holds only for the fields somebody remembered. This module instead builds a profile
whose every field carries a distinctive sentinel, runs the whole lifecycle, and asserts no
sentinel appears anywhere in the captured output — plus a completeness test that FAILS when a
field is added to UserProfile without a sentinel. So a field added later is covered by default,
which is the same shape Property 39 uses for provenance.

One sentinel choice needs explaining: the location name is `commute`, not `home`. Requirement
17.5 permits both, but sweeping the captured output for the literal "home" would false-positive
on any log line carrying a filesystem path (`/home/...`) — a test that fails for a reason
unrelated to what it checks is worse than no test.

Requirement 17.10's second half — Readings selected by another user's locations — can only be
asserted end to end once geographic selection exists (task 21) and the response assembler exists
(task 24). What IS assertable now is the seam it depends on: the positions a query is built
from derive from the resolving identity's own profile and nothing else. The tests below go
through the store by identity, so a mis-keyed lookup or a shared default really does fail them.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect

import pytest

from aqm_ingestion.adapters.memory import InMemoryAuditStore, InMemoryProfileStore
from aqm_ingestion.domain.profile import (
    RECOGNIZED_CONSENT_VERSIONS,
    ActivityLevel,
    Condition,
    LocationName,
    SensitivityLevel,
    UserProfile,
)
from aqm_ingestion.ingest.archive import archive_payload
from aqm_ingestion.observability.logging import configure_logging, get_logger
from aqm_ingestion.ports.protocols import ArchiveMeta, VerifiedIdentity
from aqm_ingestion.serving.profiles import ProfileService, query_positions

_CONSENT_VERSION = next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS)))

# One distinctive value per profile field. The KEY is the UserProfile field it stands for, which
# is what lets the completeness test below prove the sweep covers the whole model.
_SENTINELS: dict[str, tuple[str, ...]] = {
    "condition": ("asthma",),
    "sensitivity_level": ("elevated",),
    # both the submitted precision and the coarser stored form
    "locations": ("commute", "51.5074", "51.507", "-0.1278", "-0.128"),
    "personal_thresholds": ("37.5",),
    "activity_level": ("vigorous",),
    "activity_duration_hours": ("3.25",),
    # A medication name is the single most sensitive string a profile now holds: it implies the
    # diagnosis even though no diagnosis field exists. A routine's day and hour are a movement
    # pattern, so both the day name and the time are swept.
    "medications": ("beclometasone", "salbutamol"),
    "routines": ("thursday", "06:45", "6:45"),
    # an instant is not health data, but a created_at reveals when this person enrolled
    "consent": (_CONSENT_VERSION,),
    "created_at": ("2019-03-04",),
    "updated_at": ("2019-03-05",),
}

_IDENTITY_FIELDS = frozenset({"user_id"})
"""Requirement 17.9 explicitly PERMITS the pseudonymous identity in diagnostics."""


def _loud_fields(user_id: str = "user-sentinel") -> dict[str, object]:
    """A profile whose every field carries a value that cannot appear by accident."""
    return {
        "user_id": user_id,
        "condition": Condition.ASTHMA,
        "sensitivity_level": SensitivityLevel.ELEVATED,
        "personal_thresholds": {"PM25": {"kind": "sub_index", "value": 37.5}},
        "locations": [
            {"name": LocationName.COMMUTE, "latitude": 51.5074, "longitude": -0.1278}
        ],
        "activity_level": ActivityLevel.VIGOROUS,
        "activity_duration_hours": 3.25,
        "medications": [
            {"name": "salbutamol", "role": "reliever"},
            {"name": "beclometasone", "role": "preventer"},
        ],
        "routines": [
            {
                "days": ["thursday"],
                "start_time": dt.time(6, 45),
                "duration_hours": 1.5,
                "activity_level": ActivityLevel.VIGOROUS,
                "location": LocationName.COMMUTE,
            }
        ],
        "consent": {
            "version": _CONSENT_VERSION,
            "given_at": dt.datetime(2019, 3, 4, tzinfo=dt.UTC),
        },
        "created_at": dt.datetime(2019, 3, 4, tzinfo=dt.UTC),
        "updated_at": dt.datetime(2019, 3, 5, tzinfo=dt.UTC),
    }


def _service() -> ProfileService:
    return ProfileService(profiles=InMemoryProfileStore(), audit=InMemoryAuditStore())


def _all_sentinels() -> tuple[str, ...]:
    return tuple(value for values in _SENTINELS.values() for value in values)


# --- Req 17.9: nothing reaches a log ------------------------------------

def test_no_profile_field_reaches_the_log_across_the_whole_lifecycle(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("debug")  # the most talkative level, so nothing hides at INFO
    service = _service()
    identity = VerifiedIdentity(user_id="user-sentinel")

    service.write(identity, _loud_fields())
    service.resolve(identity)
    service.resolve(VerifiedIdentity(user_id="somebody-else"))
    service.delete(identity)

    output = capsys.readouterr().out
    leaked = [sentinel for sentinel in _all_sentinels() if sentinel in output]
    assert leaked == [], f"profile values reached the log: {leaked}"


def test_the_identity_itself_is_still_logged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The counterpart to the sweep: if NOTHING were logged the sweep above would pass
    # vacuously, and Req 17.9 explicitly permits the pseudonymous identity.
    configure_logging("info")
    service = _service()
    service.write(VerifiedIdentity(user_id="user-sentinel"), _loud_fields())
    assert "user-sentinel" in capsys.readouterr().out


def test_the_sweep_covers_every_profile_field() -> None:
    # Two-sided completeness: adding a field to UserProfile without giving it a sentinel makes
    # the sweep silently weaker, so this test fails until the sentinel exists.
    modelled = set(UserProfile.model_fields) - _IDENTITY_FIELDS
    assert modelled == set(_SENTINELS), (
        f"unswept profile fields: {sorted(modelled - set(_SENTINELS))}; "
        f"stale sentinels: {sorted(set(_SENTINELS) - modelled)}"
    )


def test_the_leak_sweep_would_actually_catch_a_leak(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Proves the DETECTOR detects, the same self-check the task-3.4 architecture rules carry.
    # Without this, the sweep above passes just as happily against a logger that emits nothing,
    # and would keep passing if logging were later disabled or the capture broke.
    configure_logging("info")
    get_logger("test.leak-control").info(
        "deliberate_control_event", note="asthma"
    )
    output = capsys.readouterr().out
    leaked = [sentinel for sentinel in _all_sentinels() if sentinel in output]
    assert leaked == ["asthma"], (
        "the sweep failed to see a value that WAS emitted, so a real leak could pass unnoticed"
    )


# --- Req 17.9: nothing reaches an error message -------------------------

def test_an_identity_mismatch_error_names_no_profile_value() -> None:
    service = _service()
    with pytest.raises(ValueError) as caught:
        service.write(VerifiedIdentity(user_id="user-a"), _loud_fields(user_id="user-b"))
    message = str(caught.value)
    for sentinel in _all_sentinels():
        assert sentinel not in message


def test_a_rejected_field_does_not_reach_the_service_level_error() -> None:
    # 17.1 proved this for build_profile; the service is a different call path, so the guarantee
    # has to hold here too rather than being assumed to carry over.
    from pydantic import ValidationError

    service = _service()
    with pytest.raises(ValidationError) as caught:
        service.write(
            VerifiedIdentity(user_id="user-a"),
            _loud_fields(user_id="user-a") | {"diagnosis_code": "J45.909"},
        )
    assert "J45.909" not in str(caught.value)


# --- Req 17.9: nothing reaches the archive ------------------------------

def test_the_archive_has_no_parameter_a_profile_could_travel_in() -> None:
    # Structural rather than behavioural: Req 17.9 is satisfied because there is nowhere to put
    # a profile, not because callers remember not to. This fails if a parameter is ever added.
    assert set(inspect.signature(archive_payload).parameters) == {
        "archive",
        "payload",
        "meta",
    }


def test_archive_meta_carries_no_profile_field() -> None:
    assert set(ArchiveMeta.__dataclass_fields__) == {
        "ingested_at",
        "transport",
        "source",
    }


def test_no_archive_meta_field_is_typed_on_a_profile() -> None:
    # The field NAMES could stay innocent while a type changed underneath them.
    for spec in dataclasses.fields(ArchiveMeta):
        assert "profile" not in str(spec.type).lower()


def test_a_profile_cannot_be_archived_as_a_payload() -> None:
    # The archive path is typed on bytes, so a profile is REFUSED rather than quietly
    # serialised. archive_payload deliberately does not catch this: §5 says catch the failures
    # you expect, and passing the wrong type is a programming error, not an I/O failure.
    configure_logging("info")  # rebind the handler: an earlier test's capsys stream is closed
    service = _service()
    profile = service.write(VerifiedIdentity(user_id="user-sentinel"), _loud_fields())
    with pytest.raises(TypeError):
        archive_payload(
            _RefusingArchive(),
            profile,  # type: ignore[arg-type]  # deliberately wrong: that is the test
            ArchiveMeta(
                ingested_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
                transport="mqtt",
                source="aqm/sensors/X/data",
            ),
        )


def test_even_an_interpolated_profile_would_leak_nothing() -> None:
    # The backstop behind every assertion above, and the non-circular one: whatever a future
    # error message or log line does with a profile, rendering it cannot expose its contents,
    # because the model refuses to render them (task 17.1). Independent of any call path.
    service = _service()
    profile = service.write(VerifiedIdentity(user_id="user-sentinel"), _loud_fields())
    for rendered in (repr(profile), str(profile), f"{profile}", f"{profile!r}"):
        for sentinel in _all_sentinels():
            assert sentinel not in rendered
        assert "user-sentinel" in rendered  # the identity is still there to diagnose with


class _RefusingArchive:
    """An archive that insists on bytes, as the real port's contract requires."""

    def write(self, payload: bytes, meta: ArchiveMeta) -> str:
        """Refuse anything that is not bytes."""
        if not isinstance(payload, bytes):
            raise TypeError("archive payload must be bytes")
        return "unreachable-in-this-test"


# --- Req 17.10: one user's data never reaches another -------------------

def test_each_user_resolves_only_their_own_profile() -> None:
    service = _service()
    service.write(VerifiedIdentity(user_id="user-a"), _loud_fields(user_id="user-a"))
    service.write(
        VerifiedIdentity(user_id="user-b"),
        _loud_fields(user_id="user-b")
        | {
            "condition": Condition.NONE_DECLARED,
            "locations": [
                {"name": LocationName.WORK, "latitude": 48.8566, "longitude": 2.3522}
            ],
        },
    )

    a = service.resolve(VerifiedIdentity(user_id="user-a")).profile
    b = service.resolve(VerifiedIdentity(user_id="user-b")).profile

    assert a.condition is Condition.ASTHMA
    assert b.condition is Condition.NONE_DECLARED
    assert [loc.name for loc in a.locations] == [LocationName.COMMUTE]
    assert [loc.name for loc in b.locations] == [LocationName.WORK]


def test_query_positions_come_only_from_the_resolving_users_locations() -> None:
    # The seam Req 17.10's second half rests on. Goes through the store BY IDENTITY, so a
    # mis-keyed lookup fails it — this is not a vacuous single-profile assertion.
    service = _service()
    service.write(VerifiedIdentity(user_id="user-a"), _loud_fields(user_id="user-a"))
    service.write(
        VerifiedIdentity(user_id="user-b"),
        _loud_fields(user_id="user-b")
        | {
            "locations": [
                {"name": LocationName.WORK, "latitude": 48.8566, "longitude": 2.3522}
            ]
        },
    )

    b_positions = query_positions(service.resolve(VerifiedIdentity(user_id="user-b")))
    assert b_positions == ((48.857, 2.352),)
    # and user A's London position is nowhere in it
    assert all(abs(lat - 51.507) > 0.001 for lat, _lon in b_positions)


def test_a_user_with_no_locations_selects_nothing() -> None:
    # Req 17.12's default has no locations, so it cannot borrow anyone else's sites.
    service = _service()
    service.write(VerifiedIdentity(user_id="user-a"), _loud_fields(user_id="user-a"))
    assert query_positions(service.resolve(VerifiedIdentity(user_id="nobody"))) == ()


def test_two_users_do_not_share_one_default_profile_object() -> None:
    # A memoized or module-level default would hand both users the SAME object, so one of them
    # would carry the other's identity and any later mutation would cross over.
    service = _service()
    a = service.resolve(VerifiedIdentity(user_id="user-a")).profile
    b = service.resolve(VerifiedIdentity(user_id="user-b")).profile
    assert a is not b
    assert a.user_id == "user-a"
    assert b.user_id == "user-b"


def test_deleting_one_user_leaves_the_other_resolvable() -> None:
    service = _service()
    service.write(VerifiedIdentity(user_id="user-a"), _loud_fields(user_id="user-a"))
    service.write(VerifiedIdentity(user_id="user-b"), _loud_fields(user_id="user-b"))
    service.delete(VerifiedIdentity(user_id="user-a"))
    assert service.resolve(VerifiedIdentity(user_id="user-b")).used_default is False


def test_a_profile_is_immutable_so_it_cannot_be_altered_after_a_read() -> None:
    # Two callers holding one stored profile must not be able to change what the other sees.
    from pydantic import ValidationError

    service = _service()
    identity = VerifiedIdentity(user_id="user-a")
    service.write(identity, _loud_fields(user_id="user-a"))
    profile = service.resolve(identity).profile
    with pytest.raises(ValidationError):
        profile.condition = Condition.NONE_DECLARED


def test_positions_are_ordered_so_selection_is_deterministic() -> None:
    # §2: the order reaches a query, so it must be defined rather than incidental.
    service = _service()
    service.write(
        VerifiedIdentity(user_id="user-a"),
        _loud_fields(user_id="user-a")
        | {
            "locations": [
                {"name": LocationName.HOME, "latitude": 51.5074, "longitude": -0.1278},
                {"name": LocationName.WORK, "latitude": 48.8566, "longitude": 2.3522},
            ]
        },
    )
    resolved = service.resolve(VerifiedIdentity(user_id="user-a"))
    assert query_positions(resolved) == ((51.507, -0.128), (48.857, 2.352))
    assert query_positions(resolved) == query_positions(resolved)


def test_query_positions_takes_exactly_one_resolved_profile() -> None:
    # There is no second profile it could consult, which is what makes the isolation structural
    # rather than a promise. Fails if a wider source is ever threaded in.
    assert set(inspect.signature(query_positions).parameters) == {"resolved"}
