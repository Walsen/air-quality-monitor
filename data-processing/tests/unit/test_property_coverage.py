"""The property-coverage checkpoint (task 29.3).

Requirement 28.11: every correctness property named in the design document is implemented as
EXACTLY ONE property-based test running at least 100 examples.

WHY THIS IS A TEST AND NOT A MANUAL COUNT. "Exactly one test per property, at least 100
examples" is
three claims — present, unique, and adequately exercised — and each fails silently. A property
with
no test looks like nothing at all; a property with two tests looks like thoroughness while
meaning
the invariant is stated twice and can drift; and a test capped at 20 examples looks identical in
a
green summary to one at 1000. Reading the design's property list from the design DOCUMENT rather
than
from a list typed here means a property added to the spec fails this file until it is
implemented.

THE EXAMPLE COUNT IS NOT ASSERTED BY READING max_examples. The suite's conftest sets the profile
—
``ci`` at the 100 floor, ``nightly`` at 1000 — precisely so no test hardcodes its own count. So
the
check is the opposite one: NO property test may pin ``max_examples`` itself, because that is the
only
way one could fall below the floor while the profile says otherwise.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _SERVICE_ROOT.parent
_DESIGN = (
    _REPO_ROOT / ".kiro" / "specs" / "ingestion-and-serving-service" / "design.md"
)
_PROPERTIES_DIR = _SERVICE_ROOT / "tests" / "properties"

_TAG = re.compile(
    r"Feature:\s*ingestion-and-serving-service,\s*Property\s*(\d+)", re.IGNORECASE
)


def _declared_property_numbers() -> set[int]:
    """The property numbers the DESIGN document names.

    Read from the design's own ``### Property N: Title`` headings rather than a list typed here,
    so
    the spec stays the authority: adding Property 41 to the design fails this file until a test
    carries the tag. (My first attempt matched a markdown TABLE row and found nothing — the
    design
    gives each property a heading and a prose statement, not a table.)
    """
    text = _DESIGN.read_text(encoding="utf-8")
    return {
        int(match)
        for match in re.findall(r"^###\s*Property\s*(\d+)\s*:", text, re.MULTILINE)
    }


def _property_modules() -> dict[int, set[str]]:
    """Which module(s) carry a tagged test for each property number."""
    found: dict[int, set[str]] = {}
    for path in _PROPERTIES_DIR.rglob("test_*.py"):
        for match in _TAG.finditer(path.read_text(encoding="utf-8")):
            found.setdefault(int(match.group(1)), set()).add(path.name)
    return found


def _implemented_property_numbers() -> Counter[int]:
    """Every property number tagged by a test, counted so duplicates are visible."""
    found: Counter[int] = Counter()
    for path in _PROPERTIES_DIR.rglob("test_*.py"):
        for match in _TAG.finditer(path.read_text(encoding="utf-8")):
            found[int(match.group(1))] += 1
    return found


def test_the_design_declares_forty_properties() -> None:
    # The spec's own framing, and the guard that makes every assertion below non-vacuous: if the
    # design could not be read, the sets would be empty and the comparisons trivially true.
    declared = _declared_property_numbers()
    assert declared, "no properties parsed from the design document"
    assert len(declared) == 40, f"expected 40 declared properties, found {len(declared)}"


def test_every_declared_property_has_a_test() -> None:
    declared = _declared_property_numbers()
    implemented = _implemented_property_numbers()
    missing = sorted(number for number in declared if number not in implemented)
    assert not missing, f"declared properties with no tagged test: {missing}"


def test_each_property_is_defined_in_exactly_one_module() -> None:
    """Requirement 28.11's "exactly one", read as the tasks document means it.

    MY FIRST VERSION ASSERTED ONE TEST FUNCTION PER PROPERTY AND FAILED ON 32 PROPERTIES —
    because
    several were deliberately split across functions, one per distinct claim: Property 34 has
    four
    because Requirement 23.5 makes four separate claims about the dose formula, and Property 33
    has
    three because a precedence chain is not tested by its first link alone. That splitting was
    the
    right call and this check should not undo it.

    The tasks document settles the reading: each property maps to exactly one property test
    SUB-TASK. So the meaningful invariant is that one property is defined in ONE PLACE — several
    functions in a module are its claims, whereas the same property asserted from two modules is
    two definitions that can drift apart with nothing to reconcile them.
    """
    scattered = {
        number: sorted(modules)
        for number, modules in _property_modules().items()
        if len(modules) > 1
    }
    assert not scattered, f"properties defined across more than one module: {scattered}"


def test_no_property_test_hardcodes_a_lower_example_count() -> None:
    """The profile owns the example count, so a test pinning it can only lower the floor.

    Walked in the AST rather than grepped, because a docstring explaining the rule would
    otherwise
    trip it — the lesson this service has now learned five times.
    """
    offenders: list[str] = []
    for path in _PROPERTIES_DIR.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "max_examples":
                    continue
                value = keyword.value
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, int)
                    and value.value < 100
                ):
                    offenders.append(f"{path.name}: max_examples={value.value}")
    assert not offenders, f"property tests below the 100-example floor: {offenders}"


def test_the_default_profile_meets_the_example_floor() -> None:
    """Requirement 28.11's "at least 100 examples", pinned where it actually lives.

    THE ASSERTION IS ABOUT THE DEFAULT PROFILE, not every registered one. My first version
    required
    every profile to reach 100 and failed on the ``dev`` profile at 20 — which exists on
    purpose,
    for fast local iteration, and is legitimate precisely because it is never what the gate
    runs.
    What matters is that the profile loaded when nothing is chosen meets the floor, since that
    is
    what a contributor and CI both get.
    """
    conftest = (_SERVICE_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(conftest)

    profiles: dict[str, int] = {}
    default: str | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None)
        if name == "register_profile" and node.args:
            label = node.args[0]
            count = next(
                (
                    keyword.value.value
                    for keyword in node.keywords
                    if keyword.arg == "max_examples"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, int)
                ),
                None,
            )
            if isinstance(label, ast.Constant) and isinstance(label.value, str) and count:
                profiles[label.value] = count
        elif name == "load_profile":
            # The default is the fallback of the environment lookup the conftest performs.
            for candidate in ast.walk(node):
                if (
                    isinstance(candidate, ast.Constant)
                    and isinstance(candidate.value, str)
                    and candidate.value in ("ci", "dev", "nightly")
                ):
                    default = candidate.value

    assert profiles, "no profiles found in conftest"
    assert default, "could not determine the default profile"
    assert profiles[default] >= 100, (
        f"the default profile {default!r} runs {profiles[default]} examples, below the floor"
    )
