"""Packaging assertions read the Dockerfile's PARSED INSTRUCTIONS, never its raw text.

WHY THE PARSER EXISTS, WITH EVIDENCE. Task 20.1 requires assertions that "read the parsed
structure rather than raw text so a comment cannot satisfy them", and Service 2's equivalent
test shows why. It asserts `"--frozen" in dockerfile` against the whole file, and Service 2's
image explains itself in a header comment containing the words `uv sync --frozen`. Removing
`--frozen` from BOTH of that image's `RUN uv sync` lines was tried here: its test still passed.
The image would re-resolve dependencies at build time and ship a set the suite never gated, and
the gate reported success.

So every assertion below runs against instructions produced by :func:`_instructions`, which
drops comments and joins backslash continuations. A comment cannot satisfy any of them, which
is the whole requirement.

WHAT THIS FILE DOES NOT DO. It never builds the image. A build needs a container engine, which
Requirement 26.6 fences out of the offline suite, so these are structural assertions over the
build definition -- they prove what the image WOULD do, not that a build succeeded. The
container-fenced job is where an actual build belongs.
"""

from __future__ import annotations

import pathlib
import re

_ADVISOR = pathlib.Path(__file__).resolve().parents[2]
_DOCKERFILE = _ADVISOR / "Dockerfile"

_ENTRY_MODULE = "aqm_advisor.main"
"""The module the image starts. Task 21.1 creates it; see the entry-module test below."""


def _instructions() -> list[tuple[str, str]]:
    """Every Dockerfile instruction as `(verb, argument)`, comments removed.

    Backslash continuations are joined first, so a multi-line `RUN` is one instruction rather
    than a fragment plus orphan text. A `#` line is dropped WHOLE, which is the property the
    raw-text version lacked: prose about an instruction can no longer stand in for it.
    """
    joined = _DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    found: list[tuple[str, str]] = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        verb, _, argument = line.partition(" ")
        found.append((verb.upper(), argument.strip()))
    return found


def _arguments(verb: str) -> list[str]:
    return [argument for found, argument in _instructions() if found == verb]


# --- the parser itself, before anything relies on it -------------------


def test_the_parser_drops_comments_entirely() -> None:
    # The self-check that makes every assertion below meaningful. The Dockerfile's own header
    # mentions `uv sync --frozen` in prose; if that prose reached the instruction list, these
    # tests would inherit exactly the defect they exist to avoid.
    verbs = [verb for verb, _ in _instructions()]
    assert "#" not in verbs
    assert not [v for v in verbs if v.startswith("#")]
    prose = "DEPENDENCIES INSTALL FROM THE COMMITTED LOCKFILE"
    assert prose in _DOCKERFILE.read_text(encoding="utf-8"), "the header prose moved"
    assert not [a for _, a in _instructions() if prose in a], "comment text reached an argument"


def test_the_parser_finds_the_instructions_that_are_there() -> None:
    verbs = {verb for verb, _ in _instructions()}
    assert {"FROM", "RUN", "COPY", "USER", "EXPOSE", "CMD", "ENV", "WORKDIR"} <= verbs


def test_the_parser_joins_a_continued_instruction() -> None:
    # A continued `ENV` block must arrive as ONE argument. Were it split, an assertion over the
    # tail lines would silently examine fragments.
    envs = _arguments("ENV")
    assert any("PYTHONUNBUFFERED=1" in e and "UV_COMPILE_BYTECODE=1" in e for e in envs)


# --- Req 32.1: the runtime's platform, host and port -------------------


def test_the_base_image_is_pinned_to_arm64() -> None:
    # Req 32.1. AgentCore Runtime takes a linux/arm64 image. Without the platform on the base, a
    # build on an x86 host produces an amd64 image that passes every local check and fails at
    # deploy.
    froms = _arguments("FROM")
    assert froms, "no FROM instruction"
    assert all("--platform=linux/arm64" in f for f in froms), froms


def test_the_interpreter_is_pinned_to_an_exact_patch() -> None:
    # Req 26.2 names Python 3.12. `python:3.12-slim` floats across patch releases, so two builds
    # a month apart can differ; Service 1's image pins the patch and this follows it.
    froms = _arguments("FROM")
    assert all(re.search(r"python:3\.12\.\d+-slim", f) for f in froms), froms


def test_the_image_exposes_the_port_the_runtime_contract_fixes() -> None:
    assert _arguments("EXPOSE") == ["8080"]


def test_the_image_does_not_expose_another_service_port() -> None:
    # Both sibling services expose 8000. Copying one of their Dockerfiles and forgetting the
    # port is the realistic mistake, and it produces a container the platform cannot reach.
    assert "8000" not in _arguments("EXPOSE")


# --- Req 26.2: the lockfile is authoritative ---------------------------


