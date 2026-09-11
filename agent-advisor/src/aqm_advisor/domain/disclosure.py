"""Detecting self-disclosure (Requirement 18.4), and why the rest of Req 18 is not a detector.

**Reqs 18.1 and 18.2 are deliberately NOT implemented as detection here.** You cannot reliably
recognise an instruction hidden in prose, and a service that believed it could would be trusting
a filter that fails silently. Compliance is instead made impossible to express:

* **Req 18.3** — the output checks run unconditionally. Nothing in an utterance can switch
  them off, because the verification ledger starts unverified and `TurnPipeline.run` refuses
  to assemble without a positive verdict. A successful injection therefore still cannot
  produce a Forbidden_Claim; it can only produce a withheld turn.
* **Req 18.5** — retrieved data reaches the model as `json.dumps` output, and JSON escapes
  newlines. A field carrying a fake turn boundary arrives as the two characters backslash-n,
  not a line break. That is a guarantee from the encoder rather than a filter this service
  has to get right.

**Req 18.4 is the clause that needs a real check, and over-breadth is the trap.** The system
prompt talks about asthma, emergencies, particulates and relievers — so any naive overlap test
against it would reject every legitimate answer, which is the `you have` defect all over again.

The signal is therefore a VERBATIM SPAN: eight or more consecutive words shared with the
protected text. Quoting a run of an instruction is disclosure; sharing vocabulary is not.
Comparison is on lowercased, whitespace-collapsed words, so a reflowed or recapitalised
quotation does not slip through.

**Protected text is matched LITERALLY, never compiled.** The Forbidden_Claim patterns are
regexes, and compiling one to search a generation would make this report a disclosure whenever
the generation merely MATCHED the pattern — so an answer describing symptoms would be flagged as
having revealed the rule. The patterns are compared as plain word sequences.

**A finding never quotes what leaked.** These strings reach logs, and a log echoing the system
prompt has disclosed it a second time — the same reasoning as Req 21.8's field-name-only
warning.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

MIN_VERBATIM_WORDS = 8
"""How many consecutive shared words count as a quotation rather than a coincidence.

Chosen for this domain, not in general. The vocabulary here is narrow — a legitimate answer and
the system prompt both talk about relievers, particulates and emergency care — so a short run is
expected and only a long one is evidence. Lower it and ordinary guidance becomes unpublishable;
raise it and a short instruction can be recited whole.
"""

_WORDS = re.compile(r"[a-z0-9']+")


def required_texts() -> tuple[str, ...]:
    """Every text the service is OBLIGED to emit, and which therefore cannot be disclosure.

    Assembled here so a caller cannot forget the exemption: `reveals_configuration` without it
    would withhold a turn for correctly explaining the particulate lag, because the system
    prompt asks for that exact wording.

    Imported lazily inside the function to keep the module-level import graph acyclic —
    `reporting` and `deference` are peers in `domain/`, and a top-level import here would couple
    three modules together for the sake of one list.
    """
    from aqm_advisor.domain.attribution import unavailable_text
    from aqm_advisor.domain.deference import (
        CLINICIAN_SUGGESTION_TEXT,
        DEFERENCE_TEXT,
    )
    from aqm_advisor.domain.reporting import (
        GASEOUS_SAME_DAY_TEXT,
        PARTICULATE_LAG_TEXT,
        TIMING_TEMPLATES,
    )
    from aqm_advisor.domain.snapshot import DEFAULT_PROFILE_TEXT

    return (
        PARTICULATE_LAG_TEXT,
        GASEOUS_SAME_DAY_TEXT,
        DEFERENCE_TEXT,
        CLINICIAN_SUGGESTION_TEXT,
        DEFAULT_PROFILE_TEXT,
        unavailable_text("ozone"),
        *TIMING_TEMPLATES,
    )


def _normalised_words(text: str) -> list[str]:
    """Lowercased word tokens, so reflowing or recapitalising a quotation does not hide it."""
    return _WORDS.findall(text.casefold())


def verbatim_spans(
    text: str, protected: str, *, min_words: int = MIN_VERBATIM_WORDS
) -> tuple[str, ...]:
    """The word runs of at least `min_words` that appear in BOTH texts.

    Returns the runs as they appear in `text`, for the caller to count. The disclosure check
    deliberately does not pass them on: they are the protected text.
    """
    left = _normalised_words(text)
    right = _normalised_words(protected)
    if len(left) < min_words or len(right) < min_words:
        return ()

    right_runs = {
        " ".join(right[index : index + min_words])
        for index in range(len(right) - min_words + 1)
    }
    found: list[str] = []
    for index in range(len(left) - min_words + 1):
        run = " ".join(left[index : index + min_words])
        if run in right_runs and run not in found:
            found.append(run)
    return tuple(found)


def reveals_configuration(
    text: str,
    *,
    protected: Iterable[str] | Sequence[str],
    exempt: Iterable[str] = (),
) -> tuple[str, ...]:
    """Report whether a generation quotes its own instructions or configuration (Req 18.4).

    **`exempt` is not a convenience: it is required for correctness.** The system prompt states
    the particulate-lag and gaseous-same-day explanations in the SAME WORDS the service is
    obliged to emit (`PARTICULATE_LAG_TEXT`, `GASEOUS_SAME_DAY_TEXT`), because the prompt asks
    for that wording. Without an exemption, an answer correctly explaining the three-day lag
    would be reported as having disclosed the system prompt, and the turn would be withheld —
    the `you have` defect exactly, where a check keyed on the words rather than the act makes a
    required text unpublishable. A test caught this before it shipped.

    So a span is disclosure only when it appears in a protected text and in NO exempt text.
    Quoting the "Hard limits" section is disclosure; quoting the lag sentence is the service
    doing its job.

    Each finding names WHICH protected item was quoted and how long the run was — never the run
    itself, and never the protected text. A log echoing the system prompt would disclose it a
    second time.
    """
    exempt_runs: set[str] = set()
    for allowed in exempt:
        words = _normalised_words(allowed)
        for index in range(max(0, len(words) - MIN_VERBATIM_WORDS + 1)):
            exempt_runs.add(" ".join(words[index : index + MIN_VERBATIM_WORDS]))

    findings: list[str] = []
    for index, item in enumerate(protected):
        spans = tuple(
            span for span in verbatim_spans(text, item) if span not in exempt_runs
        )
        if spans:
            findings.append(
                f"protected item {index}: a run of {len(spans[0].split())} "
                "consecutive words was quoted"
            )
    return tuple(findings)


__all__ = [
    "MIN_VERBATIM_WORDS",
    "required_texts",
    "reveals_configuration",
    "verbatim_spans",
]
