"""The AgentCore deployment boundary: `POST /invocations` and `GET /ping` (task 17).

**THIS IS THE ONLY MODULE IN THE SERVICE THAT MAY IMPORT `bedrock_agentcore` (Req 32.2).** No
domain
or advisory component imports an AgentCore type, so the offline suite is unaffected by the
deploy
target and the advisory logic can be exercised without the SDK at all. An AST test enforces it.

**NO `@app.ping` HANDLER IS REGISTERED, AND THAT IS THE CORRECT IMPLEMENTATION.** Writing one is
the
obvious move and the wrong one. Read from the installed SDK: `get_current_ping_status` returns
`HealthyBusy` when `_active_tasks` is non-empty, and it advances `time_of_last_update` ONLY when
the
status actually changes. Req 32.4 forbids advancing that timestamp on every ping — a timestamp
that
always moves signals a continuous status change, which stops the idle session timeout from ever
firing, so sessions persist to `MaxLifetime` and can exhaust the account's session quota. The
SDK
already gets this right; a hand-rolled handler is how it gets broken. Req 32.4 says as much.

**THE TURN MUST DECLARE AN ASYNC TASK OR THE HEALTH CONTRACT IS SILENTLY UNMET (Reqs 32.4,
33.8).**
`_handle_invocation` never touches `_active_tasks`, so the SDK does NOT infer "busy" from a
request
being in flight. An entrypoint that merely runs a turn answers `Healthy` throughout. Bracketing
the
turn with `add_async_task` / `complete_async_task` is both what the SDK sanctions and what makes
Req 32.4b's offline assertion true rather than decorative.

**THE SDK'S OWN ERROR PATH IS EXACTLY WHAT Req 32.5 FORBIDS.** It catches an entrypoint
exception and
answers `JSONResponse({"error": str(e)}, status_code=500)`. AgentCore surfaces a container 5xx
to the
caller as an opaque `424 RuntimeClientError`, which replaces a documented degraded answer and
loses
the Guardrail_Envelope and any Escalation with it — and `str(e)` puts the exception's own words
in the
body, which Reqs 5.2 and 21.4 forbid. So the broad catch here has to fire FIRST. This is Req
21.5's
"true top-level boundary", and it is the ONLY broad catch in the service.

**THE TURN RUNNER IS INJECTED, NOT BUILT HERE.** There is no concrete `TurnPipeline` in the
service
yet; composing one from every adapter is task 21's composition root. This module therefore takes
a
callable and stays a transport boundary, which is also what lets the deployment contract be
tested
with a scripted turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext

from aqm_advisor.agent.boundary import fault_for, fault_response, handle_at_top_level
from aqm_advisor.domain.envelope import resolve_envelope
from aqm_advisor.domain.models import AdvisoryRequest, AdvisoryResponse
from aqm_advisor.observability.logging import EventLogger, get_logger
from aqm_advisor.ports.clock import Clock

TurnRunner = Callable[[AdvisoryRequest], AdvisoryResponse]
"""One advisory turn, synchronously.

