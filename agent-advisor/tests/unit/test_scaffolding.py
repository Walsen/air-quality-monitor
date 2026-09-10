"""Tests for the project scaffolding and its pinned dependencies (task 1.1).

These are not ceremony. Three of them pin facts the rest of the design rests on, and one of them
already caught the design being wrong:

``test_the_model_abc_has_exactly_these_abstract_methods`` is the important one. Design
decision DD2 says the Strands ``Model`` abstract class IS the Model_Port, with no wrapper —
which means the scripted offline model must satisfy the REAL abstract surface. The research
note behind DD2 recorded only ``stream()``. The installed SDK has FOUR abstract methods, so a
fake implementing one could not be instantiated at all and the entire offline suite would be
unbuildable. This test states the actual surface and fails if an SDK upgrade changes it.
"""

from __future__ import annotations

import abc
import ast
import inspect
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _ROOT / "pyproject.toml"


def _manifest() -> dict[str, object]:
    return tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))


def _dependencies() -> list[str]:
    project = _manifest()["project"]
    assert isinstance(project, dict)
    deps = project["dependencies"]
    assert isinstance(deps, list)
    return [str(entry) for entry in deps]


# --- Req 26.3: every direct dependency pinned exactly -------------------

def test_every_direct_dependency_is_pinned_to_one_exact_version() -> None:
    for entry in _dependencies():
        assert "==" in entry, f"{entry!r} is not pinned to an exact version"
        assert not any(
            loose in entry for loose in (">=", "<=", "~=", ">", "<", "*")
        ), f"{entry!r} carries a range rather than a pin"


def test_every_dev_dependency_is_pinned_too() -> None:
    groups = _manifest()["dependency-groups"]
    assert isinstance(groups, dict)
    for entry in groups["dev"]:
        assert "==" in str(entry), f"{entry!r} is not pinned"


def test_the_lockfile_is_committed() -> None:
    # Req 26.3's other half: a pin without a lock still lets a transitive dependency drift.
    assert (_ROOT / "uv.lock").is_file()


def test_the_python_version_is_pinned_to_the_projects_interpreter() -> None:
    project = _manifest()["project"]
    assert isinstance(project, dict)
    assert project["requires-python"] == "==3.12.*"


# --- A9: the tool surface is deliberately narrow -------------------------

def test_the_general_purpose_tool_library_is_not_a_dependency() -> None:
    # Assumption A9. `strands-agents-tools` would add file-system and shell tools that a health
    # advisor has no use for, and the reachable tool set is a security property here. This
    # is the test that stops it arriving later as a convenience.
    joined = " ".join(_dependencies())
    assert "strands-agents-tools" not in joined


def test_strands_is_installed_with_the_otel_extra() -> None:
    # Requirement 24 wires observability through the framework's own instrumentor rather than a
    # hand-rolled one, which is what the extra provides.
    assert any(entry.startswith("strands-agents[otel]==") for entry in _dependencies())


# --- DD2: the Model ABC is the port, so its real surface matters ---------

def test_the_model_abc_has_exactly_these_abstract_methods() -> None:
    from strands.models import Model

    assert Model.__abstractmethods__ == frozenset(
        {"update_config", "get_config", "structured_output", "stream"}
    ), (
        "DD2 makes the Strands Model ABC the injected port, so the scripted offline model must "
        "implement this exact surface. An SDK upgrade changing it breaks the offline suite, "
        "which is why this is pinned rather than discovered at runtime."
    )


def test_the_model_abc_is_genuinely_abstract() -> None:
    # If Model were instantiable, a fake that forgot a method would silently be a live model.
    from strands.models import Model

    assert isinstance(Model, abc.ABCMeta)
    with pytest.raises(TypeError):
        Model()  # type: ignore[abstract]


