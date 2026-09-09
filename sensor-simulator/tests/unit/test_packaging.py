"""Unit tests for the packaging artifacts (tasks 20.1, 20.2).

These read the committed files rather than building or running anything, so they
belong to the offline suite; the checks that need a container engine or a broker
are marked `integration` in test_integration.py (task 20.4).

- Req 16.5: one image runs the whole swarm in one process, defaults to REST-only
  with no config file, and answers GET /health shortly after start.
- Req 16.6: the Compose stack is the simulator plus a local MQTT broker and
  starts with NO AWS credentials present.
- Req 16.8: no secret is committed — the API key and X.509 material arrive at
  run time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_DOCKERFILE = _ROOT / "sensor-simulator" / "Dockerfile"
_COMPOSE = _ROOT / "docker-compose.yml"
_MOSQUITTO = _ROOT / "sensor-simulator" / "compose" / "mosquitto.conf"


def _dockerfile() -> str:
    return _DOCKERFILE.read_text()


def _compose() -> str:
    return _COMPOSE.read_text()


# --- Req 16.5 the image ----------------------------------------------------

def test_dockerfile_exists() -> None:
    assert _DOCKERFILE.exists()


def test_image_pins_the_interpreter_to_the_devbox_version() -> None:
    # the toolchain must not drift between the image and a contributor's shell
    assert "python:3.12.14-slim" in _dockerfile()


def test_image_installs_from_the_committed_lockfile() -> None:
    body = _dockerfile()
    assert "uv.lock" in body
    assert "--frozen" in body  # fails rather than re-resolving a stale lock


def test_image_runs_one_process_for_the_whole_swarm() -> None:
    # a single entry point, not a per-sensor process
    assert 'ENTRYPOINT ["python", "-m", "aqm_simulator.cli"]' in _dockerfile()


def test_image_defaults_to_the_rest_interface() -> None:
    assert "AQM_INTERFACE=rest" in _dockerfile()


def test_image_declares_a_health_probe() -> None:
    body = _dockerfile()
    assert "HEALTHCHECK" in body
    assert "/health" in body


def test_image_runs_unprivileged() -> None:
    body = _dockerfile()
    assert "USER simulator" in body
    assert "useradd" in body


def test_image_bakes_in_no_secret() -> None:
    body = _dockerfile()
    # no key material or API key value may be present in a committed file
    assert "AQM_API_KEY=" not in body
    assert "BEGIN PRIVATE KEY" not in body
    assert "BEGIN CERTIFICATE" not in body


# --- Req 16.6 the Compose stack -------------------------------------------

def test_compose_defines_simulator_and_broker() -> None:
    body = _compose()
    assert "simulator:" in body
    assert "broker:" in body
    assert "eclipse-mosquitto" in body


def _compose_effective() -> str:
    """The Compose file with comments removed.

    Assertions about what the stack DOES must read the effective configuration:
    a comment mentioning ~/.aws is prose, while a mount of it would be real.
    """
    lines = []
    for line in _COMPOSE.read_text().splitlines():
        stripped = line.split("#", 1)[0] if not line.strip().startswith("#") else ""
        lines.append(stripped)
    return "\n".join(lines)


def test_compose_sets_no_aws_credentials() -> None:
    body = _compose_effective()
    for forbidden in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "~/.aws",
        ".aws:",
    ):
        assert forbidden not in body


def test_compose_takes_the_api_key_from_the_environment() -> None:
    body = _compose()
    assert "AQM_API_KEY: ${AQM_API_KEY" in body  # interpolated, never literal


def test_compose_commits_no_secret_value() -> None:
    body = _compose()
    assert "BEGIN PRIVATE KEY" not in body
    assert "BEGIN CERTIFICATE" not in body


def test_compose_binds_published_ports_to_loopback() -> None:
    body = _compose()
    # every published port stays on loopback for local development
    for line in body.splitlines():
        stripped = line.strip()
        is_published_port = (
            stripped.startswith('- "')
            and "/" not in stripped
            and stripped.count(":") >= 2
        )
        if is_published_port:
            assert "127.0.0.1:" in stripped, stripped


def test_compose_mounts_credentials_read_only() -> None:
    assert "/app/certs:ro" in _compose()


# --- broker configuration --------------------------------------------------

def test_broker_requires_tls_with_client_certificates() -> None:
    body = _MOSQUITTO.read_text()
    assert "require_certificate true" in body
    assert "cafile" in body
    assert "listener 8883" in body


def test_broker_certificate_paths_are_generated_not_committed() -> None:
    body = _MOSQUITTO.read_text()
    assert "/mosquitto/certs/" in body
    # the referenced tree is git-ignored
    gitignore = (_ROOT / ".gitignore").read_text()
    assert "certs/" in gitignore


@pytest.mark.parametrize(
    "pattern", ["*.crt", "*.key", "*.pem", "certs/", ".env.local"]
)
def test_gitignore_excludes_credential_material(pattern: str) -> None:
    assert pattern in (_ROOT / ".gitignore").read_text()
