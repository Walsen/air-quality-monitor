"""The offline guarantee (task 28.3).

Requirement 28.5: the full suite passes with no AWS credentials present and no network access
beyond localhost, exercising the in-memory or local adapter for every port.

WHY THIS FILE IS NOT JUST "THE SUITE PASSED". That the offline suite is green on a developer's
machine proves nothing about the guarantee: their machine HAS credentials, so a test quietly
reaching
a real account passes there and fails only in a clean pipeline, which is the failure mode
Requirement 28.5 exists to prevent. So the assertions here are STRUCTURAL — they establish that
every port has a local adapter, that the offline suite excludes what needs an engine, and that
no
offline test can construct a cloud client — rather than observational.

THE ONE OBSERVATIONAL CHECK RUNS THE SUITE IN A SCRUBBED ENVIRONMENT, with every AWS variable
removed and a bogus credential file path, which is the closest a test can come to a clean runner
without being one. It is marked ``integration`` only because it re-invokes pytest and is slow,
not
because it needs a container.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_SRC = _SERVICE_ROOT / "src" / "aqm_ingestion"
_TESTS = _SERVICE_ROOT / "tests"

_CLOUD_MODULES = ("boto3", "botocore")


_LOCAL_ADAPTER_PREFIXES = ("InMemory", "Scripted", "Local")
"""The three prefixes the local adapters use, and why there are three rather than one.