def test_every_dependency_install_is_a_frozen_resolve() -> None:
    # THE ASSERTION SERVICE 2'S VERSION ONLY APPEARED TO MAKE. Each `uv sync` must carry
    # `--frozen` in the instruction itself, so removing it from a RUN line fails here even
    # though the words survive in the header comment.
    syncs = [a for a in _arguments("RUN") if "uv sync" in a]
    assert syncs, "no dependency install found"
    unfrozen = [s for s in syncs if "--frozen" not in s]
    assert not unfrozen, f"these installs would re-resolve at build time: {unfrozen}"


def test_no_dependency_install_pulls_development_extras() -> None:
    syncs = [a for a in _arguments("RUN") if "uv sync" in a]
    assert all("--no-dev" in s for s in syncs), syncs


def test_the_manifest_and_lock_are_copied_before_the_source() -> None:
    # The layer-cache ordering, asserted rather than trusted: if src/ were copied first, every
    # source edit would re-resolve and re-download the whole dependency set.
    copies = _arguments("COPY")
    lock = next(i for i, c in enumerate(copies) if "uv.lock" in c)
    source = next(i for i, c in enumerate(copies) if c.startswith("src"))
    assert lock < source, copies


def test_the_lockfile_is_committed() -> None:
    # `--frozen` fails without it, so the image could not build. Asserting the file's presence
    # turns that build failure into an offline test failure.
    assert (_ADVISOR / "uv.lock").is_file()


# --- Req 26.7 and least privilege --------------------------------------


def test_the_image_runs_as_an_unprivileged_user() -> None:
    users = _arguments("USER")
    assert users, "no USER directive, so the container runs as root"
    assert users[-1] != "root", users


def test_the_unprivileged_user_owns_the_application_directory() -> None:
    # A USER directive alone is not enough: if /app stays root-owned, the venv on PATH is not
    # writable and a first-run bytecode write fails in a way that reads as a code fault.
    assert any("chown" in a and "aqm" in a for a in _arguments("RUN"))


def test_the_image_bakes_no_secret() -> None:
    # Req 26.7. Structural markers rather than entropy heuristics, per the dev-environment
    # steering note about scanners that fire on ordinary source text.
    forbidden = ("BEGIN RSA PRIVATE KEY", "BEGIN PRIVATE KEY", "aws_secret_access_key")
    body = _DOCKERFILE.read_text(encoding="utf-8")
    assert not [marker for marker in forbidden if marker in body]


def test_no_instruction_sets_a_credential_shaped_variable() -> None:
    # A secret arriving through ENV at BUILD time is baked into the image layer even if it is
    # overridden at runtime. This reads the parsed ENV and ARG arguments, so a comment naming a
    # variable does not trip it.
    credentialish = ("SECRET", "PASSWORD", "TOKEN", "PRIVATE_KEY", "AWS_SECRET")
    declared = _arguments("ENV") + _arguments("ARG")
    offenders = [a for a in declared if any(word in a.upper() for word in credentialish)]
    assert not offenders, offenders


# --- the entry point, and the task that owns it ------------------------


def test_the_image_starts_the_entry_module() -> None:
    cmds = _arguments("CMD") + _arguments("ENTRYPOINT")
    assert cmds, "the image has no entry point"
    assert any(_ENTRY_MODULE in c for c in cmds), cmds


def test_the_entry_module_exists_and_is_importable() -> None:
    # REPLACED the absence test at task 21.1, per the instruction that test carried. The
    # Dockerfile's note about the entry module being task 21's went in the same commit, so the
    # image can no longer be documented as unrunnable once its entry point exists.
    module = _ADVISOR / "src" / "aqm_advisor" / "main.py"
    assert module.is_file(), f"the image's CMD names {_ENTRY_MODULE}, which does not exist"


def test_the_entry_module_builds_the_app_from_configuration() -> None:
    # The assertion that matters: the module the container starts must actually assemble a
    # servable app from a validated config, with no AWS involved. Local adapters throughout, so
    # this belongs in the offline suite.
    import os

    from aqm_advisor.config.loader import resolve_and_validate
    from aqm_advisor.main import build_from_config

    env = {
        "AQM_ADVISOR_SERVING_BASE_URL": "https://serving.test",
        "AQM_ADVISOR_EMERGENCY_GUIDANCE_FALLBACK": "If you cannot breathe, call 999.",
        "AQM_ADVISOR_ADAPTERS": (
            "serving_client=scripted,guardrail_checker=local,advice_audit_store=memory,"
            "model=scripted,clock=system,association_trigger=recording"
        ),
    }
    config = resolve_and_validate({**{k: v for k, v in os.environ.items() if False}, **env})
    app = build_from_config(config)

    paths = {str(getattr(route, "path", "")) for route in app.routes}  # type: ignore[attr-defined]
    assert paths == {"/invocations", "/ping"}
