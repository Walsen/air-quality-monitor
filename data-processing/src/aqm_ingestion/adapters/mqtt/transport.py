"""The real MQTT transport, behind the MqttTransport port.

Requirements 4.1, 4.5, 4.6, 29.2.

THE PORT'S SHAPE IS WHY THIS ADAPTER IS POSSIBLE. As first designed the port yielded
``(topic, payload)`` pairs with no acknowledgement, which could not express Requirement
4.6 at all — there was nothing to acknowledge and no way to leave a message unacked so
the broker redelivers it. Task 16.1 corrected it to yield a delivery tag with a separate
``acknowledge`` call, and this adapter is where that correction pays: manual
acknowledgement is exactly what a real broker offers, so the port now matches the
transport instead of the transport having to pretend.

NO KEY MATERIAL IS EVER AN ARGUMENT. The constructor takes certificate and key PATHS,
never contents, and loads them at connect time. A path is configuration; a private key
is not (§7), and a value the object never holds cannot be logged, repr'd or serialised
out of it. A test pins the parameter set so a later "convenience" argument carrying PEM
text fails.

MANUAL ACKNOWLEDGEMENT REQUIRES QoS 1. QoS 0 has no acknowledgement in the protocol, so
subscribing at QoS 0 would make ``acknowledge`` a silent no-op and Requirement 4.6's
redelivery guarantee would evaporate with nothing failing. The QoS is therefore not
configurable here.
"""

from __future__ import annotations

import queue
import ssl
from collections.abc import Iterator

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion

from aqm_ingestion.observability.logging import get_logger

_logger = get_logger("adapters.mqtt")

DEFAULT_TOPIC_FILTER = "aqm/sensors/+/data"
"""Requirement 4.1's default topic filter.

Single-level `+`, matching the ingest entry point's anchored extraction: a multi-level wildcard
would let `aqm/sensors/A/B/data` read as site `A/B` and attribute a reading to the WRONG SITE.
"""

SUBSCRIBE_QOS = 1
"""QoS 1, because manual acknowledgement is meaningless at QoS 0 (see the module docstring)."""

DEFAULT_KEEPALIVE_SECONDS = 60


class MqttTransportError(RuntimeError):
    """The transport could not be established or was lost.

    Raised rather than degraded because the push interface has nothing to serve without a
    subscription — unlike the forecast adapter, where Requirement 24.4 defines a useful
    degraded answer.
    """


class PahoMqttTransport:
    """The MqttTransport port over a real broker with mutual TLS.

    Messages arrive on the client's network thread and are handed to the consumer through a
    queue, so ``messages()`` stays a plain synchronous iterator: the port is synchronous because
    the ingest pipeline is, and pushing async through the whole pipeline would buy nothing while
    making the archive-before-ack ordering harder to see.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: str,
        ca_cert_path: str,
        client_cert_path: str,
        client_key_path: str,
        keepalive_seconds: int = DEFAULT_KEEPALIVE_SECONDS,
    ) -> None:
        """Configure the client. Nothing connects until ``subscribe`` is called.

        paho is imported at module level rather than lazily: it is a PINNED direct dependency,
        so
        a guarded import would have been unreachable defensive code for a library that is always
        present, and the real types are needed for the callbacks below.
        """
        self._host = host
        self._port = port
        self._keepalive = keepalive_seconds
        self._queue: queue.Queue[tuple[str, bytes, int]] = queue.Queue()
        self._client = paho.Client(
            client_id=client_id,
            callback_api_version=CallbackAPIVersion.VERSION2,
        )
        # Manual acknowledgement is the whole reason the port has an acknowledge() call.
        self._client.manual_ack_set(True)
        self._client.tls_set(
            ca_certs=ca_cert_path,
            certfile=client_cert_path,
            keyfile=client_key_path,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    def subscribe(self, topic_filter: str) -> None:
        """Connect and subscribe, logging one event (Requirement 4.1).

        Raises:
            MqttTransportError: naming the failure kind but never a credential path's contents.
        """
        try:
            self._client.connect(self._host, self._port, keepalive=self._keepalive)
            self._client.subscribe(topic_filter, qos=SUBSCRIBE_QOS)
        except OSError as failure:
            kind = type(failure).__name__
            _logger.error("mqtt_connect_failed", host=self._host, failure_kind=kind)
            raise MqttTransportError(f"could not subscribe to {topic_filter}: {kind}") from None

        self._client.loop_start()
        _logger.info(
            "mqtt_subscribed", host=self._host, topic_filter=topic_filter, qos=SUBSCRIBE_QOS
        )

    def messages(self) -> Iterator[tuple[str, bytes, int]]:
        """Yield (topic, payload, delivery_tag) as messages arrive."""
        while True:
            yield self._queue.get()

    def acknowledge(self, delivery_tag: int) -> None:
        """Acknowledge one message so the broker stops redelivering it (Requirement 4.6)."""
        self._client.ack(delivery_tag, SUBSCRIBE_QOS)

    def close(self) -> None:
        """Stop the network loop and disconnect."""
        self._client.loop_stop()
        self._client.disconnect()

    def _on_message(
        self, _client: paho.Client, _userdata: object, message: paho.MQTTMessage
    ) -> None:
        """Queue an arriving message. Runs on the client's network thread."""
        self._queue.put((message.topic, bytes(message.payload), int(message.mid)))

    def _on_disconnect(
        self,
        _client: paho.Client,
        _userdata: object,
        _flags: paho.DisconnectFlags,
        reason: object = None,
        _properties: object = None,
    ) -> None:
        """Log a lost connection at warning; paho's own retry handles reconnection.

        §6: a recoverable issue is a WARNING, and a broker reconnect is named in the practices
        as
        exactly that. Requirement 4.5's indefinite retry is the client's automatic reconnect, so
        this observes rather than drives it.
        """
        _logger.warning("mqtt_disconnected", host=self._host, reason=str(reason))


__all__ = [
    "DEFAULT_KEEPALIVE_SECONDS",
    "DEFAULT_TOPIC_FILTER",
    "SUBSCRIBE_QOS",
    "MqttTransportError",
    "PahoMqttTransport",
]
