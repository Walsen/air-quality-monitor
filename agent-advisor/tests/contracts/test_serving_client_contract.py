"""The shared behavioural suite for `ServingClient` (Req 26.8, task 16.1).

**EVERY ASSERTION HERE MUST BE TRUE OF EVERY ADAPTER, or it does not belong in this file.** That
is the whole point of a shared suite: an adapter swap must not change behaviour, so this file
may only encode the PORT's contract. A test that happens to hold for the scripted client but not
for an HTTP one would fail the moment task 16.3 lands and would be "fixed" by weakening it — at
which point the suite protects nothing.

The contract, taken from `ports/protocols.py` rather than from either implementation:

- Every method raises `ServingClientError` on failure rather than returning a sentinel,
  because Req 21.1 needs a
  degraded response naming the failure KIND and a sentinel would let a caller read "unavailable"
  as "nothing found".
- The error carries a `ServingFailureKind` and nothing else — no body, no URL, no
  credential (Reqs 5.4, 21.4).
- `profile_put` takes a required `idempotency_key` (Req 32.4c).
- The credential is opaque: forwarded, never parsed, and never present in anything the
  adapter raises or returns.
"""

from __future__ import annotations

import datetime as dt
import inspect

import pytest

from aqm_advisor.ports.protocols import (
    ServingClient,
    ServingClientError,
    ServingFailureKind,
)
from tests.contracts.registry import AdapterCase, cases_for, would_skip

pytestmark = pytest.mark.contract

_CASES = cases_for("serving_client")
_CREDENTIAL = "SENTINEL-CRED-Q7X-do-not-log"


@pytest.fixture(params=_CASES, ids=lambda c: c.name)
def case(request: pytest.FixtureRequest) -> AdapterCase:
    """The CASE, for tests needing its declared seams rather than a built adapter.

    Skipped through `would_skip`, which is the predicate the session fence in `conftest.py`
    recognises — so a skip here is one the fence can explain rather than an unexplained
    disappearance.
    """
    import os

    selected: AdapterCase = request.param
    if would_skip(selected, os.environ):
        pytest.skip(
            f"{selected.name} needs {selected.requires_endpoint}, which is not configured"
        )
    return selected


@pytest.fixture
def client(case: AdapterCase) -> ServingClient:
    """One adapter of this port, built from the case above."""
    return case.build()  # type: ignore[no-any-return]


# --- the shape of the port --------------------------------------------


def test_the_adapter_satisfies_the_protocol(client: ServingClient) -> None:
    assert isinstance(client, ServingClient)


def test_profile_put_requires_an_idempotency_key(client: ServingClient) -> None:
    # Req 32.4c made this a REQUIRED parameter so a new adapter cannot omit it and silently lose
    # the protection. Asserted on the adapter rather than only on the Protocol, because it is
    # the
    # adapter a caller actually holds.
    parameters = inspect.signature(client.profile_put).parameters
    assert "idempotency_key" in parameters, list(parameters)
    assert parameters["idempotency_key"].default is inspect.Parameter.empty, (
        "an optional key can be omitted, which is how the protection is lost"
    )


def _deep_render(obj: object, depth: int = 3) -> str:
    """Every string reachable from `obj` within `depth` hops.

    A review defeated the first version, which was `repr(vars(client))`: a credential held in
    `__slots__` (no `__dict__`, so it fell back to a bare `<Foo object at 0x…>`) or one
    attribute-hop away inside a nested object both passed while genuinely retained. `__slots__`
    is
    not exotic — it is what a performance-minded HTTP client is likely to use, and an
    `httpx.Client` holding an Authorization header is exactly the nested case.
    """
    if depth < 0:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    parts = [repr(obj)]
    if isinstance(obj, dict):
        for key, value in obj.items():
            parts.append(_deep_render(key, depth - 1))
            parts.append(_deep_render(value, depth - 1))
    elif isinstance(obj, list | tuple | set | frozenset):
        for item in obj:
            parts.append(_deep_render(item, depth - 1))
    else:
        for name in getattr(obj, "__dict__", {}):
            parts.append(_deep_render(getattr(obj, name, None), depth - 1))
        for name in getattr(type(obj), "__slots__", ()):
            parts.append(_deep_render(getattr(obj, name, None), depth - 1))
    return " ".join(parts)


def test_the_credential_is_not_retained_anywhere_reachable(client: ServingClient) -> None:
    # Req 5.2's shape: the credential is per call and opaque. An adapter that stored it would
    # put
    # it somewhere a repr, a pickle or a debugger dump could reach — which is the threat model
    # the
    # first version of this test claimed and did not actually cover.
    client.air_quality(_CREDENTIAL)
    assert _CREDENTIAL not in _deep_render(client)


def test_the_deep_render_catches_every_shape_the_shallow_one_missed() -> None:
    # Self-check over the three shapes that defeated the shallow version, because a scanner
    # which
    # found nothing would report this guarantee for free.
    class _Slotted:
        __slots__ = ("token",)

        def __init__(self, token: str) -> None:
            self.token = token

    class _Nested:
        def __init__(self, token: str) -> None:
            self.box = _Slotted(token)

    class _Plain:
        def __init__(self, token: str) -> None:
            self._token = token

    for holder in (_Slotted(_CREDENTIAL), _Nested(_CREDENTIAL), _Plain(_CREDENTIAL)):
        assert _CREDENTIAL in _deep_render(holder), type(holder).__name__


# --- failure is an exception carrying a KIND --------------------------


def _failing(case: AdapterCase) -> ServingClient:
    """The adapter rigged to fail, or a hard FAILURE when it declares no seam.

    NOT a skip. A review found the previous helper returning None for any adapter without the
    scripted client's `air_quality_body` kwarg, which silently deleted the two most important
    tests in this file for exactly the adapter that can leak a credential over a network. "We
    could not test the failure path" is not a pass.
    """
    if case.build_failing is None:
        pytest.fail(
            f"{case.port}:{case.name} declares no build_failing seam, so Reqs 21.1, 5.4 "
            "and 21.4 cannot be checked for it — add one to its AdapterCase"
        )
    return case.build_failing()  # type: ignore[no-any-return]


def test_a_failure_raises_rather_than_returning_a_sentinel(case: AdapterCase) -> None:
    # THE clause. Req 21.1 needs a degraded response naming the failure kind, and a sentinel
    # return
    # would let a caller treat "unavailable" as "nothing found" — the difference between telling
    # someone the data is missing and telling them the air is clean.
    with pytest.raises(ServingClientError) as caught:
        _failing(case).air_quality(_CREDENTIAL)
    assert isinstance(caught.value.kind, ServingFailureKind)


def test_the_error_discloses_neither_the_credential_nor_a_body(case: AdapterCase) -> None:
    # Reqs 5.4 and 21.4. The exception is the most likely thing a caller logs verbatim, so it
    # must
    # carry nothing but the kind. Scanned deeply, for the same reason the credential test is.
    with pytest.raises(ServingClientError) as caught:
        _failing(case).air_quality(_CREDENTIAL)
    assert _CREDENTIAL not in _deep_render(caught.value)
    assert str(caught.value).strip() != ""


# --- reads return a mapping ------------------------------------------


def test_a_successful_read_returns_a_mapping(client: ServingClient) -> None:
    body = client.air_quality(_CREDENTIAL)
    assert hasattr(body, "keys"), type(body)


def test_history_accepts_a_window(client: ServingClient) -> None:
    end = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
    body = client.history(_CREDENTIAL, end - dt.timedelta(days=7), end)
    assert hasattr(body, "keys"), type(body)
