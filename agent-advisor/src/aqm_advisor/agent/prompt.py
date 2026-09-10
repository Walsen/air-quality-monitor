"""Loading the system prompt as data (Requirement 31.4).

The prompt lives in `prompts/system.md`, a package resource, rather than as a literal inside a
function. That is Req 31.4's point: its text is then reviewable, diffable and testable AS DATA.
A prompt built by string concatenation inside a function shows up in a diff as an unreadable
blob, and nobody reviews it — which is a problem, because the prompt is where most of this
service's safety behaviour is actually asked for.

Tests hold it to the same standards as the rest of the service: it must contain no credential
placeholder (a placeholder is an invitation to interpolate, and Req 5.2 keeps the credential out
of every prompt), it must not trip the Forbidden_Claim patterns (a prompt promising what the
verifiers reject produces turns that fail their own checks), and it must instruct digits for
numerals — the documented mitigation for grounding's digit-only limit.
"""

from __future__ import annotations

from importlib import resources

DEFAULT_SYSTEM_PROMPT_RESOURCE = "system.md"
"""The packaged prompt file. A `.md` so a reviewer, and a diff, read it as prose."""

_PACKAGE = "aqm_advisor.agent.prompts"


def load_system_prompt(configured: str | None = None) -> str:
    """Return the system prompt, preferring a configured one over the packaged default.

    A configured prompt REPLACES the default rather than extending it, matching how Req 8.7
    treats the Forbidden_Claim patterns: a deployment that needs different wording needs to be
    able to remove the default's, and an extend-only mechanism cannot do that.

    Raises:
        ValueError: if a configured prompt is blank. Running with no instructions would look
        like a model
            problem rather than a configuration one, and would be diagnosed in the wrong place
            for hours.
    """
    if configured is not None:
        if not configured.strip():
            raise ValueError("a configured system prompt must not be blank")
        return configured
    return (
        resources.files(_PACKAGE)
        .joinpath(DEFAULT_SYSTEM_PROMPT_RESOURCE)
        .read_text(encoding="utf-8")
    )


__all__ = ["DEFAULT_SYSTEM_PROMPT_RESOURCE", "load_system_prompt"]
