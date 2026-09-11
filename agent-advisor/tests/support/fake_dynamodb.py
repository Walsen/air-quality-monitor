"""A faithful in-process DynamoDB table fake.

Shared by the unit suite and the contract registry.

ONE definition, imported by both, because a review found two independent reimplementations that
agreed only by sharing the same misconception — and when that misconception was fixed, two fakes
would have had to be corrected in lockstep or the suites would disagree about what `query`
accepts.

**THIS FAKE REFUSES THE BUG IT WAS WRITTEN FOR.** `query` requires a `ConditionBase`, exactly as
the
real boto3 Table resource does, and raises on a bare string. The previous fakes accepted a
string and
treated it as the partition-key VALUE, which is the one reading the real API does not apply — so
a
broken adapter passed every test. A fake that accepts what the service rejects does not reduce
risk,
it hides it.

It also models `LastEvaluatedKey` pagination, because erasure that reads one page silently
under-deletes, and a fake returning everything in one dict can never show that.
"""

from __future__ import annotations

from typing import Any

from boto3.dynamodb.conditions import ConditionBase, ConditionExpressionBuilder

_PARTITION_KEY = "userId"
_SORT_KEY = "turnKey"


class FakeTable:
    """A table that stores items and answers key-condition queries the way the real one does."""

    def __init__(
        self,
        *,
        fail_on_put: bool = False,
        page_size: int | None = None,
        fail_delete_after: int | None = None,
    ) -> None:
        """Configure the fake.

        `page_size` forces pagination so the erasure loop is exercised. `fail_delete_after`
        makes the
        Nth delete raise, so the partial-failure count can be asserted rather than assumed.
        """
        self.items: list[dict[str, Any]] = []
        self.fail_on_put = fail_on_put
        self.page_size = page_size
        self.fail_delete_after = fail_delete_after
        self.deleted_keys: list[dict[str, Any]] = []
        self.queries: list[dict[str, Any]] = []

    def put_item(self, *, Item: dict[str, Any]) -> None:  # noqa: N803 - boto3 wire name
        """Write one item."""
        if self.fail_on_put:
            raise OSError("the audit store is unavailable")
        self.items.append(Item)

    def query(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401 - boto3's own shape
        """Answer one page, requiring a real key condition.

        The `ConditionBase` is COMPILED with boto3's own `ConditionExpressionBuilder`, so the
        fake
        reads the partition-key value the same way the service would rather than trusting the
        caller
        to have passed something convenient.
        """
        self.queries.append(dict(kwargs))
        condition = kwargs.get("KeyConditionExpression")
        if not isinstance(condition, ConditionBase):
            raise TypeError(
                "KeyConditionExpression must be a ConditionBase (e.g. Key('userId').eq(...)); "
                f"boto3 forwards a bare {type(condition).__name__} verbatim as an "
                "expression, "
                "which DynamoDB rejects"
            )
        built = ConditionExpressionBuilder().build_expression(
            condition, is_key_condition=True
        )
        wanted = next(iter(built.attribute_value_placeholders.values()))
        matching = [i for i in self.items if i[_PARTITION_KEY] == wanted]

        start = 0
        if (marker := kwargs.get("ExclusiveStartKey")) is not None:
            start = next(
                (
                    n + 1
                    for n, i in enumerate(matching)
                    if i[_SORT_KEY] == marker[_SORT_KEY]
                ),
                0,
            )
        page = matching[start:]
        if self.page_size is not None:
            page = page[: self.page_size]

        out: dict[str, Any] = {"Items": page}
        if page and (start + len(page)) < len(matching):
            last = page[-1]
            out["LastEvaluatedKey"] = {
                _PARTITION_KEY: last[_PARTITION_KEY],
                _SORT_KEY: last[_SORT_KEY],
            }
        return out

    def delete_item(self, *, Key: dict[str, Any]) -> None:  # noqa: N803
        """Remove one item by its full key."""
        if (
            self.fail_delete_after is not None
            and len(self.deleted_keys) >= self.fail_delete_after
        ):
            raise OSError("the table refused the delete")
        self.deleted_keys.append(Key)
        self.items = [
            i
            for i in self.items
            if not (
                i[_PARTITION_KEY] == Key[_PARTITION_KEY]
                and i[_SORT_KEY] == Key[_SORT_KEY]
            )
        ]


__all__ = ["FakeTable"]
