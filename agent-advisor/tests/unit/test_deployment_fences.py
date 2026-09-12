"""Service-wide structural fences for the deployment boundary. Reqs 32.2 and 21.5.

Both tests here are about WHERE something is allowed to appear, which no single module can
assert
about itself. `tests/unit/test_boundary.py` already proves `agent/boundary.py` contains no broad
catch;
this file proves the other half — that the one broad catch lives at the entrypoint and nowhere
else —
and adds Req 32.2's import fence.

Each fence carries a self-check that the detector would fire on planted code, because a
structural
test that silently matches nothing is worse than no test: it certifies.
"""

from __future__ import annotations

import ast
import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "aqm_advisor"
_BOUNDARY = _SRC / "agentcore"


def _modules() -> list[pathlib.Path]:
    return sorted(p for p in _SRC.rglob("*.py") if "__pycache__" not in p.parts)


# --- Req 32.2: AgentCore types live only at the deployment boundary ---


def _imports_agentcore(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "bedrock_agentcore" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root == "bedrock_agentcore":
                return True
    return False


def test_only_the_deployment_boundary_imports_agentcore() -> None:
    # Req 32.2: no domain or advisory component imports an AgentCore type, so the offline suite
    # is
    # unaffected by the deploy target and the advisory logic runs without the SDK at all.
    # Asserted
    # structurally because an import is exactly the kind of thing added casually.
    offenders = [
        str(path.relative_to(_SRC))
        for path in _modules()
        if _BOUNDARY not in path.parents and _imports_agentcore(ast.parse(path.read_text()))
    ]
    assert not offenders, f"AgentCore imported outside agentcore/: {offenders}"


def test_the_boundary_really_does_import_agentcore() -> None:
    # Non-vacuity for the fence above: if nothing imported the SDK, the test would pass while
    # proving
    # nothing. This pins that the exemption is USED, so the fence is measuring a real boundary.
    boundary = [
        path
        for path in _modules()
        if _BOUNDARY in path.parents and _imports_agentcore(ast.parse(path.read_text()))
    ]
    assert boundary, "no module under agentcore/ imports the SDK; the fence proves nothing"


def test_the_agentcore_import_detector_would_find_a_planted_one() -> None:
    for source in (
        "import bedrock_agentcore",
        "import bedrock_agentcore.runtime",
        "from bedrock_agentcore.runtime import BedrockAgentCoreApp",
        "from bedrock_agentcore import x",
    ):
        assert _imports_agentcore(ast.parse(source)), source
    assert not _imports_agentcore(ast.parse("from aqm_advisor.domain import models"))


# --- Req 21.5: no BARE catch, and every broad one is justified --------


_BROAD = {"Exception", "BaseException"}

_JUSTIFIED_BROAD_CATCHES: dict[str, str] = {
    "agent/generation.py": (
        "an unrecognised model failure becomes the MODEL_FAILED outcome, so no text is emitted"
    ),
    "agent/verification.py": (
        "a verifier that raises records passed=False with only the exception TYPE, so a broken "
        "verifier cannot pass a generation"
    ),
    "adapters/guardrail/bedrock.py": (
        "Req 34.6 needs an UNAVAILABLE VERDICT rather than a raise, because the conditional "
        "fail-closed decision belongs to the caller that knows the local check's result too"
    ),
    "agentcore/app.py": (
        "Req 21.5's one true top-level boundary: converts any failure into a "
        "response, "
        "because the SDK would otherwise answer 500 and AgentCore would surface an opaque 424"
    ),
}
"""Where a broad catch is allowed, and the reason each earns it.

MY FIRST VERSION OF THIS FENCE BANNED BROAD CATCHES OUTSIDE THE ENTRYPOINT, AND ITS PREMISE WAS
WRONG. Req 21.5 forbids a BARE catch (`except:`) outside a top-level boundary; a typed-but-broad
`except Exception` that converts an unknown failure into an explicit FAIL-CLOSED outcome is the
pattern this codebase uses deliberately, in three places that each document why. The fence would
have
forced correct code to be rewritten to satisfy a misreading.

So the enforceable rule is an allowlist: a NEW broad catch fails this test until someone adds it
here
with a justification, which is the review conversation the requirement actually wants.
"""


def _bare_catches(tree: ast.AST) -> int:
    """Truly bare `except:` handlers — what Req 21.5 names."""
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    )


