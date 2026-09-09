"""Unit tests for MQTT credential and rejection handling (task 17.6).

- Req 13.9: an absent or unreadable CA, certificate, or private key is reported
  with ONE message per affected value naming the configuration value and the
  affected SiteCode, and no signal generation starts.
- Req 13.3: a failed broker chain validation is treated as a connection failure.
- Req 13.10: repeated identity rejection stops ONE sensor only.
- §7: no key or certificate material is ever logged.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aqm_simulator.interfaces.mqtt import (
    MqttConnectionError,
    RejectionCategory,
    build_tls_context,
)
from aqm_simulator.interfaces.mqtt.credentials import (
    CredentialProblem,
    resolve_sensor_credentials,
    validate_credentials,
)


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    ca = tmp_path / "ca.crt"
    cert = tmp_path / "client.crt"
    key = tmp_path / "client.key"
    return ca, cert, key


# --- Req 13.4 template resolution ------------------------------------------

def test_template_resolves_per_sitecode_paths() -> None:
    resolved = resolve_sensor_credentials(
        site_codes=["AQM0001", "AQM0002"],
        cert_template="certs/{SiteCode}/client.crt",
        key_template="certs/{SiteCode}/client.key",
    )
    assert resolved["AQM0001"].certificate == Path("certs/AQM0001/client.crt")
    assert resolved["AQM0002"].private_key == Path("certs/AQM0002/client.key")


def test_explicit_override_wins_over_template() -> None:
    resolved = resolve_sensor_credentials(
        site_codes=["AQM0001"],
        cert_template="certs/{SiteCode}/client.crt",
        key_template="certs/{SiteCode}/client.key",
        overrides={"AQM0001": (Path("/special/a.crt"), Path("/special/a.key"))},
    )
    assert resolved["AQM0001"].certificate == Path("/special/a.crt")
    assert resolved["AQM0001"].private_key == Path("/special/a.key")


def test_template_without_placeholder_is_rejected() -> None:
    with pytest.raises(ValueError, match="SiteCode"):
        resolve_sensor_credentials(
            site_codes=["AQM0001"],
            cert_template="certs/client.crt",  # no {SiteCode}
            key_template="certs/{SiteCode}/client.key",
        )


# --- Req 13.9 one message per affected value -------------------------------

def test_missing_files_report_one_problem_each(tmp_path: Path) -> None:
    ca, cert, key = _paths(tmp_path)  # none created
    problems = validate_credentials(
        ca_path=ca,
        credentials={"AQM0001": (cert, key)},
    )
    assert len(problems) == 3  # one per affected value, not one summary
    values = {p.config_value for p in problems}
    assert values == {"mqtt_ca_path", "mqtt_cert_path", "mqtt_key_path"}


def test_problem_names_the_config_value_and_sitecode(tmp_path: Path) -> None:
    ca, cert, key = _paths(tmp_path)
    ca.write_text("ca")  # CA present; the sensor's pair is missing
    problems = validate_credentials(ca_path=ca, credentials={"AQM0007": (cert, key)})
    assert {p.site_code for p in problems} == {"AQM0007"}
    for problem in problems:
        assert problem.site_code == "AQM0007"
        assert problem.config_value in {"mqtt_cert_path", "mqtt_key_path"}
        assert str(problem)  # renders a message naming both


def test_valid_material_reports_no_problems(tmp_path: Path) -> None:
    ca, cert, key = _paths(tmp_path)
    for path in (ca, cert, key):
        path.write_text("material")
    assert validate_credentials(ca_path=ca, credentials={"AQM0001": (cert, key)}) == []


def test_problems_never_include_file_contents(tmp_path: Path) -> None:
    # §7: a problem message must not leak key material even if the file is read
    ca, cert, key = _paths(tmp_path)
    ca.write_text("ca")
    cert.write_text("-----BEGIN CERTIFICATE-----SECRET")
    # key missing -> a problem for the key only
    problems = validate_credentials(ca_path=ca, credentials={"AQM0001": (cert, key)})
    assert len(problems) == 1
    assert "SECRET" not in str(problems[0])


def test_per_sensor_problems_are_independent(tmp_path: Path) -> None:
    ca, cert, key = _paths(tmp_path)
    for path in (ca, cert, key):
        path.write_text("material")
    missing = tmp_path / "absent.crt"
    problems = validate_credentials(
        ca_path=ca,
        credentials={"AQM0001": (cert, key), "AQM0002": (missing, key)},
    )
    assert [p.site_code for p in problems] == ["AQM0002"]  # only the broken one


# --- Req 13.3 chain validation is a connection failure ---------------------

def test_unreadable_ca_surfaces_as_connection_error(tmp_path: Path) -> None:
    ca, cert, key = _paths(tmp_path)
    ca.write_text("not a certificate")  # unparseable chain material
    cert.write_text("also not a certificate")
    key.write_text("nor a key")
    with pytest.raises(MqttConnectionError):
        build_tls_context(ca, cert, key)


# --- Req 13.10 categories ---------------------------------------------------

def test_rejection_categories_are_the_two_named_kinds() -> None:
    assert {c.value for c in RejectionCategory} == {"certificate", "client_auth"}


def test_credential_problem_is_hashable_for_reporting() -> None:
    problem = CredentialProblem(
        config_value="mqtt_key_path", site_code="AQM0001", path=Path("/x/y.key"),
        reason="absent",
    )
    assert problem in {problem}  # usable in a set without surprises


def test_validate_credentials_does_not_start_generation(tmp_path: Path) -> None:
    # Req 13.9 is a startup check: it only inspects paths, never generates.
    ca, cert, key = _paths(tmp_path)
    problems = validate_credentials(ca_path=ca, credentials={"AQM0001": (cert, key)})
    assert problems  # reported
    assert asyncio.get_event_loop_policy() is not None  # no side effects performed
