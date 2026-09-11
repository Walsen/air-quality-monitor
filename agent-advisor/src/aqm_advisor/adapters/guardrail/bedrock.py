"""The `ApplyGuardrail` adapter: Bedrock Guardrails as the independent output check (Req 34).

**THIS IS THE AUTHORITY, NOT THE FAST PATH.** `domain/forbidden.py`'s pattern set is a cheap
deterministic
pre-filter whose docstring records that it caught 6 of 77 plausible forbidden claims before
widening, and that the
remaining ceiling is the method's rather than the tuning's. Req 34.5 runs both and makes neither
conditional on the
other: the local check runs offline and when Bedrock is unreachable, and this one catches
phrasings no pattern
anticipated.

**EVERY API FACT BELOW WAS READ FROM BOTOCORE'S SERVICE MODEL.** `ApplyGuardrailRequest`
requires
`guardrailIdentifier`, `guardrailVersion`, `source` and `content`; `source` is an enum of
exactly `INPUT` and
`OUTPUT`; `GuardrailTextBlock.text` is a plain string with NO minimum length; and
`GuardrailAction` carries exactly
two values, `NONE` and `GUARDRAIL_INTERVENED`.

**THE SUCCESS VALUE IS MATCHED EXPLICITLY (Req 34.2b).** With two actions, `action == "NONE"`
and
`action != "GUARDRAIL_INTERVENED"` are equivalent today. They stop being equivalent the day AWS
adds a third value,
and they fail in OPPOSITE directions: the inverted test reads an unrecognised action as passed
and emits unverified
health-adjacent text. For a safety control the only acceptable default is closed, so anything
not recognised as
success is an intervention.

**EMPTY TEXT NEVER REACHES THE API (Req 34.2a).** Not because botocore rejects it — the model
puts no minimum on
`text`, so it does not — but because the service's own rejection would arrive as an exception,
this adapter would map
it to `UNAVAILABLE`, and Req 34.6's fail-closed path would fire for a text that is trivially
clean. Emptiness must
not masquerade as unavailability. An empty generation is Req 6.5's problem.

**UNAVAILABILITY IS A VERDICT, NEVER AN EXCEPTION (Req 34.6).** The requirement's fail-closed
decision is
CONDITIONAL — it applies "for any generation the local check cannot clear" — so the decision
belongs to the caller
that knows both results. An adapter that raised would take that decision away by crashing the
turn.
"""

from __future__ import annotations

from typing import Any, Protocol

from aqm_advisor.ports.protocols import GuardrailResult, GuardrailVerdict

DENIED_TOPIC_NAMES: tuple[str, ...] = ("diagnosis", "dosing", "medication_administration")
"""Req 34.3's Denied Topics, as data.

Held here rather than only in the provisioning template so the topic names and the category
mapping cannot disagree:
a topic renamed in one place and not the other would produce interventions this service could
not categorise, and
Req 34.4 requires the rejection be counted by category.
"""

_SUCCESS_ACTION = "NONE"
"""The ONLY action that yields a pass. See Req 34.2b and this module's docstring."""

_UNCATEGORISED = "uncategorised_intervention"
"""The category for an intervention whose assessment named none.

An intervention must be countable (Req 34.4), so an empty tuple is not an option: it would make
the rejection
invisible to the metric that exists to count it.
"""

_UNAVAILABLE_CATEGORY = "guardrail_unavailable"
"""The category for a failed call.

A CATEGORY, never the exception's message. That message can name an account, a region or an ARN,
none of which
belongs in a verdict a caller may log — the same reasoning Req 21.4 applies to a serving
failure.
"""


class _GuardrailRuntime(Protocol):
    """The one method this adapter needs from `bedrock-runtime`.

    Narrow by design (Interface Segregation): the adapter is handed something with
    `apply_guardrail` rather than a
    whole boto3 client, so a test can supply a fake that records the request shape — which is
    the part Req 34.2
    constrains and therefore the part that can be wrong.
    """

    def apply_guardrail(
        self,
        *,
        guardrailIdentifier: str,  # noqa: N803 - the AWS wire name
        guardrailVersion: str,  # noqa: N803
        source: str,
        content: list[dict[str, dict[str, str]]],
    ) -> dict[str, object]:
        """Evaluate text against a guardrail.

        Spelled with the AWS wire names because boto3 takes them literally as keyword arguments;
        renaming them here would make the call fail at runtime while reading more naturally.
        """
        ...


class ApplyGuardrailChecker:
    """Bedrock Guardrails as a `GuardrailChecker` (Reqs 34.2, 34.2a, 34.2b, 34.4, 34.6).

    The client is INJECTED rather than constructed here, so this class holds no session, no
    region resolution and
    no credential — the call is authenticated by the process's own role, not by the caller's
    token, so there is
    nothing credential-shaped for it to hold and no field in which to hold one.
    """

    def __init__(
        self,
        *,
        client: _GuardrailRuntime,
        guardrail_identifier: str,
        guardrail_version: str,
    ) -> None:
        """Record the client and the configured guardrail (Req 34.9)."""
        self._client = client
        self._guardrail_identifier = guardrail_identifier
        self._guardrail_version = guardrail_version

    def check(self, text: str) -> GuardrailResult:
        """Return the verdict for one generation. Never raises for an intervention or a failure.

        Req 34.2a short-circuits empty or whitespace-only text before the call. Req 34.6 turns
        any client
        failure into `UNAVAILABLE` so the CALLER can apply the conditional fail-closed rule,
        which needs the
        local check's result too and is therefore not this adapter's decision to make.
        """
        if not text.strip():
            return GuardrailResult(verdict=GuardrailVerdict.PASSED)
        try:
            response = self._client.apply_guardrail(
                guardrailIdentifier=self._guardrail_identifier,
                guardrailVersion=self._guardrail_version,
                source="OUTPUT",
                content=[{"text": {"text": text}}],
            )
        except (Exception, TimeoutError):
            # A BROAD catch, deliberately, and bounded: `BaseException` is NOT caught, so a
            # `KeyboardInterrupt` or `SystemExit` still propagates and the process stays
            # killable during a
            # turn. Every other failure is one Req 34.6 has an answer for, and enumerating
            # boto3's error
            # taxonomy here would let an unlisted one escape as an exception — at which point
            # the
            # requirement's decision is never reached and the turn crashes instead of degrading.
            return GuardrailResult(
                verdict=GuardrailVerdict.UNAVAILABLE,
                categories=(_UNAVAILABLE_CATEGORY,),
            )
        return _verdict_for(response)


