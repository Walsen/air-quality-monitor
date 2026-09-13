"""Tests for the chatbot app: the access gate, the /chat proxy, and response shaping.

Every test uses a FAKE AdvisorClient, so the suite needs no AWS and no network —
the same offline guarantee the rest of the monorepo holds. The app is driven
through a tiny in-process ASGI caller (tests/unit/asgi.py) rather than starlette's
TestClient, whose httpx/anyio deprecation warnings would trip filterwarnings=error.
The gate is the security boundary, so it gets the most attention.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from aqm_chatbot.app import build_app
from aqm_chatbot.client import AdvisorError
from tests.unit.asgi import Response, call

_KEY = "s3cr3t-demo-key"

_GOOD_RESPONSE: dict[str, Any] = {
    "guidance": "Overall AQI is 68, Moderate, driven by PM2.5.",
    "escalation": None,
    "basis": {"records": []},
    "envelope": {
        "emergency_guidance": "If you are struggling to breathe, call your local emergency number.",
        "advisory_scope": None,
        "disclaimer": "This is not medical advice.",
    },
    "degraded": False,
    "answered_at": "2026-07-01T12:00:00Z",
}


class _FakeAdvisor:
    """Records the call and returns a scripted response (or raises)."""

    def __init__(self, response: dict[str, Any] | None = None, error: str | None = None) -> None:
        self._response = response if response is not None else _GOOD_RESPONSE
        self._error = error
        # Records (utterance, session_id, credential) so tests can assert exactly
        # what the credential channel forwarded (Property 2).
        self.calls: list[tuple[str, str, str | None]] = []

    def advise(
        self, utterance: str, *, session_id: str, credential: str | None
    ) -> dict[str, Any]:
        self.calls.append((utterance, session_id, credential))
        if self._error is not None:
            raise AdvisorError(self._error)
        return self._response


def _app(advisor: _FakeAdvisor | None = None) -> Any:
    return build_app(advisor=advisor or _FakeAdvisor(), access_key=_KEY)


# A valid signed-in caller sends both the shared access key and a bearer JWT.
_JWT = "id.jwt.token.value"
_BEARER = f"Bearer {_JWT}"


def _post(
    app: Any,
    body: dict[str, Any],
    key: str | None = None,
    authorization: str | None = _BEARER,
) -> Response:
    headers: dict[str, str] = {}
    if key is not None:
        headers["X-Access-Key"] = key
    if authorization is not None:
        headers["Authorization"] = authorization
    return call(app, "POST", "/chat", json_body=body, headers=headers)


# --- the access gate ----------------------------------------------------


def test_build_refuses_a_blank_access_key() -> None:
    # An open proxy to the advisor is exactly what the gate exists to prevent.
    with pytest.raises(ValueError, match="access key"):
        build_app(advisor=_FakeAdvisor(), access_key="   ")


def test_chat_without_a_key_is_rejected() -> None:
    assert _post(_app(), {"utterance": "hi"}).status == 401


def test_chat_with_a_wrong_key_is_rejected() -> None:
    assert _post(_app(), {"utterance": "hi"}, key="nope").status == 401


def test_a_wrong_key_never_reaches_the_advisor() -> None:
    # The gate must short-circuit BEFORE any advisor call — otherwise an attacker
    # spends Bedrock tokens on every rejected request.
    advisor = _FakeAdvisor()
    _post(_app(advisor), {"utterance": "hi"}, key="nope")
    assert advisor.calls == []


# --- a successful turn --------------------------------------------------


def test_a_valid_turn_returns_the_shaped_guidance() -> None:
    advisor = _FakeAdvisor()
    res = _post(_app(advisor), {"utterance": "how is the air?"}, key=_KEY)
    assert res.status == 200
    body = res.json()
    assert body["guidance"] == _GOOD_RESPONSE["guidance"]
    assert body["degraded"] is False
    assert body["emergency_guidance"] == _GOOD_RESPONSE["envelope"]["emergency_guidance"]
    assert body["disclaimer"] == _GOOD_RESPONSE["envelope"]["disclaimer"]
    assert body["session_id"], "a session id is always returned"
    assert advisor.calls[0][0] == "how is the air?"


# --- the JWT pass-through (Requirements 2.1, 2.2, 2.5; Property 2) --------


def test_a_valid_turn_forwards_the_bearer_token_to_the_advisor() -> None:
    # /chat reads the browser's Authorization bearer, strips the framing, and
    # hands the raw token to the advisor client as credential.
    advisor = _FakeAdvisor()
    _post(_app(advisor), {"utterance": "hi"}, key=_KEY, authorization=_BEARER)
    assert advisor.calls[0][2] == _JWT


def test_the_forwarded_credential_is_byte_identical_to_the_browser_token() -> None:
    # Property 2: the credential is passed through unchanged — not decoded,
    # parsed, or reframed. Only the transport "Bearer " framing is stripped.
    token = "eyJhbGciOi.JIUzI1Ni.sig-With_Odd~Chars.and.dots=="
    advisor = _FakeAdvisor()
    _post(_app(advisor), {"utterance": "hi"}, key=_KEY, authorization=f"Bearer {token}")
    assert advisor.calls[0][2] == token


def test_a_lowercase_bearer_scheme_is_accepted() -> None:
    # RFC 6750 makes the scheme case-insensitive; the token must survive intact.
    advisor = _FakeAdvisor()
    _post(_app(advisor), {"utterance": "hi"}, key=_KEY, authorization=f"bearer {_JWT}")
    assert advisor.calls[0][2] == _JWT


def test_a_turn_without_a_jwt_is_refused_and_never_reaches_the_advisor() -> None:
    # A diary turn needs the user's identity; without a JWT the page is told to
    # sign in and the advisor is not called (no Bedrock spend, no anonymous turn).
    advisor = _FakeAdvisor()
    res = _post(_app(advisor), {"utterance": "hi"}, key=_KEY, authorization=None)
    assert res.status == 401
    assert advisor.calls == []


def test_an_empty_bearer_token_is_refused() -> None:
    advisor = _FakeAdvisor()
    res = _post(_app(advisor), {"utterance": "hi"}, key=_KEY, authorization="Bearer ")
    assert res.status == 401
    assert advisor.calls == []


def test_a_non_bearer_authorization_is_refused() -> None:
    advisor = _FakeAdvisor()
    res = _post(_app(advisor), {"utterance": "hi"}, key=_KEY, authorization="Token abc")
    assert res.status == 401
    assert advisor.calls == []


def test_the_jwt_is_never_echoed_in_the_response_body() -> None:
    advisor = _FakeAdvisor()
    res = _post(_app(advisor), {"utterance": "hi"}, key=_KEY, authorization=_BEARER)
    assert _JWT not in res.text


def test_the_jwt_is_never_logged(caplog: Any) -> None:
    advisor = _FakeAdvisor()
    with caplog.at_level(logging.DEBUG):
        _post(_app(advisor), {"utterance": "hi"}, key=_KEY, authorization=_BEARER)
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert _JWT not in combined


def test_a_supplied_session_id_is_forwarded_and_returned() -> None:
    advisor = _FakeAdvisor()
    sid = "web-" + "a" * 60
    res = _post(_app(advisor), {"utterance": "again", "session_id": sid}, key=_KEY)
    assert res.json()["session_id"] == sid
    assert advisor.calls[0][1] == sid


def test_a_degraded_response_is_flagged() -> None:
    degraded = {**_GOOD_RESPONSE, "degraded": True}
    res = _post(_app(_FakeAdvisor(degraded)), {"utterance": "x"}, key=_KEY)
    assert res.json()["degraded"] is True


def test_an_escalation_is_passed_through() -> None:
    esc = {**_GOOD_RESPONSE, "escalation": {"markers": ["severe_breathlessness"]}}
    res = _post(_app(_FakeAdvisor(esc)), {"utterance": "I cannot breathe"}, key=_KEY)
    assert res.json()["escalation"] == {"markers": ["severe_breathlessness"]}


# --- failures -----------------------------------------------------------


def test_an_advisor_error_becomes_a_502_naming_the_kind() -> None:
    res = _post(_app(_FakeAdvisor(error="advisor_unreachable")), {"utterance": "x"}, key=_KEY)
    assert res.status == 502
    assert res.json()["error"] == "advisor_unreachable"


def test_a_blank_utterance_is_rejected_by_validation() -> None:
    assert _post(_app(), {"utterance": ""}, key=_KEY).status == 422  # pydantic min_length


# --- page + health ------------------------------------------------------


def test_the_index_page_loads_and_has_no_embedded_key() -> None:
    res = call(_app(), "GET", "/")
    assert res.status == 200
    assert "Air Quality Advisor" in res.text
    assert _KEY not in res.text, "the shared key must never be embedded in the page"


def test_health_is_open_and_reveals_nothing() -> None:
    assert call(_app(), "GET", "/health").json() == {"status": "ok"}
