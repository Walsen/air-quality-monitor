"""Tests for the chatbot `POST /login` endpoint.

`/login` sits behind the same shared X-Access-Key gate as `/chat`, calls an
injected Cognito auth client (a fake here, so no AWS), and returns the JWT for the
browser to hold. It must never store the token server-side, never log the token
or password, and must not reveal which field was wrong on failure (Requirement
1.4). Driven through the same in-process ASGI caller as test_app.py.
"""

from __future__ import annotations

import logging
from typing import Any

from aqm_chatbot.app import build_app
from aqm_chatbot.auth import AuthError
from aqm_chatbot.client import AdvisorError
from tests.unit.asgi import Response, call

_KEY = "s3cr3t-demo-key"
_TOKEN = "id.jwt.token.value"
_PASSWORD = "sup3r-secret-pw"


class _FakeAdvisor:
    """A no-op advisor; /login does not touch it, but build_app requires one."""

    def advise(
        self, utterance: str, *, session_id: str, credential: str | None
    ) -> dict[str, Any]:
        raise AdvisorError("advisor_unreachable")


class _FakeCognito:
    """Records login attempts and returns a token, or raises the auth error."""

    def __init__(self, token: str | None = None, error: str | None = None) -> None:
        self._token = token
        self._error = error
        self.calls: list[tuple[str, str]] = []

    def authenticate(self, username: str, password: str) -> str:
        self.calls.append((username, password))
        if self._error is not None:
            raise AuthError(self._error)
        assert self._token is not None
        return self._token


def _app(cognito: _FakeCognito | None = None) -> Any:
    return build_app(
        advisor=_FakeAdvisor(),
        access_key=_KEY,
        cognito=cognito or _FakeCognito(token=_TOKEN),
        cognito_client_id="app-client-123",
    )


def _login(app: Any, body: dict[str, Any], key: str | None = None) -> Response:
    headers = {"X-Access-Key": key} if key is not None else {}
    return call(app, "POST", "/login", json_body=body, headers=headers)


# --- the access gate ----------------------------------------------------


def test_login_without_the_shared_key_is_rejected() -> None:
    assert _login(_app(), {"username": "alice", "password": _PASSWORD}).status == 401


def test_login_without_the_key_never_reaches_cognito() -> None:
    cognito = _FakeCognito(token=_TOKEN)
    _login(_app(cognito), {"username": "alice", "password": _PASSWORD})
    assert cognito.calls == []


# --- a successful sign-in -----------------------------------------------


def test_valid_credentials_return_a_token() -> None:
    cognito = _FakeCognito(token=_TOKEN)
    res = _login(_app(cognito), {"username": "alice", "password": _PASSWORD}, key=_KEY)
    assert res.status == 200
    assert res.json()["token"] == _TOKEN
    assert cognito.calls == [("alice", _PASSWORD)]


def test_the_token_is_not_stored_on_the_app() -> None:
    # The chatbot hands the token to the browser and keeps nothing server-side.
    cognito = _FakeCognito(token=_TOKEN)
    app = _app(cognito)
    _login(app, {"username": "alice", "password": _PASSWORD}, key=_KEY)
    # No attribute anywhere on the app object graph should hold the token.
    assert _TOKEN not in repr(app.__dict__)
    assert _TOKEN not in repr(getattr(app, "state", object()).__dict__)


# --- failures -----------------------------------------------------------


def test_invalid_credentials_return_401() -> None:
    cognito = _FakeCognito(error="invalid_credentials")
    res = _login(_app(cognito), {"username": "alice", "password": "wrong"}, key=_KEY)
    assert res.status == 401


def test_invalid_credentials_body_does_not_reveal_which_field_was_wrong() -> None:
    cognito = _FakeCognito(error="invalid_credentials")
    res = _login(_app(cognito), {"username": "alice", "password": "wrong"}, key=_KEY)
    body = res.text.lower()
    # Must not disclose that it was specifically the username or the password,
    # and must not echo the supplied values back.
    assert "username" not in body
    assert "password" not in body
    assert "alice" not in body
    assert "wrong" not in body


def test_a_blank_username_is_rejected_by_validation() -> None:
    assert _login(_app(), {"username": "", "password": _PASSWORD}, key=_KEY).status == 422


def test_a_blank_password_is_rejected_by_validation() -> None:
    assert _login(_app(), {"username": "alice", "password": ""}, key=_KEY).status == 422


def test_a_missing_field_is_rejected_by_validation() -> None:
    assert _login(_app(), {"username": "alice"}, key=_KEY).status == 422


# --- no secret leaks to logs --------------------------------------------


def test_login_never_logs_the_password_or_token(caplog: Any) -> None:
    cognito = _FakeCognito(token=_TOKEN)
    with caplog.at_level(logging.DEBUG):
        _login(_app(cognito), {"username": "alice", "password": _PASSWORD}, key=_KEY)
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert _PASSWORD not in combined
    assert _TOKEN not in combined


def test_a_failed_login_never_logs_the_password(caplog: Any) -> None:
    cognito = _FakeCognito(error="invalid_credentials")
    with caplog.at_level(logging.DEBUG):
        _login(_app(cognito), {"username": "alice", "password": _PASSWORD}, key=_KEY)
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert _PASSWORD not in combined
