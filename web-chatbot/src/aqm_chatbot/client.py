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
from collections.abc import Callable
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

    def advise(
        self, utterance: str, *, session_id: str, credential: str | None
    ) -> dict[str, Any]:
        """Send `utterance` to the advisor and return its parsed response body.

        `credential` is the signed-in user's raw bearer token (the `Bearer `
        framing already stripped by the caller), or None when the turn carries no
        user identity. When present it is forwarded to the advisor runtime as the
        `Authorization: Bearer <credential>` header and is passed through
        byte-for-byte — never decoded, parsed, logged, or reissued (Property 2).
        """
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

    def advise(
        self, utterance: str, *, session_id: str, credential: str | None
    ) -> dict[str, Any]:
        """Invoke the runtime and parse its JSON body, mapping failures to a kind.

        When `credential` is supplied it is placed on the outgoing request as the
        `Authorization: Bearer <credential>` header — the exact header the advisor's
        entrypoint reads (`context.request_headers["Authorization"]`). The
        `invoke_agent_runtime` API models no Authorization parameter, so the header
        is injected with a per-call botocore `before-send` handler that runs after
        SigV4 signing and overwrites the signer's Authorization value; AgentCore
        forwards that header verbatim into the runtime container. The token is
        passed through unmodified and is never decoded, parsed, or logged
        (Property 2). No credential means no override — the SigV4 Authorization
        stands and the turn carries no user identity.
        """
        if len(session_id) < _MIN_SESSION_ID_LEN:
            session_id = new_session_id()
        unregister = self._register_credential_header(credential)
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
        finally:
            unregister()
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError) as error:
            raise AdvisorError("advisor_bad_response") from error
        if not isinstance(parsed, dict):
            raise AdvisorError("advisor_bad_response")
        return parsed

    def _register_credential_header(self, credential: str | None) -> Callable[[], None]:
        """Register a per-call `before-send` hook that sets the Authorization header.

        Returns a no-argument function that removes the hook again, so the token
        never outlives the one call it belongs to — a client-level header would
        leak the previous user's credential onto the next turn. `before-send` is
        botocore's post-signing hook point: the request handed to the handler is
        already SigV4-signed, and overwriting `Authorization` here replaces the
        signature with the user's bearer for the hop AgentCore forwards to the
        runtime. With no credential nothing is registered and the signature stands.
        """
        if credential is None:
            return lambda: None

        # A unique id keeps concurrent/nested calls from unregistering each other.
        event = "before-send.bedrock-agentcore.InvokeAgentRuntime"
        unique_id = f"aqm-user-credential-{uuid.uuid4().hex}"
        bearer = f"Bearer {credential}"

        def _inject(request: Any, **_kwargs: Any) -> None:
            # The token is set, never read back or logged (Property 2). Returning
            # None lets botocore proceed to actually send the (now re-headered)
            # request rather than short-circuiting it.
            request.headers["Authorization"] = bearer

        events = self._client.meta.events
        events.register(event, _inject, unique_id=unique_id)
        return lambda: events.unregister(event, _inject, unique_id=unique_id)


__all__ = [
    "AdvisorClient",
    "AdvisorError",
    "AgentCoreAdvisorClient",
    "new_session_id",
]
