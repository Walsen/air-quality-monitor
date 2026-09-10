"""Reading, writing, deleting and defaulting a User_Profile.

Three rules shape this module, and each rules out an implementation that would otherwise look
fine:

**The verified identity is the only key** (Requirement 17.1). A write whose body names a
different ``user_id`` is REFUSED rather than silently redirected to the caller's own key: an
authenticated caller must not be able to edit anyone else's profile, and quietly rewriting the
field would hide an attempt to.

**An absent profile serves the configured default and SAYS SO** (Requirement 17.12) rather than
failing the request — and the default is NOT written to the store, because serving a default
must not create a profile the user never consented to.

**Deletion confirms what it did** (Requirement 17.8). It removes the profile and de-identifies
every Audit_Record naming the user, while RETAINING the de-identified counts, which stop being
personal data once the identity is gone. It is idempotent, because a user may reasonably ask
twice and the second request must still confirm rather than error.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass

from aqm_ingestion.domain.profile import (
    DEFAULT_PROFILE_CONDITION,
    DEFAULT_PROFILE_SENSITIVITY,
    ConsentRecord,
    ProfileLimits,
    UserProfile,
    build_profile,
)
from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.ports.protocols import AuditStore, ProfileStore, VerifiedIdentity

_logger = get_logger("serving.profiles")

_DEFAULT_CONSENT_VERSION = "default-profile"
"""Marks a served default as not resting on a user's consent record.

A default profile carries no user-supplied data, so there is no consent to record — but the
model requires a Consent_Record, and inventing a real version would misrepresent the user as
having agreed to something. This sentinel says plainly that no consent was given because none
was needed.
"""


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    """The profile to personalize with, and whether it is the default."""

    profile: UserProfile
    used_default: bool


@dataclass(frozen=True, slots=True)
class DeletionReceipt:
    """What a deletion did, so the response can confirm it (Requirement 17.8)."""

    profile_deleted: bool
    audit_records_de_identified: int


class ProfileService:
    """Profile access for the serving path."""

    def __init__(
        self,
        profiles: ProfileStore,
        audit: AuditStore,
        limits: ProfileLimits | None = None,
    ) -> None:
        """Hold the stores and the configured profile limits."""
        self._profiles = profiles
        self._audit = audit
        self._limits = limits or ProfileLimits()

    def resolve(self, identity: VerifiedIdentity) -> ResolvedProfile:
        """Return the user's profile, or the configured default (Requirement 17.12)."""
        stored = self._profiles.get(identity.user_id)
        if stored is not None:
            return ResolvedProfile(profile=stored, used_default=False)
        return ResolvedProfile(
            profile=self._default_for(identity.user_id), used_default=True
        )

    def write(
        self, identity: VerifiedIdentity, fields: Mapping[str, object]
    ) -> UserProfile:
        """Validate and store a profile under the VERIFIED identity.

        Raises:
            ValueError: if the body names a different user. See the module docstring.
            pydantic.ValidationError: for any allowlist or consent violation, already
                sanitised of submitted values by ``build_profile``.
        """
        claimed = fields.get("user_id")
        if claimed is not None and claimed != identity.user_id:
            # Named without echoing either value beyond the verified identity, which
            # Requirement 17.9 permits in diagnostics.
            raise ValueError(
                "profile body names a different identity than the verified one; a write is "
                "keyed by the verified identity only"
            )

        profile = build_profile(
            {**fields, "user_id": identity.user_id}, limits=self._limits
        )
        stored = self._profiles.put(profile)
        # Only the pseudonymous identity (Requirement 17.9). The profile itself refuses to
        # render its contents, so even passing it here would be safe — but not passing it is
        # better still.
        _logger.info("profile_written", user_id=identity.user_id)
        return stored

    def delete(self, identity: VerifiedIdentity) -> DeletionReceipt:
        """Erase the profile and de-identify the audit trail (Requirement 17.8)."""
        existed = self._profiles.get(identity.user_id) is not None
        if existed:
            self._profiles.delete(identity.user_id)
        de_identified = self._audit.forget_user(identity.user_id)

        _logger.info(
            "profile_deleted",
            user_id=identity.user_id,
            profile_deleted=existed,
            audit_records_de_identified=de_identified,
        )
        return DeletionReceipt(
            profile_deleted=existed, audit_records_de_identified=de_identified
        )

    def _default_for(self, user_id: str) -> UserProfile:
        """Build Requirement 17.12's default profile for an identity.

        Constructed rather than stored, so it cannot drift from the declared defaults and
        cannot be mistaken for a profile the user created.
        """
        instant = self._epoch()
        return UserProfile(
            user_id=user_id,
            condition=DEFAULT_PROFILE_CONDITION,
            sensitivity_level=DEFAULT_PROFILE_SENSITIVITY,
            personal_thresholds={},
            locations=(),
            consent=ConsentRecord(
                version=_DEFAULT_CONSENT_VERSION, given_at=instant
            ),
            created_at=instant,
            updated_at=instant,
        )

    @staticmethod
    def _epoch() -> dt.datetime:
        """A fixed instant for a default profile.

        Deliberately NOT the current time: a default profile was never created or updated, and
        stamping it with "now" would make an absent profile look like a freshly written one in
        any diagnostic that compares instants.
        """
        return dt.datetime(1970, 1, 1, tzinfo=dt.UTC)

def query_positions(resolved: ResolvedProfile) -> tuple[tuple[float, float], ...]:
    """The positions a geographic query may be built from, in declared order.

    Takes ONE resolved profile and nothing else, which is what makes Requirement 17.10's
    selection half structural: there is no second profile to consult, so a query cannot be built
    from another user's locations even by mistake. Geographic selection itself is task 21; this
    is only the boundary it must draw its inputs across.

    Order follows the profile's own location order rather than being sorted here, because the
    order reaches a query and §2 requires it be defined rather than incidental.
    """
    return tuple(
        (location.latitude, location.longitude)
        for location in resolved.profile.locations
    )


__all__ = ["DeletionReceipt", "ProfileService", "ResolvedProfile", "query_positions"]
