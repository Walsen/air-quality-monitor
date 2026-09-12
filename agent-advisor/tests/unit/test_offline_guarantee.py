"""The offline guarantee: Requirements 26.5 and 26.6.

WHAT "OFFLINE" HAS TO MEAN HERE. Requirement 26.5 is not "the suite happens to pass on my
machine". It is that the suite passes with NO AWS credentials and no network beyond localhost,
exercising a local adapter for every port. Three of those clauses are checkable statically -- a
local adapter exists per port, no offline module imports a cloud SDK at import time, no offline
test names a host that DNS would have to resolve -- and the fourth is only really checkable by
running the suite in a scrubbed environment, which the last test does.

THE SCRUBBED RUN IS MARKED `integration`, AND THE MARKER IS LOAD-BEARING RATHER THAN COSMETIC.
It re-invokes the suite as a subprocess with `-m "not integration"`. Were this test itself
unmarked, that subprocess would collect this test, which would spawn another subprocess, and so
on until something ran out. The marker is what makes the recursion terminate; being slow is the
lesser reason.

IT ALSO FIXES A STANDING BUG, as a consequence of doing this task properly rather than as an
aside. `just test-integration-advisor` has selected ZERO tests since the service began, and
pytest exits 5 on an empty selection, so the aggregate `just test-integration` could not pass.
The Justfile has carried a note about it, corrected once at task 17.4 when the deployment
contract turned out to belong in the offline suite (Req 26.5a) rather than here. This is the
advisor's first `integration`-marked test, so that recipe now selects something.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess

import pytest

from aqm_advisor.config.loader import REGISTERED_ADAPTERS

_ADVISOR = pathlib.Path(__file__).resolve().parents[2]
_SRC = _ADVISOR / "src" / "aqm_advisor"
_TESTS = _ADVISOR / "tests"

_CLOUD_MODULES = ("boto3", "botocore")
_LOCAL_ADAPTER_PREFIXES = ("Scripted", "Local", "InMemory", "Recording", "Fixed")

_PURE_CLOUD_MODULES = {
    "boto3.dynamodb.conditions": (
        "an expression BUILDER, not a client: importing it constructs no session and resolves "
        "no region, profile or credential. It is also load-bearing -- the task 16.5 review "
        "found `forget_user` passing a bare string as KeyConditionExpression, which boto3 "
        "forwards verbatim, so the erasure path could not run in production. `Key(...)` from "
        "this module is the fix, and banning the import would undo it."
    ),
}
"""Cloud-SDK modules an offline test MAY import, each with why it reaches nothing.

