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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from aqm_ingestion.domain.aqi.breakpoints import BreakpointTableRegistry
from aqm_ingestion.domain.calibration import CalibrationRegistry
from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import (
    AuditStore,
    Authenticator,
    AuthRejectedError,
    ReadingsStore,
    RejectionCategory,
    SensorRegistryStore,
)
from aqm_ingestion.serving.assembler import DEFAULT_TABLE_ID, ResponseAssembler
from aqm_ingestion.serving.audit import audit_record_for
from aqm_ingestion.serving.basis import assemble_basis
from aqm_ingestion.serving.guardrails import (
    GuardrailViolationError,
    enforce_guardrails,
    guardrail_envelope,
)
from aqm_ingestion.serving.history import (
    DEFAULT_MAX_HISTORY_SPAN_DAYS,
    parse_history_window,
)
from aqm_ingestion.serving.models import (
    HistoryReadingOut,
    HistoryResponse,
    basis_out,
    iso_z,
)
from aqm_ingestion.serving.profiles import ProfileService, ResolvedProfile

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
    max_history_span_days: int = DEFAULT_MAX_HISTORY_SPAN_DAYS

    def __post_init__(self) -> None:
        """Refuse a limit that would reject every request (§5)."""
        if self.rate_limit_per_minute < 1:
            raise ValueError(
                f"rate_limit_per_minute must be at least 1, got "
                f"{self.rate_limit_per_minute}"
            )
        if self.max_history_span_days < 1:
            raise ValueError(
                f"max_history_span_days must be at least 1, got "
                f"{self.max_history_span_days}"
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
    assembler: ResponseAssembler | None = None,
    profiles: ProfileService | None = None,
    readings: ReadingsStore | None = None,
    registry: SensorRegistryStore | None = None,
    audit: AuditStore | None = None,
    breakpoints: BreakpointTableRegistry | None = None,
    settings: ServingSettings | None = None,
) -> FastAPI:
    """Build the serving application with its authentication gate and routes.

    ``clock`` is required rather than defaulted: the rate-limit window is measured from it, and
    a SystemClock default would read the wall clock and make the window untestable (§2).

    The data-bearing dependencies are optional so the gate can be exercised on its own — the
    task-23 tests build an app with none of them and assert only 401/403/429 behaviour, which is
    the whole point of authenticating BEFORE any handler logic (Requirement 18.1).
    """
    resolved = settings or ServingSettings()
    tables = breakpoints or BreakpointTableRegistry.with_defaults()
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

        # Requirement 18.5: a request NAMING a different identity is refused with 403.
        #
        # Every data route is /me-scoped, so nothing another user owns is addressable at all —
        # which is the strongest reading of Requirements 18.5 and 18.6. But a client may still
        # send a userId (copied from another integration, say), and silently IGNORING it would
        # let them believe they had fetched that user's data. So the mismatch is refused rather
        # than dropped, and the check sits in the middleware so it applies to every route
        # uniformly instead of being remembered per handler.
        claimed = request.query_params.get("userId")
        if claimed is not None and claimed != identity.user_id:
            _logger.warning("scope_refused", route=route)
            return JSONResponse(
                status_code=403,
                content={"detail": "requested resource is outside the caller's scope"},
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
    async def health() -> dict[str, object]:
        """Liveness plus the resolved configuration (Requirement 19.10).

        Carries the resolved Breakpoint_Table and Calibration_Strategy names and NOTHING else —
        no Reading, no profile data, no credential — which a test asserts on the whole key set
        rather than by inspecting for known-bad values.
        """
        return {
            "status": "ok",
            "checkedAt": iso_z(clock.now()),
            "breakpointTable": DEFAULT_TABLE_ID,
            "calibrationStrategies": sorted(
                CalibrationRegistry.with_defaults().names()
            ),
        }

    @app.get("/v1/air-quality/me")
    async def air_quality_me(request: Request) -> JSONResponse:
        """Requirement 19.1's per-user response.

        Assembly, guardrail enforcement, then audit — in that order, because Requirement 25.11
        holds that a guardrail-violating body must never be emitted, so it must not be audited
        as served either.
        """
        identity = request.state.identity
        if assembler is None or audit is None:  # pragma: no cover - wiring guard
            raise RuntimeError("the /me route needs an assembler and an audit store")

        assembled = assembler.assemble(identity)
        body = assembled.response.model_dump()
        _enforce_or_fail(body, route="/v1/air-quality/me")

        audit.append(
            audit_record_for(
                user_id=identity.user_id,
                served_at=clock.now(),
                route="/v1/air-quality/me",
                basis=assembled.basis,
                threshold_crossed=assembled.threshold_crossed,
            )
        )
        return JSONResponse(status_code=200, content=body)

    @app.get("/v1/air-quality/history")
    async def air_quality_history(
        request: Request,
        siteCode: str,  # noqa: N803  (the wire name is camelCase)
        startTime: str,  # noqa: N803
        endTime: str,  # noqa: N803
        species: str | None = None,
    ) -> JSONResponse:
        """Requirement 19.3's history window, with the Requirement 25.12 envelope."""
        identity = request.state.identity
        if readings is None or registry is None or audit is None:  # pragma: no cover
            raise RuntimeError("the history route needs the readings and registry stores")

        window = parse_history_window(
            site_code=siteCode,
            start=startTime,
            end=endTime,
            species=species,
            registry=registry,
            max_span_days=resolved.max_history_span_days,
        )
        if window.problems:
            return _bad_request(window.problems)
        if window.unknown_site:
            return JSONResponse(
                status_code=404,
                content={"detail": f"unknown siteCode {siteCode!r}"},
            )

        result = readings.query_window(
            site_code=siteCode,
            species=frozenset({species}) if species else None,
            start=window.start,
            end=window.end,
        )
        ordered = sorted(result.readings, key=lambda r: (r.key.interval_start, r.key.species))
        basis = assemble_basis(ordered)
        envelope = guardrail_envelope()

        body = HistoryResponse(
            siteCode=siteCode,
            startTime=window.start,
            endTime=window.end,
            readings=tuple(
                HistoryReadingOut(
                    dateTime=reading.key.interval_start,
                    species=reading.key.species,
                    correctedValue=reading.corrected_value,
                    units=reading.units,
                    qualityFlag=str(reading.quality_flag),
                    confidence=str(reading.confidence),
                    subIndex=reading.sub_index,
                    band=reading.band,
                )
                for reading in ordered
            ),
            truncated=result.truncated,
            basis=basis_out(basis),
            advisoryScope=envelope.advisory_scope,
            emergencyGuidance=envelope.emergency_guidance,
            disclaimer=envelope.disclaimer,
        ).model_dump()
        _enforce_or_fail(body, route="/v1/air-quality/history")

        audit.append(
            audit_record_for(
                user_id=identity.user_id,
                served_at=clock.now(),
                route="/v1/air-quality/history",
                basis=basis,
                threshold_crossed=False,
            )
        )
        return JSONResponse(status_code=200, content=body)

    @app.get("/v1/profile/me")
    async def get_profile(request: Request) -> JSONResponse:
        """Requirement 19.4's profile read, declaring a default (Requirement 17.12)."""
        if profiles is None:  # pragma: no cover - wiring guard
            raise RuntimeError("the profile routes need a profile service")
        resolved_profile = profiles.resolve(request.state.identity)
        return JSONResponse(status_code=200, content=_profile_out(resolved_profile))

    @app.put("/v1/profile/me")
    async def put_profile(request: Request) -> JSONResponse:
        """Requirement 19.4's profile replacement, validated per Requirement 17."""
        if profiles is None:  # pragma: no cover - wiring guard
            raise RuntimeError("the profile routes need a profile service")
        identity = request.state.identity
        submitted = await request.json()
        try:
            profiles.write(identity, dict(submitted))
        except (ValidationError, ValueError) as invalid:
            # §5: bad INPUT is a 400, never a 500 — distinct from Requirement 25.11's deliberate
            # 500 for a guardrail-violating OUTPUT. The message is already sanitised of
            # submitted
            # values by build_profile (Requirement 17.9).
            _logger.warning("profile_write_rejected", user_id=identity.user_id)
            return JSONResponse(
                status_code=400,
                content={
                    "detail": "the submitted profile was rejected",
                    "problems": _problem_locations(invalid),
                },
            )
        return JSONResponse(
            status_code=200, content=_profile_out(profiles.resolve(identity))
        )

    @app.delete("/v1/profile/me")
    async def delete_profile(request: Request) -> JSONResponse:
        """Requirement 19.5's deletion, per Requirement 17.8."""
        if profiles is None:  # pragma: no cover - wiring guard
            raise RuntimeError("the profile routes need a profile service")
        receipt = profiles.delete(request.state.identity)
        return JSONResponse(
            status_code=200,
            content={
                "profileDeleted": receipt.profile_deleted,
                "auditRecordsDeIdentified": receipt.audit_records_de_identified,
            },
        )

    _ = tables  # resolved for the health route's disclosure
    return app


def _enforce_or_fail(body: Mapping[str, object], route: str) -> None:
    """Run the Requirement 25.11 check, converting a violation into a 500.

    Raises:
        HTTPException: 500 with the body WITHHELD. Requirement 25.11 holds that emitting a
            guardrail-violating body is worse than emitting none, so the offending text does not
            travel — only the pattern reaches the log.
    """
    try:
        enforce_guardrails(body)
    except GuardrailViolationError as violation:
        _logger.error("guardrail_violation", route=route, pattern=violation.pattern)
        raise HTTPException(
            status_code=500,
            detail="the response was withheld because it violated a guardrail",
        ) from violation


def _bad_request(problems: Sequence[str]) -> JSONResponse:
    """A 400 naming EVERY offending parameter (Requirement 19.6, §5).

    Accumulated rather than first-wins: a caller fixing one parameter at a time needs a round
    trip per fault, and the requirement says "naming each offending parameter".
    """
    return JSONResponse(
        status_code=400,
        content={"detail": "one or more parameters were rejected", "problems": list(problems)},
    )


def _problem_locations(error: Exception) -> list[str]:
    """Field locations from a validation failure, never the submitted values.

    Requirement 17.9 covers error messages, so only the LOCATION of a rejected field travels.
    """
    if isinstance(error, ValidationError):
        return sorted(
            ".".join(str(part) for part in problem["loc"]) for problem in error.errors()
        )
    return ["request body"]


def _profile_out(resolved: ResolvedProfile) -> dict[str, object]:
    """A profile as Requirement 19.4 returns it, declaring a default per Requirement 17.12."""
    profile = resolved.profile
    return {
        "condition": str(profile.condition),
        "sensitivity_level": str(profile.sensitivity_level),
        "locations": [
            {"name": str(loc.name), "latitude": loc.latitude, "longitude": loc.longitude}
            for loc in profile.locations
        ],
        "personal_thresholds": {
            species: {"kind": str(threshold.kind), "value": threshold.value}
            for species, threshold in sorted(profile.personal_thresholds.items())
        },
        "activity_level": None
        if profile.activity_level is None
        else str(profile.activity_level),
        "activity_duration_hours": profile.activity_duration_hours,
        "usedDefaultProfile": resolved.used_default,
    }


__all__ = [
    "DEFAULT_RATE_LIMIT_PER_MINUTE",
    "HEALTH_PATH",
    "ServingSettings",
    "build_app",
]
