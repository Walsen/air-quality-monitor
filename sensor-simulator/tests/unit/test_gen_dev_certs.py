"""Unit tests for development certificate generation (task 20.3).

- Req 16.11: material exists for EVERY sensor in the configured swarm at the
  paths resolved from the credential path template.
- Req 13.3: each Virtual_Sensor gets a certificate and key used by no other.
- Req 13.4: paths come from the {SiteCode} template.
- §7: a private key is written with owner-only permissions and never printed.

These tests generate real RSA material, so they keep the swarm tiny.
"""

from __future__ import annotations

import datetime as dt
import stat
import sys
from pathlib import Path

from cryptography import x509

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(_SCRIPTS))

from gen_dev_certs import build_dev_ca, generate, issue_sensor_certificate  # noqa: E402


def _env(tmp_path: Path, size: int = 2) -> dict[str, str]:
    return {
        "AQM_SWARM_SIZE": str(size),
        "AQM_SEED": "5",
        "AQM_MQTT_CA_PATH": str(tmp_path / "ca.crt"),
        "AQM_MQTT_CA_KEY_PATH": str(tmp_path / "ca.key"),
        "AQM_MQTT_CERT_TEMPLATE": str(tmp_path / "{SiteCode}" / "client.crt"),
        "AQM_MQTT_KEY_TEMPLATE": str(tmp_path / "{SiteCode}" / "client.key"),
    }


def test_generates_material_for_every_sensor(tmp_path: Path) -> None:
    site_codes = generate(env=_env(tmp_path, size=2))
    assert len(site_codes) == 2
    for site_code in site_codes:
        assert (tmp_path / site_code / "client.crt").exists()
        assert (tmp_path / site_code / "client.key").exists()


def test_ca_is_written(tmp_path: Path) -> None:
    generate(env=_env(tmp_path))
    assert (tmp_path / "ca.crt").exists()
    assert (tmp_path / "ca.key").exists()


def test_each_sensor_key_is_distinct(tmp_path: Path) -> None:
    site_codes = generate(env=_env(tmp_path, size=2))
    keys = {(tmp_path / code / "client.key").read_bytes() for code in site_codes}
    assert len(keys) == 2  # no key is shared between Virtual_Sensors (Req 13.3)


def test_private_keys_are_owner_only(tmp_path: Path) -> None:
    site_codes = generate(env=_env(tmp_path, size=1))
    key_path = tmp_path / site_codes[0] / "client.key"
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600  # §7: not group- or world-readable


def test_certificate_common_name_is_the_sitecode(tmp_path: Path) -> None:
    site_codes = generate(env=_env(tmp_path, size=1))
    pem = (tmp_path / site_codes[0] / "client.crt").read_bytes()
    certificate = x509.load_pem_x509_certificate(pem)
    common_names = [
        attribute.value
        for attribute in certificate.subject
        if attribute.oid == x509.oid.NameOID.COMMON_NAME
    ]
    assert common_names == [site_codes[0]]


def test_sensor_certificate_is_signed_by_the_ca(tmp_path: Path) -> None:
    site_codes = generate(env=_env(tmp_path, size=1))
    ca = x509.load_pem_x509_certificate((tmp_path / "ca.crt").read_bytes())
    leaf = x509.load_pem_x509_certificate(
        (tmp_path / site_codes[0] / "client.crt").read_bytes()
    )
    assert leaf.issuer == ca.subject


def test_ca_is_marked_as_a_certificate_authority() -> None:
    certificate, _ = build_dev_ca(dt.datetime(2026, 1, 1))
    constraints = certificate.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.value.ca is True


def test_leaf_is_not_a_certificate_authority() -> None:
    ca_certificate, ca_key = build_dev_ca(dt.datetime(2026, 1, 1))
    leaf, _ = issue_sensor_certificate(
        "CB0001", ca_certificate, ca_key, dt.datetime(2026, 1, 1)
    )
    constraints = leaf.extensions.get_extension_for_class(x509.BasicConstraints)
    assert constraints.value.ca is False
