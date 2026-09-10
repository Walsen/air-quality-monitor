"""The Cognito JWT authenticator (Requirements 18.1, 18.2, 18.3, 18.7, 18.8).

THE CLAIM SET IS VERIFIED HERE AND DISCARDED HERE, and that resolves what looks like a
contradiction
between two clauses. Requirement 18.1 says the gate establishes "the identity AND the claim
set",
which reads as though both should travel onward; Requirement 18.7 forbids any claim beyond the
identity reaching a response, a log entry, or an Audit_Record. The only shape satisfying both is
to
check every claim where it arrives and hand on the identity alone — which is why
``VerifiedIdentity``
has exactly one field, and why that is a feature rather than an omission. Whose SUBJECT each
clause
names settles it, the reading habit this service has now applied five times.

THE SIGNING KEY IS RESOLVED THROUGH AN INJECTED CALLABLE, not fetched here. A real deployment
reads
Cognito's JWKS endpoint over the network; a test supplies a literal. Putting that behind a
parameter
keeps this module free of an HTTP client and lets the whole verification path — signature,
audience,
issuer, expiry, token use — run in the offline suite, which is where Requirement 18.3's three
named
rejection conditions actually get exercised.

NOTHING HERE LOGS THE TOKEN. A bearer token IS the credential (§7), so the rejection log
names the
CATEGORY only, and ``AuthRejectedError`` was deliberately built at task 3.2 to carry nothing
else.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, cast

import jwt

from aqm_ingestion.observability.logging import get_logger, log_handled_error
from aqm_ingestion.ports.protocols import (
    AuthRejectedError,
    RejectionCategory,
    VerifiedIdentity,
)

_logger = get_logger("adapters.auth")

DEFAULT_ALGORITHMS: tuple[str, ...] = ("RS256",)
"""Cognito signs with RS256.

An explicit allow-list, never the token's own ``alg`` header: accepting that is the classic JWT
confusion attack, where an attacker sets ``alg`` to ``none`` or to a symmetric algorithm and
signs
with the public key. PyJWT requires the list, and this constant is why it is never derived.
"""

EXPECTED_TOKEN_USE = "id"
"""Requirement 18.1 authenticates a USER, so an access token is the wrong kind of token.

Cognito issues both from the same pool with the same signature and issuer, and only
``token_use``
tells them apart — so without this check an access token would authenticate a request that
Requirement 18.2 says must present an identity token.
"""


class CognitoAuthenticator:
    """Verifies a Cognito JWT and yields the pseudonymous identity alone."""

    def __init__(
        self,
        user_pool_id: str,
        client_id: str,
        region: str,
        key_resolver: Callable[[str], object],
        algorithms: Sequence[str] = DEFAULT_ALGORITHMS,
    ) -> None:
        """Configure the verifier.

        Args:
            user_pool_id: the pool, which fixes the expected issuer.
            client_id: the expected audience (Requirement 18.3's wrong-audience condition).
            region: part of the issuer URL Cognito publishes.
            key_resolver: returns the signing key for a token. Injected so this module needs no
                HTTP client and the whole verification path runs offline.
            algorithms: the accepted signature algorithms, never read from the token.
        """
        self._client_id = client_id
        self._issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        self._key_resolver = key_resolver
        self._algorithms = list(algorithms)

    def verify(self, credential: str) -> VerifiedIdentity:
        """Verify a token and return the identity.

        Raises:
            AuthRejectedError: carrying a CATEGORY and nothing else. Every branch below maps to
            one
                of Requirement 18.3's named conditions, and none of them echoes the token.
        """
        try:
            key = self._key_resolver(credential)
        except Exception as lookup_failure:
            # A JWKS lookup failure is not the caller's fault, but it cannot authenticate
            # either. Logged through the CENTRAL handled-error helper rather than
            # `logger.exception`, so the formatter's redaction still applies to the stack
            # text (§6) — the helper passes the stack as a string for exactly that reason.
            log_handled_error(
                _logger,
                "auth_rejected",
                lookup_failure,
                category=RejectionCategory.INVALID_SIGNATURE,
            )
            raise AuthRejectedError(RejectionCategory.INVALID_SIGNATURE) from None

        try:
            claims = jwt.decode(
                credential,
                # cast, not `object`: PyJWT accepts a key union this module deliberately
                # does not name in its own signature, because the resolver is injected
                # precisely so no key type, and no HTTP client to fetch one, leaks here.

                key=cast("Any", key),
                algorithms=self._algorithms,
                audience=self._client_id,
                issuer=self._issuer,
                options={"require": ["exp", "sub", "aud", "iss"]},
            )
        except jwt.ExpiredSignatureError:
            raise self._reject(RejectionCategory.EXPIRED) from None
        except jwt.InvalidAudienceError:
            raise self._reject(RejectionCategory.WRONG_AUDIENCE) from None
        except jwt.InvalidIssuerError:
            raise self._reject(RejectionCategory.INVALID_SIGNATURE) from None
        except jwt.InvalidTokenError:
            # Covers a malformed token, a bad signature, and a missing required claim. One
            # category rather than three, because Requirement 18.7 permits no detail to travel
            # and a finer split would only be visible to an attacker probing the difference.
            raise self._reject(RejectionCategory.INVALID_SIGNATURE) from None

        if claims.get("token_use") != EXPECTED_TOKEN_USE:
            raise self._reject(RejectionCategory.INVALID_SIGNATURE) from None

        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise self._reject(RejectionCategory.INVALID_SIGNATURE) from None

        # ONLY the subject crosses this boundary. Every other claim goes out of scope here,
        # which
        # is Requirement 18.7 enforced by construction rather than by remembering.
        return VerifiedIdentity(user_id=subject)

    def _reject(self, category: RejectionCategory) -> AuthRejectedError:
        """Log the rejection by category and build the error, never naming the token."""
        _logger.warning("auth_rejected", category=category)
        return AuthRejectedError(category)


__all__ = ["DEFAULT_ALGORITHMS", "EXPECTED_TOKEN_USE", "CognitoAuthenticator"]
