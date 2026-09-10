"""Tests for the session correlation identifier and its baggage propagation (task 1.3).

The length test is not a style check. Req 32.11 requires a `runtimeSessionId` of at least 33
characters wherever this service originates one, and AgentCore rejects a shorter one — so a
generator that happened to emit 32 characters would fail only once deployed, which is the class
of fault this spec keeps trying to convert into an ordinary failing test.
"""

from __future__ import annotations

import pytest
from opentelemetry import baggage

from aqm_advisor.observability.correlation import (
    SESSION_ID_BAGGAGE_KEY,
    SESSION_ID_MIN_LENGTH,
    InvalidSessionIdError,
    current_session_id,
    new_session_id,
    session_scope,
    validate_session_id,
)


def test_the_minimum_length_is_thirty_three() -> None:
    # Pinned against Req 32.11. A drift guard on a constant that an external platform owns.
    assert SESSION_ID_MIN_LENGTH == 33


def test_a_generated_identifier_satisfies_the_platform_minimum() -> None:
    assert len(new_session_id()) >= SESSION_ID_MIN_LENGTH


def test_generated_identifiers_are_distinct() -> None:
    # A correlation id that repeated would attribute two sessions' spans to one session.
    assert len({new_session_id() for _ in range(256)}) == 256


def test_a_generated_identifier_is_accepted_by_the_validator() -> None:
    # The generator and the validator must agree, or the service could originate an id it then
    # refuses. Cheap to assert, and it is exactly the pair that drifts.
    validate_session_id(new_session_id())


def test_a_short_identifier_is_refused_naming_the_minimum() -> None:
    with pytest.raises(InvalidSessionIdError) as caught:
        validate_session_id("x" * (SESSION_ID_MIN_LENGTH - 1))
    assert str(SESSION_ID_MIN_LENGTH) in str(caught.value)


def test_the_boundary_is_tested_in_both_directions() -> None:
    validate_session_id("x" * SESSION_ID_MIN_LENGTH)
    with pytest.raises(InvalidSessionIdError):
        validate_session_id("x" * (SESSION_ID_MIN_LENGTH - 1))


def test_a_blank_identifier_is_refused() -> None:
    with pytest.raises(InvalidSessionIdError):
        validate_session_id("   ")


def test_the_refusal_does_not_echo_the_identifier() -> None:
    # The id is not secret, but a refusal message is logged and an id supplied by a caller is
    # untrusted input. Naming the constraint is enough to fix the fault.
    with pytest.raises(InvalidSessionIdError) as caught:
        validate_session_id("suspicious-inbound-value")
    assert "suspicious-inbound-value" not in str(caught.value)


# --- baggage propagation (Req 32.11) ------------------------------------

def test_no_session_is_current_outside_a_scope() -> None:
    assert current_session_id() is None


def test_the_scope_publishes_the_identifier_to_baggage() -> None:
    session = new_session_id()
    with session_scope(session):
        assert current_session_id() == session
        assert baggage.get_baggage(SESSION_ID_BAGGAGE_KEY) == session


def test_the_scope_detaches_on_exit() -> None:
    # A leaked context would attribute the NEXT session's spans to this one, which is worse than
    # having no correlation at all because it looks correct.
    with session_scope(new_session_id()):
        pass
    assert current_session_id() is None


def test_the_scope_detaches_even_when_the_body_raises() -> None:
    with pytest.raises(RuntimeError), session_scope(new_session_id()):
        raise RuntimeError("the turn failed")
    assert current_session_id() is None


def test_nested_scopes_restore_the_outer_session() -> None:
    outer, inner = new_session_id(), new_session_id()
    with session_scope(outer):
        with session_scope(inner):
            assert current_session_id() == inner
        assert current_session_id() == outer
    assert current_session_id() is None


def test_the_scope_refuses_an_invalid_identifier() -> None:
    # Validation at the boundary: a scope entered with a bad id would publish an id AgentCore
    # rejects, and the failure would surface far from here.
    with pytest.raises(InvalidSessionIdError), session_scope("short"):
        pass


def test_a_refused_scope_leaves_no_context_behind() -> None:
    with pytest.raises(InvalidSessionIdError), session_scope("short"):
        pass
    assert current_session_id() is None


def test_this_module_configures_no_collector() -> None:
    # Req 32.10: on AgentCore, instrumentation is automatic and this service configures no
    # collector.
    import ast
    import pathlib

    from aqm_advisor.observability import correlation

    source = pathlib.Path(correlation.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in (
        "opentelemetry.sdk.trace",
        "opentelemetry.sdk.metrics",
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        "strands.telemetry",
    ):
        assert forbidden not in imported, f"{forbidden} would install or export telemetry"
