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
from tests.contracts.registry import cases_for, would_skip

pytestmark = pytest.mark.contract

_CASES = cases_for("serving_client")
_CREDENTIAL = "SENTINEL-CRED-Q7X-do-not-log"


@pytest.fixture(params=_CASES, ids=lambda c: c.name)
def client(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> ServingClient:
    """One adapter of this port, skipped when it needs an endpoint this environment lacks.

    The skip goes through `would_skip`, the SAME predicate the fence asserts over, so a
    parameter cannot be excused here by logic the fence does not see.
    """
    import os

    case = request.param
    if would_skip(case, os.environ):
        pytest.skip(f"{case.name} needs {case.requires_endpoint}, which is not configured")
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


def test_the_credential_is_not_retained_as_an_attribute(client: ServingClient) -> None:
    # Req 5.2's shape: the credential is per call and opaque. An adapter that stored it would
    # put it
    # somewhere a repr, a pickle or a debugger dump could reach.
    client.air_quality(_CREDENTIAL)
    rendered = repr(vars(client)) if hasattr(client, "__dict__") else repr(client)
    assert _CREDENTIAL not in rendered


# --- failure is an exception carrying a KIND --------------------------


def test_a_failure_raises_rather_than_returning_a_sentinel(client: ServingClient) -> None:
    # THE clause. Req 21.1 needs a degraded response naming the failure kind, and a sentinel
    # return
    # would let a caller treat "unavailable" as "nothing found" — the difference between telling
    # someone the data is missing and telling them the air is clean.
    #
    # Driven through whatever mechanism the adapter offers for forcing a failure. An adapter
    # with no
    # such mechanism cannot be contract-tested here, which is itself worth knowing.
    failing = _failing_variant(client)
    if failing is None:
        pytest.skip(f"{type(client).__name__} exposes no way to force a failure")
    with pytest.raises(ServingClientError) as caught:
        failing.air_quality(_CREDENTIAL)
    assert isinstance(caught.value.kind, ServingFailureKind)


def test_the_error_discloses_neither_the_credential_nor_a_body(client: ServingClient) -> None:
    # Reqs 5.4 and 21.4. The exception is the most likely thing to be logged verbatim by a
    # caller,
    # so it must carry nothing but the kind.
    failing = _failing_variant(client)
    if failing is None:
        pytest.skip(f"{type(client).__name__} exposes no way to force a failure")
    with pytest.raises(ServingClientError) as caught:
        failing.air_quality(_CREDENTIAL)
    rendered = f"{caught.value} {caught.value.args!r}"
    assert _CREDENTIAL not in rendered
    assert rendered.strip() != ""


def _failing_variant(client: ServingClient) -> ServingClient | None:
    """A copy of `client` rigged to fail, or None when the adapter offers no such control.

    Kept as a helper rather than a fixture parameter because HOW an adapter is made to fail is
    adapter-specific, while THAT it must raise is the port's contract. This is the seam between
    the two, and putting it anywhere else would leak an implementation detail into the shared
    suite.
    """
    if hasattr(client, "air_quality_body"):
        return type(client)(  # type: ignore[call-arg]
            air_quality_body=ServingFailureKind.UNREACHABLE
        )
    return None


# --- reads return a mapping ------------------------------------------


def test_a_successful_read_returns_a_mapping(client: ServingClient) -> None:
    body = client.air_quality(_CREDENTIAL)
    assert hasattr(body, "keys"), type(body)


def test_history_accepts_a_window(client: ServingClient) -> None:
    end = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)
    body = client.history(_CREDENTIAL, end - dt.timedelta(days=7), end)
    assert hasattr(body, "keys"), type(body)
