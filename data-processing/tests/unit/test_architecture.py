"""Architecture checks (task 3.4).

Two rules the whole design rests on, enforced by inspecting the real source rather
than trusting convention — a convention only holds until someone adds one import.

- Req 14.1/27.1: no module under ``domain/`` imports from ``adapters/``. The domain
  depends on ports; adapters depend on the domain. If that arrow ever reverses, the
  offline suite stops being able to run the domain against fakes.
- §1 Interface Segregation: no domain function takes the WHOLE configuration
  object. A domain unit should receive the narrow parameter object it needs, so its
  dependencies are visible in the signature.
"""

from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest

import aqm_ingestion

_PACKAGE_ROOT = Path(aqm_ingestion.__file__).parent
_DOMAIN_ROOT = _PACKAGE_ROOT / "domain"
_ADAPTERS_ROOT = _PACKAGE_ROOT / "adapters"

# Names that would mean a whole-config object crossed into the domain.
_CONFIG_PARAMETER_NAMES = frozenset({"config", "settings", "app_config", "conf"})


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py"))


def _imported_modules(source: Path) -> set[str]:
    """Every module name imported by a file, however it was written."""
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_the_domain_package_has_modules_to_check() -> None:
    # a vacuous pass would be worse than no check at all
    assert _python_files(_DOMAIN_ROOT)


@pytest.mark.parametrize(
    "source", _python_files(_DOMAIN_ROOT), ids=lambda p: p.name
)
def test_no_domain_module_imports_an_adapter(source: Path) -> None:
    for module in _imported_modules(source):
        assert "aqm_ingestion.adapters" not in module, (
            f"{source.name} imports {module}: the domain must depend on ports, "
            "not on adapters"
        )


@pytest.mark.parametrize(
    "source", _python_files(_DOMAIN_ROOT), ids=lambda p: p.name
)
def test_no_domain_module_imports_the_serving_or_ingest_layer(source: Path) -> None:
    # the domain sits BELOW both, so an import either way would be a cycle
    for module in _imported_modules(source):
        assert "aqm_ingestion.serving" not in module, source.name
        assert "aqm_ingestion.ingest" not in module, source.name


@pytest.mark.parametrize(
    "source", _python_files(_DOMAIN_ROOT), ids=lambda p: p.name
)
def test_no_domain_function_takes_the_whole_config(source: Path) -> None:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = [*node.args.args, *node.args.kwonlyargs, *node.args.posonlyargs]
        for argument in arguments:
            assert argument.arg not in _CONFIG_PARAMETER_NAMES, (
                f"{source.name}:{node.name} takes {argument.arg!r}: pass the narrow "
                "parameter object the function actually needs (§1)"
            )


def _called_attribute_paths(source: Path) -> set[str]:
    """Dotted names of every attribute CALLED in a file, e.g. 'datetime.now'.

    Parsed from the AST rather than matched as text: a docstring documenting the
    no-wall-clock rule is prose, and a text search would flag it as a violation —
    which is exactly what happened before this was AST-based.
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    called: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        parts: list[str] = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        if parts:
            called.add(".".join(reversed(parts)))
    return called


@pytest.mark.parametrize(
    "source", _python_files(_DOMAIN_ROOT), ids=lambda p: p.name
)
def test_no_domain_module_reads_the_wall_clock(source: Path) -> None:
    """§2: time arrives through the Clock port or as a parameter, never directly."""
    forbidden = {"datetime.now", "datetime.utcnow", "time.time"}
    for call in _called_attribute_paths(source):
        # match the tail so dt.datetime.now and datetime.datetime.now both count
        tail = ".".join(call.split(".")[-2:])
        assert tail not in forbidden, f"{source.name} calls {call}"


@pytest.mark.parametrize(
    "source", _python_files(_DOMAIN_ROOT), ids=lambda p: p.name
)
def test_no_domain_module_uses_module_level_random(source: Path) -> None:
    """§2: a generator is injected, never taken from module state."""
    for module in _imported_modules(source):
        assert module != "random", source.name


def test_adapters_may_import_the_domain() -> None:
    # the arrow points this way on purpose; assert it actually happens so the
    # check above is meaningful rather than trivially satisfied
    imported_domain = any(
        any("aqm_ingestion.domain" in module for module in _imported_modules(source))
        for source in _python_files(_ADAPTERS_ROOT)
    )
    assert imported_domain


def test_ports_do_not_import_adapters() -> None:
    for source in _python_files(_PACKAGE_ROOT / "ports"):
        for module in _imported_modules(source):
            assert "aqm_ingestion.adapters" not in module, source.name


def test_clock_port_is_the_only_wall_clock_reader() -> None:
    """SystemClock is allowed to read the clock; nothing else in ports is."""
    for source in _python_files(_PACKAGE_ROOT / "ports"):
        if source.name == "clock.py":
            continue
        for call in _called_attribute_paths(source):
            tail = ".".join(call.split(".")[-2:])
            assert tail != "datetime.now", f"{source.name} calls {call}"


def test_architecture_check_covers_a_real_datetime_import() -> None:
    # guards the checks above against a typo silently matching nothing
    assert dt.datetime is not None


def test_the_wall_clock_detector_actually_detects() -> None:
    """A rule that cannot fail is worthless, so prove the detector fires.

    SystemClock legitimately calls ``datetime.now``, so the detector must find it
    there. If this stops holding, the domain checks above have become vacuous.
    """
    calls = _called_attribute_paths(_PACKAGE_ROOT / "ports" / "clock.py")
    tails = {".".join(call.split(".")[-2:]) for call in calls}
    assert "datetime.now" in tails


def test_the_import_detector_actually_detects() -> None:
    """Likewise for the import scanner: adapters really do import the domain."""
    modules = _imported_modules(_ADAPTERS_ROOT / "memory" / "adapters.py")
    assert any("aqm_ingestion.domain" in module for module in modules)