def _is_broad(caught: ast.expr | None) -> bool:
    """Whether a handler's caught type is broad: bare, a broad name, or a tuple with one."""
    if caught is None:
        return True
    if isinstance(caught, ast.Name):
        return caught.id in _BROAD
    if isinstance(caught, ast.Tuple):
        return any(isinstance(e, ast.Name) and e.id in _BROAD for e in caught.elts)
    return False


def _broad_catches(tree: ast.AST) -> int:
    """Handlers catching Exception/BaseException, by name or in a tuple, plus bare ones."""
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and _is_broad(node.type)
    )


def test_no_module_uses_a_bare_except() -> None:
    # The literal Req 21.5 prohibition. Even the top-level boundary does not need one: it
    # catches
    # `Exception` by name, which excludes cancellation and interpreter shutdown.
    offenders = {
        str(path.relative_to(_SRC)): _bare_catches(ast.parse(path.read_text()))
        for path in _modules()
    }
    assert not {k: v for k, v in offenders.items() if v}, offenders


def test_every_broad_catch_is_on_the_justified_list() -> None:
    # A new broad catch fails here until it is added above WITH a reason. That is the point: the
    # fence
    # forces the justification to be written down rather than assumed.
    found = {
        str(path.relative_to(_SRC))
        for path in _modules()
        if _broad_catches(ast.parse(path.read_text()))
    }
    unjustified = found - set(_JUSTIFIED_BROAD_CATCHES)
    assert not unjustified, f"broad catch with no recorded justification: {unjustified}"


def test_the_justified_list_has_no_stale_entries() -> None:
    # The other direction, so the list cannot rot into a permanent exemption for code that no
    # longer
    # has a broad catch — which is how an allowlist stops meaning anything.
    found = {
        str(path.relative_to(_SRC))
        for path in _modules()
        if _broad_catches(ast.parse(path.read_text()))
    }
    stale = set(_JUSTIFIED_BROAD_CATCHES) - found
    assert not stale, f"listed as justified but has no broad catch: {stale}"


def test_the_entrypoint_has_exactly_one_broad_catch() -> None:
    # One, not zero and not several. Zero would mean Req 32.5's guarantee is missing; several
    # would
    # mean the boundary has grown extra swallowing points that could hide a failure from the
    # log.
    #
    # Task 11.2 deferred this assertion here deliberately: `agent/boundary.py` holds the handler
    # but
    # not the catch, so the placement claim could not be made until the entrypoint existed.
    total = sum(
        _broad_catches(ast.parse(path.read_text()))
        for path in _modules()
        if _BOUNDARY in path.parents
    )
    assert total == 1, f"expected exactly one broad catch at the boundary, found {total}"


def test_the_broad_catch_detector_would_find_each_planted_shape() -> None:
    # Four shapes, because catching only `except Exception:` by name would miss the others and
    # the
    # fence would read as passing.
    for source in (
        "try:\n    x()\nexcept:\n    pass",
        "try:\n    x()\nexcept Exception:\n    pass",
        "try:\n    x()\nexcept BaseException:\n    pass",
        "try:\n    x()\nexcept (ValueError, Exception):\n    pass",
    ):
        assert _broad_catches(ast.parse(source)) == 1, source
    assert _broad_catches(ast.parse("try:\n    x()\nexcept ValueError:\n    pass")) == 0
    assert _bare_catches(ast.parse("try:\n    x()\nexcept:\n    pass")) == 1
    assert _bare_catches(ast.parse("try:\n    x()\nexcept Exception:\n    pass")) == 0
