"""Tests for the `ApplyGuardrail` adapter (task 16.4).

Validates Reqs 34.2, 34.2a, 34.2b, 34.3, 34.4, 34.6 and 34.9.

**Every API fact here was read from botocore's own service model, not assumed.**
`ApplyGuardrailRequest` requires
`guardrailIdentifier`, `guardrailVersion`, `source` and `content`; `source` is an enum of
exactly `INPUT` and
`OUTPUT`; and `GuardrailAction` carries exactly two values, `NONE` and `GUARDRAIL_INTERVENED`.

**The fail-open trap is the reason for Req 34.2b.** With only two actions, `action !=
"GUARDRAIL_INTERVENED"` and
`action == "NONE"` are equivalent TODAY. They stop being equivalent the day AWS adds a third
value, and they fail in
opposite directions: the inverted test would read an unrecognised action as PASSED and emit
unverified
health-adjacent text. So the success value is matched explicitly and everything else is an
intervention.

**Empty text never reaches the API (Req 34.2a).** Not because botocore rejects it — the model
puts no minimum on
`text`, so it does not — but because the service's own rejection would arrive as an exception,
the adapter would map
it to UNAVAILABLE, and Req 34.6's fail-closed path would then fire for a text that is trivially
clean.

The adapter is exercised through an injected client, so every test here is offline. `moto` and a
live endpoint are
both unnecessary to verify the mapping, which is the part that can be wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from aqm_advisor.adapters.guardrail.bedrock import (
    DENIED_TOPIC_NAMES,
    ApplyGuardrailChecker,
)
from aqm_advisor.ports.protocols import GuardrailChecker, GuardrailVerdict


class _FakeRuntime:
    """A stand-in for the `bedrock-runtime` client, recording what it was asked.

    Deliberately not a `Mock`: the request shape is what Req 34.2 constrains, so a fake that
    records the real
    kwargs lets a test assert on them, where a Mock would accept any shape and assert nothing.
    """

    def __init__(
        self, response: dict[str, Any] | None = None, raises: BaseException | None = None
    ) -> None:
        self.response = response or {"action": "NONE", "assessments": []}
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def apply_guardrail(self, **kwargs: object) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        if self.raises is not None:
            raise self.raises
        return self.response


def _checker(runtime: _FakeRuntime) -> ApplyGuardrailChecker:
    return ApplyGuardrailChecker(
        client=runtime, guardrail_identifier="gr-1", guardrail_version="DRAFT"
    )


# --- Req 34.2: the request shape ---------------------------------------


def test_the_adapter_satisfies_the_port() -> None:
    assert isinstance(_checker(_FakeRuntime()), GuardrailChecker)


def test_the_source_is_output() -> None:
    # Req 34.2. INPUT would check the user's utterance, which is a different control entirely —
    # and would
    # leave the generated text unverified while looking like it was checked.
    runtime = _FakeRuntime()
    _checker(runtime).check("Air quality is moderate today.")
    assert runtime.calls[0]["source"] == "OUTPUT"


def test_the_configured_identifier_and_version_are_sent() -> None:
    # Req 34.9 makes both configuration. Sending a literal would make the deployed guardrail
    # unchangeable without a code change.
    runtime = _FakeRuntime()
    _checker(runtime).check("Air quality is moderate today.")
    assert runtime.calls[0]["guardrailIdentifier"] == "gr-1"
    assert runtime.calls[0]["guardrailVersion"] == "DRAFT"


def test_the_text_is_sent_as_a_content_block() -> None:
    runtime = _FakeRuntime()
    _checker(runtime).check("Air quality is moderate today.")
    content = runtime.calls[0]["content"]
    assert content == [{"text": {"text": "Air quality is moderate today."}}], content


# --- Req 34.2b: match the success value, never its inverse ------------


def test_action_none_passes() -> None:
    runtime = _FakeRuntime({"action": "NONE", "assessments": []})
    assert _checker(runtime).check("clean text").verdict is GuardrailVerdict.PASSED


def test_action_intervened_is_an_intervention() -> None:
    runtime = _FakeRuntime({"action": "GUARDRAIL_INTERVENED", "assessments": []})
    assert _checker(runtime).check("bad text").verdict is GuardrailVerdict.INTERVENED


@pytest.mark.parametrize(
    "action",
    ["SOMETHING_NEW", "BLOCKED", "", "none", "None", "MASKED"],
    ids=["future-value", "renamed", "empty", "lowercase", "titlecase", "masked"],
)
def test_an_unrecognised_action_is_an_intervention_and_never_passed(action: str) -> None:
    # THE clause, and Req 34.2b's whole reason. `action != "GUARDRAIL_INTERVENED"` would read
    # every one of
    # these as PASSED and emit unverified health-adjacent text. Matching the success value
    # explicitly fails
    # CLOSED on anything it does not recognise.
    #
    # The casing cases matter as much as the future one: an adapter comparing case-insensitively
    # would treat
    # "none" as success, and nothing in the service model promises the casing will not change.
    runtime = _FakeRuntime({"action": action, "assessments": []})
    assert _checker(runtime).check("text").verdict is not GuardrailVerdict.PASSED


def test_a_missing_action_is_an_intervention() -> None:
    # A response with no `action` at all is not a pass. `.get()` returning None must not fall
    # through to
    # success — which is exactly what an inverted comparison would do.
    runtime = _FakeRuntime({"assessments": []})
    assert _checker(runtime).check("text").verdict is not GuardrailVerdict.PASSED


# --- Req 34.4: categories, never the text -----------------------------


def test_an_intervention_names_categories_and_never_the_text() -> None:
    # Req 34.4 counts the rejection BY CATEGORY, and Req 8.6 forbids recording the rejected
    # text. Driven
    # with a sentinel, because a phrase-coupled check only samples the property.
    runtime = _FakeRuntime(
        {
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [
                {"topicPolicy": {"topics": [{"name": "diagnosis", "action": "BLOCKED"}]}}
            ],
        }
    )
    result = _checker(runtime).check("SENTINEL-GEN-Q7X you are having an attack")
    assert "diagnosis" in result.categories
    for category in result.categories:
        assert "SENTINEL-GEN-Q7X" not in category


def test_an_intervention_with_no_assessment_still_reports_a_category() -> None:
    # Req 34.4 requires the rejection be counted by category. An intervention that reported none
    # would be
    # uncountable, so a fallback category is used rather than an empty tuple.
    runtime = _FakeRuntime({"action": "GUARDRAIL_INTERVENED", "assessments": []})
    assert _checker(runtime).check("text").categories, "an intervention must be countable"


def test_categories_are_deduplicated_and_ordered() -> None:
    # A log line must be stable across runs, and a repeated topic must not inflate a count.
    runtime = _FakeRuntime(
        {
            "action": "GUARDRAIL_INTERVENED",
            "assessments": [
                {"topicPolicy": {"topics": [{"name": "dosing"}, {"name": "diagnosis"}]}},
                {"topicPolicy": {"topics": [{"name": "dosing"}]}},
            ],
        }
    )
    categories = _checker(runtime).check("text").categories
    assert categories == tuple(sorted(set(categories)))
    assert len(categories) == len(set(categories))


# --- Req 34.2a: empty text never reaches the API ----------------------


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_guidance_passes_without_calling_the_api(text: str) -> None:
    # Req 34.2a. The failure mode being avoided is specific: the service would reject empty
    # content with a
    # ValidationException, the adapter would map that to UNAVAILABLE, and Req 34.6's fail-closed
    # path would
    # fire for a text that is trivially clean. Emptiness must not masquerade as unavailability.
    runtime = _FakeRuntime()
    result = _checker(runtime).check(text)
    assert result.verdict is GuardrailVerdict.PASSED
    assert runtime.calls == [], "the API was called for text with nothing to deny"


def test_non_empty_text_does_reach_the_api() -> None:
    # The other direction, so the short-circuit cannot be satisfied by never calling the API at
    # all.
    runtime = _FakeRuntime()
    _checker(runtime).check("a")
    assert len(runtime.calls) == 1


# --- Req 34.6: unavailability is a verdict, never an exception --------


def test_a_client_error_becomes_unavailable_and_does_not_raise() -> None:
    # Req 34.6 needs the CALLER to decide fail-closed, which it cannot do if the adapter raises.
    # And
    # UNAVAILABLE is distinct from INTERVENED on purpose: an operator must tell "the guardrail
    # stopped
    # this" from "the guardrail could not look".
    runtime = _FakeRuntime(raises=RuntimeError("throttled"))
    result = _checker(runtime).check("text")
    assert result.verdict is GuardrailVerdict.UNAVAILABLE


def test_the_unavailable_result_carries_no_exception_detail() -> None:
    # Req 21.4's reasoning applied here: a category, never a raw error body. The message could
    # name an
    # account, a region or an ARN, none of which belongs in a verdict a caller may log.
    runtime = _FakeRuntime(raises=RuntimeError("SENTINEL-ERR-Q7X account 123456789012"))
    result = _checker(runtime).check("text")
    for category in result.categories:
        assert "SENTINEL-ERR-Q7X" not in category
        assert "123456789012" not in category


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), TimeoutError("slow"), OSError("socket"), ValueError("bad")],
)
def test_every_client_failure_kind_becomes_unavailable(error: Exception) -> None:
    # Total over the failure kinds a boto3 call can raise. An adapter catching only one would
    # let the
    # others escape as exceptions, and Req 34.6's decision would never be reached.
    runtime = _FakeRuntime(raises=error)
    assert _checker(runtime).check("text").verdict is GuardrailVerdict.UNAVAILABLE


def test_a_keyboard_interrupt_is_not_swallowed() -> None:
    # The limit of the broad catch. A cancellation is not a guardrail outcome, and swallowing it
    # would make
    # the process unkillable during a turn.
    runtime = _FakeRuntime(raises=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _checker(runtime).check("text")


# --- Req 34.3: the denied topics are named ----------------------------


def test_the_denied_topic_names_cover_the_requirement() -> None:
    # Req 34.3 names diagnosis, medication administration and dosing. The names live here as
    # data so the
    # provisioning step and the category mapping cannot disagree about them.
    assert DENIED_TOPIC_NAMES == ("diagnosis", "dosing", "medication_administration")


# --- the adapter holds no credential ----------------------------------


def test_the_adapter_stores_no_credential() -> None:
    # The guardrail call is authenticated by the process's own role, not by the caller's token,
    # so there is
    # nothing credential-shaped to hold. A field for one would be a place to put it.
    checker = _checker(_FakeRuntime())
    assert not any(
        "credential" in name or "token" in name for name in vars(checker)
    ), vars(checker)


# --- Req 34.4 across all six policy types (mutation-review findings) ---


def _intervened(assessment: dict[str, Any]) -> _FakeRuntime:
    return _FakeRuntime({"action": "GUARDRAIL_INTERVENED", "assessments": [assessment]})


@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        ({"topicPolicy": {"topics": [{"name": "dosing", "action": "BLOCKED"}]}}, "dosing"),
        (
            {"contentPolicy": {"filters": [{"type": "PROMPT_ATTACK", "action": "BLOCKED"}]}},
            "content_PROMPT_ATTACK",
        ),
        (
            {"wordPolicy": {"managedWordLists": [{"type": "PROFANITY", "action": "BLOCKED"}]}},
            "word_PROFANITY",
        ),
        (
            {
                "sensitiveInformationPolicy": {
                    "piiEntities": [
                        {"type": "UK_NATIONAL_HEALTH_SERVICE_NUMBER", "action": "BLOCKED"}
                    ]
                }
            },
            "pii_UK_NATIONAL_HEALTH_SERVICE_NUMBER",
        ),
        (
            {
                "contextualGroundingPolicy": {
                    "filters": [{"type": "GROUNDING", "action": "BLOCKED"}]
                }
            },
            "grounding_GROUNDING",
        ),
    ],
    ids=["topic", "content", "managed-word", "pii", "grounding"],
)
def test_every_policy_type_yields_its_own_category(
    assessment: dict[str, Any], expected: str
) -> None:
    # A review found only `topicPolicy` being read, so an intervention from any of the other
    # five
    # policies reported NO category and fell back to `uncategorised_intervention`. Req 34.4's
    # total
    # survived but its by-category breakdown collapsed — an operator could not tell a
    # prompt-injection block from a diagnosis block. The six member names come from botocore's
    # service model.
    result = _checker(_intervened(assessment)).check("text")
    assert result.verdict is GuardrailVerdict.INTERVENED
    assert expected in result.categories, result.categories


def test_a_topic_that_was_evaluated_but_not_blocked_is_not_counted() -> None:
    # `GuardrailTopicPolicyAction` is an enum of BLOCKED and NONE, so an assessment can name a
    # topic
    # it evaluated and did not block. Counting those inflated the rejection count with reasons
    # that
    # never fired.
    runtime = _intervened(
        {
            "topicPolicy": {
                "topics": [
                    {"name": "diagnosis", "action": "BLOCKED"},
                    {"name": "dosing", "action": "NONE"},
                ]
            }
        }
    )
    assert _checker(runtime).check("text").categories == ("diagnosis",)


def test_a_custom_word_never_puts_the_matched_text_in_a_category() -> None:
    # `customWords[].match` IS the offending text, so harvesting it would breach Req 8.6 by
    # putting
    # rejected content into a category label. The entry contributes a fixed label instead.
    runtime = _intervened(
        {"wordPolicy": {"customWords": [{"match": "SENTINEL-WORD-Q7X", "action": "BLOCKED"}]}}
    )
    categories = _checker(runtime).check("text").categories
    assert "word_custom" in categories
    for category in categories:
        assert "SENTINEL-WORD-Q7X" not in category, categories


def test_a_pii_entity_never_puts_the_matched_text_in_a_category() -> None:
    # Same rule for `piiEntities[].match`, which for a PII hit is the personal data itself.
    runtime = _intervened(
        {
            "sensitiveInformationPolicy": {
                "piiEntities": [
                    {"type": "EMAIL", "match": "SENTINEL-PII-Q7X", "action": "BLOCKED"}
                ]
            }
        }
    )
    categories = _checker(runtime).check("text").categories
    assert "pii_EMAIL" in categories
    for category in categories:
        assert "SENTINEL-PII-Q7X" not in category, categories
