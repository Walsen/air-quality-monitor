"""Architecture enforcement over real source (task 2.5).

Four rules, each asserted by walking the SYNTAX TREE rather than searching characters. That is
not
fastidiousness: a module docstring that DOCUMENTS one of these rules mentions the very name the
rule
forbids, and a text search flags it. That has now happened three times in this repository —
twice in
Service 2 and once in this service's own scaffolding test — so the rule is: a check about code
inspects the AST, never the text.

**Every detector carries a self-check.** A rule that cannot fail is worse than no rule, because
it
reports safety it does not provide. So each detector is pointed at deliberately offending source
and
must flag it, and at innocent-but-similar source and must not. Without that, an AST walk with a
broken matcher passes exactly as happily as a clean codebase.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import aqm_advisor

_ROOT = pathlib.Path(next(iter(aqm_advisor.__path__)))
_DOMAIN = _ROOT / "domain"


def _modules(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(root.rglob("*.py"))


def _imported_names(tree: ast.AST) -> set[str]:
    """Every module name a tree imports, from both import forms."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _called_paths(tree: ast.AST) -> set[str]:
    """Every called expression, rendered — so ``dt.datetime.now`` is comparable as a path."""
    return {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }


# --- rule 1: domain/ imports nothing outward ----------------------------

_FORBIDDEN_IN_DOMAIN = ("aqm_advisor.adapters", "aqm_advisor.agentcore", "aqm_advisor.agent")