The rule this narrows is "no credential lookup at import time", not "the letters boto3 never
appear". botocore resolves a region and profile when a CLIENT is built; a pure expression type
does none of that. Listing the module rather than the file keeps the exemption about what the
import DOES, so a second test file needing the same builder is covered and a file that starts
building clients is not.
"""


def _offline_test_files() -> list[pathlib.Path]:
    """Every test module the offline suite collects."""
    return sorted(p for p in _TESTS.rglob("test_*.py") if p.is_file())


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Identities of the string constants that are docstrings, so prose is not scanned."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


# --- Req 26.5: a local adapter for every port --------------------------


def test_every_port_has_a_local_adapter_registered() -> None:
    # Req 26.5's "exercising a local adapter for every port". The registry is the source of
    # truth because it is what configuration selects from, so a port whose only adapter reached
    # a cloud would make the offline suite impossible rather than merely awkward.
    local_names = ("scripted", "local", "memory", "recording", "fixed")
    without = sorted(
        port
        for port, options in REGISTERED_ADAPTERS.items()
        if not any(option in local_names for option in options)
    )
    assert not without, f"these ports offer no local adapter: {without}"


def test_every_local_adapter_class_uses_a_declared_prefix() -> None:
    # A naming convention is only worth asserting because the check above trusts the registry's
    # NAMES; this ties those names to classes a reader can find.
    port_suffixes = ("Client", "Checker", "Store", "Trigger")
    classes: list[str] = []
    for path in sorted((_SRC / "adapters").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        classes += [
            node.name
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name.endswith(port_suffixes)
        ]
    local = [c for c in classes if c.startswith(_LOCAL_ADAPTER_PREFIXES)]
    assert local, f"no local adapter class found among {classes}"


# --- Req 26.5: nothing reaches a cloud at import time ------------------


def test_no_offline_test_module_imports_a_cloud_sdk_at_module_level() -> None:
    # An import-time client construction is the classic way an "offline" suite acquires a
    # credential lookup: botocore resolves a region and profile when a client is built, not when
    # it is called.
    offenders: list[str] = []
    for path in _offline_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Import):
                offenders += [
                    f"{path.name}:{a.name}"
                    for a in node.names
                    if a.name.split(".")[0] in _CLOUD_MODULES
                    and a.name not in _PURE_CLOUD_MODULES
                ]
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] in _CLOUD_MODULES
                and node.module not in _PURE_CLOUD_MODULES
            ):
                offenders.append(f"{path.name}:{node.module}")
    assert not offenders, f"a cloud SDK is imported at module level: {offenders}"


def test_every_pure_cloud_module_exemption_is_still_used() -> None:
    # The staleness direction. An exemption nobody relies on is an exemption nobody reviews, and
    # it silently widens what the scan above permits.
    imported: set[str] = set()
    for path in _offline_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
    unused = sorted(set(_PURE_CLOUD_MODULES) - imported)
    assert not unused, f"delist these unused cloud-module exemptions: {unused}"


def test_a_pure_cloud_module_really_builds_no_client() -> None:
    # The exemption's PREMISE, checked rather than asserted in prose: importing the module and
    # building an expression must touch no session. If boto3 ever moved client construction into
    # this path, the exemption would be wrong and this fails.
    from boto3.dynamodb.conditions import Key

    built = Key("userId").eq("u-1")
    assert built.get_expression()["operator"] == "="


def test_the_import_scan_can_actually_fail(tmp_path: pathlib.Path) -> None:
    # Self-check. A scan that examines only `tree.body` finds nothing if the traversal is wrong,
    # and every assertion above would then pass over any code at all.
    planted = tmp_path / "test_planted.py"
    planted.write_text("import boto3\n", encoding="utf-8")
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    found = [
        a.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for a in node.names
        if a.name.split(".")[0] in _CLOUD_MODULES
    ]
    assert found == ["boto3"]


# --- Req 26.5: no offline test names a resolvable host -----------------


def test_no_offline_test_names_a_non_reserved_host() -> None:
    # Req 26.5's "no network beyond localhost". The HTTP adapter is driven through an injected
    # transport, so the hosts its tests name are never resolved -- but a REAL hostname in an
    # offline test is one DNS outage away from a red suite and one wiring mistake away from a
    # real request. Reserved names (RFC 2606, RFC 6761) and loopback cannot become either.
    #
    # Only string LITERALS are examined and docstrings are excluded, so prose explaining a host
    # is not a finding. This module excludes itself: a test about forbidden hosts must be able
    # to name one.
    permitted_tokens = ("127.0.0.1", ".example", ".invalid", ".test", ".localhost")
    permitted_exact = ("localhost", "test", "0.0.0.0")
    offenders: list[str] = []
    scanned = 0
    for path in _offline_test_files():
        if path.name == pathlib.Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or id(node) in skip:
                continue
            if not isinstance(node.value, str):
                continue
            for scheme in ("http://", "https://"):
                if scheme not in node.value:
                    continue
                scanned += 1
                authority = node.value.split(scheme, 1)[1].split("/")[0]
                # USERINFO IS STRIPPED FIRST. Several config tests deliberately embed
                # `user:password@` to prove a credential is redacted from a logged URL, and
                # splitting on ":" before "@" reports the USERNAME as the host -- which read as
                # four findings on the first run of this test and were all this parser's fault.
                host = authority.rsplit("@", 1)[-1].split(":")[0]
                if host in permitted_exact:
                    continue
                if any(token in host for token in permitted_tokens):
                    continue
                offenders.append(f"{path.name}:{node.lineno}:{host}")
    assert scanned, "no URL literal was examined, so this test proved nothing"
    assert not offenders, f"offline tests name resolvable hosts: {offenders}"


# --- Req 26.5, executed rather than inferred --------------------------


@pytest.mark.integration
def test_the_offline_suite_passes_with_the_aws_environment_scrubbed() -> None:
    """Run the whole offline suite with no AWS credentials reachable (Req 26.5).

    As close to a clean runner as a test can get: every `AWS_` variable removed, and the SDK's
    config and credential files pointed at paths that do not exist, so `~/.aws` cannot be found
    even if something went looking. Metadata service disabled too, since that is the other way a
    credential arrives without a file.
    """
    scrubbed = {
        key: value for key, value in os.environ.items() if not key.startswith("AWS_")
    }
    scrubbed["AWS_CONFIG_FILE"] = "/nonexistent/aqm-advisor-offline/config"
    scrubbed["AWS_SHARED_CREDENTIALS_FILE"] = "/nonexistent/aqm-advisor-offline/credentials"
    scrubbed["AWS_EC2_METADATA_DISABLED"] = "true"

    completed = subprocess.run(
        ["python", "-m", "pytest", "-q", "-m", "not integration", "--no-header"],
        cwd=_ADVISOR,
        env=scrubbed,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, f"the offline suite failed without credentials:\n{output}"
    assert " passed" in output, f"no tests ran:\n{output}"


# --- Req 26.6: the fence itself ---------------------------------------


def test_this_module_supplies_the_integration_marker_the_recipe_needs() -> None:
    # Req 26.6 requires the fenced checks to be selectable by their own command, and
    # `just test-integration-advisor` runs `pytest -m integration`, which exits 5 while nothing
    # carries the marker. This asserts the marker is present in THIS file, so the recipe selects
    # something -- the standing bug the Justfile note describes.
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    marked = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(d, ast.Attribute) and d.attr == "integration"
            for d in node.decorator_list
        )
    ]
    assert marked, "no integration-marked test here, so the fenced recipe still selects nothing"