def test_stream_accepts_the_arguments_the_pipeline_will_pass() -> None:
    from strands.models import Model

    parameters = inspect.signature(Model.stream).parameters
    for expected in ("messages", "tool_specs", "system_prompt", "tool_choice"):
        assert expected in parameters, f"stream() has no {expected!r} parameter"


def test_structured_output_is_part_of_the_abstract_surface() -> None:
    # Req 6.3b obtains the response's structured fields through Strands structured output.
    # Because it is ABSTRACT the scripted model must implement it too — a fake that only
    # scripted stream() could not be instantiated at all.
    from strands.models import Model

    assert "structured_output" in Model.__abstractmethods__


# --- Req 32.2: the deployment SDK is importable and fenced --------------

def test_the_agentcore_app_is_importable_without_aws() -> None:
    # Req 26.5a rests on this: app.run() serves /invocations and /ping locally with no AWS,
    # so the deployment contract is assertable in the offline suite.
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    assert callable(BedrockAgentCoreApp)


def _modules_importing(root: Path, target: str) -> list[str]:
    """Modules under ``root`` with a real import of ``target``, found via the AST.

    An AST walk rather than a substring search, because a docstring that DOCUMENTS this rule
    mentions the module name in prose — and a text scan flags it, which is precisely how the
    first version of this check failed. That has now happened three times in this repository, so
    the rule is: a check about code inspects the syntax tree, never the characters.
    """
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        if path.parent.name == "agentcore":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name == target or name.startswith(f"{target}.") for name in names):
                offenders.append(str(path.relative_to(root.parents[1])))
                break
    return offenders


def test_only_the_agentcore_package_may_import_the_deployment_sdk() -> None:
    # Req 32.2. Asserted over real source rather than trusted: this is the rule that keeps the
    # advisory turn runnable without the runtime.
    offenders = _modules_importing(_ROOT / "src" / "aqm_advisor", "bedrock_agentcore")
    assert offenders == [], f"bedrock_agentcore imported outside agentcore/: {offenders}"


def test_the_import_detector_would_actually_catch_an_import(tmp_path: Path) -> None:
    # Proves the DETECTOR detects. Without this the check above passes just as happily against a
    # walk that never matches anything, and would keep passing if the AST logic broke.
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "innocent.py").write_text(
        '"""Mentions bedrock_agentcore in prose only."""\n', encoding="utf-8"
    )
    (package / "guilty.py").write_text(
        "from bedrock_agentcore.runtime import BedrockAgentCoreApp\n", encoding="utf-8"
    )
    found = _modules_importing(package, "bedrock_agentcore")
    assert len(found) == 1, f"expected exactly the guilty module, got {found}"
    assert "guilty.py" in found[0]
    assert "innocent.py" not in " ".join(found), (
        "a prose mention must not be flagged — the bug this detector was rewritten to fix"
    )


# --- the package tree the design declares --------------------------------

@pytest.mark.parametrize(
    "package",
    [
        "domain",
        "ports",
        "agent",
        "adapters",
        "adapters.model",
        "adapters.serving",
        "adapters.guardrail",
        "adapters.audit",
        "agentcore",
        "config",
        "observability",
    ],
)
def test_every_declared_package_exists_and_documents_itself(package: str) -> None:
    parts = package.split(".")
    path = _ROOT / "src" / "aqm_advisor" / Path(*parts) / "__init__.py"
    assert path.is_file(), f"missing package {package}"
    assert path.read_text(encoding="utf-8").lstrip().startswith('"""'), (
        f"{package} has no docstring recording its layering rule"
    )


def test_the_package_ships_type_information() -> None:
    assert (_ROOT / "src" / "aqm_advisor" / "py.typed").is_file()


# --- Req 26.9: the hypothesis floor --------------------------------------

def test_the_property_floor_is_the_documented_one() -> None:
    from tests.conftest import CI_EXAMPLES, NIGHTLY_EXAMPLES

    assert CI_EXAMPLES == 100
    assert NIGHTLY_EXAMPLES == 1000
