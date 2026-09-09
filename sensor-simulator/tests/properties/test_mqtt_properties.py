"""MQTT property tests (tasks 17.4, 17.5).

Feature: sensor-simulator-service
- Property 36: MQTT publish topic and single-record payloads (Req 13.1, 13.2)
- Property 37: MQTT buffering and ordered flush (Req 13.6, 13.7, 13.8, 13.11)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.contract.records import SensorDataRecord, SpeciesName
from aqm_simulator.interfaces.mqtt import MqttPublisher, PublishedMessage
from aqm_simulator.interfaces.mqtt.buffering import (
    BackoffPolicy,
    RecordBuffer,
    ResilientPublisher,
)
from aqm_simulator.pipeline.determinism import build_pipeline

_SPECIES: tuple[SpeciesName, ...] = ("PM25", "NO2", "PM25Index", "NO2Index")


class RecordingTransport:
    def __init__(self) -> None:
        self.messages: list[PublishedMessage] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def publish(self, topic: str, payload: str) -> None:
        self.messages.append(PublishedMessage(topic=topic, payload=payload))


class OutageTransport:
    """Refuses the first ``fail_count`` publishes, then accepts everything."""

    def __init__(self, fail_count: int) -> None:
        self._remaining = fail_count
        self.messages: list[PublishedMessage] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def publish(self, topic: str, payload: str) -> None:
        if self._remaining > 0:
            self._remaining -= 1
            raise OSError("broker unavailable")
        self.messages.append(PublishedMessage(topic=topic, payload=payload))


async def _no_sleep(seconds: float) -> None:
    return None


def _record(site: str, hour: int, species: SpeciesName) -> SensorDataRecord:
    return SensorDataRecord(
        Species=species,
        Source="Measurement",
        Units="index" if species.endswith("Index") else "ug.m-3",
        SiteCode=site,
        DateTime=f"2026-07-01T{hour:02d}:00:00Z",
        Duration="PT1H",
        ScaledValue=12.5,
        RatificationStatus="R",
        SensorContract="AQMesh",
    )


@given(seed=st.integers(min_value=0, max_value=5_000), size=st.integers(2, 4))
@settings(deadline=None)
def test_property_36_topic_and_single_record_payload(seed: int, size: int) -> None:
    """Feature: sensor-simulator-service, Property 36."""
    pipeline = build_pipeline(seed=seed, size=size)
    records = pipeline.run_interval(
        dt.datetime(2026, 7, 1, 9, tzinfo=dt.UTC),
        reference_time=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
    )
    transport = RecordingTransport()
    asyncio.run(MqttPublisher(transport).publish_all(records))

    assert len(transport.messages) == len(records)  # one message per record
    for message, record in zip(transport.messages, records, strict=True):
        assert message.topic == f"aqm/sensors/{record.SiteCode}/data"
        # each payload is exactly ONE record object, never a batch
        payload = json.loads(message.payload)
        assert isinstance(payload, dict)
        assert payload["SiteCode"] == record.SiteCode
        assert payload["Species"] == record.Species


@given(
    hours=st.integers(min_value=2, max_value=8),
    outage=st.integers(min_value=1, max_value=6),
    capacity=st.integers(min_value=1, max_value=40),
)
@settings(deadline=None)
def test_property_37_buffering_and_ordered_flush(
    hours: int, outage: int, capacity: int
) -> None:
    """Feature: sensor-simulator-service, Property 37."""
    transport = OutageTransport(fail_count=outage)
    publisher = ResilientPublisher(
        transport,
        buffer=RecordBuffer(maximum=capacity),
        backoff=BackoffPolicy(maximum_seconds=60),
        sleep=_no_sleep,
    )
    generated = [
        _record("AQM0001", hour, species)
        for hour in range(hours)
        for species in _SPECIES
    ]
    # generation continues interval by interval while the broker may be down
    reports = [
        asyncio.run(publisher.publish_all(generated[i : i + len(_SPECIES)]))
        for i in range(0, len(generated), len(_SPECIES))
    ]

    published = [json.loads(m.payload) for m in transport.messages]
    # nothing is invented and nothing is published twice
    seen = [(p["SiteCode"], p["Species"], p["DateTime"]) for p in published]
    assert len(seen) == len(set(seen))  # exactly once each

    # per SiteCode, published DateTimes are non-decreasing (Req 13.8)
    times = [p["DateTime"] for p in published]
    assert times == sorted(times)

    # every generated record is either published, still buffered, or dropped —
    # nothing vanishes silently, and drops are counted (Req 13.7)
    assert len(published) + publisher.buffered + publisher.dropped == len(generated)
    # an unpublished record is always reported with its full identity (Req 13.11)
    for report in reports:
        for entry in report.unpublished:
            assert entry.site_code and entry.species and entry.date_time
