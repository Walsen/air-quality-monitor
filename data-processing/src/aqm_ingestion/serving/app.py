"""The serving application and its authentication gate.

Requirement 18, plus Requirement 19 criterion 10's health endpoint.

THE GATE IS MIDDLEWARE, NOT A ROUTE DEPENDENCY, and Requirement 18.4 names the reason itself:
"authenticate before validating query parameters, so that an unauthenticated request carrying a
malformed parameter receives 401 rather than 400". FastAPI validates a route's query model
BEFORE its dependencies run, so a dependency-based gate answers 422 exactly where the spec
demands 401. Middleware precedes routing and validation, so it is the only placement that
satisfies the clause. Service 1 hit this same wall and the finding carries over unchanged.

THE PORT RETURNS AN IDENTITY AND NO CLAIM SET, which Requirement 18.1's wording ("a verified
user identity and claim set") might look to contradict. It does not: Requirement 18.7 forbids
any claim value beyond the user identity from reaching a response, a log, or an Audit_Record,
and the strongest way to honour that is for the claim set never to leave the adapter that
verified it. 18.1 describes what the Authenticator RESOLVES; 18.6 requires the identity be
derived from those verified claims, which the adapter did before discarding them.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import (
    Authenticator,
    AuthRejectedError,
    RejectionCategory,
)

_logger = get_logger("serving.app")

HEALTH_PATH = "/health"
"""Requirement 19.10's unauthenticated health endpoint — the ONLY exemption."""

DEFAULT_RATE_LIMIT_PER_MINUTE = 60
"""Requirement 18.11's default per-identity request rate limit."""

_BEARER_PREFIX = "bearer"
_RATE_WINDOW_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ServingSettings:
    """The configured serving bounds (§1: narrow, not a config blob)."""

    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE

    def __post_init__(self) -> None:
        """Refuse a limit that would reject every request (§5)."""
        if self.rate_limit_per_minute < 1:
            raise ValueError(
                f"rate_limit_per_minute must be at least 1, got "
                f"{self.rate_limit_per_minute}"
            )


class _RateLimiter:
    """A per-identity sliding window over the injected Clock.

    Clock-driven rather than wall-clock so the window is testable without sleeping (§2), and
    per-identity so one caller exhausting their allowance cannot lock another out.
    """

    def __init__(self, clock: Clock, limit: int) -> None:
        """Hold the clock and the configured limit."""
        self._clock = clock
        self._limit = limit
        self._seen: defaultdict[str, deque[dt.datetime]] = defaultdict(deque)

    def allow(self, user_id: str) -> bool:
        """Record a request and report whether it is within the limit."""
        now = self._clock.now()
        floor = now - dt.timedelta(seconds=_RATE_WINDOW_SECONDS)
        window = self._seen[user_id]
        while window and window[0] <= floor:
            window.popleft()
        if len(window) >= self._limit:
            return False
        window.append(now)
        return True


def _credential_from(header: str | None) -> str:
    """Extract a bearer credential, or raise the Requirement 18.2 rejection.

    A malformed header is refused HERE rather than handed to the authenticator: Requirement 18.2
    requires no store access for such a request, and the same reasoning says not to hand
    unparseable material onward either.

    Raises:
        AuthRejectedError: MISSING when the header is absent, MALFORMED otherwise.
    """
    if header is None or not header.strip():
        raise AuthRejectedError(RejectionCategory.MISSING)
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != _BEARER_PREFIX:
        raise AuthRejectedError(RejectionCategory.MALFORMED)
    if not parts[1].strip():
        raise AuthRejectedError(RejectionCategory.MISSING)
    return parts[1].strip()


