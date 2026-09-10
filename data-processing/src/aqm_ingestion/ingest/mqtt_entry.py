"""The MQTT entry point: broker messages into the Ingest_Pipeline.

Deliberately THIN. It translates transport events into ``(bytes, ArchiveMeta)`` and owns
only three policies the pipeline has no business knowing about: which topic to subscribe to,
when to acknowledge, and how to back off on reconnect. Everything about what a payload MEANS
belongs to the pipeline, which is why this module can be swapped for the feed poller without
either knowing about the other.

**Acknowledgement is the subtle part** (Requirement 4.6). A message is acknowledged only
after the archive write has SUCCEEDED, so a failure afterwards cannot lose the payload
silently — an unacknowledged message is redelivered. The pipeline already signals this by
raising ``ArchiveWriteFailedError`` before it processes anything, so the rule here is simply
to acknowledge on return and not on that exception.

A QUARANTINE IS STILL ACKNOWLEDGED, which is worth stating because the opposite looks safer
and is not: the archive write succeeded, so the payload is recoverable, and redelivering it
would archive the same bytes again and quarantine them again forever. A quarantine is a
decision about the data, not a delivery failure.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from aqm_ingestion.contract.parser import ParseError, parse_data_records
from aqm_ingestion.ingest.archive import ArchiveWriteFailedError, archive_payload
from aqm_ingestion.ingest.pipeline import IngestPipeline
from aqm_ingestion.observability.logging import get_logger, log_handled_error
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import ArchiveMeta, MqttTransport

_logger = get_logger("ingest.mqtt")

DEFAULT_TOPIC_FILTER = "aqm/sensors/+/data"
"""Requirement 4.1's default subscription filter."""

DEFAULT_TOPIC_PATTERN = "aqm/sensors/{SiteCode}/data"
"""Requirement 4.3's default pattern for extracting the site identifier."""

DEFAULT_MAX_BACKOFF_SECONDS = 60.0
"""Requirement 4.5's default maximum reconnect interval."""

_SITE_PLACEHOLDER = "{SiteCode}"


@dataclass(frozen=True, slots=True)
class MqttSettings:
    """The configured values the entry point needs — narrow, not a whole config (§1)."""

    enabled: bool = True
    topic_filter: str = DEFAULT_TOPIC_FILTER
    topic_pattern: str = DEFAULT_TOPIC_PATTERN
    max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS


@dataclass(frozen=True, slots=True)
class SubscriptionReport:
    """What one pass over the subscription did, for a caller and for tests."""

    ingested: int = 0
    mismatched: int = 0
    unmatched_topics: int = 0
    unacknowledged: int = 0
    failed: int = 0
    archive_ids: tuple[str, ...] = field(default_factory=tuple)


def site_code_from_topic(topic: str, pattern: str) -> str | None:
    """Extract the site identifier from a topic (Requirement 4.3).

    ANCHORED, and the placeholder matches a single topic level only. Both matter: an
    unanchored match would accept ``aqm/sensors/X/data/extra``, and a placeholder allowing
    ``/`` would let ``aqm/sensors/A/B/data`` read as site ``A/B``, either of which would
    silently attribute a reading to the wrong site.
    """
    if _SITE_PLACEHOLDER not in pattern:
        return None
    prefix, suffix = pattern.split(_SITE_PLACEHOLDER, 1)
    expression = f"^{re.escape(prefix)}([^/]+){re.escape(suffix)}$"
    matched = re.match(expression, topic)
    return matched.group(1) if matched else None


def backoff_delays(maximum_seconds: float) -> Iterator[float]:
    """Yield reconnect delays: 1 s doubling to a maximum, forever (Requirement 4.5).

    Never terminates, because Requirement 4.5 retries INDEFINITELY — a schedule that ran out
    would silently stop reconnecting, which is worse than reconnecting slowly.

    Raises:
        ValueError: if the maximum is not positive, which would make every delay zero and
            turn the backoff into a busy loop (§5).
    """
    if maximum_seconds <= 0:
        raise ValueError(
            f"maximum backoff must be greater than zero, got {maximum_seconds}"
        )
    delay = 1.0
    while True:
        yield min(delay, maximum_seconds)
        delay = min(delay * 2, maximum_seconds)


