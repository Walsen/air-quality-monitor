"""Unit tests for credential and API-key resolution (task 6.3).

Requirement 13.4: expand the {SiteCode} credential path template into one
certificate and one key path per sensor, with an optional per-SiteCode override.
Requirement 14.6: the REST API key comes from an environment variable or a
runtime-injected secret of 16..256 characters, never from source or a committed
file.
Requirements 14.9/16.9/15.10: an absent/unreadable required value is reported
with one message per value naming the config value (and SiteCode for creds),
and the secret value itself never appears in any message.
"""

from __future__ import annotations

import pytest

from aqm_simulator.config.credentials import (
    ApiKeyError,
    resolve_api_key,
    resolve_credential_paths,
)


def test_template_expands_per_sitecode() -> None:
    paths = resolve_credential_paths(
        site_codes=["CB0001", "CB0002"],
        cert_template="certs/{SiteCode}/client.crt",
        key_template="certs/{SiteCode}/client.key",
        overrides={},
    )
    assert paths["CB0001"].cert == "certs/CB0001/client.crt"
    assert paths["CB0001"].key == "certs/CB0001/client.key"
    assert paths["CB0002"].cert == "certs/CB0002/client.crt"


def test_per_sitecode_override_replaces_template() -> None:
    paths = resolve_credential_paths(
        site_codes=["CB0001", "CB0002"],
        cert_template="certs/{SiteCode}/client.crt",
        key_template="certs/{SiteCode}/client.key",
        overrides={"CB0002": ("/custom/cb2.crt", "/custom/cb2.key")},
    )
    assert paths["CB0002"].cert == "/custom/cb2.crt"
    assert paths["CB0002"].key == "/custom/cb2.key"
    assert paths["CB0001"].cert == "certs/CB0001/client.crt"  # template still


def test_api_key_from_env() -> None:
    key = resolve_api_key(env={"AQM_API_KEY": "k" * 20})
    assert key == "k" * 20


def test_api_key_absent_raises_without_leaking() -> None:
    with pytest.raises(ApiKeyError) as exc:
        resolve_api_key(env={})
    msg = str(exc.value)
    assert "AQM_API_KEY" in msg  # names the config value


def test_api_key_too_short_rejected_without_echoing_value() -> None:
    secret = "short"
    with pytest.raises(ApiKeyError) as exc:
        resolve_api_key(env={"AQM_API_KEY": secret})
    msg = str(exc.value)
    assert "AQM_API_KEY" in msg
    assert secret not in msg  # the value itself must never appear (Req 14.10/15.10)


def test_api_key_too_long_rejected() -> None:
    with pytest.raises(ApiKeyError):
        resolve_api_key(env={"AQM_API_KEY": "x" * 257})


def test_api_key_boundary_lengths_accepted() -> None:
    assert resolve_api_key(env={"AQM_API_KEY": "a" * 16}) == "a" * 16
    assert resolve_api_key(env={"AQM_API_KEY": "b" * 256}) == "b" * 256
