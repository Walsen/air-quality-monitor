"""Drift guards over the advisor README (task 21.2, Requirement 26.4).

A README is the one artifact nothing else verifies, so its numbers rot silently — and a
contributor who trusts a wrong default debugs the service instead of the document. These tests
mirror Service 2's `test_packaging.py` guards: each documented claim is compared against the
CONSTANT THAT OWNS IT, read from the code rather than repeated here, so a default that changes
fails these tests until the README follows.

They read files only: no AWS, no network, no container engine.
"""

from __future__ import annotations

import pathlib

import pytest

_ADVISOR = pathlib.Path(__file__).resolve().parents[2]
_REPO_ROOT = _ADVISOR.parent
_README = _ADVISOR / "README.md"


def _readme() -> str:
    return _README.read_text(encoding="utf-8")


def _row_for(readme: str, variable: str) -> str | None:
    """The README table row that documents `variable`, or None."""
    return next(
        (row for row in readme.splitlines() if row.startswith(f"| `{variable}`")), None
    )


def test_the_readme_exists() -> None:
    assert _README.is_file(), f"expected an advisor README at {_README.name}"


def test_the_readme_documents_the_real_defaults() -> None:
    """Every documented scalar default must match the constant that owns it.

    The expected value is READ FROM THE CODE, never hardcoded here, so this test and the README
    both track the same source. Only the scalars that HAVE a default are checked — the
    interface/None defaults render as an empty cell and are covered by the completeness test
    below, not by a value comparison.
    """
    from aqm_advisor.config.loader import _SCALARS

    readme = _readme()

    # Every scalar whose default is not None (None renders as "no default" in the README).
    documented_with_default = {
        env_var: default for env_var, default in _SCALARS.values() if default is not None
    }
    assert documented_with_default, "expected at least one scalar with a concrete default"

    for env_var, default in documented_with_default.items():
        row = _row_for(readme, env_var)
        assert row, f"{env_var} is not documented in the README"
        if default is True:
            expected = "true"
        elif default is False:
            expected = "false"
        else:
            expected = str(default)
        assert f"`{expected}`" in row, (
            f"{env_var}: README disagrees with the code (expected `{expected}`)"
        )


def test_the_readme_documents_every_scalar_setting() -> None:
    """No configuration setting is left undocumented.

    Catches the drift in the other direction: a scalar ADDED to the loader with no README row is
    a setting a contributor cannot discover. The env var name is what a reader searches for, so
    presence of the row is what is asserted.
    """
    from aqm_advisor.config.loader import _SCALARS

    readme = _readme()
    for env_var, _default in _SCALARS.values():
        assert f"| `{env_var}`" in readme, f"{env_var} has no README row"


def test_the_readme_lists_every_registered_adapter() -> None:
    """Every port and every adapter name a contributor may select must appear.

    The adapter table is the part a contributor copies from, so a name missing here is a name
    nobody knows they can select.
    """
    from aqm_advisor.config.loader import REGISTERED_ADAPTERS

    readme = _readme()
    for port, names in REGISTERED_ADAPTERS.items():
        assert f"`{port}`" in readme, f"port {port} is not documented"
        for name in names:
            assert f"`{name}`" in readme, f"adapter {port}={name} is not documented"


def test_the_readme_names_every_tool() -> None:
    """Every retrieval tool must be named.

    There is no single tool-name constant to import — the tools are built and returned as a
    tuple by `build_retrieval_tools` in `aqm_advisor.agent.tools`. The names are listed here
    explicitly with that source noted; if a tool is added or renamed there, add it here and to
    the README together.
    """
    tool_names = (
        "air_quality",
        "history",
        "profile_get",
        "profile_put",
        "symptom_entry_put",
    )
    readme = _readme()
    for name in tool_names:
        assert f"`{name}`" in readme, f"tool {name} is not documented"


def test_the_readme_states_the_permitted_log_levels() -> None:
    # The log-level row names the permitted band. Read the set from the logging module so a
    # change to the permitted levels surfaces here.
    from aqm_advisor.observability.logging import PERMITTED_LOG_LEVELS

    row = _row_for(_readme(), "AQM_ADVISOR_LOG_LEVEL")
    assert row, "the log level is not documented"
    for level in PERMITTED_LOG_LEVELS:
        assert f"`{level}`" in row, f"log level {level} is not listed in its README row"


@pytest.mark.parametrize(
    "recipe",
    [
        "test-advisor",
        "test-contracts-advisor",
        "test-integration-advisor",
        "test-advisor-eval",
        "test-advisor-nightly",
        "lint-advisor",
        "fmt-advisor",
        "typecheck-advisor",
        "run-advisor",
        "synth-advisor-infra",
    ],
)
def test_the_documented_recipes_exist(recipe: str) -> None:
    # Req 26.4: a contributor and a pipeline invoke the SAME code path, which is what a recipe
    # is for — a shell snippet in a README does not satisfy it (dev-environment steering says
    # so). Every recipe the README's Commands table names must exist in the root Justfile.
    justfile = (_REPO_ROOT / "Justfile").read_text(encoding="utf-8")
    assert f"\n{recipe}:" in justfile, (
        f"the README documents `just {recipe}`, which is not a recipe"
    )