def _outward_imports(root: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for path in _modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imported_names(tree):
            if name.startswith(_FORBIDDEN_IN_DOMAIN):
                offenders.append(f"{path.name} imports {name}")
    return offenders


def test_the_domain_imports_nothing_outward() -> None:
    # DD1's dependency direction. The domain knows nothing about Bedrock, HTTP, AgentCore or the
    # clock; those arrive as injected abstractions.
    assert _outward_imports(_DOMAIN) == []


def test_the_outward_import_detector_would_catch_one(tmp_path: pathlib.Path) -> None:
    guilty = tmp_path / "guilty.py"
    guilty.write_text(
        "from aqm_advisor.adapters.local import LocalGuardrailChecker\n", encoding="utf-8"
    )
    innocent = tmp_path / "innocent.py"
    innocent.write_text(
        '"""Mentions aqm_advisor.adapters in prose only."""\n', encoding="utf-8"
    )
    found = _outward_imports(tmp_path)
    assert len(found) == 1, f"expected exactly the guilty module, got {found}"
    assert "guilty.py" in found[0]


# --- rule 2: no wall-clock read in domain/ ------------------------------

_CLOCK_CALLS = (
    "dt.datetime.now",
    "datetime.now",
    "datetime.datetime.now",
    "dt.date.today",
    "date.today",
    "time.time",
    "time.monotonic",
)


def _clock_reads(root: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for path in _modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _called_paths(tree):
            if call in _CLOCK_CALLS:
                offenders.append(f"{path.name} calls {call}")
    return offenders


def test_the_domain_reads_no_wall_clock() -> None:
    # Requirement 25.2: every instant comes from the injected Clock, or a turn is not
    # reproducible.
    assert _clock_reads(_DOMAIN) == []


def test_the_clock_detector_would_catch_a_read(tmp_path: pathlib.Path) -> None:
    guilty = tmp_path / "guilty.py"
    guilty.write_text(
        "import datetime as dt\n\n\ndef f():\n    return dt.datetime.now()\n",
        encoding="utf-8",
    )
    innocent = tmp_path / "innocent.py"
    innocent.write_text(
        '"""Never call dt.datetime.now() in the domain."""\n', encoding="utf-8"
    )
    found = _clock_reads(tmp_path)
    assert len(found) == 1, (
        f"expected exactly the guilty module, got {found} — a docstring naming the rule must "
        "not be flagged, which is why this walks the AST"
    )
    assert "guilty.py" in found[0]


def test_the_system_clock_is_the_only_wall_clock_read_in_the_package() -> None:
    # Non-vacuity for the rule above: SOMETHING must read the real clock, or the ban is trivial.
    readers = {
        entry.split()[0] for entry in _clock_reads(_ROOT)
    }
    assert readers == {"clock.py"}, (
        f"the wall clock should be read only at the process edge, found {readers}"
    )


# --- rule 3: no randomness in domain/ -----------------------------------

def _random_imports(root: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for path in _modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imported_names(tree):
            if name.split(".")[0] in {"random", "secrets", "uuid"}:
                offenders.append(f"{path.name} imports {name}")
    return offenders


def test_the_domain_imports_no_randomness() -> None:
    # Requirement 25.3: a turn must be reproducible, and a random identifier inside the domain
    # would make two runs over identical inputs differ.
    assert _random_imports(_DOMAIN) == []


def test_the_randomness_detector_would_catch_an_import(tmp_path: pathlib.Path) -> None:
    (tmp_path / "guilty.py").write_text("import random\n", encoding="utf-8")
    (tmp_path / "innocent.py").write_text(
        '"""Never import random in the domain."""\n', encoding="utf-8"
    )
    found = _random_imports(tmp_path)
    assert len(found) == 1
    assert "guilty.py" in found[0]


# --- rule 4: no whole-config parameter in a domain function -------------

_CONFIG_TYPES = ("AdvisorConfig", "ServiceConfig", "Settings", "Config")


def _whole_config_parameters(root: pathlib.Path) -> list[str]:
    """Domain functions annotated with a whole-configuration object (§1).

    Interface Segregation: a domain function takes the narrow parameter object it needs, not the
    entire configuration. Handing it everything makes it depend on settings it never reads,
    which
    is how a change to an unrelated setting starts breaking a domain test.
    """
    offenders: list[str] = []
    for path in _modules(root):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for argument in [*node.args.args, *node.args.kwonlyargs]:
                if argument.annotation is None:
                    continue
                rendered = ast.unparse(argument.annotation)
                if any(name in rendered for name in _CONFIG_TYPES):
                    offenders.append(f"{path.name}:{node.name} takes {rendered}")
    return offenders


def test_no_domain_function_takes_a_whole_config_object() -> None:
    assert _whole_config_parameters(_DOMAIN) == []


def test_the_whole_config_detector_would_catch_one(tmp_path: pathlib.Path) -> None:
    (tmp_path / "guilty.py").write_text(
        "def f(config: AdvisorConfig) -> None:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "innocent.py").write_text(
        '"""Never take an AdvisorConfig in the domain."""\n'
        "def g(limit: int) -> None:\n    pass\n",
        encoding="utf-8",
    )
    found = _whole_config_parameters(tmp_path)
    assert len(found) == 1
    assert "guilty.py" in found[0]


# --- rule 5: only agentcore/ imports the deployment SDK -----------------

def _deployment_sdk_importers(root: pathlib.Path) -> list[str]:
    offenders: list[str] = []
    for path in _modules(root):
        if path.parent.name == "agentcore":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imported_names(tree):
            if name.split(".")[0] == "bedrock_agentcore":
                offenders.append(path.name)
    return offenders


def test_only_the_agentcore_package_imports_the_deployment_sdk() -> None:
    # Requirement 32.2, so the advisory turn stays runnable without the runtime.
    assert _deployment_sdk_importers(_ROOT) == []


def test_the_deployment_sdk_detector_would_catch_an_import(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "guilty.py").write_text(
        "from bedrock_agentcore.runtime import BedrockAgentCoreApp\n", encoding="utf-8"
    )
    (tmp_path / "innocent.py").write_text(
        '"""Only agentcore/ may import bedrock_agentcore."""\n', encoding="utf-8"
    )
    found = _deployment_sdk_importers(tmp_path)
    assert len(found) == 1
    assert found[0] == "guilty.py"


# --- the checks are pointed at real source ------------------------------

def test_the_domain_package_exists_so_these_rules_are_not_vacuous() -> None:
    # Every rule above scans `domain/`. If that directory were missing or empty, all four would
    # pass over nothing — which is the failure mode this test exists to prevent while the
    # package
    # is still being built out.
    assert _DOMAIN.is_dir()
    assert _modules(_DOMAIN), "domain/ has no modules, so the layering rules scan nothing"


def test_the_package_has_modules_for_the_sdk_rule_to_scan() -> None:
    assert len(_modules(_ROOT)) >= 5


@pytest.mark.parametrize(
    "detector",
    [_outward_imports, _clock_reads, _random_imports, _whole_config_parameters],
    ids=["outward-imports", "clock-reads", "randomness", "whole-config"],
)
def test_every_detector_returns_a_list_it_could_populate(detector: object) -> None:
    # A structural guard against a detector that silently returns None or a constant.
    assert isinstance(detector(_DOMAIN), list)  # type: ignore[operator]
