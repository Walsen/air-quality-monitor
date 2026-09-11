"""Shared behavioural suites for three small ports (Req 26.8, task 16.1).

The guardrail checker, the audit store and the association trigger.

Three small ports in one file, because each contract is a handful of assertions and splitting
them would spread one idea across three files. The `ServingClient` suite is separate only
because it is large.

**Every assertion must be true of EVERY adapter of its port.** A test that holds for the
in-memory store but not for DynamoDB would fail when task 16.5 lands and would be "fixed" by
weakening it, at which point the suite protects nothing.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from aqm_advisor.domain.records import AdviceRecord
from aqm_advisor.ports.protocols import (
    AdviceAuditStore,
    AssociationTrigger,
    GuardrailChecker,
    GuardrailResult,
    GuardrailVerdict,
)
from tests.contracts.registry import AdapterCase, cases_for, would_skip

pytestmark = pytest.mark.contract


def _skip_unless_available(case: AdapterCase) -> None:
    """Skip through the SAME predicate the fence asserts over."""
    if would_skip(case, os.environ):
        pytest.skip(f"{case.name} needs {case.requires_endpoint}, which is not configured")


def _record(user_id: str = "user-1") -> AdviceRecord:
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
        idempotency_key="a" * 64,
    )


# --- GuardrailChecker -------------------------------------------------


@pytest.fixture(params=cases_for("guardrail_checker"), ids=lambda c: c.name)
def checker(request: pytest.FixtureRequest) -> GuardrailChecker:
    _skip_unless_available(request.param)
    return request.param.build()  # type: ignore[no-any-return]


def test_the_checker_satisfies_the_protocol(checker: GuardrailChecker) -> None:
    assert isinstance(checker, GuardrailChecker)


def test_a_clean_text_passes(checker: GuardrailChecker) -> None:
    result = checker.check("Air quality is moderate today near your saved location.")
    assert isinstance(result, GuardrailResult)
    assert result.verdict is GuardrailVerdict.PASSED


def test_an_intervention_is_returned_not_raised(checker: GuardrailChecker) -> None:
    # THE clause for this port. Req 34.4 wants a verdict a caller can act on; an exception would
    # make an intervention indistinguishable from the checker being broken, and Req 34.6
    # requires
    # those two be treated differently — unavailability fails closed, an intervention is a
    # rejection.
    result = checker.check("You are having an asthma attack.")
    assert isinstance(result, GuardrailResult)
    assert result.verdict is not GuardrailVerdict.PASSED


def test_an_intervention_names_categories_and_not_the_text(checker: GuardrailChecker) -> None:
    # Req 8.6: the rejected text is the thing that must not be recorded, so the result carries
    # categories. Plural, as the field is named — a single category would lose the second
    # reason.
    result = checker.check("You are having an asthma attack. Take two puffs now.")
    assert result.categories, result
    for category in result.categories:
        assert "asthma attack" not in category
        assert "puffs" not in category


def test_the_verdict_is_total_over_any_text(checker: GuardrailChecker) -> None:
    # A checker that returned None for an input it did not understand would make the caller's
    # branch on verdict unsound. Every text gets a verdict.
    for text in ("", "   ", "?!", "a" * 4000, "you have asthma"):
        assert isinstance(checker.check(text).verdict, GuardrailVerdict), repr(text[:20])


# --- AdviceAuditStore ------------------------------------------------


@pytest.fixture(params=cases_for("advice_audit_store"), ids=lambda c: c.name)
def store(request: pytest.FixtureRequest) -> AdviceAuditStore:
    _skip_unless_available(request.param)
    return request.param.build()  # type: ignore[no-any-return]


def test_the_store_satisfies_the_protocol(store: AdviceAuditStore) -> None:
    assert isinstance(store, AdviceAuditStore)


def test_an_appended_record_is_erasable_and_the_count_is_reported(
    store: AdviceAuditStore,
) -> None:
    # Req 20.1 and the erasure clause. The COUNT is the contract, not a boolean: a caller
    # answering
    # a data-subject request has to say how many records were removed, and "something was
    # deleted"
    # is not an answer to that.
    store.append(_record())
    assert store.forget_user("user-1") == 1


def test_erasing_a_user_with_no_records_reports_zero(store: AdviceAuditStore) -> None:
    # Zero is a legitimate answer and must not be an error: a user who never used the service
    # still
    # gets a truthful response to an erasure request.
    assert store.forget_user("nobody") == 0


def test_erasure_does_not_touch_another_user(store: AdviceAuditStore) -> None:
    store.append(_record("user-1"))
    store.append(_record("user-2"))
    assert store.forget_user("user-1") == 1
    assert store.forget_user("user-2") == 1, (
        "the second user's record was erased with the first"
    )


def test_erasure_is_idempotent(store: AdviceAuditStore) -> None:
    # A repeated request must not report a second deletion, or a caller auditing erasures would
    # double-count. It must also not raise: a retried request is normal.
    store.append(_record())
    assert store.forget_user("user-1") == 1
    assert store.forget_user("user-1") == 0


# --- AssociationTrigger ----------------------------------------------


@pytest.fixture(params=cases_for("association_trigger"), ids=lambda c: c.name)
def trigger(request: pytest.FixtureRequest) -> AssociationTrigger:
    _skip_unless_available(request.param)
    return request.param.build()  # type: ignore[no-any-return]


def test_the_trigger_satisfies_the_protocol(trigger: AssociationTrigger) -> None:
    assert isinstance(trigger, AssociationTrigger)


def test_the_trigger_declares_no_return_value(trigger: AssociationTrigger) -> None:
    # Req 33.1: never awaited during a turn. A return value would invite a caller to wait for
    # it,
    # and a caller who waits has made the association synchronous — the thing forbidden.
    #
    # Asserted on the DECLARED annotation, not by comparing the call to None. mypy pointed out
    # that
    # `assert trigger.request(...) is None` is a tautology when the protocol already says `->
    # None`:
    # it passes for every conforming adapter without checking anything. The annotation is the
    # contract, so that is what is checked — and the call is still made, so a raising adapter
    # fails.
    import typing

    trigger.request("user-1", "corr-1")
    hints = typing.get_type_hints(type(trigger).request)
    assert hints.get("return") is type(None), hints


def test_the_trigger_carries_a_correlation_identifier(trigger: AssociationTrigger) -> None:
    # Req 33.6: the identifier is carried through, so an asynchronous evaluation can be traced
    # back
    # to the turn that asked for it. Asserted as a required parameter rather than by inspecting
    # the
    # adapter's storage, which differs per adapter.
    import inspect

    parameters = inspect.signature(trigger.request).parameters
    assert "correlation_id" in parameters, list(parameters)
    assert parameters["correlation_id"].default is inspect.Parameter.empty
