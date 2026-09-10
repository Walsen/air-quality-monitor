"""Resolving the temperature and pressure a conversion runs at.

Requirement 9.3 uses the same three-source order as Requirement 8.4's humidity: a
meteorology channel record for that site and interval, then the MeteorologyProvider,
then the configured defaults. The channel lookup itself is shared with the humidity path
(:func:`aqm_ingestion.ingest.calibrate.channel_value`) rather than written a third time.

Two decisions the requirement implies but does not spell out:

- The pair is resolved TOGETHER, never half from a channel and half from defaults.
  Requirement 9.7 records ONE source for the conversion, so a mixed pair could not be
  described honestly — and silently pairing a measured temperature with a default
  pressure would report a measurement-backed conversion that was partly a guess.
- A non-positive measured value is a Requirement 9.9 failure, NOT a reason to fall back
  to defaults. Falling back would turn an implausible sensor reading into a plausible
  sub-index and hide the fault; the requirement instead says compute no sub-index and
  log an error.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass

from aqm_ingestion.domain.conversion import (
    DEFAULT_SITE_CONDITIONS,
    ConversionUnavailableError,
    SiteConditions,
)
from aqm_ingestion.domain.models import Confidence, ConversionSource, cap_confidence
from aqm_ingestion.ingest.calibrate import (
    ChannelObservation,
    ObservationSource,
    channel_value,
)
from aqm_ingestion.observability.logging import get_logger, log_handled_error

_logger = get_logger("ingest.conditions")


@dataclass(frozen=True, slots=True)
class ResolvedConditions:
    """The conditions a conversion will use, and where they came from (Req 9.7).

    ``conditions`` is None exactly when ``available`` is False, which is Requirement
    9.9's state: no NO2 sub-index may be computed for the reading.
    """

    conditions: SiteConditions | None
    source: ConversionSource
    available: bool = True


def resolve_site_conditions(
    *,
    site_code: str,
    interval_start: dt.datetime,
    observations: Sequence[ChannelObservation],
    provider: ObservationSource | None,
    defaults: SiteConditions = DEFAULT_SITE_CONDITIONS,
) -> ResolvedConditions:
    """Resolve temperature and pressure for one reading (Requirement 9.3).

    Args:
        site_code: the reading's site.
        interval_start: the reading's interval.
        observations: meteorology channel records from this payload.
        provider: the MeteorologyProvider port, if configured.
        defaults: the configured default site conditions.

    Returns:
        The conditions and their source. When a resolved measurement is unusable, an
        unavailable result — Requirement 9.9 forbids computing a sub-index from it.
    """
    channel_temperature = channel_value(
        observations, "temperature", site_code, interval_start
    )
    channel_pressure = channel_value(
        observations, "pressure", site_code, interval_start
    )
    if channel_temperature is not None and channel_pressure is not None:
        return _build(channel_temperature, channel_pressure, "channel", site_code)

    if provider is not None:
        observed = provider.observation(site_code, interval_start)
        if (
            observed is not None
            and observed.temperature_k is not None
            and observed.pressure_pa is not None
        ):
            return _build(
                observed.temperature_k, observed.pressure_pa, "provider", site_code
            )

    # Defaults are known-good by construction, so this branch cannot be unavailable.
    return ResolvedConditions(conditions=defaults, source="default")


def _build(
    temperature_k: float,
    pressure_pa: float,
    source: ConversionSource,
    site_code: str,
) -> ResolvedConditions:
    """Validate a measured pair, reporting Requirement 9.9 rather than falling back."""
    try:
        conditions = SiteConditions(
            temperature_k=temperature_k, pressure_pa=pressure_pa
        )
    except ConversionUnavailableError as error:
        log_handled_error(
            _logger,
            "conversion_unavailable",
            error,
            SiteCode=site_code,
            source=source,
            field=error.field,
            offending_value=error.value,
        )
        return ResolvedConditions(conditions=None, source=source, available=False)
    return ResolvedConditions(conditions=conditions, source=source)


def cap_confidence_for_source(
    confidence: Confidence, source: ConversionSource
) -> Confidence:
    """Cap Confidence at medium when the conditions were defaulted (Req 9.8).

    A CAP, not an assignment: a reading already at low confidence stays low, because a
    defaulted conversion cannot make a doubtful reading more trustworthy. The comparison
    uses the domain's single confidence ordering, so this rule and Requirement 12.8's
    floor cannot disagree about which confidence is lower.
    """
    if source != "default":
        return confidence
    return cap_confidence(confidence, Confidence.MEDIUM)
