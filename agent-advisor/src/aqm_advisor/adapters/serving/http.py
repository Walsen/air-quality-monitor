"""The HTTP `ServingClient`: Service 2's serving API over `httpx` (task 16.3).

**THE CREDENTIAL IS BUILT PER CALL, NEVER FOLDED INTO A CLIENT.** This is the load-bearing
decision
here. An `httpx.Client(headers={"Authorization": ...})` is the obvious shape and it is wrong:
the
client is held as an attribute, so the credential becomes reachable from this adapter and lands
in
anything that renders it — a repr in a log line, a traceback, a debugger. Req 5.2 forbids the
credential reaching any log entry, record, response or error message, and the contract suite's
`_deep_render` walks three hops through `__dict__` and `__slots__` looking for exactly that. So
the
`Authorization` header is constructed at call time and goes out of scope when the call returns.

**THE CREDENTIAL IS OPAQUE (assumption A4a).** It is never decoded, parsed, validated, cached or
reissued. AgentCore validates it inbound and Service 2 validates it on receipt; a third parse
here
would add a place for three things to disagree, and a component that parses a token is a
component
that can log a claim.

**A REJECTED CREDENTIAL IS AN AUTH FAILURE, NOT A SERVICE FAULT (Req 32.8a).** A forwarded token
keeps ageing, so it can be accepted at the front door and be near expiry by a later call in the
same
turn. 401 and 403 both map to `UNAUTHORIZED`, and there is NO retry and no refresh — refreshing
is
the caller's responsibility, and a retry here would re-send a credential that was just rejected.

**NO `httpx` TYPE APPEARS IN A PORT SIGNATURE (DD1).** Every public method keeps the port's
exact
shape: an opaque `str` credential in, a `Mapping[str, object]` out, `ServingClientError` on
failure.
The transport is injectable so a test can drive the whole adapter offline through
`httpx.MockTransport`, which is why this file needs no new dependency to be testable.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping

import httpx

from aqm_advisor.ports.protocols import ServingClientError, ServingFailureKind

_AIR_QUALITY_PATH = "/v1/air-quality/me"
_HISTORY_PATH = "/v1/air-quality/history"
_PROFILE_PATH = "/v1/profile/me"
_SYMPTOMS_PATH = "/v1/symptoms/me"
"""Service 2's routes. `symptoms` is PLURAL — the singular spelling 404s."""

_IDEMPOTENCY_HEADER = "Idempotency-Key"


