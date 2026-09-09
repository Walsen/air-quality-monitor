"""Unit tests for unpublished reporting and auth give-up (task 17.3).

- Req 13.11: in an invocation-scoped deployment every record left unpublished is
  reported with its SiteCode, Species and DateTime, and the report does NOT rely
  on buffered records surviving the invocation, so a later invocation can
  republish that Publish_Interval.
- Req 13.10: after the configured number of CONSECUTIVE authentication or
  certificate rejections (default 5) the publisher stops attempting for THAT
  Virtual_Sensor, reports its SiteCode and the rejection category, and keeps
  publishing for the rest of the swarm.
"""

from __future__ import annotations

import asyncio

from aqm_simulator.contract.records import SensorDataRecord, SpeciesName
from aqm_simulator.interfaces.mqtt import (
    MqttAuthRejectionError,
    PublishedMessage,
    RejectionCategory,
)
from aqm_simulator.interfaces.mqtt.buffering import (
    BackoffPolicy,
    RecordBuffer,
    ResilientPublisher,
)


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


async def _no_sleep(seconds: float) -> None:
    return None


class DownTransport:
    """Always fails with an ordinary connection failure."""

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def publish(self, topic: str, payload: str) -> None:
        raise OSError("broker unavailable")


class RejectingTransport:
    """Rejects one SiteCode for auth reasons; publishes for every other."""

    def __init__(self, rejected_site: str, category: RejectionCategory) -> None:
        self.rejected_site = rejected_site
        self.category = category
        self.messages: list[PublishedMessage] = []
        self.attempts_for_rejected = 0

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def publish(self, topic: str, payload: str) -> None:
        if f"/{self.rejected_site}/" in topic:
            self.attempts_for_rejected += 1
            raise MqttAuthRejectionError(self.rejected_site, self.category)
        self.messages.append(PublishedMessage(topic=topic, payload=payload))


# --- Req 13.11 unpublished reporting ---------------------------------------

def test_unpublished_records_are_reported_with_identity() -> None:
    publisher = ResilientPublisher(
        DownTransport(), buffer=RecordBuffer(maximum=10),
        backoff=BackoffPolicy(maximum_seconds=60), sleep=_no_sleep,
    )
    records = [_record("AQM0001", 0, "PM25"), _record("AQM0002", 1, "NO2")]
    report = asyncio.run(publisher.publish_all(records))
    assert len(report.unpublished) == 2
    identities = {(u.site_code, u.species, u.date_time) for u in report.unpublished}
    assert identities == {
        ("AQM0001", "PM25", "2026-07-01T00:00:00Z"),
        ("AQM0002", "NO2", "2026-07-01T01:00:00Z"),
    }


def test_report_survives_without_the_buffer() -> None:
    # Req 13.11: the report must not depend on buffered records persisting, so
    # discarding the buffer entirely leaves the report intact.
    publisher = ResilientPublisher(
        DownTransport(), buffer=RecordBuffer(maximum=1),
        backoff=BackoffPolicy(maximum_seconds=60), sleep=_no_sleep,
    )
    report = asyncio.run(
        publisher.publish_all([_record("AQM0001", h) for h in range(4)])
    )
    assert len(report.unpublished) == 4  # every record reported
    assert publisher.buffered <= 1  # even though the buffer held at most one


# --- Req 13.10 per-sensor give-up ------------------------------------------

def test_gives_up_for_one_sensor_after_consecutive_rejections() -> None:
    transport = RejectingTransport("AQM0002", RejectionCategory.CLIENT_AUTH)
    publisher = ResilientPublisher(
        transport, buffer=RecordBuffer(maximum=100),
        backoff=BackoffPolicy(maximum_seconds=60), sleep=_no_sleep,
        max_rejections=5,
    )
    # publish repeatedly; the rejected sensor should be abandoned, others continue
    for hour in range(10):
        asyncio.run(
            publisher.publish_all([_record("AQM0001", hour), _record("AQM0002", hour)])
        )
    assert publisher.abandoned == {"AQM0002"}
    # attempts stopped at the threshold rather than continuing for all 10 rounds
    assert transport.attempts_for_rejected <= 5
    # the rest of the swarm kept publishing
    assert all("/AQM0001/" in m.topic for m in transport.messages)
    assert len(transport.messages) == 10


def test_rejection_category_is_reported() -> None:
    transport = RejectingTransport("AQM0002", RejectionCategory.CERTIFICATE)
    publisher = ResilientPublisher(
        transport, buffer=RecordBuffer(maximum=100),
        backoff=BackoffPolicy(maximum_seconds=60), sleep=_no_sleep,
        max_rejections=2,
    )
    for hour in range(4):
        asyncio.run(publisher.publish_all([_record("AQM0002", hour)]))
    assert publisher.rejection_category("AQM0002") is RejectionCategory.CERTIFICATE


def test_ordinary_failure_does_not_abandon_the_sensor() -> None:
    publisher = ResilientPublisher(
        DownTransport(), buffer=RecordBuffer(maximum=100),
        backoff=BackoffPolicy(maximum_seconds=60), sleep=_no_sleep,
        max_rejections=2,
    )
    for hour in range(6):
        asyncio.run(publisher.publish_all([_record("AQM0001", hour)]))
    # a connection failure is retried forever, never a give-up (Req 13.5 vs 13.10)
    assert publisher.abandoned == set()
