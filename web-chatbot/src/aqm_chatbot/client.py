"""The boundary to the advisor runtime on Bedrock AgentCore.

`AdvisorClient` is the port; `AgentCoreAdvisorClient` is the production adapter.
Keeping the boundary a protocol is what lets `/chat` be tested with a fake client
and no AWS — the same dependency-inversion the rest of the monorepo uses at every
seam it swaps (broker, model, store).

The advisor runtime is fronted by a custom JWT (inbound) authorizer. A runtime
that integrates OAuth/JWT auth cannot be invoked through the AWS SDK's
`InvokeAgentRuntime` (SigV4): the SDK signs the request itself, and there is no
way to also present the user's bearer as the request's own authentication. So
this adapter makes a plain HTTPS POST to the AgentCore data-plane endpoint with
`Authorization: Bearer <jwt>` as the request's transport auth — no SigV4 at all.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import uuid
from typing import Any, Protocol

# A session id AgentCore accepts must be at least this long (the advisor's own
# correlation rule, mirrored here so a too-short id never reaches the runtime).
_MIN_SESSION_ID_LEN = 33

# The data-plane session-id header AgentCore reads for InvokeAgentRuntime.
_SESSION_ID_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

# Under the API Gateway 29s integration ceiling, leaving headroom for the hop.
_DEFAULT_TIMEOUT_SECONDS = 25.0

_logger = logging.getLogger(__name__)


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
    """Invokes the JWT-authorized advisor runtime over plain HTTPS.

    The runtime uses a custom JWT inbound authorizer, so the call is NOT
    SigV4-signed: it is a direct HTTPS POST to the AgentCore data-plane invoke
    endpoint carrying the user's bearer as its own `Authorization` header. The
    user's chat text is the only body forwarded.

    The HTTP client is injectable so the adapter is testable offline with a fake
    httpx-like client; in production a lazily-created `httpx.Client` is used.
    """

    def __init__(
        self, *, runtime_arn: str, region: str, http_client: Any | None = None
    ) -> None:
        self._runtime_arn = runtime_arn
        # URL-encode the ARN whole (colons and slashes included) into the path.
        encoded_arn = urllib.parse.quote(runtime_arn, safe="")
        self._url = (
            f"https://bedrock-agentcore.{region}.amazonaws.com"
            f"/runtimes/{encoded_arn}/invocations"
        )
        self._http_client = http_client

    def _client(self) -> Any:
        """The HTTP client, created lazily so import stays free of httpx in tests."""
        if self._http_client is None:
            import httpx

            self._http_client = httpx.Client(timeout=_DEFAULT_TIMEOUT_SECONDS)
        return self._http_client

    def advise(
        self, utterance: str, *, session_id: str, credential: str | None
    ) -> dict[str, Any]:
        """POST the utterance to the runtime and parse its JSON body.

        The runtime is JWT-authorized, so a turn with no credential cannot
        authenticate — it is refused immediately with `advisor_unauthenticated`
        rather than sent (the app already refuses no-bearer turns upstream with a
        401, so this is defense in depth). The credential is placed on the request
        as `Authorization: Bearer <credential>` — the exact header the authorizer
        and the runtime entrypoint read — and is passed through unmodified: never
        decoded, parsed, reframed, or logged (Property 2).
        """
        if credential is None:
            raise AdvisorError("advisor_unauthenticated")
        if len(session_id) < _MIN_SESSION_ID_LEN:
            session_id = new_session_id()

        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            _SESSION_ID_HEADER: session_id,
        }
        content = json.dumps({"utterance": utterance}).encode("utf-8")

        try:
            response = self._client().post(
                self._url,
                headers=headers,
                params={"qualifier": "DEFAULT"},
                content=content,
            )
            response.raise_for_status()
            raw = response.text
        except AdvisorError:
            raise
        except Exception as error:
            # Any transport/HTTP failure (httpx.HTTPError, non-2xx) is opaque to
            # the browser. Log a non-sensitive event only — never the URL query,
            # the credential, or the body (Property 2 + §6).
            _logger.warning("advisor request failed", extra={"kind": "advisor_unreachable"})
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
