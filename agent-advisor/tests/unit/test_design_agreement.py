"""Guard the design document's model blocks against drifting from the implementation.

This exists because that drift actually happened. `design.md` specified
`utterance: str = Field(min_length=1, max_length=4000)` while the implementation deliberately
carries none — Req 1.5 makes the maximum configured, so a pinned constraint would be a second
authority. The document and the code disagreed, and nothing failed.

A design document is not executable, so it can only be checked by reading it. This reads it:
every Python fence in `design.md` is parsed, and for each class that IS implemented, the field
names must match. A class the design describes but nobody has built yet is skipped by name, so
the guard does not block the spec running ahead of the code — the normal state of SDD.

What this deliberately does NOT check is types, defaults or method bodies. Those are prose-level
design decisions where the document is allowed to be looser than the code
(`def confirm(self) -> Self: ...`), and asserting over them would make the guard fire on every
legitimate elaboration.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import aqm_advisor.domain.models as models_module
import aqm_advisor.domain.records as records_module

_DESIGN = (
    pathlib.Path(__file__).resolve().parents[3]
    / ".kiro"
    / "specs"
    / "agent-advisor-service"
    / "design.md"
)

_NOT_YET_IMPLEMENTED = frozenset(
    {
        # Described in the design, built in a later task. Named explicitly so the guard
        # cannot go quiet by accident: a class escapes only while it is on this list.
        "AdvisorConfig",
        "RedFlagMatcher",
        "GroundingReport",
        "ActionRegistry",
    }
)


def _design_classes() -> dict[str, set[str]]:
    """Field names per class, read from every Python fence in the design document."""
    text = _DESIGN.read_text(encoding="utf-8")
    found: dict[str, set[str]] = {}
    inside = False
    block: list[str] = []
    for line in text.split("\n"):
        if line.startswith("```python"):
            inside, block = True, []
            continue
        if inside and line.startswith("```"):
            inside = False
            try:
                tree = ast.parse("\n".join(block))
            except SyntaxError:
                # A fence showing an excerpt rather than whole source. Skipped rather
                # than failed: the design is prose and may legitimately elide.
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    found[node.name] = {
                        item.target.id
                        for item in node.body
                        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
                    }
            continue
        if inside:
            block.append(line)
    return found


def _implemented_classes() -> dict[str, set[str]]:
    """Field names per implemented model, from the real classes."""
    implemented: dict[str, set[str]] = {}
    for module in (models_module, records_module):
        for name in getattr(module, "__all__", []):
            obj = getattr(module, name, None)
            if isinstance(obj, type):
                fields = getattr(obj, "model_fields", None)
                if fields is not None:
                    implemented[name] = set(fields)
                elif hasattr(obj, "__dataclass_fields__"):
                    implemented[name] = set(obj.__dataclass_fields__)
    return implemented


def test_the_design_document_is_present() -> None:
    # If the path were wrong every comparison below would silently find nothing to compare.
    assert _DESIGN.is_file(), f"design not found at {_DESIGN}"


def test_the_design_declares_model_classes_this_guard_can_read() -> None:
    # Non-vacuity: a fence-parsing bug would leave the mapping empty and every
    # comparison below would pass.
    declared = _design_classes()
    assert len(declared) >= 8, f"only parsed {sorted(declared)}"
    assert "AdvisoryRequest" in declared


def test_the_guard_compares_at_least_the_models_already_built() -> None:
    overlap = set(_design_classes()) & set(_implemented_classes())
    assert len(overlap) >= 6, f"only comparing {sorted(overlap)}"


@pytest.mark.parametrize(
    "class_name",
    sorted(set(_design_classes()) & set(_implemented_classes())),
)
def test_a_designed_model_matches_its_implementation(class_name: str) -> None:
    designed = _design_classes()[class_name]
    implemented = _implemented_classes()[class_name]
    assert designed == implemented, (
        f"{class_name} has drifted between design.md and the code. "
        f"in the design only: {sorted(designed - implemented)}; "
        f"in the code only: {sorted(implemented - designed)}"
    )


def test_every_implemented_model_appears_in_the_design() -> None:
    # The other direction: a model built without being designed is undocumented, and
    # the design is what a reviewer reads.
    missing = set(_implemented_classes()) - set(_design_classes()) - _NOT_YET_IMPLEMENTED
    assert missing == set(), f"implemented but absent from design.md: {sorted(missing)}"


def test_the_design_carries_no_competing_utterance_maximum() -> None:
    # The specific drift this guard was written for. Req 1.5 makes the maximum configured, so a
    # `max_length` in the design would put the document back at odds with the code.
    fences = _DESIGN.read_text(encoding="utf-8")
    inside = False
    for line in fences.split("\n"):
        if line.startswith("```python"):
            inside = True
            continue
        if inside and line.startswith("```"):
            inside = False
            continue
        if inside and "utterance" in line:
            assert "max_length" not in line, (
                f"a pinned utterance maximum is back: {line.strip()!r}"
            )