class HttpServingClient:
    """Service 2's serving API as a `ServingClient` (Reqs 5.2, 5.4, 21.1, 32.8a).

    Holds the base URL and the timeout — configuration, both loggable — and deliberately holds
    no
    credential and no authenticated session.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: int,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Record the endpoint and timeout. `transport` is the offline seam for tests."""
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    # --- the port ------------------------------------------------------

    def air_quality(self, credential: str) -> Mapping[str, object]:
        """Return the per-user air-quality view (Requirement 2.1)."""
        return self._request("GET", _AIR_QUALITY_PATH, credential)

    def history(
        self,
        credential: str,
        site_code: str,
        start: dt.datetime,
        end: dt.datetime,
        species: frozenset[str] | None = None,
    ) -> Mapping[str, object]:
        """Return a readings history over a window (Requirements 3.1, 3.1a).

        The query names are camelCase because that is Service 2's wire contract (its Req 19.3);
        a
        snake_case parameter is simply absent as far as FastAPI is concerned, which produces a
        422
        before Service 2's handler runs rather than an obvious error.

        `species` is OMITTED when nothing was selected rather than sent empty, because an empty
        filter and no filter are different requests — one narrows to nothing.
        """
        params: dict[str, str] = {
            "siteCode": site_code,
            "startTime": start.isoformat(),
            "endTime": end.isoformat(),
        }
        if species:
            params["species"] = ",".join(sorted(species))
        return self._request("GET", _HISTORY_PATH, credential, params=params)

    def profile_get(self, credential: str) -> Mapping[str, object]:
        """Return the user's stored profile (Requirement 4.1)."""
        return self._request("GET", _PROFILE_PATH, credential)

    def profile_put(
        self,
        credential: str,
        patch: Mapping[str, object],
        idempotency_key: str,
    ) -> Mapping[str, object]:
        """Apply a profile patch under a stable key (Requirement 32.4c).

        The key travels in a header so a re-invoked entrypoint's second delivery is recognised
        by
        Service 2 as the same write rather than a new one.
        """
        return self._request(
            "PUT",
            _PROFILE_PATH,
            credential,
            json_body=dict(patch),
            extra_headers={_IDEMPOTENCY_HEADER: idempotency_key},
        )

    def profile_delete(self, credential: str) -> Mapping[str, object]:
        """Erase the user's profile and report the receipt."""
        return self._request("DELETE", _PROFILE_PATH, credential)

    def symptom_entry_put(
        self, credential: str, entry: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Record one diary entry (Requirement 28.3)."""
        return self._request(
            "PUT", _SYMPTOMS_PATH, credential, json_body=dict(entry)
        )

    # --- the one place a request is made ------------------------------

    def _request(
        self,
        method: str,
        path: str,
        credential: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        """Make one call and translate every outcome into the port's vocabulary.

        ONE method rather than per-route error handling, so no route can accidentally get a
        different failure mapping — the kind of drift that leaves one call site raising a raw
        `httpx` error into a turn that has no handler for it.

        The `Authorization` header is a LOCAL here. It is built for this call, handed to httpx,
        and
        gone when the frame unwinds; nothing on `self` ever holds it (Req 5.2).
        """
        headers = {"Authorization": f"Bearer {credential}", "Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        try:
            with httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.request(
                    method, path, params=params, json=json_body, headers=headers
                )
        except httpx.TimeoutException:
            # Its own kind, not UNREACHABLE: Req 21.1 logs the failure kind, and an operator
            # reading "timeout" learns something different from "unreachable".
            raise ServingClientError(ServingFailureKind.TIMEOUT) from None
        except httpx.HTTPError:
            # `from None` throughout: chaining would attach the original exception, whose
            # message
            # and request object can name the URL and carry the Authorization header — which is
            # precisely what Reqs 5.2 and 21.4 forbid surfacing.
            raise ServingClientError(ServingFailureKind.UNREACHABLE) from None
        return _body_of(response)


def _body_of(response: httpx.Response) -> Mapping[str, object]:
    """The parsed body, or the failure kind the status and shape imply.

    Status mapping is explicit rather than a range test on `is_success`, because 401 and 403
    must
    NOT fall into the same bucket as 400: Req 32.8a makes a rejected credential an
    authentication
    failure under Req 5.4, which the caller reports as "re-authenticate", not as a bad request.
    """
    status = response.status_code
    if status in (401, 403):
        # No retry, no refresh (Req 32.8a). Re-sending a credential that was just rejected
        # cannot
        # succeed and would put it on the wire a second time.
        raise ServingClientError(ServingFailureKind.UNAUTHORIZED)
    if status >= 500:
        raise ServingClientError(ServingFailureKind.SERVER_ERROR)
    if status >= 400:
        # 404 included: a site absent from Service 2's registry is a request naming something
        # that
        # does not exist, which is the closest honest reading among the six kinds.
        raise ServingClientError(ServingFailureKind.BAD_REQUEST)
    try:
        parsed = response.json()
    except ValueError:
        raise ServingClientError(ServingFailureKind.UNUSABLE_BODY) from None
    if not isinstance(parsed, dict):
        # The port promises a Mapping. A list parses as JSON but cannot be returned, and letting
        # it
        # through would fail later at a `.get()` far from the cause.
        raise ServingClientError(ServingFailureKind.UNUSABLE_BODY)
    return parsed


__all__ = ["HttpServingClient"]
