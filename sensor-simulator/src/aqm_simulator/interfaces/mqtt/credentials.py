"""MQTT credential resolution and startup validation.

Requirement 13.4 resolves one certificate and one private key per
Virtual_Sensor from a path template containing a ``{SiteCode}`` placeholder, with
an optional explicit override for an individual sensor, so the same build targets
a local broker or AWS IoT Core with no source change.

Requirement 13.9 makes the check fail-fast at startup: every absent or unreadable
value produces its OWN problem naming the configuration value and the affected
``SiteCode``, so the operator sees the full list at once rather than fixing one
path per restart (engineering-practices §5). Nothing here reads file CONTENTS, so
no key or certificate material can reach a log or a message (§7).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_PLACEHOLDER = "{SiteCode}"
_CA_VALUE = "mqtt_ca_path"
_CERT_VALUE = "mqtt_cert_path"
_KEY_VALUE = "mqtt_key_path"


@dataclass(frozen=True, slots=True)
class SensorCredentials:
    """One Virtual_Sensor's client identity (Requirement 13.3)."""

    certificate: Path
    private_key: Path


@dataclass(frozen=True, slots=True)
class CredentialProblem:
    """One absent or unreadable credential value (Requirement 13.9)."""

    config_value: str
    site_code: str | None
    path: Path
    reason: str

    def __str__(self) -> str:
        where = f" for {self.site_code}" if self.site_code else ""
        return f"{self.config_value}{where}: {self.path} is {self.reason}"


def resolve_sensor_credentials(
    site_codes: list[str],
    cert_template: str,
    key_template: str,
    overrides: dict[str, tuple[Path, Path]] | None = None,
) -> dict[str, SensorCredentials]:
    """Resolve per-sensor credential paths from the templates (Requirement 13.4)."""
    for name, template in (("certificate", cert_template), ("private key", key_template)):
        if _PLACEHOLDER not in template:
            raise ValueError(
                f"{name} path template must contain the {_PLACEHOLDER} placeholder; "
                f"got {template!r}"
            )
    supplied = overrides or {}
    resolved: dict[str, SensorCredentials] = {}
    for site_code in site_codes:
        if site_code in supplied:
            cert, key = supplied[site_code]
            resolved[site_code] = SensorCredentials(cert, key)
            continue
        resolved[site_code] = SensorCredentials(
            Path(cert_template.replace(_PLACEHOLDER, site_code)),
            Path(key_template.replace(_PLACEHOLDER, site_code)),
        )
    return resolved


def _inspect(path: Path, config_value: str, site_code: str | None) -> CredentialProblem | None:
    """Return a problem when the path is absent or unreadable, else None."""
    if not path.exists():
        return CredentialProblem(config_value, site_code, path, "absent")
    if not os.access(path, os.R_OK):
        return CredentialProblem(config_value, site_code, path, "unreadable")
    return None


def validate_credentials(
    ca_path: Path,
    credentials: dict[str, tuple[Path, Path]],
) -> list[CredentialProblem]:
    """Report every absent or unreadable credential value (Requirement 13.9).

    Accumulates rather than raising on the first fault, so one startup reports
    every problem. The caller exits non-zero when the list is non-empty and does
    not begin signal generation.
    """
    problems: list[CredentialProblem] = []
    ca_problem = _inspect(ca_path, _CA_VALUE, None)
    if ca_problem is not None:
        problems.append(ca_problem)
    for site_code, (cert, key) in credentials.items():
        for path, value in ((cert, _CERT_VALUE), (key, _KEY_VALUE)):
            problem = _inspect(path, value, site_code)
            if problem is not None:
                problems.append(problem)
    return problems
