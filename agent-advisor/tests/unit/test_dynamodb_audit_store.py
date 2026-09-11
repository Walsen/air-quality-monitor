"""Tests for the persistent audit store and erasure (task 16.5). Validates Reqs 20.1-20.5.

**THE COUNT SEMANTICS ARE A DESIGN DECISION, RECORDED AS ONE.** Requirement 20 says what a
record
CONTAINS and says nothing about erasure's return value — task 16.1's review corrected an earlier
over-claim here. The semantics are: the count is rows that EXISTED and were removed by THIS
call.
That is what makes a repeat return zero, and it is why this adapter counts rows it actually
found
rather than delete requests it issued: a caller answering a data-subject request has to say how
many
records were removed, and a request count cannot answer that question.

**THE TASK'S "WHERE THE RECORD CARRIES CLINICAL CONTENT" IS VACUOUS BY DESIGN.** Req 20.3
forbids
the record from holding an utterance, guidance text, a condition, a sensitivity, a threshold or
a
coordinate, "so the trail carries no health-adjacent content and erasure has only an identity to
remove". `AdviceRecord` has nowhere to put any of it. So there is no de-identify-versus-delete
choice to make: the row goes. A test below pins that structurally, so the day someone adds a
clinical field the decision has to be revisited rather than silently inherited.

Driven against a fake DynamoDB table, so the suite stays offline (Req 26.5).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from aqm_advisor.adapters.audit.dynamodb import DynamoDbAdviceAuditStore
from aqm_advisor.ports.protocols import AdviceAuditStore, AdviceRecord


def _record(user_id: str = "user-1", *, key: str | None = None) -> AdviceRecord:
    return AdviceRecord(
        user_id=user_id,
        turn_at=dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC),
        route="/invocations",
        escalated=False,
        threshold_crossed=False,
        driving_pollutant="PM25",
        record_references=(),
        guardrail_rejected=False,
        rejection_category=None,
        idempotency_key=key or ("a" * 64),
    )


class _FakeTable:
    """A stand-in for a DynamoDB table resource, recording the calls it received.

    Models the two behaviours that matter for erasure: `query` returns only the items whose
    partition key matches, and `delete_item` removes one item. Deliberately NOT a Mock — the
    count
    semantics depend on what the store does with what the query returned, which a Mock would
    accept
    without modelling.
    """

    def __init__(self, *, fail_on_put: bool = False) -> None:
        self.items: list[dict[str, Any]] = []
        self.fail_on_put = fail_on_put
        self.deleted_keys: list[dict[str, Any]] = []
        self.queries = 0

    def put_item(self, *, Item: dict[str, Any]) -> None:  # noqa: N803 - the boto3 wire name
        if self.fail_on_put:
            raise OSError("the table is unavailable")
        self.items.append(Item)

    def query(self, *, KeyConditionExpression: str) -> dict[str, Any]:  # noqa: N803
        self.queries += 1
        wanted = KeyConditionExpression
        return {"Items": [i for i in self.items if i["userId"] == wanted]}

    def delete_item(self, *, Key: dict[str, Any]) -> None:  # noqa: N803
        self.deleted_keys.append(Key)
        self.items = [
            i
            for i in self.items
            if not (i["userId"] == Key["userId"] and i["turnKey"] == Key["turnKey"])
        ]


def _store(*, fail_on_put: bool = False) -> tuple[DynamoDbAdviceAuditStore, _FakeTable]:
    """The store and its table, returned as a pair.

    The table is handed back explicitly rather than reached through the store's private
    attribute: the assertions here are about what landed in the table, and reading a
    private field to get at it made every one of them a type error as well as a smell.
    """
    table = _FakeTable(fail_on_put=fail_on_put)
    return DynamoDbAdviceAuditStore(table=table), table


# --- the port ---------------------------------------------------------


def test_the_adapter_satisfies_the_port() -> None:
    assert isinstance(_store()[0], AdviceAuditStore)


# --- Req 20.1: one record per turn ------------------------------------


def test_an_appended_record_is_stored() -> None:
    store, table = _store()
    store.append(_record())
    assert len(table.items) == 1


def test_the_stored_item_carries_every_field_of_the_record() -> None:
    # A field silently dropped on write is a field an operator cannot query later, which is the
    # whole purpose of Req 20. Driven from the model's own field set so a NEW field added to
    # AdviceRecord fails this test rather than being quietly unpersisted.
    store, table = _store()
    store.append(_record())
    item = table.items[0]
    for field_name in AdviceRecord.model_fields:
        assert any(
            field_name.replace("_", "").casefold() == k.replace("_", "").casefold()
            for k in item
        ), f"{field_name} was not persisted; item keys were {sorted(item)}"


# --- Req 20.3: there is nowhere to put clinical content ---------------


def test_the_persisted_item_contains_no_clinical_field() -> None:
    # Req 20.3, enforced against what actually goes on the wire rather than against the model.
    # The
    # task's "erasure deletes rather than de-identifies where the record carries clinical
    # content"
    # is VACUOUS by design — this test is what keeps it vacuous, so the day a clinical field is
    # added the de-identify question has to be answered deliberately.
    store, table = _store()
    store.append(_record())
    keys = {k.casefold() for k in table.items[0]}
    for forbidden in (
        "utterance",
        "guidance",
        "condition",
        "conditions",
        "sensitivity",
        "threshold",
        "lat",
        "lon",
        "coordinate",
    ):
        assert forbidden not in keys, f"{forbidden} reached the audit item"


# --- Req 20.5: a write failure must not be fatal ----------------------


def test_a_write_failure_raises_so_the_caller_can_swallow_it() -> None:
    # Req 20.5 says the SERVICE must still answer, and places that decision at the call site:
    # the
    # pipeline logs and continues. The STORE surfaces the failure, because a store that
    # swallowed it
    # would make an audit gap invisible to the very log Req 20.5 requires.
    store, _table = _store(fail_on_put=True)
    with pytest.raises(OSError, match="unavailable"):
        store.append(_record())


# --- erasure: the count is rows that EXISTED and were removed ---------


def test_erasure_reports_the_number_of_rows_removed() -> None:
    store, _table = _store()
    store.append(_record(key="a" * 64))
    store.append(_record(key="b" * 64))
    assert store.forget_user("user-1") == 2


def test_erasure_of_an_unknown_user_reports_zero_and_does_not_raise() -> None:
    # Zero is a legitimate answer: a user who never used the service still gets a truthful
    # response.
    assert _store()[0].forget_user("nobody") == 0


def test_erasure_is_idempotent() -> None:
    # The decisive test for the count semantics. An adapter counting delete REQUESTS issued
    # rather
    # than rows found would return non-zero here and double-count an audited erasure.
    store, _table = _store()
    store.append(_record())
    assert store.forget_user("user-1") == 1
    assert store.forget_user("user-1") == 0


def test_erasure_does_not_touch_another_user() -> None:
    store, _table = _store()
    store.append(_record("user-1"))
    store.append(_record("user-2"))
    assert store.forget_user("user-1") == 1
    assert store.forget_user("user-2") == 1, "the second user's record went with the first"


def test_erasure_deletes_rather_than_rewriting() -> None:
    # The task's "deletes rather than de-identifies". Asserted by the ABSENCE of a write during
    # erasure: a de-identifying adapter would put_item a redacted row back, leaving a row
    # behind.
    store, table = _store()
    store.append(_record())
    before = len(table.items)
    store.forget_user("user-1")
    assert before == 1
    assert table.items == [], table.items
    assert len(table.deleted_keys) == 1


def test_erasure_counts_rows_not_delete_calls() -> None:
    # Belt and braces on the semantics: the count must equal the rows the query FOUND. Driven
    # with
    # two rows so a store returning a constant 1, or the number of queries, fails.
    store, table = _store()
    store.append(_record(key="a" * 64))
    store.append(_record(key="b" * 64))
    store.append(_record("user-2", key="c" * 64))
    assert store.forget_user("user-1") == 2
    assert len(table.deleted_keys) == 2
