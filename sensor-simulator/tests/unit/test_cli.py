"""Unit tests for the entry-point wiring (task 20.6).

- Req 15.6/15.7: with NO config file every value resolves from environment and
  documented defaults without error.
- Req 14.1/13.1: only the SELECTED interface is activated, defaulting to `rest`.
- Req 14.9: `rest` selected with no API key exits non-zero before serving.
- Req 13.9: `mqtt` selected with missing credential material exits non-zero,
  reporting one problem per affected value, and starts no generation.
- §2: the clock and random streams are injected, never taken from module state.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

import httpx
import pytest

from aqm_simulator.cli import StartupError, build_runtime, main

_KEY = "a-development-api-key-value"
_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _env(**overrides: str) -> dict[str, str]:
    base = {"AQM_SWARM_SIZE": "3", "AQM_SEED": "7"}
    base.update(overrides)
    return base


# --- Req 15.6/15.7 no config file needed -----------------------------------

def test_builds_with_no_config_file() -> None:
    runtime = build_runtime(env=_env(AQM_API_KEY=_KEY), now=lambda: _NOW)
    assert runtime.config.interface == "rest"  # documented default
    assert runtime.pipeline.swarm_size == 3


def test_defaults_to_rest_interface() -> None:
    runtime = build_runtime(env=_env(AQM_API_KEY=_KEY), now=lambda: _NOW)
    assert runtime.app is not None  # REST activated
    assert runtime.mqtt_credentials is None  # MQTT not activated


def test_only_selected_interface_is_activated() -> None:
    runtime = build_runtime(
        env=_env(
            AQM_INTERFACE="mqtt",
            AQM_MQTT_CA_PATH=__file__,  # an existing readable file stands in
            AQM_MQTT_CERT_TEMPLATE=__file__,
            AQM_MQTT_KEY_TEMPLATE=__file__,
        ),
        now=lambda: _NOW,
    )
    assert runtime.app is None  # REST not activated, so no key is required
    assert runtime.mqtt_credentials is not None


# --- Req 14.1 the app serves /health --------------------------------------

def test_health_is_served_without_a_config_file() -> None:
    runtime = build_runtime(env=_env(AQM_API_KEY=_KEY), now=lambda: _NOW)
    app = runtime.app
    assert app is not None

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://sim"
        ) as client:
            return await client.get("/health")

    response = asyncio.run(call())
    assert response.status_code == 200
    assert response.json()["SwarmSize"] == 3


# --- Req 14.9 missing API key --------------------------------------------

def test_rest_without_api_key_raises_startup_error() -> None:
    with pytest.raises(StartupError) as caught:
        build_runtime(env=_env(), now=lambda: _NOW)
    assert "API key" in str(caught.value)


def test_main_exits_non_zero_without_api_key() -> None:
    assert main(env=_env(), now=lambda: _NOW, serve=False) != 0


def test_main_exits_zero_when_wiring_succeeds() -> None:
    assert main(env=_env(AQM_API_KEY=_KEY), now=lambda: _NOW, serve=False) == 0


def test_startup_error_never_echoes_the_key() -> None:
    # §7: a diagnostic must not disclose secret material
    with pytest.raises(StartupError) as caught:
        build_runtime(env=_env(AQM_API_KEY="short"), now=lambda: _NOW)
    assert "short" not in str(caught.value)


# --- Req 13.9 missing MQTT credentials -----------------------------------

def test_mqtt_with_missing_credentials_raises_startup_error(tmp_path: Path) -> None:
    absent = str(tmp_path / "nope" / "{SiteCode}.crt")
    with pytest.raises(StartupError) as caught:
        build_runtime(
            env=_env(
                AQM_INTERFACE="mqtt",
                AQM_MQTT_CA_PATH=str(tmp_path / "absent-ca.crt"),
                AQM_MQTT_CERT_TEMPLATE=absent,
                AQM_MQTT_KEY_TEMPLATE=absent.replace(".crt", ".key"),
            ),
            now=lambda: _NOW,
        )
    message = str(caught.value)
    assert "mqtt_ca_path" in message  # one problem per affected value
    assert "mqtt_cert_path" in message


def test_mqtt_credential_failure_exits_non_zero(tmp_path: Path) -> None:
    code = main(
        env=_env(
            AQM_INTERFACE="mqtt",
            AQM_MQTT_CA_PATH=str(tmp_path / "absent.crt"),
            AQM_MQTT_CERT_TEMPLATE=str(tmp_path / "{SiteCode}.crt"),
            AQM_MQTT_KEY_TEMPLATE=str(tmp_path / "{SiteCode}.key"),
        ),
        now=lambda: _NOW,
        serve=False,
    )
    assert code != 0


# --- invalid configuration ------------------------------------------------

def test_invalid_configuration_exits_non_zero() -> None:
    # a 7-minute publish interval is not an integer multiple of a 2-minute tick
    assert main(
        env=_env(AQM_API_KEY=_KEY, AQM_TICK_MINUTES="2", AQM_PUBLISH_MINUTES="7"),
        now=lambda: _NOW,
        serve=False,
    ) != 0


def test_out_of_range_publish_interval_exits_non_zero() -> None:
    # outside the permitted 1 minute to 24 hours
    assert main(
        env=_env(AQM_API_KEY=_KEY, AQM_PUBLISH_MINUTES="2000"),
        now=lambda: _NOW,
        serve=False,
    ) != 0


def test_unknown_interface_is_rejected() -> None:
    with pytest.raises(StartupError, match="interface"):
        build_runtime(env=_env(AQM_API_KEY=_KEY, AQM_INTERFACE="carrier-pigeon"),
                      now=lambda: _NOW)


# --- §2 determinism -------------------------------------------------------

def test_same_seed_builds_an_identical_swarm() -> None:
    a = build_runtime(env=_env(AQM_API_KEY=_KEY), now=lambda: _NOW)
    b = build_runtime(env=_env(AQM_API_KEY=_KEY), now=lambda: _NOW)
    assert [s.site_code for s in a.pipeline.swarm] == [
        s.site_code for s in b.pipeline.swarm
    ]