class MqttSubscriber:
    """Drives the MQTT push path."""

    def __init__(
        self,
        transport: MqttTransport,
        pipeline: IngestPipeline,
        clock: Clock,
        settings: MqttSettings,
    ) -> None:
        """Hold the transport, the pipeline, the clock, and the settings."""
        self._transport = transport
        self._pipeline = pipeline
        self._clock = clock
        self._settings = settings

    def run(self) -> SubscriptionReport:
        """Subscribe and process every delivered message.

        Returns:
            A report of the pass. Nothing is subscribed or processed when the interface is
            disabled (Requirement 4.8) — the caller still starts the serving API and the
            pull interface independently.
        """
        if not self._settings.enabled:
            _logger.info("mqtt_disabled", reason="push_interface_disabled_by_config")
            return SubscriptionReport()

        self._transport.subscribe(self._settings.topic_filter)
        _logger.info("mqtt_subscribed", topic_filter=self._settings.topic_filter)

        ingested = 0
        mismatched = 0
        unmatched = 0
        unacknowledged = 0
        failed = 0
        archive_ids: list[str] = []

        for topic, payload, delivery_tag in self._transport.messages():
            site_code = site_code_from_topic(topic, self._settings.topic_pattern)
            if site_code is None:
                # Not our pattern. Logged and skipped rather than raised: a broker may
                # deliver on a filter wider than the pattern, which is a configuration
                # mismatch rather than a payload problem.
                _logger.warning("mqtt_topic_unmatched", topic=topic)
                unmatched += 1
                continue

            outcome = self._handle(topic, payload, delivery_tag, site_code)
            ingested += outcome.ingested
            mismatched += outcome.mismatched
            unacknowledged += outcome.unacknowledged
            failed += outcome.failed
            archive_ids.extend(outcome.archive_ids)

        return SubscriptionReport(
            ingested=ingested,
            mismatched=mismatched,
            unmatched_topics=unmatched,
            unacknowledged=unacknowledged,
            failed=failed,
            archive_ids=tuple(archive_ids),
        )

    def _handle(
        self, topic: str, payload: bytes, delivery_tag: int, site_code: str
    ) -> SubscriptionReport:
        """Process one message, isolating its failures (Requirement 4.4).

        Every failure mode is caught here so the subscription survives it, and each is
        logged with the topic, the extracted site identifier, and the error type.
        """
        meta = ArchiveMeta(
            ingested_at=self._clock.now(), transport="mqtt", source=topic
        )

        if self._disagrees(payload, site_code):
            # Requirement 4.3. Archived FIRST so the payload stays recoverable, then
            # quarantined without reaching the pipeline's store path.
            try:
                outcome = archive_payload(self._pipeline.archive, payload, meta)
            except ArchiveWriteFailedError:
                return SubscriptionReport(unacknowledged=1)
            self._transport.acknowledge(delivery_tag)
            return SubscriptionReport(mismatched=1, archive_ids=(outcome.archive_id,))

        try:
            summary = self._pipeline.ingest(payload, meta)
        except ArchiveWriteFailedError as error:
            # Requirement 4.6: NOT acknowledged, so the broker redelivers rather than the
            # payload being lost silently. The pipeline already logged the failure.
            log_handled_error(
                _logger,
                "mqtt_archive_failed",
                error,
                topic=topic,
                SiteCode=site_code,
            )
            return SubscriptionReport(unacknowledged=1)
        except (ValueError, KeyError, OSError, RuntimeError) as error:
            log_handled_error(
                _logger,
                "mqtt_message_failed",
                error,
                topic=topic,
                SiteCode=site_code,
            )
            # The archive write succeeded before this point, so acknowledging is safe and
            # redelivery would only repeat the same failure.
            self._transport.acknowledge(delivery_tag)
            return SubscriptionReport(failed=1)

        self._transport.acknowledge(delivery_tag)
        return SubscriptionReport(
            ingested=summary.accepted, archive_ids=(summary.archive_id,)
        )

    def _disagrees(self, payload: bytes, site_code: str) -> bool:
        """Whether any record's SiteCode disagrees with the topic's (Requirement 4.3).

        Parsed leniently: a payload that will not parse is not a MISMATCH, it is a parse
        failure, and the pipeline reports that with its own rejection reason.
        """
        try:
            parsed = parse_data_records(payload.decode("utf-8", errors="replace"))
        except ParseError:
            return False
        for record in parsed.records:
            if record.SiteCode != site_code:
                _logger.warning(
                    "topic_site_mismatch",
                    topic_site_code=site_code,
                    record_site_code=record.SiteCode,
                    reason="topic_and_record_site_disagree",
                )
                return True
        return False
