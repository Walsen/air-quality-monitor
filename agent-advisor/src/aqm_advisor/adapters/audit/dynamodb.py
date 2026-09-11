"""The persistent Advice_Record store and erasure (task 16.5, Req 20).

**WHY A TABLE AT ALL, AND WHY NOT AN AGENTCORE CAPABILITY.** AgentCore Memory was evaluated
first
and rejected: it is built for conversational context a model reads back, whereas Req 20's trail
is an
OPERATOR record — queried by identity and date, never fed to a model, and required to survive
after
the session that produced it is gone. Req 20.3 also strips it of exactly the content Memory
exists to
carry. A key-value table indexed by user is the shape the queries have, so that is what this
uses.

**THE COUNT IS ROWS THAT EXISTED AND WERE REMOVED BY THIS CALL.** Requirement 20 is silent on
erasure's return value — task 16.1's review corrected an earlier over-claim that cited 20.1/20.2
for
it — so this is a design decision, recorded as one. It follows from the port signature plus Req
20.3's
reasoning that erasure "has only an identity to remove". Counting delete REQUESTS issued instead
would
make a repeated erasure report a second deletion and double-count an audited request, which is
why
the count comes from what the query FOUND.

**ERASURE DELETES; IT DOES NOT DE-IDENTIFY.** For this record that is not a judgement call: Req
20.3
leaves the row with nothing but an identity and some flags, so de-identifying it would leave a
row
whose only remaining purpose was the identity just removed. Note the ASYMMETRY with Service 2,
whose
`/v1/profile/me` DELETE reports `auditRecordsDeIdentified` — it de-identifies because its audit
rows
carry servable content worth keeping. This service has no such content to keep.
"""

from __future__ import annotations

from typing import Any, Protocol

from aqm_advisor.ports.protocols import AdviceRecord

_PARTITION_KEY = "userId"
_SORT_KEY = "turnKey"
"""The item's identity.

The sort key is the record's idempotency key rather than its instant: two deliveries of ONE turn
carry the same key and must collapse to one row, which a timestamp sort key would not do — a
re-invoked entrypoint reads the clock again and would write a second row for the same turn.
"""


class _Table(Protocol):
    """The three operations this store needs from a DynamoDB table resource.

    Narrow on purpose, so a test can supply a fake that models query-then-delete faithfully. The
    count semantics depend on what the store does with what the query returned, which is
    behaviour a
    Mock would accept without modelling.
    """

    def put_item(self, *, Item: dict[str, Any]) -> None:  # noqa: N803 - boto3 wire name
        """Write one item."""
        ...

    def query(self, *, KeyConditionExpression: str) -> dict[str, Any]:  # noqa: N803
        """Return the items for one partition key."""
        ...

    def delete_item(self, *, Key: dict[str, Any]) -> None:  # noqa: N803
        """Remove one item by its full key."""
        ...


class DynamoDbAdviceAuditStore:
    """Advice_Records in a table, with erasure by user (Reqs 20.1, 20.3, 20.5).

    The table resource is INJECTED, so this class resolves no session, no region and no
    credential —
    and so the whole store is exercisable offline.
    """

    def __init__(self, *, table: _Table) -> None:
        """Record the table this store writes to."""
        self._table = table

    def append(self, record: AdviceRecord) -> None:
        """Record one turn (Req 20.1).

        Raises on failure rather than swallowing. Req 20.5 requires the SERVICE to still answer
        and
        to LOG the failure, which is a decision for the call site — a store that swallowed the
        error
        would make an audit gap invisible to the log that requirement asks for.
        """
        self._table.put_item(Item=_item_for(record))

    def forget_user(self, user_id: str) -> int:
        """Erase this user's records and return how many were removed.

        Queries first, then deletes what it found, and counts THAT — so a repeat returns zero.
        The
        query is the source of the count for the reason in this module's docstring.
        """
        found = self._table.query(KeyConditionExpression=user_id).get("Items") or []
        for item in found:
            self._table.delete_item(
                Key={_PARTITION_KEY: item[_PARTITION_KEY], _SORT_KEY: item[_SORT_KEY]}
            )
        return len(found)


def _item_for(record: AdviceRecord) -> dict[str, Any]:
    """The record as a table item.

    Field names are camelCase for the table's own conventions, and the mapping is EXPLICIT
    rather
    than a `model_dump()` splat: a new field on `AdviceRecord` should fail the persistence test
    loudly instead of arriving in the table under a name nothing queries. Req 20.3's
    minimisation
    means there is no field here to redact — every one is an identity, a flag, a category or a
    public sensor fact.
    """
    return {
        _PARTITION_KEY: record.user_id,
        _SORT_KEY: record.idempotency_key,
        "turnAt": record.turn_at.isoformat(),
        "route": record.route,
        "escalated": record.escalated,
        "thresholdCrossed": record.threshold_crossed,
        "drivingPollutant": record.driving_pollutant,
        "recordReferences": list(record.record_references),
        "guardrailRejected": record.guardrail_rejected,
        "rejectionCategory": record.rejection_category,
        "idempotencyKey": record.idempotency_key,
    }


__all__ = ["DynamoDbAdviceAuditStore"]
