"""The MQTT push interface.

The publisher depends on a narrow :class:`MqttTransport` abstraction rather than
on a concrete client (engineering-practices §1 Dependency Inversion, §4 Adapter),
so a local Mosquitto broker and AWS IoT Core are interchangeable with no source
change, and the unit suite drives an in-memory double with no broker present.

- Requirement 13.1: each record is published to ``aqm/sensors/{SiteCode}/data``.
- Requirement 13.2: the payload is the Serializer output for exactly ONE record;
  records are never combined into one message.
- Requirement 13.3: :class:`TlsMqttTransport` connects only over TLS, validates
  the broker chain against the configured CA (a failed validation surfaces as a
  connection failure), and authenticates with a per-Virtual_Sensor certificate
  and private key.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from aqm_simulator.contract.records import SensorDataRecord
from aqm_simulator.contract.serializer import serialize_data

_TOPIC_TEMPLATE = "aqm/sensors/{site_code}/data"


def topic_for(site_code: str) -> str:
    """Return the publish topic for a Virtual_Sensor (Requirement 13.1)."""
    return _TOPIC_TEMPLATE.format(site_code=site_code)


@dataclass(frozen=True, slots=True)
class PublishedMessage:
    """One MQTT message: a topic and a single-record payload."""

    topic: str
    payload: str


@runtime_checkable
class MqttTransport(Protocol):
    """The narrow transport boundary the publisher depends on."""

    async def connect(self) -> None:
        """Establish the connection, raising on failure."""
        ...

    async def disconnect(self) -> None:
        """Close the connection."""
        ...

    async def publish(self, topic: str, payload: str) -> None:
        """Publish one payload to one topic."""
        ...


class MqttConnectionError(RuntimeError):
    """A connection could not be established or was lost.

    A failed broker chain validation is reported as this, so a TLS mismatch is
    handled on the same path as any other connection failure (Requirement 13.3).
    """


class RejectionCategory(StrEnum):
    """Why the broker refused a Virtual_Sensor (Requirement 13.10)."""

    CERTIFICATE = "certificate"
    CLIENT_AUTH = "client_auth"


class MqttAuthRejectionError(RuntimeError):
    """The broker refused this Virtual_Sensor's identity.

    Distinct from :class:`MqttConnectionError` because the two have opposite
    remedies: a connection failure is retried for as long as the Simulator runs
    (Requirement 13.5), while repeated identity rejections mean the credentials
    will not start working, so attempts stop for that Virtual_Sensor alone
    (Requirement 13.10).
    """

    def __init__(self, site_code: str, category: RejectionCategory) -> None:
        super().__init__(f"broker rejected {site_code}: {category}")
        self.site_code = site_code
        self.category = category


class MqttPublisher:
    """Publishes Sensor_Data_Records, one record per message."""

    def __init__(self, transport: MqttTransport) -> None:
        self._transport = transport

    async def publish_all(self, records: list[SensorDataRecord]) -> None:
        """Publish each record as its own message, preserving the given order."""
        for record in records:
            await self.publish_one(record)

    async def publish_one(self, record: SensorDataRecord) -> None:
        """Publish exactly one record (Requirement 13.2)."""
        await self._transport.publish(
            topic_for(record.SiteCode),
            serialize_data(record),
        )


def build_tls_context(ca_path: Path, cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Build the TLS context for one Virtual_Sensor's identity.

    Chain validation against the configured CA is mandatory, and the client is
    authenticated with a certificate and key used by no other Virtual_Sensor
    (Requirement 13.3). A missing or unreadable file raises ``OSError``, which
    the caller reports per affected value (Requirement 13.9).
    """
    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH, cafile=str(ca_path)
    )
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context
