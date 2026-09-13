"""The chat web app: a page, a health check, and a gated `/chat` proxy.

`build_app` takes its collaborators and configuration as arguments so importing
this module has no side effect and a test can build the app with a fake advisor
client and no AWS. `main()` resolves the real configuration from the environment
and is what the container / local dev server runs.

THE ACCESS GATE IS SERVER-SIDE. A shared key is compared here, never shipped to
the browser, so a public URL is not an open proxy to the account's Bedrock spend.
The comparison is constant-time. When no key is configured the service refuses to
start rather than serving an open endpoint by accident.
"""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from aqm_chatbot.auth import AuthError, CognitoAuthClient
from aqm_chatbot.client import AdvisorClient, AdvisorError, new_session_id

_STATIC = Path(__file__).parent / "static"


class ChatRequest(BaseModel):
    """One chat turn from the browser. `session_id` continues a conversation."""

    utterance: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class LoginRequest(BaseModel):
    """Sign-in credentials from the browser. Both fields must be non-empty.

    The values are used only to call Cognito and are never logged or echoed back;
    `min_length=1` rejects a blank field at the edge (422) before that call.
    """

    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=256)


def _bearer_token(authorization: str | None) -> str | None:
    """The raw token from a canonical `Bearer <token>` header, else None.

    Only the transport framing is inspected: the scheme is matched
    case-insensitively (RFC 6750) and the single following field is returned
    unchanged. The token itself is never decoded, parsed, or validated here — that
    is the ingestion service's job (Property 2). An absent header, a non-Bearer
    scheme, or an empty token all yield None so `/chat` can refuse the turn.
    """
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].casefold() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def _render(response: dict[str, Any]) -> dict[str, Any]:
    """Shape the Advisory_Response into what the page needs, dropping nothing safe.

    The browser gets the guidance, the degraded flag, any escalation, and the
    envelope's emergency guidance and disclaimer — the health-safety framing the
    advisor attaches — rather than a flattened string.
    """
    envelope = response.get("envelope") or {}
    return {
        "guidance": response.get("guidance"),
        "escalation": response.get("escalation"),
        "degraded": bool(response.get("degraded")),
        "emergency_guidance": envelope.get("emergency_guidance"),
        "disclaimer": envelope.get("disclaimer"),
        "answered_at": response.get("answered_at"),
    }


def build_app(
    *,
    advisor: AdvisorClient,
    access_key: str,
    cognito: CognitoAuthClient | None = None,
    cognito_client_id: str | None = None,
) -> FastAPI:
    """Assemble the app from its collaborators and the shared access key.

    A blank access key is refused: an open proxy to the advisor is exactly what
    the gate exists to prevent, and starting without one would defeat it silently.

    `cognito` and `cognito_client_id` are injected for the sign-in step. They are
    optional so an advisor-only deployment (and the existing `/chat` tests) build
    unchanged; `/login` is only served when a Cognito client is supplied, and
    returns 503 otherwise rather than pretending to authenticate.
    """
    if not access_key or not access_key.strip():
        raise ValueError(
            "an access key is required; the chatbot must not run as an open proxy "
            "to the advisor runtime"
        )
    expected = access_key.strip()
    app = FastAPI(title="AQM Advisor Chatbot", docs_url=None, redoc_url=None)

    def _authorized(supplied: str | None) -> bool:
        return supplied is not None and hmac.compare_digest(supplied.strip(), expected)

    @app.get("/health")
    def health() -> dict[str, str]:
        """Liveness only. Never reveals the key or the runtime target."""
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        """Serve the chat page. The key is entered by the user, never embedded."""
        return (_STATIC / "index.html").read_text(encoding="utf-8")

    @app.post("/chat")
    def chat(
        body: ChatRequest,
        x_access_key: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        """Gate, forward one turn to the advisor, and return the shaped response.

        Two gates, in order. The shared `X-Access-Key` is the coarse outer gate
        against an open proxy; it does NOT identify the user. The user's identity
        is the Cognito JWT the browser sends as `Authorization: Bearer <jwt>`. A
        diary turn needs that identity, so a turn with no usable bearer is refused
        with a generic 401 the page shows as "sign in again" — and the advisor is
        never called, so no anonymous turn reaches the runtime.

        The raw token (the `Bearer ` framing stripped as a transport detail, not
        parsed) is handed to the advisor client as `credential`; from there it is
        forwarded to the runtime unchanged. It is never logged or echoed back
        (Property 2).
        """
        if not _authorized(x_access_key):
            # 401 with a bare reason: never echo the supplied value or the expected one.
            raise HTTPException(status_code=401, detail="invalid or missing access key")
        credential = _bearer_token(authorization)
        if credential is None:
            # No verified identity: refuse rather than write a diary turn to no one.
            # Generic body so the page prompts a fresh sign-in; never names the JWT.
            raise HTTPException(status_code=401, detail="please sign in")
        session_id = body.session_id or new_session_id()
        try:
            raw = advisor.advise(
                body.utterance.strip(), session_id=session_id, credential=credential
            )
        except AdvisorError as error:
            # A handled boundary failure: name the kind, never a stack trace.
            return JSONResponse(
                status_code=502,
                content={"error": error.kind, "session_id": session_id},
            )
        payload = _render(raw)
        payload["session_id"] = session_id
        return JSONResponse(content=payload)

    @app.post("/login")
    def login(
        body: LoginRequest,
        x_access_key: str | None = Header(default=None),
    ) -> JSONResponse:
        """Gate, authenticate against Cognito, and hand the JWT to the browser.

        The token is returned, never stored server-side (the browser holds it for
        the session). On failure a GENERIC 401 is returned that discloses neither
        which field was wrong nor the supplied values (Requirement 1.4). Neither
        the password nor the token is ever logged.
        """
        if not _authorized(x_access_key):
            raise HTTPException(status_code=401, detail="invalid or missing access key")
        if cognito is None or not cognito_client_id:
            # Sign-in is not configured for this deployment; refuse rather than
            # pretend. Never a 500, and no detail about the missing configuration.
            raise HTTPException(status_code=503, detail="sign-in is not available")
        try:
            token = cognito.authenticate(body.username, body.password)
        except AuthError:
            # One generic message: never name the offending field or echo the input,
            # so an attacker cannot tell a bad username from a bad password.
            raise HTTPException(
                status_code=401, detail="sign-in failed; check your credentials"
            ) from None
        return JSONResponse(content={"token": token})

    return app


def main() -> None:  # pragma: no cover - the container / dev entrypoint
    """Resolve config from the environment, build the app, and serve."""
    import uvicorn

    from aqm_chatbot.auth import AgentCoreCognitoClient
    from aqm_chatbot.client import AgentCoreAdvisorClient

    runtime_arn = os.environ["AQM_CHATBOT_RUNTIME_ARN"]
    region = os.environ.get("AQM_CHATBOT_REGION", "us-east-1")
    access_key = os.environ.get("AQM_CHATBOT_ACCESS_KEY", "")
    advisor = AgentCoreAdvisorClient(runtime_arn=runtime_arn, region=region)
    # Deploy-time wiring of the client id is Task 20; here we just read it from env.
    cognito_client_id = os.environ.get("AQM_CHATBOT_COGNITO_CLIENT_ID", "")
    cognito = (
        AgentCoreCognitoClient(client_id=cognito_client_id, region=region)
        if cognito_client_id
        else None
    )
    app = build_app(
        advisor=advisor,
        access_key=access_key,
        cognito=cognito,
        cognito_client_id=cognito_client_id or None,
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["ChatRequest", "LoginRequest", "build_app", "main"]
