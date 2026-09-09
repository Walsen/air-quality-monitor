"""Unit tests for profile read, write, delete, and the default (task 17.2).

- 17.1: keyed by the VERIFIED identity only.
- 17.8: deletion removes the profile AND every identifying Audit_Record field, RETAINS the
  de-identified counts, and confirms the deletion.
- 17.12: with no profile, the configured default is served and the response DECLARES that
  personalization used defaults, rather than the request failing.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.adapters.memory import InMemoryAuditStore, InMemoryProfileStore
from aqm_ingestion.domain.profile import (
    DEFAULT_PROFILE_CONDITION,
    DEFAULT_PROFILE_SENSITIVITY,
    RECOGNIZED_CONSENT_VERSIONS,
    Condition,
    SensitivityLevel,
)
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.ports.protocols import AuditIdentifiers, VerifiedIdentity
from aqm_ingestion.serving.profiles import ProfileService, ResolvedProfile

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _fields(user_id: str = "user-123", **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "user_id": user_id,
        "condition": Condition.ASTHMA,
        "sensitivity_level": SensitivityLevel.ELEVATED,
        "personal_thresholds": {"PM25": {"kind": "sub_index", "value": 35.0}},
        "locations": [{"name": "home", "latitude": 51.5074, "longitude": -0.1278}],
        "consent": {
            "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
            "given_at": _NOW,
        },
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return base | overrides


def _service() -> tuple[ProfileService, InMemoryProfileStore, InMemoryAuditStore]:
    profiles = InMemoryProfileStore()
    audit = InMemoryAuditStore()
    return ProfileService(profiles=profiles, audit=audit), profiles, audit


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


# --- Req 17.1 keyed by the verified identity ----------------------------

def test_a_profile_is_written_and_read_back() -> None:
    service, _profiles, _audit = _service()
    identity = VerifiedIdentity(user_id="user-123")
    service.write(identity, _fields())
    resolved = service.resolve(identity)
    assert resolved.profile.condition is Condition.ASTHMA
    assert resolved.used_default is False


def test_a_write_for_another_identity_is_refused() -> None:
    # Req 17.1: keyed by the VERIFIED identity, so a body claiming a different user_id
    # cannot write to it — otherwise an authenticated caller could edit anyone's profile
    service, _profiles, _audit = _service()
    with pytest.raises(ValueError, match="identity"):
        service.write(VerifiedIdentity(user_id="user-123"), _fields(user_id="someone-else"))


def test_the_stored_key_is_the_verified_identity() -> None:
    service, profiles, _audit = _service()
    service.write(VerifiedIdentity(user_id="user-123"), _fields())
    assert profiles.get("user-123") is not None
    assert profiles.get("someone-else") is None


def test_one_users_profile_is_invisible_to_another() -> None:
    # Req 17.10, at the store level
    service, _profiles, _audit = _service()
    service.write(VerifiedIdentity(user_id="user-a"), _fields(user_id="user-a"))
    other = service.resolve(VerifiedIdentity(user_id="user-b"))
    assert other.used_default is True
    assert other.profile.condition is DEFAULT_PROFILE_CONDITION


# --- Req 17.12 the default profile --------------------------------------

def test_an_absent_profile_yields_the_configured_default() -> None:
    service, _profiles, _audit = _service()
    resolved = service.resolve(VerifiedIdentity(user_id="nobody"))
    assert resolved.profile.condition is DEFAULT_PROFILE_CONDITION
    assert resolved.profile.sensitivity_level is DEFAULT_PROFILE_SENSITIVITY
    assert resolved.profile.locations == ()


def test_the_default_is_declared_rather_than_the_request_failing() -> None:
    # Req 17.12 is explicit: serve defaults and SAY SO, rather than fail
    service, _profiles, _audit = _service()
    resolved = service.resolve(VerifiedIdentity(user_id="nobody"))
    assert resolved.used_default is True


def test_the_default_carries_no_locations() -> None:
    # Req 17.12 names "no User_Location", which also means a default profile cannot select
    # sites for someone who never supplied any
    service, _profiles, _audit = _service()
    assert not service.resolve(VerifiedIdentity(user_id="nobody")).profile.locations


def test_the_default_is_keyed_to_the_asking_identity() -> None:
    service, _profiles, _audit = _service()
    resolved = service.resolve(VerifiedIdentity(user_id="nobody"))
    assert resolved.profile.user_id == "nobody"


def test_the_default_is_not_written_to_the_store() -> None:
    # serving a default must not silently create a profile the user never consented to
    service, profiles, _audit = _service()
    service.resolve(VerifiedIdentity(user_id="nobody"))
    assert profiles.get("nobody") is None


def test_a_stored_profile_is_not_replaced_by_the_default() -> None:
    service, _profiles, _audit = _service()
    identity = VerifiedIdentity(user_id="user-123")
    service.write(identity, _fields())
    assert service.resolve(identity).used_default is False


# --- Req 17.8 deletion --------------------------------------------------

def test_deletion_removes_the_profile() -> None:
    service, profiles, _audit = _service()
    identity = VerifiedIdentity(user_id="user-123")
    service.write(identity, _fields())
    service.delete(identity)
    assert profiles.get("user-123") is None


def test_deletion_confirms_itself() -> None:
    service, _profiles, _audit = _service()
    identity = VerifiedIdentity(user_id="user-123")
    service.write(identity, _fields())
    receipt = service.delete(identity)
    assert receipt.profile_deleted is True


def test_deletion_de_identifies_the_audit_records() -> None:
    service, _profiles, audit = _service()
    identity = VerifiedIdentity(user_id="user-123")
    service.write(identity, _fields())
    audit.append(AuditIdentifiers(user_id="user-123", served_at=_NOW, route="/advice"))
    audit.append(AuditIdentifiers(user_id="user-123", served_at=_NOW, route="/history"))

    receipt = service.delete(identity)
    assert receipt.audit_records_de_identified == 2
    assert audit.records_for("user-123") == ()


def test_deletion_retains_the_de_identified_counts() -> None:
    # Req 17.8 keeps the COUNTS: an operator must still be able to see how much was served,
    # which is not personal data once the identity is gone
    service, _profiles, audit = _service()
    identity = VerifiedIdentity(user_id="user-123")
    audit.append(AuditIdentifiers(user_id="user-123", served_at=_NOW, route="/advice"))
    before = audit.de_identified_count()
    service.delete(identity)
    assert audit.de_identified_count() == before


def test_deletion_leaves_another_users_audit_records_alone() -> None:
    service, _profiles, audit = _service()
    audit.append(AuditIdentifiers(user_id="user-a", served_at=_NOW, route="/advice"))
    audit.append(AuditIdentifiers(user_id="user-b", served_at=_NOW, route="/advice"))
    service.delete(VerifiedIdentity(user_id="user-a"))
    assert len(audit.records_for("user-b")) == 1


def test_deleting_an_absent_profile_is_not_an_error() -> None:
    # idempotent erasure: a user may ask twice, and the second must still confirm
    service, _profiles, _audit = _service()
    receipt = service.delete(VerifiedIdentity(user_id="nobody"))
    assert receipt.profile_deleted is False
    assert receipt.audit_records_de_identified == 0


def test_deletion_is_idempotent() -> None:
    service, profiles, _audit = _service()
    identity = VerifiedIdentity(user_id="user-123")
    service.write(identity, _fields())
    service.delete(identity)
    service.delete(identity)
    assert profiles.get("user-123") is None


def test_a_deleted_user_resolves_to_the_default_again() -> None:
    service, _profiles, _audit = _service()
    identity = VerifiedIdentity(user_id="user-123")
    service.write(identity, _fields())
    service.delete(identity)
    assert service.resolve(identity).used_default is True


# --- Req 25.8 the audit record holds no health data ---------------------

def test_an_audit_record_carries_no_health_adjacent_field() -> None:
    # Req 25.8 forbids Condition, Sensitivity_Level and Personal_Threshold, which is also
    # what keeps Req 17.8's erasure simple: there is only an identity to remove
    assert set(AuditIdentifiers.__dataclass_fields__) == {
        "user_id",
        "served_at",
        "route",
    }


# --- Req 17.9 nothing leaks ---------------------------------------------

def test_writing_a_profile_logs_no_profile_field(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("debug")
    service, _profiles, _audit = _service()
    service.write(VerifiedIdentity(user_id="user-123"), _fields())
    output = capsys.readouterr().out
    for leaked in ("asthma", "elevated", "51.507", "35.0"):
        assert leaked not in output


def test_deletion_logs_only_the_pseudonymous_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    service, _profiles, _audit = _service()
    identity = VerifiedIdentity(user_id="user-123")
    service.write(identity, _fields())
    service.delete(identity)
    events = [
        e for e in _events(capsys.readouterr().out) if e.get("event") == "profile_deleted"
    ]
    assert len(events) == 1
    assert "asthma" not in json.dumps(events[0])


def test_a_rejected_write_logs_no_value(capsys: pytest.CaptureFixture[str]) -> None:
    from pydantic import ValidationError

    configure_logging("info")
    service, _profiles, _audit = _service()
    with pytest.raises(ValidationError):
        service.write(
            VerifiedIdentity(user_id="user-123"), _fields(medication="salbutamol")
        )
    assert "salbutamol" not in capsys.readouterr().out


# --- shape --------------------------------------------------------------

def test_the_resolved_profile_shape_is_minimal() -> None:
    assert set(ResolvedProfile.__dataclass_fields__) == {"profile", "used_default"}
