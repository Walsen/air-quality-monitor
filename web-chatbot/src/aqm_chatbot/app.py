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

from aqm_chatbot.client import AdvisorClient, AdvisorError, new_session_id

_STATIC = Path(__file__).parent / "static"


class ChatRequest(BaseModel):
    """One chat turn from the browser. `session_id` continues a conversation."""

    utterance: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


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


def build_app(*, advisor: AdvisorClient, access_key: str) -> FastAPI:
    """Assemble the app from an advisor client and the shared access key.

    A blank access key is refused: an open proxy to the advisor is exactly what
    the gate exists to prevent, and starting without one would defeat it silently.
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
    ) -> JSONResponse:
        """Gate, forward one turn to the advisor, and return the shaped response."""
        if not _authorized(x_access_key):
            # 401 with a bare reason: never echo the supplied value or the expected one.
            raise HTTPException(status_code=401, detail="invalid or missing access key")
        session_id = body.session_id or new_session_id()
        try:
            raw = advisor.advise(body.utterance.strip(), session_id=session_id)
        except AdvisorError as error:
            # A handled boundary failure: name the kind, never a stack trace.
            return JSONResponse(
                status_code=502,
                content={"error": error.kind, "session_id": session_id},
            )
        payload = _render(raw)
        payload["session_id"] = session_id
        return JSONResponse(content=payload)

    return app


def main() -> None:  # pragma: no cover - the container / dev entrypoint
    """Resolve config from the environment, build the app, and serve."""
    import uvicorn

    from aqm_chatbot.client import AgentCoreAdvisorClient

    runtime_arn = os.environ["AQM_CHATBOT_RUNTIME_ARN"]
    region = os.environ.get("AQM_CHATBOT_REGION", "us-east-1")
    access_key = os.environ.get("AQM_CHATBOT_ACCESS_KEY", "")
    advisor = AgentCoreAdvisorClient(runtime_arn=runtime_arn, region=region)
    app = build_app(advisor=advisor, access_key=access_key)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["ChatRequest", "build_app", "main"]
