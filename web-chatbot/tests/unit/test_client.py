"""Tests for the AgentCore advisor client, with a fake httpx-like client (no AWS).

The advisor runtime is fronted by a custom JWT inbound authorizer, so the client
makes a plain HTTPS POST carrying the user's bearer as the request's own
`Authorization` header — no SigV4, no AWS SDK. These tests drive that POST
through a fake httpx-like client so the suite needs no AWS and no network.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

import httpx
import pytest

from aqm_chatbot.client import AdvisorError, AgentCoreAdvisorClient, new_session_id

_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:1:runtime/x"


class _FakeResponse:
    """A scripted httpx-like response: a status plus a text body."""

    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            # Mirror httpx: a non-2xx raises an httpx.HTTPError subclass.
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("POST", "https://example"),
                response=httpx.Response(self.status_code),
            )


class _FakeHttpClient:
    """Records the outgoing POST and returns a scripted response or raises.

    `outcome` is either a `_FakeResponse` to return or an `Exception` to raise
    (a transport failure), matching how httpx would surface each case.
    """

    def __init__(self, outcome: _FakeResponse | Exception) -> None:
        self._outcome = outcome
        self.url: str | None = None
        self.headers: dict[str, str] = {}
        self.params: dict[str, Any] = {}
        self.content: bytes | None = None

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any],
        content: bytes,
        **_kwargs: Any,
    ) -> _FakeResponse:
        self.url = url
        self.headers = dict(headers)
        self.params = dict(params)
        self.content = content
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _client(
    outcome: _FakeResponse | Exception,
) -> tuple[AgentCoreAdvisorClient, _FakeHttpClient]:
    fake = _FakeHttpClient(outcome)
    c = AgentCoreAdvisorClient(
        runtime_arn=_RUNTIME_ARN,
        region="us-east-1",
        http_client=fake,
    )
    return c, fake


def _ok(body: dict[str, Any] | list[Any] | str) -> _FakeResponse:
    text = body if isinstance(body, str) else json.dumps(body)
    return _FakeResponse(200, text)


def test_a_valid_body_is_parsed() -> None:
    c, _ = _client(_ok({"guidance": "ok", "degraded": False}))
    out = c.advise("hello", session_id="w" * 40, credential="jwt")
    assert out["guidance"] == "ok"


def test_the_utterance_is_sent_as_the_json_body() -> None:
    c, fake = _client(_ok({"guidance": "ok"}))
    c.advise("how is the air?", session_id="w" * 40, credential="jwt")
    assert fake.content is not None
    assert json.loads(fake.content) == {"utterance": "how is the air?"}


def test_a_short_session_id_is_replaced_and_travels_in_the_header() -> None:
    # AgentCore rejects a too-short id, so the client substitutes a valid one and
    # sends it in the data-plane session-id header.
    c, fake = _client(_ok({"guidance": "ok"}))
    c.advise("x", session_id="too-short", credential="jwt")
    sent = fake.headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"]
    assert len(sent) >= 33


def test_a_valid_session_id_travels_in_the_header_unchanged() -> None:
    c, fake = _client(_ok({"guidance": "ok"}))
    session_id = "w" * 40
    c.advise("x", session_id=session_id, credential="jwt")
    assert fake.headers["X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"] == session_id


def test_a_transport_failure_maps_to_a_kind() -> None:
    c, _ = _client(httpx.ConnectError("boom"))
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40, credential="jwt")
    assert caught.value.kind == "advisor_unreachable"


def test_a_non_2xx_status_maps_to_unreachable() -> None:
    c, _ = _client(_FakeResponse(500, "server error"))
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40, credential="jwt")
    assert caught.value.kind == "advisor_unreachable"


def test_a_non_json_body_maps_to_a_kind() -> None:
    c, _ = _client(_ok("not json"))
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40, credential="jwt")
    assert caught.value.kind == "advisor_bad_response"


def test_a_non_object_body_is_refused() -> None:
    c, _ = _client(_ok([1, 2, 3]))
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40, credential="jwt")
    assert caught.value.kind == "advisor_bad_response"


def test_a_generated_session_id_is_long_enough() -> None:
    assert len(new_session_id()) >= 33


def test_the_url_targets_the_data_plane_host_and_encodes_the_arn() -> None:
    c, fake = _client(_ok({"guidance": "ok"}))
    c.advise("x", session_id="w" * 40, credential="jwt")
    assert fake.url is not None
    assert fake.url.startswith("https://bedrock-agentcore.us-east-1.amazonaws.com/")
    # The runtime ARN appears url-encoded (colons/slashes escaped) in the path.
    assert urllib.parse.quote(_RUNTIME_ARN, safe="") in fake.url
    assert _RUNTIME_ARN not in fake.url  # the raw, unencoded arn must not appear
    assert fake.params.get("qualifier") == "DEFAULT"


# --- credential handling (Property 2) ------------------------------------


def test_a_credential_is_forwarded_as_the_authorization_bearer_header() -> None:
    # The authorizer and the runtime entrypoint read Authorization as
    # "Bearer <token>", so the client must place exactly that on the request.
    c, fake = _client(_ok({"guidance": "ok"}))
    c.advise("x", session_id="w" * 40, credential="the-user-jwt")
    assert fake.headers["Authorization"] == "Bearer the-user-jwt"


def test_the_forwarded_token_is_byte_identical_to_the_input() -> None:
    # Property 2: passed through unmodified — not decoded, parsed, or reframed.
    token = "eyJhbGciOi.JIUzI1Ni.sig-With_Odd~Chars.and.dots=="
    c, fake = _client(_ok({"guidance": "ok"}))
    c.advise("x", session_id="w" * 40, credential=token)
    assert fake.headers["Authorization"] == f"Bearer {token}"
    # The exact token survives with the Bearer framing stripped back off.
    assert fake.headers["Authorization"][len("Bearer ") :] == token


def test_no_credential_is_refused_as_unauthenticated() -> None:
    # The runtime is JWT-authorized, so a turn with no bearer cannot authenticate
    # the transport at all — the client refuses it up front rather than sending an
    # unauthenticatable request. (There is no SigV4 fallback anymore.)
    c, fake = _client(_ok({"guidance": "ok"}))
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40, credential=None)
    assert caught.value.kind == "advisor_unauthenticated"
    # Nothing was sent — the refusal is before any HTTP call.
    assert fake.url is None


def test_the_credential_is_never_logged(caplog: Any) -> None:
    token = "super-secret-user-jwt"
    c, _ = _client(_ok({"guidance": "ok"}))
    with caplog.at_level(logging.DEBUG):
        c.advise("x", session_id="w" * 40, credential=token)
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in combined


def test_the_credential_is_never_logged_on_failure(caplog: Any) -> None:
    # Even when the request fails and the client logs the failure event, the
    # token must not appear in any log record (Property 2 + §6).
    token = "super-secret-user-jwt"
    c, _ = _client(httpx.ConnectError("boom"))
    with caplog.at_level(logging.DEBUG), pytest.raises(AdvisorError):
        c.advise("x", session_id="w" * 40, credential=token)
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in combined
