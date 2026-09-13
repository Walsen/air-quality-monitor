"""Tests for the Cognito auth client, with a fake boto3 cognito-idp client (no AWS).

Mirrors test_client.py: the boto3 client is a protocol boundary, so the whole
InitiateAuth path runs offline against a fake that records the call and returns a
scripted AuthenticationResult (or raises a Cognito-shaped error).
"""

from __future__ import annotations

from typing import Any

import pytest

from aqm_chatbot.auth import AgentCoreCognitoClient, AuthError


class _FakeCognitoBoto:
    """Captures the initiate_auth kwargs and returns a scripted result (or raises)."""

    def __init__(self, result: dict[str, Any] | Exception) -> None:
        self._result = result
        self.kwargs: dict[str, Any] = {}
        # exceptions is the botocore.exceptions module stand-in for the fake
        self.exceptions = _FakeExceptions

    def initiate_auth(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


# These deliberately mirror the exact names botocore gives Cognito's error classes
# on `client.exceptions`, so the adapter catches them by the same attribute names it
# would in production. The `Error` suffix N818 wants would break that mirroring.
class _NotAuthorizedException(Exception):  # noqa: N818 - mirrors the botocore class name
    pass


class _UserNotFoundException(Exception):  # noqa: N818 - mirrors the botocore class name
    pass


class _FakeExceptions:
    """Stands in for `client.exceptions`, exposing the two Cognito error classes."""

    NotAuthorizedException = _NotAuthorizedException
    UserNotFoundException = _UserNotFoundException


def _auth(result: dict[str, Any] | Exception) -> tuple[AgentCoreCognitoClient, _FakeCognitoBoto]:
    fake = _FakeCognitoBoto(result)
    client = AgentCoreCognitoClient(client_id="app-client-123", region="us-east-1", client=fake)
    return client, fake


def _ok_result() -> dict[str, Any]:
    return {
        "AuthenticationResult": {
            "IdToken": "id.jwt.token",
            "AccessToken": "access.jwt.token",
            "ExpiresIn": 3600,
            "TokenType": "Bearer",
        }
    }


def test_valid_credentials_return_the_id_token() -> None:
    # The ingestion CognitoAuthenticator sets EXPECTED_TOKEN_USE = "id" and reads the
    # `sub` claim as user_id, so the ID token is the one that authenticates downstream.
    client, _ = _auth(_ok_result())
    assert client.authenticate("alice", "pw") == "id.jwt.token"


def test_the_call_uses_user_password_auth_flow_and_parameters() -> None:
    client, fake = _auth(_ok_result())
    client.authenticate("alice", "pw")
    assert fake.kwargs["AuthFlow"] == "USER_PASSWORD_AUTH"
    assert fake.kwargs["AuthParameters"] == {"USERNAME": "alice", "PASSWORD": "pw"}
    assert fake.kwargs["ClientId"] == "app-client-123"


def test_not_authorized_maps_to_a_generic_auth_error() -> None:
    client, _ = _auth(_NotAuthorizedException("Incorrect username or password."))
    with pytest.raises(AuthError) as caught:
        client.authenticate("alice", "wrong")
    assert caught.value.kind == "invalid_credentials"


def test_user_not_found_maps_to_the_same_generic_kind() -> None:
    # A distinct Cognito error, but mapped to ONE kind so the endpoint cannot reveal
    # which field was wrong (Requirement 1.4).
    client, _ = _auth(_UserNotFoundException("User does not exist."))
    with pytest.raises(AuthError) as caught:
        client.authenticate("ghost", "pw")
    assert caught.value.kind == "invalid_credentials"


def test_a_missing_authentication_result_is_an_auth_error() -> None:
    # A challenge response (e.g. NEW_PASSWORD_REQUIRED) carries no AuthenticationResult;
    # the POC treats that as a failure to authenticate rather than crashing.
    client, _ = _auth({"ChallengeName": "NEW_PASSWORD_REQUIRED", "Session": "s"})
    with pytest.raises(AuthError) as caught:
        client.authenticate("alice", "pw")
    assert caught.value.kind == "invalid_credentials"


def test_a_missing_id_token_is_an_auth_error() -> None:
    client, _ = _auth({"AuthenticationResult": {"AccessToken": "only.access"}})
    with pytest.raises(AuthError) as caught:
        client.authenticate("alice", "pw")
    assert caught.value.kind == "invalid_credentials"


def test_an_unexpected_transport_error_is_wrapped() -> None:
    # Any other failure is still mapped to the typed error, never a raw stack trace.
    client, _ = _auth(RuntimeError("network boom"))
    with pytest.raises(AuthError):
        client.authenticate("alice", "pw")