def _unauthorized(detail: str, category: RejectionCategory) -> JSONResponse:
    """A 401 carrying the detail and the CATEGORY, and nothing else.

    Requirement 18.3 forbids disclosing which condition applied beyond the category, and
    Requirement 18.9 forbids any Reading, profile, or site metadata — so the body is exactly two
    fields and no exception text is ever interpolated into it (§5).
    """
    return JSONResponse(
        status_code=401, content={"detail": detail, "category": str(category)}
    )


def build_app(
    authenticator: Authenticator,
    clock: Clock,
    settings: ServingSettings | None = None,
) -> FastAPI:
    """Build the serving application with its authentication gate.

    ``clock`` is required rather than defaulted: the rate-limit window is measured from it, and
    a SystemClock default would read the wall clock and make the window untestable (§2).
    """
    resolved = settings or ServingSettings()
    app = FastAPI(title="Air Quality Monitor serving API", docs_url=None, redoc_url=None)
    limiter = _RateLimiter(clock, resolved.rate_limit_per_minute)

    @app.middleware("http")
    async def _authenticate(
        request: Request, call_next: Callable[[Request], Awaitable[object]]
    ) -> object:
        """Gate every route but the health endpoint (Requirements 18.1-18.4, 18.11)."""
        route = request.url.path
        if route == HEALTH_PATH:
            # Unauthenticated by Requirement 19.10, and deliberately NOT rate limited: there is
            # no identity to count against, and a liveness probe that fails under load would
            # take a healthy service out of rotation.
            return await call_next(request)

        try:
            credential = _credential_from(request.headers.get("Authorization"))
            identity = authenticator.verify(credential)
        except AuthRejectedError as rejected:
            # Requirement 18.10: ONE warning naming the route and the category, and no
            # credential material — the category is all the error carries, by design.
            _logger.warning(
                "auth_rejected", route=route, category=str(rejected.category)
            )
            return _unauthorized(
                "the Authorization header must carry a verifiable bearer credential",
                rejected.category,
            )

        if not limiter.allow(identity.user_id):
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "per-identity request rate limit exceeded",
                    "limit": resolved.rate_limit_per_minute,
                    "retryAfterSeconds": _RATE_WINDOW_SECONDS,
                },
            )

        # The verified identity travels on request state, so no handler needs to re-read the
        # header and none can take the identity from a parameter (Requirement 18.6).
        request.state.identity = identity
        return await call_next(request)

    @app.get(HEALTH_PATH)
    async def health() -> dict[str, str]:
        """Liveness, carrying no data (Requirements 19.10, 18.9)."""
        return {"status": "ok", "checkedAt": clock.now().isoformat()}

    @app.get("/v1/advice")
    async def advice(
        request: Request,
        userId: str | None = None,  # noqa: N803  (the wire name is camelCase)
        limit: int | None = None,
    ) -> JSONResponse:
        """A placeholder the gate can be exercised against.

        Task 25 replaces this with the real routes. It exists now so Requirement 18.5's scope
        refusal and Requirement 18.6's identity derivation are tested against a real request
        path rather than against the middleware in isolation.

        ``userId`` is accepted ONLY so a cross-identity attempt can be refused; the served
        identity always comes from the verified credential.

        ``limit`` is a TYPED parameter purely so Requirement 18.4 is testable: the clause is
        about authentication winning the race against query validation, and a route with no
        validatable parameter has no race to observe — the test would pass against a
        dependency-based gate too, which is exactly the arrangement 18.4 forbids.
        """
        identity = request.state.identity
        if userId is not None and userId != identity.user_id:
            # Requirement 18.5: 403 and no read of that resource — the refusal precedes any
            # store access, and the body names the violation without echoing the other identity.
            return JSONResponse(
                status_code=403,
                content={"detail": "requested resource is outside the caller's scope"},
            )
        return JSONResponse(
            status_code=200, content={"userId": identity.user_id, "limit": limit}
        )

    return app


__all__ = [
    "DEFAULT_RATE_LIMIT_PER_MINUTE",
    "HEALTH_PATH",
    "ServingSettings",
    "build_app",
]
