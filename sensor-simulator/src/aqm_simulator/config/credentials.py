"""Credential and API-key resolution.

Expands the ``{SiteCode}`` MQTT credential path template into one certificate
and one key path per sensor, honouring an optional per-``SiteCode`` override
(Requirement 13.4). Resolves the REST API key from an environment variable or a
runtime-injected secret of 16..256 characters, never from source or a committed
file (Requirement 14.6).

Security (§7): no secret is ever placed in a committed file, and no error
message here contains the secret *value* — messages reference the configuration
value by name only (Requirements 14.10, 15.10), so a misconfiguration is
diagnosable without leaking the key.
"""

from __future__ import annotations

from dataclasses import dataclass

_API_KEY_ENV = "AQM_API_KEY"
_API_KEY_MIN = 16
_API_KEY_MAX = 256


@dataclass(frozen=True, slots=True)
class CredentialPaths:
    """The resolved certificate and key paths for one Virtual_Sensor."""

    cert: str
    key: str


class ApiKeyError(ValueError):
    """Raised when the API key is absent or out of the permitted length range.

    The message names the configuration value but never the secret value.
    """


def resolve_credential_paths(
    site_codes: list[str],
    cert_template: str,
    key_template: str,
    overrides: dict[str, tuple[str, str]],
) -> dict[str, CredentialPaths]:
    """Expand the templates per SiteCode, applying any per-SiteCode override."""
    resolved: dict[str, CredentialPaths] = {}
    for site_code in site_codes:
        if site_code in overrides:
            cert, key = overrides[site_code]
        else:
            cert = cert_template.replace("{SiteCode}", site_code)
            key = key_template.replace("{SiteCode}", site_code)
        resolved[site_code] = CredentialPaths(cert=cert, key=key)
    return resolved


def resolve_api_key(env: dict[str, str]) -> str:
    """Resolve the REST API key from the environment.

    Raises :class:`ApiKeyError` naming the configuration value (never the value
    itself) when the key is absent, empty, or outside 16..256 characters.
    """
    value = env.get(_API_KEY_ENV)
    if not value:
        raise ApiKeyError(f"{_API_KEY_ENV} is not set; the REST API key secret is missing")
    length = len(value)
    if not (_API_KEY_MIN <= length <= _API_KEY_MAX):
        raise ApiKeyError(
            f"{_API_KEY_ENV} length {length} is outside the permitted "
            f"range {_API_KEY_MIN}..{_API_KEY_MAX} characters"
        )
    return value
