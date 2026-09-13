"""A tiny in-process ASGI caller, so tests drive the app without starlette's
TestClient (whose httpx/anyio deprecation warnings fight `filterwarnings=error`).

Supports exactly what these tests need: GET and POST with a JSON body and
headers, returning status, headers and the decoded body.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


def call(
    app: Any,
    method: str,
    path: str,
    *,
    json_body: Any | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    """Invoke `app` (an ASGI application) once and collect the response."""
    raw_body = b"" if json_body is None else json.dumps(json_body).encode("utf-8")
    hdrs: list[tuple[bytes, bytes]] = []
    for k, v in (headers or {}).items():
        hdrs.append((k.lower().encode(), v.encode()))
    if json_body is not None:
        hdrs.append((b"content-type", b"application/json"))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method.upper(),
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": hdrs,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
    }

    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw_body, "more_body": False}
        return {"type": "http.disconnect"}

    collected: dict[str, Any] = {"status": 500, "headers": {}, "body": b""}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            collected["status"] = message["status"]
            collected["headers"] = {
                k.decode(): v.decode() for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            collected["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return Response(
        status=collected["status"], headers=collected["headers"], body=collected["body"]
    )