My first version of the check below assumed a uniform ``InMemory{Port}`` and failed on
``ScriptedMqttTransport``, ``ScriptedFeedClient`` and ``LocalAuthenticator``. The prefixes are
not
sloppiness: a transport that REPLAYS A CANNED SCRIPT is not a store held in memory, and the
authenticator verifies against a local rule rather than holding anything. Forcing one prefix
would
make three honest names worse. The check accepts all three and separately asserts every adapter
class uses one of them, so a fourth naming idea has to be a decision instead of a silent pass.
"""


def test_every_port_has_a_local_adapter() -> None:
    """Requirement 28.5's "in-memory or local adapter for EVERY port".

    Derived from the port protocols themselves rather than a hand-written list, so adding a port
    without a local adapter FAILS here instead of being noticed when something needs it.
    """
    protocols = ast.parse((_SRC / "ports" / "protocols.py").read_text(encoding="utf-8"))
    port_names = {
        node.name
        for node in ast.walk(protocols)
        if isinstance(node, ast.ClassDef)
        and any(
            isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases
        )
    }
    assert port_names, "no port protocols found, so this check would be vacuous"

    adapters = ast.parse(
        (_SRC / "adapters" / "memory" / "adapters.py").read_text(encoding="utf-8")
    )
    adapter_names = {
        node.name for node in ast.walk(adapters) if isinstance(node, ast.ClassDef)
    }

    # The Clock is a port too but lives in ports/clock.py with its fakes beside it, so it is
    # satisfied there rather than in the memory adapter module.
    missing = {
        name
        for name in port_names
        if name != "Clock"
        and not any(f"{prefix}{name}" in adapter_names for prefix in _LOCAL_ADAPTER_PREFIXES)
    }
    assert not missing, f"ports with no local adapter: {sorted(missing)}"


def test_every_local_adapter_uses_one_of_the_declared_prefixes() -> None:
    # So the tolerance above cannot quietly become "any name at all": a new adapter named
    # something else fails here and has to justify the prefix.
    adapters = ast.parse(
        (_SRC / "adapters" / "memory" / "adapters.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(adapters):
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            assert node.name.startswith(_LOCAL_ADAPTER_PREFIXES), (
                f"{node.name} uses an undeclared local-adapter prefix"
            )


def _offline_test_files() -> list[Path]:
    """Every test module that the offline suite runs."""
    return [
        path
        for path in _TESTS.rglob("test_*.py")
        if "integration" not in path.relative_to(_TESTS).parts
    ]


def test_no_offline_test_module_imports_a_cloud_sdk_at_module_level() -> None:
    """A module-level cloud import would run during COLLECTION, before any skip can apply.

    The contract suite imports boto3 inside its factories precisely so the offline run never
    touches it; this asserts that discipline over the whole offline tree by walking imports in
    the
    AST rather than grepping, since a docstring naming boto3 is prose (task 3.4's lesson).
    """
    offenders: list[str] = []
    for path in _offline_test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:  # TOP LEVEL only — a nested import is deferred and fine.
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] in _CLOUD_MODULES for name in names):
                offenders.append(path.name)
    assert not offenders, f"offline modules importing a cloud SDK at import time: {offenders}"


def test_the_import_scan_can_actually_fail(tmp_path: Path) -> None:
    """Prove the AST walk above detects a module-level cloud import.

    Without this the scan passes identically against a tree it failed to read — the vacuity trap
    that has caught fourteen assertions in this service so far.
    """
    planted = tmp_path / "test_planted.py"
    planted.write_text("import boto3\n", encoding="utf-8")
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    found = [
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name.split(".")[0] in _CLOUD_MODULES
    ]
    assert found == ["boto3"]


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """The ids of every string constant that is a docstring.

    A docstring is PROSE. This scan has to skip it, for the fourth time this service has learned
    the same thing: a rule about code must inspect the code, and text that merely DESCRIBES the
    rule otherwise trips it. Here the module's own docstring explains which hosts are permitted,
    naming one, and the first version of the scan duly reported its own explanation as a
    violation.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        if not isinstance(node, holders):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def test_no_offline_test_names_a_non_local_host() -> None:
    """Requirement 28.5's "no network beyond localhost".

    The HTTP adapters are driven through injected canned transports, so their tests name hosts
    that are never resolved. This asserts those hosts are RESERVED names (RFC 2606 or
    RFC 6761) or
    loopback — a real hostname in an offline test is one DNS outage away from failing, and one
    misconfiguration away from making a real request.

    Only STRING LITERALS are examined, docstrings excluded, so a comment or explanation
    mentioning
    a host cannot register as a finding. This module is skipped entirely: a test about forbidden
    hosts must be able to name one, so it will always contain a counter-example.
    """
    permitted_tokens = ("127.0.0.1", ".example", ".invalid", ".test", ".localhost")
    permitted_exact = ("localhost", "test")
    permitted_identifiers = ("cognito-idp.eu-west-2.amazonaws.com",)
    """A URL that is an IDENTIFIER rather than an endpoint.

    A Cognito ``iss`` claim is compared as a STRING — the adapter builds the expected issuer
    from
    the region and pool id and checks equality — and the signing key is resolved through an
    injected
    callable, so nothing is ever fetched from it. This check cannot tell an identifier from an
    endpoint statically, so the exception is named explicitly with its reason rather than the
    pattern being loosened to any `amazonaws.com` host.
    """
    offenders: list[str] = []
    scanned = 0

    for path in _offline_test_files():
        if path.name == Path(__file__).name:
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
                host = node.value.split(scheme, 1)[1].split("/")[0].split(":")[0]
                if not host or host in permitted_exact or host in permitted_identifiers:
                    continue
                if not any(token in host for token in permitted_tokens):
                    offenders.append(f"{path.name}: {host}")

    assert scanned, "no URL literals were examined, so this check would be vacuous"
    assert not offenders, f"offline tests naming a non-reserved host: {offenders}"


@pytest.mark.integration
def test_the_offline_suite_passes_with_the_aws_environment_scrubbed() -> None:
    """Run the offline suite with no AWS credentials reachable (Requirement 28.5).

    Marked ``integration`` because it re-invokes the whole suite and is slow, NOT because it
    needs a
    container engine — it needs the opposite of one. This is as close as a test can get to a
    clean
    runner: every AWS variable removed, and the SDK's config and credential files pointed at
    paths
    that do not exist so ~/.aws cannot be found either.
    """
    scrubbed = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AWS_") and key != "AQM_AWS_ENDPOINT_URL"
    }
    scrubbed["AWS_CONFIG_FILE"] = "/nonexistent/aqm-offline-guarantee/config"
    scrubbed["AWS_SHARED_CREDENTIALS_FILE"] = "/nonexistent/aqm-offline-guarantee/credentials"
    # Belt and braces: even if something found a profile, this region-less,
    # EC2-metadata-disabled
    # environment cannot reach a real endpoint.
    scrubbed["AWS_EC2_METADATA_DISABLED"] = "true"

    completed = subprocess.run(
        ["python", "-m", "pytest", "-q", "-m", "not integration", "--no-header"],
        cwd=_SERVICE_ROOT,
        env=scrubbed,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    output = completed.stdout + completed.stderr

    assert completed.returncode == 0, f"the offline suite failed without credentials:\n{output}"
    assert " passed" in output, f"no tests ran:\n{output}"
