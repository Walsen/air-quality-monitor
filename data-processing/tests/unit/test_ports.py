"""Unit tests for the nine port protocols (task 3.2).

- Req 14.1/15.1/17.1/24.1/18.1: the domain depends on protocols, never on a
  concrete store, feed, or authenticator.
- DD1: no cloud type appears in any port signature — that is what keeps the whole
  offline suite runnable with no AWS credentials.
- Req 14.8: WindowResult carries its `truncated` flag IN THE TYPE, so reporting
  truncation is not left to a convention a caller can forget.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path
from typing import Protocol, get_type_hints

import pytest

from aqm_ingestion.ports import protocols as port_module
from aqm_ingestion.ports.protocols import (
    Authenticator,
    FeedClient,
    ForecastClient,
    MeteorologyProvider,
    MqttTransport,
    ProfileStore,
    RawArchive,
    ReadingsStore,
    SensorRegistryStore,
    WindowResult,
)

_NINE_PORTS = (
    ReadingsStore,
    SensorRegistryStore,
    RawArchive,
    ProfileStore,
    MeteorologyProvider,
    ForecastClient,
    MqttTransport,
    FeedClient,
    Authenticator,
)

# Names that would mean a cloud dependency leaked into the boundary.
_CLOUD_MARKERS = (
    "boto3", "botocore", "dynamodb", "Table", "S3", "s3_client", "ClientError",
    "aws", "AWS", "Cognito", "cognito",
)

_PORTS_DIR = Path(port_module.__file__).parent
_DOMAIN_DIR = _PORTS_DIR.parent / "domain"


def test_all_nine_ports_are_defined() -> None:
    assert len(_NINE_PORTS) == 9
    assert len(set(_NINE_PORTS)) == 9


@pytest.mark.parametrize("port", _NINE_PORTS, ids=lambda p: p.__name__)
def test_every_port_is_a_protocol(port: type) -> None:
    assert issubclass(port, Protocol)  # type: ignore[arg-type]


@pytest.mark.parametrize("port", _NINE_PORTS, ids=lambda p: p.__name__)
def test_every_port_declares_at_least_one_method(port: type) -> None:
    methods = [
        name
        for name, value in vars(port).items()
        if callable(value) and not name.startswith("_")
    ]
    assert methods, f"{port.__name__} declares no method"


@pytest.mark.parametrize("port", _NINE_PORTS, ids=lambda p: p.__name__)
def test_no_cloud_type_appears_in_a_port_signature(port: type) -> None:
    for name, value in vars(port).items():
        if name.startswith("_") or not callable(value):
            continue
        rendered = str(inspect.signature(value))
        for marker in _CLOUD_MARKERS:
            assert marker not in rendered, f"{port.__name__}.{name}: {marker}"


def test_ports_package_imports_no_cloud_sdk() -> None:
    for source in _PORTS_DIR.rglob("*.py"):
        body = source.read_text(encoding="utf-8")
        assert "import boto3" not in body
        assert "import botocore" not in body


def test_domain_imports_no_cloud_sdk() -> None:
    for source in _DOMAIN_DIR.rglob("*.py"):
        body = source.read_text(encoding="utf-8")
        assert "import boto3" not in body
        assert "import botocore" not in body


# --- Req 14.8 truncation lives in the type -------------------------------

def test_window_result_carries_a_truncated_flag() -> None:
    assert dataclasses.is_dataclass(WindowResult)
    hints = get_type_hints(WindowResult)
    assert "truncated" in hints
    assert hints["truncated"] is bool


def test_window_result_carries_the_readings() -> None:
    assert "readings" in get_type_hints(WindowResult)


def test_window_result_defaults_to_not_truncated() -> None:
    result = WindowResult(readings=())
    assert result.truncated is False


def test_window_result_is_immutable() -> None:
    result = WindowResult(readings=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.truncated = True  # type: ignore[misc]


# --- the readings store's window is half-open ---------------------------

def test_query_window_signature_names_start_and_end() -> None:
    signature = inspect.signature(ReadingsStore.query_window)
    assert {"site_code", "species", "start", "end"} <= set(signature.parameters)


def test_authenticator_verify_takes_only_a_credential() -> None:
    signature = inspect.signature(Authenticator.verify)
    # a narrow boundary: the authenticator gets the credential, not a request
    assert set(signature.parameters) == {"self", "credential"}
