"""Per-Publish_Interval reporting.

After each interval the pipeline emits one structured INFO summary event through
the central JSON logger — never ``print`` (engineering-practices §6, Requirement
17.2/17.3) — carrying the counts an operator watches:

- ``generated``  records produced by the pipeline for the interval,
- ``published``  records handed downstream (equal to ``generated`` until a
  buffering transport is added; kept as a distinct field so the summary shape
  does not change when one is),
- ``buffered``   records held back for a later interval,
- ``dropped``    records suppressed for the interval (e.g. by a dropout fault).

When records were generated but none were published, a WARNING is emitted as
well so a stalled publisher is visible (Requirement 17.6). Scenario window
open/close events are logged separately by the ScenarioEngine's caller.
"""

from __future__ import annotations

from dataclasses import dataclass

from aqm_simulator.observability.logging import _EventLogger


@dataclass(frozen=True, slots=True)
class IntervalReport:
    """The counts summarising one Publish_Interval."""

    date_time: str
    generated: int
    published: int
    buffered: int
    dropped: int


def report_interval(logger: _EventLogger, report: IntervalReport) -> None:
    """Log the per-interval summary, plus a stall WARNING if nothing published."""
    logger.info(
        "publish_interval",
        DateTime=report.date_time,
        generated=report.generated,
        published=report.published,
        buffered=report.buffered,
        dropped=report.dropped,
    )
    if report.generated > 0 and report.published == 0:
        logger.warning(
            "publish_stalled",
            DateTime=report.date_time,
            generated=report.generated,
            published=report.published,
        )
