"""Packaging assertions over the local stack definition (task 28.1).

Requirements 28.7, 28.8, 28.9.

THESE ASSERT OVER THE PARSED COMPOSE DOCUMENT, NOT ITS TEXT. Service 1 set the precedent at its
own
task 20 — a comment mentioning a cloud credential path is PROSE while a mount of it would be
REAL,
and a test grepping raw text cannot tell them apart — but it implemented that by stripping
comment
text, and my first draft copied the idea. The stripper was QUOTE-BLIND: it truncated the broker
healthcheck at the ``#`` inside ``"$$SYS/#"``, which broke the parse and, worse, would silently
drop
anything after a ``#`` in any quoted value — a false NEGATIVE on exactly the credential search
these
tests exist to perform.

So there is no stripper. YAML's own parser already excludes comments correctly, which makes it
the
right tool for both jobs: structural assertions read the document, and the credential searches
walk
its flattened values. Comments cannot satisfy an assertion because they are never in the tree,
and
nothing can be missed to a lexing mistake of mine. Same lesson as task 3.4, one level up:
inspect
the STRUCTURE, never the text.

These tests need no container engine: they parse files. Only task 28.2's checks need a daemon,
and
those carry the ``integration`` marker.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_PATH = _SERVICE_ROOT / "docker-compose.yml"
_DOCKERFILE_PATH = _SERVICE_ROOT / "Dockerfile"
_REPO_ROOT = _SERVICE_ROOT.parent


def _compose() -> dict[str, Any]:
    """The parsed Compose document. Comments are excluded by the YAML parser itself."""
    return cast(
        "dict[str, Any]", yaml.safe_load(_COMPOSE_PATH.read_text(encoding="utf-8"))
    )


def _services() -> dict[str, Any]:
    return cast("dict[str, Any]", _compose()["services"])


def _flattened(node: object) -> Iterator[str]:
    """Every key and scalar value in the document, as text.

    Walking the tree rather than the file means a credential can only be found where it would
    actually take effect, and cannot be hidden from the search by a quoting subtlety.
    """
    if isinstance(node, dict):
        for key, value in cast("dict[object, object]", node).items():
            yield str(key)
            yield from _flattened(value)
    elif isinstance(node, list):
        for item in cast("list[object]", node):
            yield from _flattened(item)
    else:
        yield str(node)


def test_the_compose_file_exists() -> None:
    assert _COMPOSE_PATH.is_file(), f"expected a Compose definition at {_COMPOSE_PATH.name}"


def test_the_stack_stands_up_a_broker_and_a_local_store() -> None:
    # Req 28.8 names all three: the service, a local MQTT broker, and a local store adapter
    # sufficient to exercise the DynamoDB and S3 adapters without a cloud account.
    services = _services()
    names = set(services)
    assert "ingestion" in names
    assert "mosquitto" in names
    assert "localstack" in names


def test_the_local_store_offers_both_dynamodb_and_s3() -> None:
    # ONE emulator rather than dynamodb-local plus minio, and the reason is already in the code:
    # the shared contract suite written at task 27.1 reads a SINGLE AQM_AWS_ENDPOINT_URL, and
    # the
    # adapters take one endpoint each. Two emulators would mean two endpoints and a suite
    # change,
    # so the existing seam picks the emulator.
    localstack = _services()["localstack"]
    declared = localstack["environment"]["SERVICES"]
    assert "dynamodb" in declared
    assert "s3" in declared


def test_every_published_port_is_bound_to_loopback_only() -> None:
    # An emulator or broker reachable from the network is a service with no authentication in
    # front of it (§7). Compose publishes to every interface unless told otherwise, so the
    # absence of a bind address is itself the defect this catches.
    for name, service in _services().items():
        for published in service.get("ports", []):
            assert str(published).startswith("127.0.0.1:"), (
                f"{name} publishes {published} beyond loopback"
            )


def test_the_stack_never_inherits_a_real_aws_credential() -> None:
    """The stack must not reach the host's credentials (Requirement 28.6).

    THE FIRST VERSION OF THIS TEST ASSERTED THE WRONG THING. It forbade the env-var NAME
    ``AWS_SECRET_ACCESS_KEY`` outright — and the emulator legitimately needs a dummy under
    exactly
    that name to accept a signed request, so I "fixed" the failure by renaming the variable to
    ``..._PLACEHOLDER``, which satisfied the test and left the emulator without the credential
    it
    needs. That is bending the code to the test.

    What is actually dangerous is INHERITANCE: a ``${AWS_SECRET_ACCESS_KEY}`` passthrough
    from the
    developer's shell, or a mount of their profile, either of which puts a real key into a stack
    the
    fence promises has none. A hardcoded dummy is not a credential. So the rule is about the
    VALUE's
    provenance, not the variable's spelling.
    """
    for value in _flattened(_compose()):
        assert "${AWS_" not in value, f"the stack inherits a host AWS value: {value}"
        assert "${AMAZON" not in value
        assert ".aws" not in value, f"the stack references a host AWS profile: {value}"
        assert value != "AWS_SESSION_TOKEN", "a session token is never a local-stack input"


def test_the_stack_carries_no_committed_secret() -> None:
    # Req 28.9. The emulator needs SOME credential value to accept a signed request, but a dummy
    # is not a secret — this asserts nothing that looks like real material is present.
    for value in _flattened(_compose()):
        assert "sk-" not in value
        assert "-----BEGIN" not in value


def test_the_emulator_credentials_are_literal_dummies() -> None:
    # The counterpart of the test above, and the reason it can be strict about provenance
    # without
    # breaking the stack: the emulator's key pair IS present, and both values are literals.
    environment = _services()["localstack"]["environment"]
    assert environment["AWS_ACCESS_KEY_ID"] == "test"
    assert environment["AWS_SECRET_ACCESS_KEY"] == "test"


def test_the_credential_search_can_actually_fail() -> None:
    """Prove the flattened walk reaches a value nested where a credential would really sit.

    Without this the searches above would pass identically against a walk that returned nothing
    —
    and the quote-blind stripper they replaced failed in exactly that direction, silently
    dropping
    everything after a ``#`` in a quoted value.
    """
    planted = {"services": {"x": {"environment": {"KEY": "${AWS_SECRET_ACCESS_KEY}"}}}}
    values = list(_flattened(planted))
    assert any("${AWS_" in value for value in values)


def test_the_credential_tree_is_git_ignored() -> None:
    # Req 28.9's second half: the repository ignores any generated credential tree.
    ignored = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "certs/" in ignored


def test_the_dockerfile_pins_the_interpreter_and_installs_from_the_lockfile() -> None:
    # Req 28.2's exact-version rule reaches the image too: an unpinned base or a resolve at
    # build
    # time would make the container drift from the uv.lock the tests ran against.
    dockerfile = _DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert "python:3.12" in dockerfile
    # --frozen fails rather than re-resolving, which is what makes the lock authoritative.
    assert "--frozen" in dockerfile


def test_the_image_runs_as_an_unprivileged_user() -> None:
    # §7 least privilege. Absent a USER directive a container runs as root.
    dockerfile = _DOCKERFILE_PATH.read_text(encoding="utf-8")
    assert any(
        line.strip().startswith("USER ") and "root" not in line
        for line in dockerfile.splitlines()
    ), "the image does not drop to an unprivileged user"


def test_the_image_bakes_no_secret() -> None:
    # Req 28.9 for the image: credentials arrive at runtime, never in a layer.
    dockerfile = _DOCKERFILE_PATH.read_text(encoding="utf-8")
    for forbidden in ("AWS_SECRET_ACCESS_KEY", "-----BEGIN", "AQM_FEED_API_KEY="):
        assert forbidden not in dockerfile


@pytest.mark.parametrize(
    "recipe",
    ["test-ingestion", "test-integration-ingestion", "lint-ingestion", "typecheck-ingestion"],
)
def test_the_documented_recipes_exist(recipe: str) -> None:
    # Req 28.7: a contributor and a pipeline invoke the SAME code path, which is what a recipe
    # is
    # for — a shell snippet in a README does not satisfy it (dev-environment steering says so).
    justfile = (_REPO_ROOT / "Justfile").read_text(encoding="utf-8")
    assert f"\n{recipe}:" in justfile


def test_the_local_stack_recipes_exist() -> None:
    # Req 28.7's local-stack command, both directions.
    justfile = (_REPO_ROOT / "Justfile").read_text(encoding="utf-8")
    assert "\nup-ingestion:" in justfile
    assert "\ndown-ingestion:" in justfile


def test_the_offline_recipe_excludes_the_fenced_checks() -> None:
    # Req 28.6 in the command surface: the default suite must EXCLUDE what needs a daemon, and a
    # separate command must run it. Asserted on the recipe bodies so the fence is real rather
    # than a convention someone remembers.
    justfile = (_REPO_ROOT / "Justfile").read_text(encoding="utf-8")
    offline = justfile.split("\ntest-ingestion:", 1)[1].split("\n\n", 1)[0]
    fenced = justfile.split("\ntest-integration-ingestion:", 1)[1].split("\n\n", 1)[0]
    assert "not integration" in offline
    assert "-m integration" in fenced
