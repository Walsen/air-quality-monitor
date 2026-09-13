"""Tests for the AgentCore advisor client, with a fake boto3 client (no AWS)."""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest

from aqm_chatbot.client import AdvisorError, AgentCoreAdvisorClient, new_session_id


class _FakeEvents:
    """A minimal stand-in for a boto3 client's `meta.events` register/emit.

    `invoke_agent_runtime` carries no `Authorization` parameter, so the client
    injects the user credential with a botocore `before-send` handler. This fake
    records the registered handlers and lets a test emit `before-send` against a
    fake prepared request, exactly as botocore's endpoint does after signing.
    """

    def __init__(self) -> None:
        self.handlers: list[tuple[str, Any, str | None]] = []

    def register(
        self, event_name: str, handler: Any, unique_id: str | None = None
    ) -> None:
        self.handlers.append((event_name, handler, unique_id))

    def unregister(
        self, event_name: str, handler: Any, unique_id: str | None = None
    ) -> None:
        self.handlers = [
            entry
            for entry in self.handlers
            if not (entry[0] == event_name and entry[2] == unique_id)
        ]

    def emit(self, event_name: str, **kwargs: Any) -> list[Any]:
        results: list[Any] = []
        for name, handler, _uid in self.handlers:
            # botocore matches on a hierarchical prefix; a startswith check is a
            # faithful enough stand-in for these tests.
            if event_name.startswith(name) or name.startswith(event_name):
                results.append(handler(**kwargs))
        return results


class _FakeMeta:
    def __init__(self) -> None:
        self.events = _FakeEvents()
        self.service_model = _FakeServiceModel()


class _FakeServiceModel:
    service_id = "Bedrock AgentCore"


class _FakePreparedRequest:
    """Enough of a botocore AWSPreparedRequest for a before-send handler to mutate."""

    def __init__(self) -> None:
        # SigV4 signing would already have set Authorization to the AWS signature;
        # start with that so a test can prove the hook overwrites it.
        self.headers: dict[str, str] = {"Authorization": "AWS4-HMAC-SHA256 Credential=..."}


class _FakeBotoClient:
    """Captures the invoke_agent_runtime kwargs and returns a scripted body.

    Also exposes `meta.events` so the credential-injection `before-send` handler
    can be registered and exercised offline, and emits that event during the call
    against a fake prepared request so tests can inspect the outgoing headers.
    """

    def __init__(self, body: bytes | Exception) -> None:
        self._body = body
        self.kwargs: dict[str, Any] = {}
        self.meta = _FakeMeta()
        self.sent_headers: dict[str, str] = {}

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        # Reproduce botocore's post-signing hook point: build the prepared request
        # (which already carries the SigV4 Authorization) and emit before-send so
        # the injected handler can overwrite Authorization with the user's bearer.
        request = _FakePreparedRequest()
        self.meta.events.emit(
            "before-send.bedrock-agentcore.InvokeAgentRuntime", request=request
        )
        self.sent_headers = dict(request.headers)
        if isinstance(self._body, Exception):
            raise self._body
        return {"response": io.BytesIO(self._body)}


def _client(body: bytes | Exception) -> tuple[AgentCoreAdvisorClient, _FakeBotoClient]:
    fake = _FakeBotoClient(body)
    c = AgentCoreAdvisorClient(
        runtime_arn="arn:aws:bedrock-agentcore:us-east-1:1:runtime/x",
        region="us-east-1",
        client=fake,
    )
    return c, fake


def test_a_valid_body_is_parsed() -> None:
    c, _ = _client(json.dumps({"guidance": "ok", "degraded": False}).encode())
    out = c.advise("hello", session_id="w" * 40, credential=None)
    assert out["guidance"] == "ok"


def test_the_utterance_is_sent_as_the_payload() -> None:
    c, fake = _client(json.dumps({"guidance": "ok"}).encode())
    c.advise("how is the air?", session_id="w" * 40, credential=None)
    payload = json.loads(fake.kwargs["payload"])
    assert payload == {"utterance": "how is the air?"}


def test_a_short_session_id_is_replaced_before_the_call() -> None:
    # AgentCore rejects a too-short id, so the client substitutes a valid one.
    c, fake = _client(json.dumps({"guidance": "ok"}).encode())
    c.advise("x", session_id="too-short", credential=None)
    assert len(fake.kwargs["runtimeSessionId"]) >= 33


def test_a_transport_failure_maps_to_a_kind() -> None:
    c, _ = _client(RuntimeError("boom"))
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40, credential=None)
    assert caught.value.kind == "advisor_unreachable"


def test_a_non_json_body_maps_to_a_kind() -> None:
    c, _ = _client(b"not json")
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40, credential=None)
    assert caught.value.kind == "advisor_bad_response"


def test_a_non_object_body_is_refused() -> None:
    c, _ = _client(json.dumps([1, 2, 3]).encode())
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40, credential=None)
    assert caught.value.kind == "advisor_bad_response"


def test_a_generated_session_id_is_long_enough() -> None:
    assert len(new_session_id()) >= 33


# --- credential pass-through (Property 2) --------------------------------


def test_a_credential_is_forwarded_as_the_authorization_bearer_header() -> None:
    # The advisor's entrypoint reads context.request_headers["Authorization"] as
    # "Bearer <token>", so the client must place exactly that on the outgoing
    # request. invoke_agent_runtime models no Authorization parameter, so it goes
    # in via a before-send hook that overwrites the SigV4 Authorization.
    c, fake = _client(json.dumps({"guidance": "ok"}).encode())
    c.advise("x", session_id="w" * 40, credential="the-user-jwt")
    assert fake.sent_headers["Authorization"] == "Bearer the-user-jwt"


def test_the_forwarded_token_is_byte_identical_to_the_input() -> None:
    # Property 2: passed through unmodified — not decoded, parsed, or reframed.
    token = "eyJhbGciOi.JIUzI1Ni.sig-With_Odd~Chars.and.dots=="
    c, fake = _client(json.dumps({"guidance": "ok"}).encode())
    c.advise("x", session_id="w" * 40, credential=token)
    assert fake.sent_headers["Authorization"] == f"Bearer {token}"
    # The exact token survives with the Bearer framing stripped back off.
    assert fake.sent_headers["Authorization"][len("Bearer ") :] == token


def test_no_credential_leaves_the_authorization_header_untouched() -> None:
    # With no user credential the client adds no Authorization override, so the
    # SigV4 signature the signer set remains in place.
    c, fake = _client(json.dumps({"guidance": "ok"}).encode())
    c.advise("x", session_id="w" * 40, credential=None)
    assert fake.sent_headers["Authorization"].startswith("AWS4-HMAC-SHA256")


def test_the_credential_is_never_logged(caplog: Any) -> None:
    token = "super-secret-user-jwt"
    c, _ = _client(json.dumps({"guidance": "ok"}).encode())
    with caplog.at_level(logging.DEBUG):
        c.advise("x", session_id="w" * 40, credential=token)
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in combined
