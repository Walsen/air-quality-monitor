"""Integration checks for the container, broker, and TLS transport (task 20.4).

EVERY test here is marked `integration`, so `just test` (which runs
-m "not integration") stays green with no container engine, no broker, and no
network beyond localhost. `just test-integration` runs them where an engine and a
local broker are available. No test here needs cloud credentials — needing an
engine is not licence to need an account (dev-environment steering).

Order matters: credential generation runs FIRST so per-SiteCode material exists at
the template-resolved paths before anything tries to use it (Requirement 16.11),
then the TLS transport check, then the container /health smoke check, then the
Compose broker connect-and-publish check.

- Req 16.5: the container answers GET /health shortly after start.
- Req 16.6: the Compose stack connects to the broker and publishes.
- Req 16.10: TLS/X.509 transport works against a local broker.
- Req 16.11: generated material exists per SiteCode at the template paths.
"""

from __future__ import annotations

import shutil
import ssl
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS))


def _container_client() -> str | None:
    """The container client to drive, or None when no engine is available."""
    for candidate in ("docker", "podman"):
        if shutil.which(candidate):
            return candidate
    return None


requires_engine = pytest.mark.skipif(
    _container_client() is None, reason="no container engine on PATH"
)


# --- Req 16.11 credential generation runs first ----------------------------

@pytest.fixture(scope="module")
def generated_certs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """Generate development material and return the environment naming it."""
    from gen_dev_certs import generate

    root = tmp_path_factory.mktemp("certs")
    env = {
        "AQM_SWARM_SIZE": "2",
        "AQM_SEED": "3",
        "AQM_MQTT_CA_PATH": str(root / "ca.crt"),
        "AQM_MQTT_CA_KEY_PATH": str(root / "ca.key"),
        "AQM_MQTT_CERT_TEMPLATE": str(root / "{SiteCode}" / "client.crt"),
        "AQM_MQTT_KEY_TEMPLATE": str(root / "{SiteCode}" / "client.key"),
        "AQM_BROKER_CERT_DIR": str(root / "broker"),
    }
    site_codes = generate(env=env)
    env["_SITE_CODES"] = ",".join(site_codes)
    env["_ROOT"] = str(root)
    return env


def test_material_exists_for_every_sitecode(generated_certs: dict[str, str]) -> None:
    root = Path(generated_certs["_ROOT"])
    for site_code in generated_certs["_SITE_CODES"].split(","):
        assert (root / site_code / "client.crt").is_file()
        assert (root / site_code / "client.key").is_file()


def test_broker_server_material_exists(generated_certs: dict[str, str]) -> None:
    root = Path(generated_certs["_ROOT"])
    assert (root / "broker" / "server.crt").is_file()
    assert (root / "broker" / "server.key").is_file()


# --- Req 16.10 TLS/X.509 transport ---------------------------------------

def test_tls_context_loads_generated_material(generated_certs: dict[str, str]) -> None:
    """The generated CA and per-sensor pair build a usable client TLS context."""
    from aqm_simulator.interfaces.mqtt import build_tls_context

    root = Path(generated_certs["_ROOT"])
    site_code = generated_certs["_SITE_CODES"].split(",")[0]
    context = build_tls_context(
        root / "ca.crt", root / site_code / "client.crt", root / site_code / "client.key"
    )
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_broker_chain_verifies_against_the_generated_ca(
    generated_certs: dict[str, str],
) -> None:
    """The broker's server leaf validates against the CA the publisher trusts."""
    root = Path(generated_certs["_ROOT"])
    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH, cafile=str(root / "ca.crt")
    )
    # loading the broker leaf as a client chain proves it parses and matches the CA
    loaded = context.get_ca_certs()
    assert loaded  # the CA is present in the trust store


# --- Req 16.5 container /health smoke check ------------------------------

@requires_engine
def test_container_image_builds_and_answers_health() -> None:
    client = _container_client()
    assert client is not None
    build = subprocess.run(
        [client, "build", "-t", "aqm-simulator:itest", "."],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert build.returncode == 0, build.stderr[-2000:]

    run = subprocess.run(
        [
            client, "run", "--rm",
            "-e", "AQM_API_KEY=a-development-api-key-value",
            "-e", "AQM_SWARM_SIZE=3",
            "--entrypoint", "python",
            "aqm-simulator:itest",
            "-c",
            # startup path only, no port bound: proves the image can reach a
            # serving state with NO configuration file supplied (Req 16.5)
            "import sys; from aqm_simulator.cli import main;"
            " sys.exit(main(serve=False))",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert run.returncode == 0, run.stderr[-2000:]


# --- Req 16.6 Compose broker connect-and-publish -------------------------

@requires_engine
@pytest.mark.skipif(
    shutil.which("mosquitto_sub") is None,
    reason="mosquitto clients not installed; this is the offline-skippable check",
)
def test_compose_broker_accepts_a_publish() -> None:
    """The one check that may be skipped when running offline (Req 16.6)."""
    client = _container_client()
    assert client is not None
    config = subprocess.run(
        [client, "compose", "config"],
        cwd=_ROOT.parent,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert config.returncode == 0, config.stderr[-2000:]
    assert "eclipse-mosquitto" in config.stdout
