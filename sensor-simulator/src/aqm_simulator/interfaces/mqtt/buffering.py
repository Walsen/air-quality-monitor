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

from aqm_simulator.contract.records import SensorDataRecord
from aqm_simulator.interfaces.mqtt import MqttPublisher, MqttTransport

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


class ResilientPublisher:
    """Publishes with retry, buffering the records an outage prevented sending."""

    def __init__(
        self,
        transport: MqttTransport,
        buffer: RecordBuffer | None = None,
        backoff: BackoffPolicy | None = None,
        sleep: SleepFn | None = None,
        max_attempts_per_batch: int = 3,
    ) -> None:
        self._publisher = MqttPublisher(transport)
        self._buffer = buffer if buffer is not None else RecordBuffer()
        self._backoff = backoff if backoff is not None else BackoffPolicy()
        self._sleep = sleep
        self._max_attempts = max_attempts_per_batch
        self._consecutive_failures = 0

    @property
    def dropped(self) -> int:
        return self._buffer.dropped

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    async def publish_all(self, records: list[SensorDataRecord]) -> None:
        """Publish buffered records first, then the newly generated ones."""
        pending = self._buffer.drain() + list(records)
        unsent: list[SensorDataRecord] = []
        for index, record in enumerate(pending):
            if not await self._publish_with_retry(record):
                unsent = pending[index:]  # keep this and every later record
                break
        for record in unsent:
            self._buffer.add(record)

    async def _publish_with_retry(self, record: SensorDataRecord) -> bool:
        """Try one record, backing off between attempts. True when published."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                await self._publisher.publish_one(record)
            except (OSError, TimeoutError):
                self._consecutive_failures += 1
                if attempt < self._max_attempts and self._sleep is not None:
                    await self._sleep(self._backoff.delay_for(self._consecutive_failures))
            else:
                self._consecutive_failures = 0
                return True
        return False
