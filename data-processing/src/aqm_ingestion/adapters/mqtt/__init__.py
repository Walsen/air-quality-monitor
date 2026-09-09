"""MQTT adapter for the push ingestion transport.

Synchronous (paho) rather than async, because the MqttTransport port is synchronous: the ingest
pipeline is, and threading async through it would buy nothing while making the
archive-before-acknowledge ordering harder to see.
"""

from aqm_ingestion.adapters.mqtt.transport import (
    DEFAULT_KEEPALIVE_SECONDS,
    DEFAULT_TOPIC_FILTER,
    SUBSCRIBE_QOS,
    MqttTransportError,
    PahoMqttTransport,
)

__all__ = [
    "DEFAULT_KEEPALIVE_SECONDS",
    "DEFAULT_TOPIC_FILTER",
    "SUBSCRIBE_QOS",
    "MqttTransportError",
    "PahoMqttTransport",
]
