"""Tests for the HTTP `ServingClient` adapter (task 16.3).

Validates Reqs 5.2, 5.4, 21.1, 32.8a, and 3.1a's site code.

Driven through `httpx.MockTransport`, which ships with the pinned `httpx==0.28.1`. No new
dependency,
no network, no credentials — so every test here runs in the offline suite Req 26.5 requires.

**The credential is the thing under test as much as the routing is.** Req 5.2 forbids it from
reaching any log, record, response or error message, and the contract suite's `_deep_render`
walks
three hops through `__dict__` and `__slots__` looking for it. That is why this adapter builds
the
`Authorization` header PER CALL instead of folding it into a long-lived `httpx.Client`: a client
holding default headers is exactly the shape `_deep_render` was written to catch.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

import httpx
import pytest

from aqm_advisor.adapters.serving.http import HttpServingClient
from aqm_advisor.ports.protocols import (
    ServingClient,
    ServingClientError,
    ServingFailureKind,
)

_CREDENTIAL = "SENTINEL-CRED-Q7X-do-not-log"
_BASE = "https://serving.example.test"
_END = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
_START = _END - dt.timedelta(days=7)


def _client(handler: object) -> HttpServingClient:
    """An adapter wired to a mock transport, so nothing leaves the process."""
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return HttpServingClient(
        base_url=_BASE, timeout_seconds=30, transport=transport
    )


def _ok(body: object = None) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body if body is not None else {"ok": True})

    return handler


def _status(code: int, body: object = None) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json=body if body is not None else {"detail": "no"})

    return handler


def _raising(error: Exception) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return handler


class _Recorder:
    """Captures the request so a test can assert on the wire form."""

    def __init__(self, body: object | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.body = body if body is not None else {"ok": True}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self.body)


# --- the port contract -------------------------------------------------


def test_the_adapter_satisfies_the_port() -> None:
    assert isinstance(_client(_ok()), ServingClient)


# --- Req 5.2: the credential is forwarded but never retained ----------


def test_the_credential_is_forwarded_as_a_bearer_header() -> None:
    # Service 2's middleware requires the `Bearer` scheme and answers 401 MALFORMED otherwise,
    # so
    # the scheme is part of the contract rather than a convention.
    recorder = _Recorder()
    _client(recorder).air_quality(_CREDENTIAL)
    assert recorder.requests[0].headers["Authorization"] == f"Bearer {_CREDENTIAL}"


def test_the_credential_is_not_retained_on_the_adapter() -> None:
    # Req 5.2, and the reason the header is built per call. An `httpx.Client` carrying a default
    # Authorization header would be reachable from the adapter and would leak the credential
    # into
    # anything that rendered it — a repr in a log line, a traceback, a debugger.
    client = _client(_ok())
    client.air_quality(_CREDENTIAL)
    rendered = repr(vars(client))
    assert _CREDENTIAL not in rendered, rendered


def test_no_stored_httpx_client_carries_the_credential() -> None:
    # One hop deeper than the test above, matching the contract suite's `_deep_render`. A client
    # held as an attribute is the specific shape that defeats a shallow check.
    client = _client(_ok())
    client.air_quality(_CREDENTIAL)
    for value in vars(client).values():
        headers = getattr(value, "headers", None)
        if headers is not None:
            assert _CREDENTIAL not in str(dict(headers))


# --- routing ----------------------------------------------------------


def test_air_quality_calls_service_2s_route() -> None:
    recorder = _Recorder()
    _client(recorder).air_quality(_CREDENTIAL)
    request = recorder.requests[0]
    assert request.url.path == "/v1/air-quality/me"
    assert request.method == "GET"


def test_history_sends_the_site_and_the_window_in_camel_case() -> None:
    # Req 3.1a's site code, and Service 2's Req 19.3 wire names. The parameters are camelCase on
    # the wire while the Python signature is snake_case — a mismatch here produces a 422 from
    # FastAPI before Service 2's handler runs, which is why it is asserted rather than assumed.
    recorder = _Recorder()
    _client(recorder).history(_CREDENTIAL, "AQM1", _START, _END)
    params = recorder.requests[0].url.params
    assert params["siteCode"] == "AQM1"
    assert params["startTime"] == _START.isoformat()
    assert params["endTime"] == _END.isoformat()


def test_history_omits_species_when_none_is_asked_for() -> None:
    # Sending an empty `species` is not the same as omitting it: Service 2 would filter to
    # nothing.
    recorder = _Recorder()
    _client(recorder).history(_CREDENTIAL, "AQM1", _START, _END)
    assert "species" not in recorder.requests[0].url.params


def test_history_sends_a_selected_species() -> None:
    recorder = _Recorder()
    _client(recorder).history(_CREDENTIAL, "AQM1", _START, _END, frozenset({"PM25"}))
    assert recorder.requests[0].url.params["species"] == "PM25"


def test_profile_routes_and_methods() -> None:
    cases: list[tuple[Callable[[HttpServingClient], object], str, str]] = [
        (lambda c: c.profile_get(_CREDENTIAL), "GET", "/v1/profile/me"),
        (lambda c: c.profile_delete(_CREDENTIAL), "DELETE", "/v1/profile/me"),
    ]
    for call, method, path in cases:
        recorder = _Recorder()
        call(_client(recorder))
        assert recorder.requests[0].method == method
        assert recorder.requests[0].url.path == path


def test_profile_put_sends_the_patch_and_the_idempotency_key() -> None:
    # Req 32.4c. The key must reach Service 2 or a re-invoked entrypoint writes twice.
    recorder = _Recorder()
    _client(recorder).profile_put(_CREDENTIAL, {"conditions": ["asthma"]}, "k" * 64)
    request = recorder.requests[0]
    assert request.method == "PUT"
    assert request.url.path == "/v1/profile/me"
    assert b"asthma" in request.content
    assert request.headers.get("Idempotency-Key") == "k" * 64


def test_symptom_entry_put_uses_the_plural_route() -> None:
    # `/v1/symptoms/me`, plural. The singular spelling 404s.
    recorder = _Recorder()
    _client(recorder).symptom_entry_put(_CREDENTIAL, {"date": "2026-07-01"})
    assert recorder.requests[0].url.path == "/v1/symptoms/me"


# --- Req 21.1: failures carry a kind, never a body or a credential ----


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (401, ServingFailureKind.UNAUTHORIZED),
        (403, ServingFailureKind.UNAUTHORIZED),
        (400, ServingFailureKind.BAD_REQUEST),
        (404, ServingFailureKind.BAD_REQUEST),
        (422, ServingFailureKind.BAD_REQUEST),
        (500, ServingFailureKind.SERVER_ERROR),
        (503, ServingFailureKind.SERVER_ERROR),
    ],
)
def test_a_status_maps_to_its_failure_kind(
    status: int, kind: ServingFailureKind
) -> None:
    with pytest.raises(ServingClientError) as caught:
        _client(_status(status)).air_quality(_CREDENTIAL)
    assert caught.value.kind is kind


def test_a_401_is_an_auth_failure_and_is_not_retried() -> None:
    # Req 32.8a: a token rejected for remaining lifetime is an AUTH failure, not a service
    # fault,
    # and must not be refreshed or retried. Asserted by counting requests — a retry would be
    # invisible to a kind assertion alone.
    recorder_calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorder_calls.append(request)
        return httpx.Response(401, json={"detail": "expired", "category": "EXPIRED"})

    with pytest.raises(ServingClientError) as caught:
        _client(handler).air_quality(_CREDENTIAL)
    assert caught.value.kind is ServingFailureKind.UNAUTHORIZED
    assert len(recorder_calls) == 1, "the credential was re-sent; Req 32.8a forbids a refresh"


def test_a_connect_failure_is_unreachable() -> None:
    with pytest.raises(ServingClientError) as caught:
        _client(_raising(httpx.ConnectError("refused"))).air_quality(_CREDENTIAL)
    assert caught.value.kind is ServingFailureKind.UNREACHABLE


def test_a_timeout_is_its_own_kind() -> None:
    # Distinct from UNREACHABLE because Req 21.1 logs the failure KIND, and an operator reading
    # "timeout" learns something different from "unreachable".
    with pytest.raises(ServingClientError) as caught:
        _client(_raising(httpx.ReadTimeout("slow"))).air_quality(_CREDENTIAL)
    assert caught.value.kind is ServingFailureKind.TIMEOUT


def test_a_non_json_body_is_unusable_rather_than_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(ServingClientError) as caught:
        _client(handler).air_quality(_CREDENTIAL)
    assert caught.value.kind is ServingFailureKind.UNUSABLE_BODY


def test_a_json_body_that_is_not_an_object_is_unusable() -> None:
    # The port promises a `Mapping[str, object]`. A list parses as JSON but cannot be returned,
    # and
    # letting it through would fail later at a `.get()` far from the cause.
    with pytest.raises(ServingClientError) as caught:
        _client(_ok([1, 2, 3])).air_quality(_CREDENTIAL)
    assert caught.value.kind is ServingFailureKind.UNUSABLE_BODY


@pytest.mark.parametrize(
    "handler_factory",
    [
        lambda: _status(401, {"detail": "SENTINEL-BODY-Q7X"}),
        lambda: _status(500, {"detail": "SENTINEL-BODY-Q7X"}),
        lambda: _raising(httpx.ConnectError("SENTINEL-BODY-Q7X")),
    ],
)
def test_an_error_discloses_neither_the_credential_nor_the_body(
    handler_factory: object,
) -> None:
    # Reqs 5.2 and 21.4 together. The response body can name an internal host or a rejection
    # detail, and Req 5.4 forbids reporting the rejection detail Service 2 returned — so the
    # exception carries the KIND and nothing else.
    with pytest.raises(ServingClientError) as caught:
        _client(handler_factory()).air_quality(_CREDENTIAL)  # type: ignore[operator]
    rendered = f"{caught.value!r} {caught.value} {vars(caught.value)}"
    assert _CREDENTIAL not in rendered, rendered
    assert "SENTINEL-BODY-Q7X" not in rendered, rendered


def test_the_error_message_is_not_empty() -> None:
    # The contract suite asserts this too: a kind nobody can read is not a diagnosis.
    with pytest.raises(ServingClientError) as caught:
        _client(_status(500)).air_quality(_CREDENTIAL)
    assert str(caught.value).strip()


# --- success returns a mapping ----------------------------------------


def test_a_successful_read_returns_the_parsed_body() -> None:
    body = {"user": "u1", "nearestSensors": []}
    assert _client(_ok(body)).air_quality(_CREDENTIAL) == body
