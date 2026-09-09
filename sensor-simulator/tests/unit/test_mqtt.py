"""Unit tests for the MQTT transport adapter and publisher (task 17.1).

The publisher depends on a narrow ``MqttTransport`` abstraction so a local
broker and AWS IoT Core are interchangeable (engineering-practices §1 DIP). The
unit suite drives a fake in-memory transport — no real broker — so it stays in
the offline suite. Coroutines are driven with ``asyncio.run`` so no async test
plugin is needed.

- Req 13.1: publish over MQTT.
- Req 13.2: one Sensor_Data_Record per message on aqm/sensors/{SiteCode}/data.
"""

from __future__ import annotations

import asyncio
import datetime as dt

from aqm_simulator.contract.records import SensorDataRecord
from aqm_simulator.contract.serializer import serialize_data
from aqm_simulator.interfaces.mqtt import MqttPublisher, PublishedMessage
from aqm_simulator.pipeline.determinism import build_pipeline


class FakeTransport:
    """An in-memory MqttTransport double: records every publish call."""

    def __init__(self) -> None:
        self.messages: list[PublishedMessage] = []
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def publish(self, topic: str, payload: str) -> None:
        self.messages.append(PublishedMessage(topic=topic, payload=payload))


def _records() -> list[SensorDataRecord]:
    pipe = build_pipeline(seed=5, size=2)
    start = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
    return pipe.run_interval(start, reference_time=dt.datetime(2026, 7, 2, tzinfo=dt.UTC))


def _publish(records: list[SensorDataRecord]) -> FakeTransport:
    transport = FakeTransport()
    publisher = MqttPublisher(transport)
    asyncio.run(publisher.publish_all(records))
    return transport


def test_publishes_one_message_per_record() -> None:
    records = _records()
    transport = _publish(records)
    assert len(transport.messages) == len(records)  # one message per record


def test_topic_is_per_sitecode_data() -> None:
    records = _records()
    transport = _publish(records)
    for msg, record in zip(transport.messages, records, strict=True):
        assert msg.topic == f"aqm/sensors/{record.SiteCode}/data"


def test_payload_is_the_serialized_record() -> None:
    records = _records()
    transport = _publish(records)
    for msg, record in zip(transport.messages, records, strict=True):
        assert msg.payload == serialize_data(record)
