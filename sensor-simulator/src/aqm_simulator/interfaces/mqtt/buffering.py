"""Retry, buffering, and ordered flush for the MQTT publisher.

In a Long_Running_Deployment the broker can be unavailable while Ticks keep
generating, so the publisher must survive an outage without losing ordering or
double-publishing (Requirement 13.5-13.8):

- :class:`BackoffPolicy` — the retry interval starts at 1 second and doubles on
  each consecutive failure up to the configured maximum (default 60 s), then
  holds there for as long as the Simulator runs (13.5).
- :class:`RecordBuffer` — a bounded in-memory buffer (1..100 000 records,
  default 1000). On overflow the OLDEST record is discarded, the newest is
  retained, and a dropped counter increments once per discard (13.6, 13.7).
- :class:`ResilientPublisher` — buffers whatever it could not publish and, once
  the connection is restored, publishes every buffered record exactly once in
  non-decreasing ``DateTime`` order per Virtual_Sensor before any newer record
  (13.8).

The sleep function is injected (engineering-practices §2), so the retry path is
fully testable without real time passing.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aqm_simulator.contract.records import SensorDataRecord
from aqm_simulator.interfaces.mqtt import (
    MqttAuthRejectionError,
    MqttPublisher,
    MqttTransport,
    RejectionCategory,
)
from aqm_simulator.observability.logging import get_logger

_logger = get_logger("mqtt")

_MIN_BUFFER = 1
_MAX_BUFFER = 100_000
_DEFAULT_BUFFER = 1000
_INITIAL_DELAY = 1

SleepFn = Callable[[float], Awaitable[None]]


class BackoffPolicy:
    """Exponential backoff from 1 s, doubling, capped at a maximum."""

    def __init__(self, maximum_seconds: int = 60) -> None:
        if maximum_seconds < _INITIAL_DELAY:
            raise ValueError(
                f"maximum backoff must be at least {_INITIAL_DELAY}s; got {maximum_seconds}"
            )
        self._maximum = maximum_seconds

    @property
    def maximum_seconds(self) -> int:
        return self._maximum

    def delay_for(self, consecutive_failures: int) -> int:
        """Delay before the nth consecutive retry (n starts at 1)."""
        if consecutive_failures < 1:
            return _INITIAL_DELAY
        # 1, 2, 4, 8, ... capped; shift is bounded so the doubling cannot overflow
        exponent = min(consecutive_failures - 1, self._maximum.bit_length() + 1)
        return min(_INITIAL_DELAY << exponent, self._maximum)


class RecordBuffer:
    """A bounded record buffer that discards the oldest entry on overflow."""

    def __init__(self, maximum: int = _DEFAULT_BUFFER) -> None:
        if not _MIN_BUFFER <= maximum <= _MAX_BUFFER:
            raise ValueError(
                f"buffer maximum must be between {_MIN_BUFFER} and {_MAX_BUFFER}; got {maximum}"
            )
        self._maximum = maximum
        self._records: deque[SensorDataRecord] = deque()
        self._dropped = 0

    @property
    def maximum(self) -> int:
        return self._maximum

    @property
    def dropped(self) -> int:
        """Records discarded because the buffer was full (Requirement 13.7)."""
        return self._dropped

    def __len__(self) -> int:
        return len(self._records)

    def add(self, record: SensorDataRecord) -> None:
        """Buffer a record, discarding the oldest if already at the maximum."""
        if len(self._records) >= self._maximum:
            self._records.popleft()  # discard the OLDEST
            self._dropped += 1
        self._records.append(record)

    def drain(self) -> list[SensorDataRecord]:
        """Remove and return every buffered record in flush order.

        Flush order is non-decreasing ``DateTime`` per Virtual_Sensor
        (Requirement 13.8). ``DateTime`` is a fixed-width ISO-8601 UTC string, so
        a lexical sort is also chronological; ``SiteCode`` keeps the order total
        and therefore deterministic (engineering-practices §2).
        """
        held = sorted(self._records, key=lambda r: (r.SiteCode, r.DateTime))
        self._records.clear()
        return held


@dataclass(frozen=True, slots=True)
class UnpublishedRecord:
    """The identity of a record that could not be published (Requirement 13.11)."""

    site_code: str
    species: str
    date_time: str


@dataclass(frozen=True, slots=True)
class PublishReport:
    """The outcome of one publish pass.

    ``unpublished`` names every record left unsent by its ``SiteCode``,
    ``Species`` and ``DateTime``, independently of whether the buffer still holds
    it, so an invocation-scoped deployment can report the gap and a later
    invocation can republish that Publish_Interval (Requirement 13.11).
    """

    published: int
    unpublished: tuple[UnpublishedRecord, ...]
    dropped: int


class ResilientPublisher:
    """Publishes with retry, buffering the records an outage prevented sending."""

    def __init__(
        self,
        transport: MqttTransport,
        buffer: RecordBuffer | None = None,
        backoff: BackoffPolicy | None = None,
        sleep: SleepFn | None = None,
        max_attempts_per_batch: int = 3,
        max_rejections: int = 5,
    ) -> None:
        self._publisher = MqttPublisher(transport)
        self._buffer = buffer if buffer is not None else RecordBuffer()
        self._backoff = backoff if backoff is not None else BackoffPolicy()
        self._sleep = sleep
        self._max_attempts = max_attempts_per_batch
        self._max_rejections = max_rejections
        self._consecutive_failures = 0
        # per-SiteCode identity-rejection state (Requirement 13.10)
        self._rejections: dict[str, int] = {}
        self._categories: dict[str, RejectionCategory] = {}
        self._abandoned: set[str] = set()

    @property
    def dropped(self) -> int:
        return self._buffer.dropped

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    @property
    def abandoned(self) -> set[str]:
        """SiteCodes no longer attempted after repeated identity rejection."""
        return set(self._abandoned)

    def rejection_category(self, site_code: str) -> RejectionCategory | None:
        """The category that caused a give-up, for reporting (Requirement 13.10)."""
        return self._categories.get(site_code)

    async def publish_all(self, records: list[SensorDataRecord]) -> PublishReport:
        """Publish buffered records first, then the newly generated ones."""
        pending = self._buffer.drain() + list(records)
        published = 0
        unsent: list[SensorDataRecord] = []
        for index, record in enumerate(pending):
            if record.SiteCode in self._abandoned:
                continue  # stopped for this sensor only; the swarm carries on
            if await self._publish_with_retry(record):
                published += 1
                continue
            if record.SiteCode in self._abandoned:
                continue  # abandoned during this attempt; nothing to buffer
            unsent = [r for r in pending[index:] if r.SiteCode not in self._abandoned]
            break
        for record in unsent:
            self._buffer.add(record)
        return PublishReport(
            published=published,
            unpublished=tuple(
                UnpublishedRecord(r.SiteCode, r.Species, r.DateTime) for r in unsent
            ),
            dropped=self._buffer.dropped,
        )

    async def _publish_with_retry(self, record: SensorDataRecord) -> bool:
        """Try one record, backing off between attempts. True when published."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                await self._publisher.publish_one(record)
            except MqttAuthRejectionError as rejection:
                if self._note_rejection(rejection):
                    return False  # threshold reached: stop for this sensor
                if attempt < self._max_attempts and self._sleep is not None:
                    await self._sleep(self._backoff.delay_for(attempt))
            except (OSError, TimeoutError):
                self._consecutive_failures += 1
                if attempt < self._max_attempts and self._sleep is not None:
                    await self._sleep(self._backoff.delay_for(self._consecutive_failures))
            else:
                self._consecutive_failures = 0
                self._rejections.pop(record.SiteCode, None)  # consecutive only
                return True
        return False

    def _note_rejection(self, rejection: MqttAuthRejectionError) -> bool:
        """Count a rejection; True once this sensor has hit the give-up threshold."""
        site = rejection.site_code
        self._categories[site] = rejection.category
        self._rejections[site] = self._rejections.get(site, 0) + 1
        if self._rejections[site] >= self._max_rejections:
            self._abandoned.add(site)
            _logger.error(
                "mqtt_sensor_abandoned",
                site_code=site,
                rejection_category=str(rejection.category),
                consecutive_rejections=self._rejections[site],
            )
            return True
        return False
