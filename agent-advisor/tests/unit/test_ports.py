"""Tests for the four port protocols (task 2.2).

The load-bearing assertion is `test_no_port_signature_names_a_cloud_type`: DD1 says no Bedrock,
AgentCore or httpx type appears in any port signature, and that is what lets the whole suite run
with no AWS credentials and no network beyond localhost (Requirement 26.5). Asserted by
RENDERING each signature rather than by reading the module, so a type introduced through an
alias or a re-export is caught too.

The port list is DERIVED from `port_protocols()` rather than written here, so a port added later
is scanned by default — the same "covered by default" shape the leak sweeps use.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import inspect
import typing

import pytest
from pydantic import ValidationError

from aqm_advisor.ports.protocols import (
    AdviceAuditStore,
    AdviceRecord,
    AssociationTrigger,
    GuardrailResult,
    GuardrailVerdict,
    ServingClient,
    ServingClientError,
    ServingFailureKind,
    port_protocols,
)

_CLOUD_MARKERS = (
    "boto3",
    "botocore",
    "bedrock",
    "agentcore",
    "httpx",
    "strands",
    "BedrockModel",
    "ClientError",
    "Response",
    "Request",
)


# --- DD1: no cloud type in any signature --------------------------------

def test_there_are_exactly_four_ports() -> None:
    # The Model_Port is deliberately absent: DD2 makes the Strands `Model` ABC the port itself.
    assert len(port_protocols()) == 4


@pytest.mark.parametrize("port", port_protocols(), ids=lambda p: p.__name__)
def test_no_port_signature_names_a_cloud_type(port: type) -> None:
    for name, member in vars(port).items():
        if name.startswith("_") or not callable(member):
            continue
        rendered = str(inspect.signature(member))
        for marker in _CLOUD_MARKERS:
            assert marker.lower() not in rendered.lower(), (
                f"{port.__name__}.{name} names {marker!r} in its signature: {rendered}"
            )


@pytest.mark.parametrize("port", port_protocols(), ids=lambda p: p.__name__)
def test_every_port_is_runtime_checkable(port: type) -> None:
    # So a local fake can be asserted to satisfy its port without inheriting from it.
    assert getattr(port, "_is_runtime_protocol", False) is True


@pytest.mark.parametrize("port", port_protocols(), ids=lambda p: p.__name__)
def test_every_port_method_is_documented(port: type) -> None:
    for name, member in vars(port).items():
        if name.startswith("_") or not callable(member):
            continue
        assert member.__doc__, f"{port.__name__}.{name} has no docstring"


def test_the_ports_module_imports_no_sdk() -> None:
    # The signature scan catches a type that APPEARS; this catches an SDK imported for any
    # reason,
    # since an import is what would make the module unloadable without the dependency present.
    import ast
    import pathlib

    import aqm_advisor.ports.protocols as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for marker in ("boto3", "botocore", "bedrock_agentcore", "httpx", "strands"):
        assert marker not in imported


# --- the Model_Port is the Strands ABC, not declared here ---------------

def test_no_model_port_is_declared_in_this_module() -> None:
    # DD2. A wrapper would need keeping in step with the framework's own abstraction for no
    # gain,
    # so its ABSENCE is the design decision and this test records it.
    from aqm_advisor.ports import protocols

    assert not hasattr(protocols, "ModelPort")
    assert not hasattr(protocols, "Model")


# --- the credential is opaque -------------------------------------------

@pytest.mark.parametrize(
    "method",
    ["air_quality", "history", "profile_get", "profile_put", "profile_delete",
     "symptom_entry_put"],
)
def test_every_serving_call_takes_the_credential_as_a_plain_string(method: str) -> None:
    # Assumption A4a: opaque, forwarded, never parsed. A richer type would invite inspection,
    # and
    # a component that parses a token is one that can log a claim.
    signature = inspect.signature(getattr(ServingClient, method))
    annotation = signature.parameters["credential"].annotation
    assert annotation in ("str", str), f"{method} types the credential as {annotation!r}"


def test_the_serving_client_exposes_exactly_the_documented_calls() -> None:
    public = {
        name
        for name, member in vars(ServingClient).items()
        if not name.startswith("_") and callable(member)
    }
    assert public == {
        "air_quality",
        "history",
        "profile_get",
        "profile_put",
        "profile_delete",
        "symptom_entry_put",
    }


# --- Req 21.1 / 21.4: a failure carries a KIND and nothing else ---------

def test_a_serving_failure_carries_only_its_kind() -> None:
    error = ServingClientError(ServingFailureKind.TIMEOUT)
    assert error.kind is ServingFailureKind.TIMEOUT
    # No body, no URL, no credential — nothing a degraded response or a log could disclose.
    assert set(vars(error)) == {"kind"}


def test_the_failure_kinds_are_the_documented_set() -> None:
    assert {kind.value for kind in ServingFailureKind} == {
        "unreachable",
        "timeout",
        "unauthorized",
        "bad_request",
        "unusable_body",
        "server_error",
    }


def test_an_unauthorized_kind_exists_so_a_re_authentication_can_be_named() -> None:
    # Req 21.5 reports a rejected credential as needing re-authentication, which needs its own
    # kind rather than being folded into a generic failure.
    assert ServingFailureKind.UNAUTHORIZED in set(ServingFailureKind)


# --- Req 34: the guardrail verdict --------------------------------------

def test_unavailable_is_distinct_from_intervened() -> None:
    # Req 34.6 fails CLOSED when the check cannot run, so both withhold the text — but an
    # operator
    # must be able to tell "the guardrail stopped this" from "the guardrail could not look".
    #
    # My first version compared the two members directly, which mypy correctly flagged as an
    # assertion that CANNOT FAIL: two distinct enum members are never equal. This asserts the
    # thing
    # that could actually regress — that three separate verdicts exist with distinct wire
    # values,
    # so folding one into another would fail here.
    values = {verdict.value for verdict in GuardrailVerdict}
    assert values == {"passed", "intervened", "unavailable"}
    assert len(GuardrailVerdict) == 3


def test_a_verdict_carries_categories_but_never_the_text() -> None:
    fields = {field.name for field in dataclasses.fields(GuardrailResult)}
    assert fields == {"verdict", "categories"}
    for forbidden in ("text", "generation", "output", "matched"):
        assert forbidden not in fields


# --- Req 20.3: the audit record cannot hold clinical content ------------

def test_the_advice_record_field_set_is_exactly_the_documented_one() -> None:
    assert set(AdviceRecord.model_fields) == {
        "user_id",
        "turn_at",
        "route",
        "escalated",
        "threshold_crossed",
        "driving_pollutant",
        "record_references",
        "guardrail_rejected",
        "rejection_category",
        "idempotency_key",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "utterance",
        "guidance",
        "text",
        "condition",
        "sensitivity",
        "threshold",
        "latitude",
        "longitude",
        "coordinates",
        "medications",
        "note",
        "severity",
    ],
)
def test_the_advice_record_has_nowhere_to_put_clinical_content(forbidden: str) -> None:
    # Req 20.3, structural: this is what keeps erasure down to removing an identity.
    assert forbidden not in set(AdviceRecord.model_fields)


def _a_record() -> AdviceRecord:
    return AdviceRecord(
        user_id="user-1",
        turn_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
        route="/invocations",
        escalated=False,
        threshold_crossed=False,
        driving_pollutant="PM25",
        record_references=(),
        guardrail_rejected=False,
        rejection_category=None,
        idempotency_key="k1",
    )


def test_the_advice_record_is_frozen() -> None:
    # Now a Pydantic model rather than a dataclass, so the refusal is a ValidationError.
    with pytest.raises(ValidationError):
        _a_record().escalated = True


def test_the_port_and_the_domain_share_one_advice_record() -> None:
    # THE consolidation guard. There used to be TWO definitions — a frozen dataclass in this
    # port
    # module and a Pydantic model in `domain/records.py` — whose field sets happened to match.
    # That
    # was luck, not a guarantee: two authorities for one shape drift the moment somebody adds a
    # field to whichever file they have open, and the `append` port would then accept a record
    # the
    # domain never validated. Asserting object IDENTITY makes drift impossible rather than
    # merely
    # detectable, which is stronger than the drift guards used for cross-SERVICE constants —
    # those
    # cannot share an object, and this can.
    from aqm_advisor.domain.records import AdviceRecord as DomainRecord

    assert AdviceRecord is DomainRecord


def test_the_advice_record_refuses_an_unknown_field() -> None:
    # What the consolidation bought. `extra="forbid"` makes Req 20.3's "exactly these fields"
    # hold
    # at CONSTRUCTION, so an attempt to record an utterance raises instead of being quietly
    # dropped
    # — and a dropped field looks identical to one that was never passed.
    with pytest.raises(ValidationError):
        AdviceRecord(  # type: ignore[call-arg]
            user_id="user-1",
            turn_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
            route="/invocations",
            escalated=False,
            threshold_crossed=False,
            driving_pollutant="PM25",
            record_references=(),
            guardrail_rejected=False,
            rejection_category=None,
            idempotency_key="k1",
            utterance="how is the air?",
        )


def test_the_record_carries_an_idempotency_key() -> None:
    # Req 32.4c: a re-invoked entrypoint delivers the same turn twice, so a record has to be
    # recognisable as a duplicate rather than appended again.
    names = set(AdviceRecord.model_fields)
    assert "idempotency_key" in names


# --- Req 33.1: the trigger is fire-and-forget ---------------------------

def test_the_association_trigger_returns_nothing() -> None:
    # A return value would invite a caller to await it, and Req 33.1 forbids awaiting the
    # derivation inside a turn.
    signature = inspect.signature(AssociationTrigger.request)
    assert signature.return_annotation in ("None", None, type(None))


def test_the_audit_store_exposes_erasure() -> None:
    public = {
        name
        for name, member in vars(AdviceAuditStore).items()
        if not name.startswith("_") and callable(member)
    }
    assert public == {"append", "forget_user"}


def test_no_port_uses_any_in_a_signature() -> None:
    # `Any` at a boundary defeats the point of typing it; the project lints ANN401 for the same
    # reason, and a port is where it would matter most.
    for port in port_protocols():
        for name, member in vars(port).items():
            if name.startswith("_") or not callable(member):
                continue
            hints = typing.get_type_hints(member)
            for hint_name, hint in hints.items():
                assert hint is not typing.Any, (
                    f"{port.__name__}.{name} types {hint_name} as Any"
                )
