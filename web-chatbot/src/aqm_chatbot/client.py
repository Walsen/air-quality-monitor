"""The boundary to the advisor runtime on Bedrock AgentCore.

`AdvisorClient` is the port; `AgentCoreAdvisorClient` is the production adapter
that signs an `invoke_agent_runtime` call with the process's IAM credentials.
Keeping the boundary a protocol is what lets `/chat` be tested with a fake client
and no AWS — the same dependency-inversion the rest of the monorepo uses at every
seam it swaps (broker, model, store).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Protocol

# A session id AgentCore accepts must be at least this long (the advisor's own
# correlation rule, mirrored here so a too-short id never reaches the runtime).
_MIN_SESSION_ID_LEN = 33


class AdvisorError(Exception):
    """The advisor could not be reached or returned an unusable response.

    Carries a short, non-sensitive kind so the handler can answer without leaking
    a stack trace or an AWS error body to the browser.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class AdvisorClient(Protocol):
    """One advisory turn: an utterance in, the Advisory_Response mapping out."""

    def advise(self, utterance: str, *, session_id: str) -> dict[str, Any]:
        """Send `utterance` to the advisor and return its parsed response body."""
        ...


def new_session_id() -> str:
    """A fresh AgentCore-valid session id (>= 33 chars)."""
    return "web-" + uuid.uuid4().hex + uuid.uuid4().hex


class AgentCoreAdvisorClient:
    """Invokes the advisor runtime via `bedrock-agentcore` invoke_agent_runtime.

    The runtime uses IAM auth, so the call is SigV4-signed by boto3 from the
    Lambda execution role (or local SSO creds in development). The user's chat
    text is the ONLY thing forwarded; the credential the advisor forwards to
    Service 2 is not this service's concern in the POC (serving is scripted).
    """

    def __init__(self, *, runtime_arn: str, region: str, client: Any | None = None) -> None:
        self._runtime_arn = runtime_arn
        if client is not None:
            self._client = client
        else:
            import boto3

            self._client = boto3.client("bedrock-agentcore", region_name=region)

    def advise(self, utterance: str, *, session_id: str) -> dict[str, Any]:
        """Invoke the runtime and parse its JSON body, mapping failures to a kind."""
        if len(session_id) < _MIN_SESSION_ID_LEN:
            session_id = new_session_id()
        try:
            response = self._client.invoke_agent_runtime(
                agentRuntimeArn=self._runtime_arn,
                runtimeSessionId=session_id,
                payload=json.dumps({"utterance": utterance}).encode("utf-8"),
                contentType="application/json",
                accept="application/json",
            )
            raw = response["response"].read()
        except Exception as error:
            raise AdvisorError("advisor_unreachable") from error
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as error:
            raise AdvisorError("advisor_bad_response") from error
        if not isinstance(parsed, dict):
            raise AdvisorError("advisor_bad_response")
        return parsed


__all__ = [
    "AdvisorClient",
    "AdvisorError",
    "AgentCoreAdvisorClient",
    "new_session_id",
]
