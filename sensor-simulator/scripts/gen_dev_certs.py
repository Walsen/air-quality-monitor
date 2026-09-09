"""Generate development X.509 material for the configured swarm.

Requirement 16.11: produces a development CA plus one certificate and one private
key for EVERY Virtual_Sensor in the configured swarm, at the paths resolved from
the credential path template (Requirement 13.4), so the MQTT interface has a
distinct identity per sensor (Requirement 13.3).

Everything written here is development-only material and is git-ignored;
Requirement 16.8 forbids committing any key, certificate, or API key. The script
never prints a private key.

Run through the documented command surface: ``just gen-certs``.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# Import from the package so identity and path resolution are single-sourced.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aqm_simulator.config.loader import resolve_config
from aqm_simulator.geography.registry import ProfileRegistry
from aqm_simulator.interfaces.mqtt.credentials import (
    resolve_sensor_credentials,
)
from aqm_simulator.observability.logging import configure_logging, get_logger
from aqm_simulator.rng.streams import RandomStreamFactory
from aqm_simulator.swarm.factory import build_swarm

_DEV_VALIDITY_DAYS = 365
_KEY_SIZE = 2048
_OWNER_ONLY = 0o600
_DEFAULT_CA_PATH = "certs/ca.crt"
_DEFAULT_CA_KEY_PATH = "certs/ca.key"
_DEFAULT_CERT_TEMPLATE = "certs/{SiteCode}/client.crt"
_DEFAULT_KEY_TEMPLATE = "certs/{SiteCode}/client.key"


def _write_private_key(key: rsa.RSAPrivateKey, path: Path) -> None:
    """Write a key with owner-only permissions and never log its contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    os.chmod(path, _OWNER_ONLY)


def _write_certificate(certificate: x509.Certificate, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Air Quality Monitor (dev)"),
        ]
    )


def build_dev_ca(
    not_before: dt.datetime,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Create a self-signed development certificate authority."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    subject = _name("Air Quality Monitor development CA")
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + dt.timedelta(days=_DEV_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return certificate, key


def issue_sensor_certificate(
    site_code: str,
    ca_certificate: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    not_before: dt.datetime,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Issue one certificate whose Common Name is the sensor's SiteCode."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(site_code))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + dt.timedelta(days=_DEV_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(site_code)]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return certificate, key


def issue_server_certificate(
    hostnames: list[str],
    ca_certificate: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    not_before: dt.datetime,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Issue the broker's server certificate.

    The Compose broker terminates TLS, so it needs its own leaf signed by the
    same development CA the publisher validates against (Requirement 13.3).
    Subject Alternative Names cover every hostname the broker is reached by, so
    hostname verification succeeds from inside the stack and from the host.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_SIZE)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(_name(hostnames[0]))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + dt.timedelta(days=_DEV_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(h) for h in hostnames]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return certificate, key


def generate(env: dict[str, str] | None = None) -> list[str]:
    """Generate the CA and per-sensor material, returning the SiteCodes covered."""
    environment = env if env is not None else dict(os.environ)
    config = resolve_config(env=environment, file_data={})

    registry = ProfileRegistry.with_builtins()
    profile = registry.get(config.profile_name)
    factory = RandomStreamFactory(seed=config.seed if config.seed is not None else 0)
    swarm = build_swarm(size=config.swarm_size, profile=profile, factory=factory)
    site_codes = [sensor.site_code for sensor in swarm]

    credentials = resolve_sensor_credentials(
        site_codes=site_codes,
        cert_template=environment.get("AQM_MQTT_CERT_TEMPLATE", _DEFAULT_CERT_TEMPLATE),
        key_template=environment.get("AQM_MQTT_KEY_TEMPLATE", _DEFAULT_KEY_TEMPLATE),
    )

    # A fixed not_before keeps repeated runs comparable; validity is dev-only.
    not_before = dt.datetime(2026, 1, 1, tzinfo=dt.UTC).replace(tzinfo=None)
    ca_certificate, ca_key = build_dev_ca(not_before)
    _write_certificate(
        ca_certificate, Path(environment.get("AQM_MQTT_CA_PATH", _DEFAULT_CA_PATH))
    )
    _write_private_key(
        ca_key, Path(environment.get("AQM_MQTT_CA_KEY_PATH", _DEFAULT_CA_KEY_PATH))
    )

    for site_code, paths in credentials.items():
        certificate, key = issue_sensor_certificate(
            site_code, ca_certificate, ca_key, not_before
        )
        _write_certificate(certificate, paths.certificate)
        _write_private_key(key, paths.private_key)

    # The Compose broker terminates TLS, so it needs its own leaf from this CA.
    # 'broker' is its service name inside the stack; localhost covers host access.
    broker_dir = Path(environment.get("AQM_BROKER_CERT_DIR", "certs/broker"))
    server_certificate, server_key = issue_server_certificate(
        ["broker", "localhost"], ca_certificate, ca_key, not_before
    )
    _write_certificate(server_certificate, broker_dir / "server.crt")
    _write_private_key(server_key, broker_dir / "server.key")

    return site_codes


def main() -> int:
    # §6: structured JSON to stdout, never print(). Only counts and the sensor
    # count are reported — never any key material (§7).
    configure_logging("info")
    logger = get_logger("gen_dev_certs")
    site_codes = generate()
    logger.info("dev_credentials_generated", sensors=len(site_codes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
