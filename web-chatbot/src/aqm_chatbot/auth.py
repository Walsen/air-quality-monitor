"""The boundary to Cognito for interactive sign-in (USER_PASSWORD_AUTH).

`CognitoAuthClient` is both the port (a `Protocol` describing `authenticate`) and
the production adapter over boto3's `cognito-idp` client. Mirroring `client.py`,
keeping the boundary a protocol is what lets `/login` be tested with a fake and no
AWS — the same dependency-inversion the rest of the monorepo uses at every seam.

WHICH TOKEN THIS RETURNS — THE ID TOKEN, DELIBERATELY. The ingestion service's
`CognitoAuthenticator` (data-processing/src/aqm_ingestion/adapters/auth/cognito.py)
sets ``EXPECTED_TOKEN_USE = "id"`` and rejects any token whose ``token_use`` is not
``"id"``, then reads the ``sub`` claim as the pseudonymous ``user_id``. So the ID
token is the only one that authenticates downstream and carries the subject the
diary is keyed by; the access token would be rejected outright. This resolves
design Open Question 2.

WHY ONE GENERIC ERROR KIND. Cognito distinguishes `NotAuthorizedException` (wrong
password, or user disabled) from `UserNotFoundException` (no such user). Mapping
both — and anything else — to a single `AuthError("invalid_credentials")` is what
lets the endpoint answer without revealing whether the username or the password was
wrong (Requirement 1.4). The distinction is exactly what an attacker probing for
valid usernames wants, so it never leaves this module.

NOTHING HERE LOGS THE PASSWORD OR THE TOKEN. The credential and the minted token are
the secrets (§7); this module neither logs them nor puts them in the raised error.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

_AUTH_FLOW = "USER_PASSWORD_AUTH"

_logger = logging.getLogger("aqm_chatbot.auth")


class AuthError(Exception):
    """Sign-in failed. Carries a short, generic kind and nothing sensitive.

    The kind is deliberately coarse (`invalid_credentials`) so a caller cannot
    learn whether the username or the password was the problem, and so no token,
    password, or AWS error body is ever attached.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class CognitoAuthClient(Protocol):
    """Exchange a username and password for the JWT the browser will hold."""

    def authenticate(self, username: str, password: str) -> str:
        """Return the ID token for valid credentials, else raise `AuthError`."""
        ...


class AgentCoreCognitoClient:
    """Authenticates against Cognito via `cognito-idp` initiate_auth.

    The app client is a public browser-side client with no secret (per the design),
    so `USER_PASSWORD_AUTH` needs no `SECRET_HASH` and the call carries only the
    username, password, and app client id. The call needs no IAM for an
    unauthenticated public-client flow.
    """

    def __init__(self, *, client_id: str, region: str, client: Any | None = None) -> None:
        self._client_id = client_id
        if client is not None:
            self._client = client
        else:
            import boto3

            self._client = boto3.client("cognito-idp", region_name=region)

    def authenticate(self, username: str, password: str) -> str:
        """Initiate USER_PASSWORD_AUTH and return the ID token, mapping failures.

        Every failure — a rejected credential, a missing user, a challenge with no
        token, or an unexpected transport error — becomes one generic `AuthError`
        so the endpoint cannot disclose which field was wrong (Requirement 1.4).
        """
        try:
            response = self._client.initiate_auth(
                AuthFlow=_AUTH_FLOW,
                AuthParameters={"USERNAME": username, "PASSWORD": password},
                ClientId=self._client_id,
            )
        except (
            self._client.exceptions.NotAuthorizedException,
            self._client.exceptions.UserNotFoundException,
        ) as error:
            # Two distinct Cognito errors collapsed to ONE kind: the difference is
            # exactly what a username-enumeration probe is after, so it never leaves here.
            # The log names only the generic kind — never the username, password, or a
            # stack trace that might quote the credential.
            _logger.warning("cognito_sign_in_rejected", extra={"kind": "invalid_credentials"})
            raise AuthError("invalid_credentials") from error
        except Exception as error:
            # A true boundary: any other failure (transport, throttling, a malformed
            # response) is still mapped to the typed error so no stack trace reaches the
            # endpoint. Logged here per §5/§6, again without the credential.
            _logger.warning("cognito_sign_in_failed", extra={"kind": "invalid_credentials"})
            raise AuthError("invalid_credentials") from error

        result = response.get("AuthenticationResult") if isinstance(response, dict) else None
        if not isinstance(result, dict):
            # A challenge (e.g. NEW_PASSWORD_REQUIRED) carries no AuthenticationResult;
            # the POC treats an incomplete sign-in as a failure to authenticate.
            raise AuthError("invalid_credentials")
        token = result.get("IdToken")
        if not isinstance(token, str) or not token:
            raise AuthError("invalid_credentials")
        return token


__all__ = ["AgentCoreCognitoClient", "AuthError", "CognitoAuthClient"]
