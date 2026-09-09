"""Unit tests for MQTT retry, buffering, and ordered flush (task 17.2).

- Req 13.5: backoff starts at 1s, doubles per consecutive failure, caps at the
  configured maximum (default 60s), and keeps retrying at that maximum.
- Req 13.6: while the connection is unavailable records are held in a bounded
  in-memory buffer (1..100_000, default 1000) and generation continues.
- Req 13.7: on overflow the OLDEST buffered record is discarded, the newest is
  retained, and a dropped counter increments once per discarded record.
- Req 13.8: on restore every buffered record is published exactly once, in
  non-decreasing DateTime order per Virtual_Sensor, before any newer record.

The sleep function is injected, so no test ever really sleeps.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from aqm_simulator.contract.records import SensorDataRecord, SpeciesName
from aqm_simulator.interfaces.mqtt import PublishedMessage
from aqm_simulator.interfaces.mqtt.buffering import (
    BackoffPolicy,
    RecordBuffer,
    ResilientPublisher,
)

_REF = dt.datetime(2026, 7, 2, tzinfo=dt.UTC)


def _record(site: str, hour: int, species: SpeciesName = "PM25") -> SensorDataRecord:
    return SensorDataRecord(
        Species=species,
        Source="Measurement",
        Units="ug.m-3",
        SiteCode=site,
        DateTime=f"2026-07-01T{hour:02d}:00:00Z",
        Duration="PT1H",
        ScaledValue=10.0,
        RatificationStatus="R",
        SensorContract="AQMesh",
    )


# --- Req 13.5 backoff -------------------------------------------------------

def test_backoff_starts_at_one_second_and_doubles() -> None:
    policy = BackoffPolicy(maximum_seconds=60)
    assert [policy.delay_for(n) for n in range(1, 8)] == [1, 2, 4, 8, 16, 32, 60]


def test_backoff_holds_at_maximum_indefinitely() -> None:
    policy = BackoffPolicy(maximum_seconds=60)
    assert policy.delay_for(50) == 60  # still retrying at the cap


def test_backoff_respects_configured_maximum() -> None:
    policy = BackoffPolicy(maximum_seconds=5)
    assert [policy.delay_for(n) for n in range(1, 5)] == [1, 2, 4, 5]


# --- Req 13.6 / 13.7 bounded buffer ----------------------------------------

def test_buffer_rejects_out_of_range_maximum() -> None:
    with pytest.raises(ValueError, match="100000"):
        RecordBuffer(maximum=100_001)
    with pytest.raises(ValueError, match="1"):
        RecordBuffer(maximum=0)


def test_buffer_default_maximum_is_1000() -> None:
    assert RecordBuffer().maximum == 1000


def test_overflow_discards_oldest_and_counts_it() -> None:
    buffer = RecordBuffer(maximum=2)
    buffer.add(_record("AQM0001", 0))
    buffer.add(_record("AQM0001", 1))
    buffer.add(_record("AQM0001", 2))  # overflows
    held = buffer.drain()
    assert [r.DateTime for r in held] == [
        "2026-07-01T01:00:00Z",
        "2026-07-01T02:00:00Z",
    ]  # oldest discarded, newest retained
    assert buffer.dropped == 1


def test_dropped_counter_increments_per_discard() -> None:
    buffer = RecordBuffer(maximum=1)
    for hour in range(5):
        buffer.add(_record("AQM0001", hour))
    assert buffer.dropped == 4


# --- Req 13.8 ordered exactly-once flush -----------------------------------

class FlakyTransport:
    """Fails for the first ``fail_times`` publishes, then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.messages: list[PublishedMessage] = []
        self.attempts = 0

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def publish(self, topic: str, payload: str) -> None:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("broker unavailable")
        self.messages.append(PublishedMessage(topic=topic, payload=payload))


def test_unavailable_connection_buffers_then_flushes_in_order() -> None:
    transport = FlakyTransport(fail_times=3)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    publisher = ResilientPublisher(
        transport, buffer=RecordBuffer(maximum=10),
        backoff=BackoffPolicy(maximum_seconds=60), sleep=fake_sleep,
    )
    # three records generated while the broker is down, then one after
    records = [_record("AQM0001", h) for h in (0, 1, 2)]
    asyncio.run(publisher.publish_all(records))
    asyncio.run(publisher.publish_all([_record("AQM0001", 3)]))

    published = [m.payload for m in transport.messages]
    assert len(published) == 4  # every record exactly once
    times = [p.split('"DateTime":"')[1][:20] for p in published]
    assert times == sorted(times)  # non-decreasing per sensor, buffered first
    assert slept  # backoff was applied rather than a busy loop


def test_flush_publishes_buffered_before_newer_records() -> None:
    transport = FlakyTransport(fail_times=1)
    async def fake_sleep(seconds: float) -> None:
        return None

    publisher = ResilientPublisher(
        transport, buffer=RecordBuffer(maximum=10),
        backoff=BackoffPolicy(maximum_seconds=60), sleep=fake_sleep,
    )
    asyncio.run(publisher.publish_all([_record("AQM0001", 0)]))  # fails, buffers
    asyncio.run(publisher.publish_all([_record("AQM0001", 5)]))  # restore
    payloads = [m.payload for m in transport.messages]
    assert "T00:00:00Z" in payloads[0]  # the buffered record went first
    assert "T05:00:00Z" in payloads[1]