Sync on purpose. `TurnPipeline.run` is synchronous, and Req 32.4a permits either awaiting on the
async path OR running on a separate thread. Offloading a sync runner to a thread satisfies it
without
rewriting the pipeline, and keeps the event loop free so `/ping` stays answerable — which is the
whole
point of 32.4a.
"""

_BEARER = "bearer"
_TASK_NAME = "advisory_turn"


def build_app(
    *,
    run_turn: TurnRunner,
    emergency_guidance: str,
    turn_budget_seconds: int,
    clock: Clock,
    logger: EventLogger | None = None,
) -> BedrockAgentCoreApp:
    """Build the deployment app (Reqs 32.1, 32.3, 32.4, 32.4a, 32.5, 32.8, 32.13, 33.8).

    `debug` is deliberately left at the SDK default of False. With it on, the SDK exposes
    `_agent_core_app_action` on `/invocations`, including `force_healthy` and `force_busy` — so
    any
    caller could make the container lie to the platform about its health. Not a requirement;
    found by
    reading the SDK, and pinned by a test.

    A NON-POSITIVE `turn_budget_seconds` IS REFUSED. The first version wrote
    `asyncio.timeout(turn_budget_seconds or None)`, and `0 or None` is `None`, which means NO
    TIMEOUT
    — so a zero budget silently disabled the Req 32.13 protection instead of expiring. A test
    with a
    zero budget then hung for ninety minutes, which is how it was found. "No budget" is not a
    state
    Req 32.13 permits, so it is refused at build time rather than reinterpreted.
    """
    if turn_budget_seconds <= 0:
        raise ValueError(
            "turn_budget_seconds must be positive; Req 32.13 requires an ordinary turn to be "
            f"bounded, and {turn_budget_seconds} would leave it unbounded"
        )
    app = BedrockAgentCoreApp()
    events = logger or get_logger(__name__)

    @app.entrypoint
    async def invoke(
        payload: dict[str, object], context: RequestContext
    ) -> dict[str, object]:
        """Run one advisory turn and always answer with an Advisory_Response body.

        Async so no blocking work occupies the event loop (Req 32.4a). The turn itself goes to a
        worker thread; `/ping` is served from this loop and stays answerable throughout.

        RETURNS A JSON-SAFE DICT, NOT THE MODEL, and that is not a style choice. The SDK's
        serializer
        tries `json.dumps(obj)`, then `convert_complex_objects` (which calls `model_dump()`),
        then
        falls back to `json.dumps(str(obj))`. `model_dump()` leaves `answered_at` as a
        `datetime`,
        which `json.dumps` cannot encode — so BOTH real attempts fail and the third succeeds,
        making
        the `/invocations` body the model's REPR STRING. That silently violates Req 32.3 while
        looking
        like it works: the response is 200 and the body is a quoted Python repr. `mode="json"`
        renders
        the instants as strings so the first attempt succeeds.

        NOTE WHAT THE BUDGET BOUNDS: `asyncio.timeout` cancels the AWAIT, and a worker thread
        that is
        already blocked keeps running, because Python cannot kill a thread. So the budget bounds
        when
        the CALLER gets an answer, not when the work stops. That is the right guarantee for Reqs
        32.13
        and 32.8a — both are about the response and the credential's remaining life — but an
        operator
        reading "budget" should not expect the thread to die with it.
        """
        answered_at = clock.now()
        task_id = app.add_async_task(_TASK_NAME)
        try:
            async with asyncio.timeout(turn_budget_seconds):
                request = _request_from(payload, context, events)
                response = await asyncio.to_thread(run_turn, request)
                return _body_of(response)
        except TimeoutError as expiry:
            # DISTINGUISHED from a generic failure deliberately. A review found the budget
            # expiry
            # folded into the broad catch below, which logs only the exception TYPE — so an
            # operator
            # could not tell an expiry from an ordinary model error. They are not equivalent:
            # only
            # the expiry leaves a WORKER THREAD STILL RUNNING, because `asyncio.timeout` cancels
            # the
            # await and Python cannot kill a thread. Repeated expiries occupy executor slots,
            # and a
            # saturated pool makes later turns answer degraded WITHOUT EVER EXECUTING while the
            # container still reports healthy. That deserves its own log line.
            events.warning(
                "advisory_turn_budget_expired",
                turn_budget_seconds=turn_budget_seconds,
            )
            return _body_of(
                handle_at_top_level(
                    expiry,
                    logger=events,
                    envelope=resolve_envelope(
                        served=None,
                        cached=None,
                        configured_emergency_guidance=emergency_guidance,
                    ).envelope,
                    answered_at=answered_at,
                )
            )
        except Exception as error:
            # EXPECTED exceptions go through `fault_for` FIRST. A review of the non-object-body
            # case
            # exposed something wider: this handler went straight to `handle_at_top_level`, so
            # even a
            # genuine pydantic ValidationError — a caller's malformed field — was answered with
            # "something went wrong on my side". `fault_for`'s docstring describes the intended
            # flow,
            # returning None only for a surprise, and skipping it told users a server-side story
            # about
            # their own mistake.
            envelope = resolve_envelope(
                served=None,
                cached=None,
                configured_emergency_guidance=emergency_guidance,
            ).envelope
            fault = fault_for(error)
            if fault is not None:
                return _body_of(
                    fault_response(
                        fault, envelope=envelope, answered_at=answered_at
                    )
                )
            # The ONLY broad catch in the service, and the reason it is here rather than in
            # `agent/boundary.py`: that module receives an already-caught error.
            # `handle_at_top_level` logs the exception TYPE and never its message, then returns
            # a
            # response — so a failure leaves as a 200 Advisory_Response carrying the envelope,
            # not as
            # a container 5xx.
            #
            # `Exception`, not `BaseException`: a cancellation or interpreter shutdown is not a
            # turn
            # outcome, and converting one into a cheerful degraded answer would be a lie. So the
            # guarantee is precisely "every handled TURN failure answers 200", not "every
            # failure".
            return _body_of(
                handle_at_top_level(
                    error,
                    logger=events,
                    envelope=envelope,
                    answered_at=answered_at,
                )
            )
        finally:
            # ALWAYS completed. A task left open pins the session `HealthyBusy` forever, which
            # is
            # precisely the session-quota exhaustion Req 32.4 warns about.
            app.complete_async_task(task_id)

    return app


def _body_of(response: AdvisoryResponse) -> dict[str, object]:
    """The Advisory_Response as a JSON-safe mapping (Req 32.3).

    `mode="json"` is load-bearing: it renders `answered_at` as a string. Without it the SDK's
    serializer fails on the `datetime`, falls back to `json.dumps(str(obj))`, and the response
    body
    becomes the model's repr — a 200 with a quoted Python object in it.
    """
    return response.model_dump(mode="json")


def _request_from(
    payload: object, context: RequestContext, events: EventLogger
) -> AdvisoryRequest:
    """Build the Advisory_Request, taking the credential from the inbound header (Req 32.8).

    The credential comes from the request-header allowlist, NOT from the request body: a body
    field
    would let a caller supply a credential the front door never validated. It is passed through
    unmodified and never inspected — this function does not decode, parse, validate, cache or
    reissue
    it (Reqs 5.5, 5.6, assumption A4a). The `Bearer` prefix is stripped as a transport framing
    detail,
    which is not inspection of the token.
    """
    if not isinstance(payload, dict):
        # A JSON list or string body arrives here as-is. Without this guard `payload.items()`
        # raised
        # AttributeError, which the broad catch turned into "something went wrong on my side" —
        # a
        # SERVER-side story for a client-side mistake. A ValueError routes through the
        # boundary's
        # invalid-request fault instead, so the caller is told to correct the body.
        raise ValueError("the /invocations body must be a JSON object")
    body = {k: v for k, v in payload.items() if k != "credential"}
    return AdvisoryRequest(credential=_credential_from(context, events), **body)  # type: ignore[arg-type]


def _credential_from(context: RequestContext, events: EventLogger) -> str:
    """The inbound bearer token, or empty when the header is absent or not Bearer-framed.

    ONLY CANONICAL BEARER FRAMING IS ACCEPTED, and a review found why that matters. The first
    version fell back to returning the raw header value, and `HttpServingClient` wraps whatever
    it
    is given in `Bearer `. So a header of `Token abc` was forwarded as `Bearer Token abc`, and a
    bare `abc` as `Bearer abc` — the credential Service 2 evaluates was NOT the one the caller
    sent, which Req 32.8's "forward it unmodified" forbids. Refusing beats corrupting: a mangled
    forward is rejected at Service 2 for a reason invisible from here.

    THE TOKEN is what must survive unmodified, not the framing. RFC 6750 makes the scheme
    case-insensitive and allows more than one space before the token, so accepting `bearer` and
    collapsing the separator changes only the frame and leaves the token byte-identical — which
    is
    what Req 32.8 protects. Separating frame from token is also not the "parse" Req 5.6 forbids:
    that clause is about cracking the JWT open to read claims, which nothing here does.

    An absent or unframed header is LOGGED. Req 32.7's authorizer refuses unauthenticated
    invocations before this code runs, so arriving here without a usable credential means the
    request-header allowlist is misconfigured — and without this log an operator cannot tell
    that
    infra fault from a user's expired token, because both reach the user as "re-authenticate".
    """
    headers = context.request_headers or {}
    raw = headers.get("Authorization") or headers.get("authorization") or ""
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].casefold() == _BEARER:
        return parts[1]
    events.warning(
        "entrypoint_credential_not_bearer_framed", header_present=bool(raw)
    )
    return ""


__all__ = ["TurnRunner", "build_app"]
