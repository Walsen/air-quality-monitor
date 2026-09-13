"""Tests for the AgentCore advisor client, with a fake boto3 client (no AWS)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest

from aqm_chatbot.client import AdvisorError, AgentCoreAdvisorClient, new_session_id


class _FakeBotoClient:
    """Captures the invoke_agent_runtime kwargs and returns a scripted body."""

    def __init__(self, body: bytes | Exception) -> None:
        self._body = body
        self.kwargs: dict[str, Any] = {}

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
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
    out = c.advise("hello", session_id="w" * 40)
    assert out["guidance"] == "ok"


def test_the_utterance_is_sent_as_the_payload() -> None:
    c, fake = _client(json.dumps({"guidance": "ok"}).encode())
    c.advise("how is the air?", session_id="w" * 40)
    payload = json.loads(fake.kwargs["payload"])
    assert payload == {"utterance": "how is the air?"}


def test_a_short_session_id_is_replaced_before_the_call() -> None:
    # AgentCore rejects a too-short id, so the client substitutes a valid one.
    c, fake = _client(json.dumps({"guidance": "ok"}).encode())
    c.advise("x", session_id="too-short")
    assert len(fake.kwargs["runtimeSessionId"]) >= 33


def test_a_transport_failure_maps_to_a_kind() -> None:
    c, _ = _client(RuntimeError("boom"))
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40)
    assert caught.value.kind == "advisor_unreachable"


def test_a_non_json_body_maps_to_a_kind() -> None:
    c, _ = _client(b"not json")
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40)
    assert caught.value.kind == "advisor_bad_response"


def test_a_non_object_body_is_refused() -> None:
    c, _ = _client(json.dumps([1, 2, 3]).encode())
    with pytest.raises(AdvisorError) as caught:
        c.advise("x", session_id="w" * 40)
    assert caught.value.kind == "advisor_bad_response"


def test_a_generated_session_id_is_long_enough() -> None:
    assert len(new_session_id()) >= 33