def _verdict_for(response: dict[str, Any]) -> GuardrailResult:
    """Map a guardrail response to a verdict (Req 34.2b).

    Matches the success value explicitly. Everything else — an unrecognised action, a
    differently cased one, a
    response with no action at all — is an intervention, because for a safety control the only
    acceptable
    default is closed.
    """
    if response.get("action") == _SUCCESS_ACTION:
        return GuardrailResult(verdict=GuardrailVerdict.PASSED)
    return GuardrailResult(
        verdict=GuardrailVerdict.INTERVENED,
        categories=_categories_for(response),
    )


def _categories_for(response: dict[str, Any]) -> tuple[str, ...]:
    """The categories an intervention fired on, deduplicated and ordered (Req 34.4).

    READS ALL SIX POLICY TYPES, not just topics. A review found the first version reading only
    `topicPolicy`, so an intervention from a content filter, a word list, a PII rule or
    contextual
    grounding produced no category and fell back to `uncategorised_intervention`: the Req 34.4
    count survived, but its by-category breakdown silently collapsed and an operator could not
    tell
    a prompt-injection block from a diagnosis block. The six member names were read from
    botocore's
    service model, not guessed.

    NEVER HARVESTS A `match` FIELD. `customWords[].match` and `piiEntities[].match` hold THE
    OFFENDING TEXT, so recording one would put rejected content into a category label and breach
    Req 8.6. Types and operator-configured names only.

    Sorted and deduplicated so a log line is stable across runs and a repeated category cannot
    inflate a count. Falls back to a single uncategorised entry rather than an empty tuple,
    because
    an intervention reporting no category would be invisible to the metric Req 34.4 feeds.
    """
    found: set[str] = set()
    for assessment in response.get("assessments") or ():
        if not isinstance(assessment, dict):
            continue
        found.update(_harvest(assessment))
    return tuple(sorted(found)) or (_UNCATEGORISED,)


def _blocked(entry: object) -> bool:
    """Whether one policy entry actually FIRED, rather than merely being evaluated.

    `action == "BLOCKED"` is the positive test. `detected` alone is not enough: every policy
    entry
    carries both, and `GuardrailTopicPolicyAction` is an enum of `BLOCKED` and `NONE`, so an
    assessment can name a topic it evaluated and did not block. Counting those inflated the Req
    34.4
    rejection count with reasons that never fired.
    """
    return isinstance(entry, dict) and entry.get("action") == "BLOCKED"


def _entries(assessment: dict[str, Any], policy: str, field: str) -> list[object]:
    """One policy's entry list, or empty when the response does not carry it."""
    holder = assessment.get(policy)
    if not isinstance(holder, dict):
        return []
    entries = holder.get(field)
    return list(entries) if isinstance(entries, list) else []


def _labels(
    assessment: dict[str, Any], policy: str, field: str, *, key: str, prefix: str
) -> list[str]:
    """Blocked entries' `key` values, prefixed so two policies cannot collide on one name."""
    out: list[str] = []
    for entry in _entries(assessment, policy, field):
        if not _blocked(entry) or not isinstance(entry, dict):
            continue
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            out.append(f"{prefix}{value}")
    return out


def _harvest(assessment: dict[str, Any]) -> list[str]:
    """Every blocked category in one assessment, across all six policy types."""
    out: list[str] = []
    out.extend(_labels(assessment, "topicPolicy", "topics", key="name", prefix=""))
    out.extend(
        _labels(assessment, "contentPolicy", "filters", key="type", prefix="content_")
    )
    out.extend(
        _labels(
            assessment, "wordPolicy", "managedWordLists", key="type", prefix="word_"
        )
    )
    # `customWords` entries expose only `match`, which IS the offending text, so they contribute
    # a
    # FIXED label rather than anything drawn from the entry (Req 8.6).
    out.extend(
        "word_custom"
        for entry in _entries(assessment, "wordPolicy", "customWords")
        if _blocked(entry)
    )
    out.extend(
        _labels(
            assessment,
            "sensitiveInformationPolicy",
            "piiEntities",
            key="type",
            prefix="pii_",
        )
    )
    # A regex rule's `name` is operator-configured, so it is a category; its `match` is not.
    out.extend(
        _labels(
            assessment,
            "sensitiveInformationPolicy",
            "regexes",
            key="name",
            prefix="regex_",
        )
    )
    out.extend(
        _labels(
            assessment,
            "contextualGroundingPolicy",
            "filters",
            key="type",
            prefix="grounding_",
        )
    )
    return out


__all__ = [
    "DENIED_TOPIC_NAMES",
    "ApplyGuardrailChecker",
]
