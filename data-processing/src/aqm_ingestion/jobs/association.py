"""The scheduled Exposure_Association derivation (Requirement 32.12).

Requirement 32.12 forbids computing an association on the serving path and requires the
derivation
to run "on its own schedule, with the Learned_Threshold it produces stored for the serving path
to
read". This module is that schedule's body: it reads a diary and a Readings history, calls the
pure
:func:`compute_association_report`, and writes the result through
:meth:`SymptomLogStore.put_learned_thresholds`.

It computes NOTHING itself. Every rule about lags, minimum observations, strength and the floor
lives in ``domain/association.py`` as a pure function; this module only gathers the two series
and
hands them over, which is what keeps the derivation reproducible from an audit.

Three decisions the requirement leaves open, made here and documented rather than buried.

**A day's exposure is that day's MAXIMUM Sub_Index, not its mean.** A symptom response tracks
the
worst part of a day; averaging would let a clean evening dilute a bad morning and would flatten
exactly the peaks the association exists to find. It is also the same choice Requirement 12.1
already makes for the Overall_AQI, which is a maximum across species for the same reason.

**Across sites, the maximum again.** The user's nearest sites are alternative measurements of
one
exposure, so taking the highest is the conservative reading, and it matches how a crossing is
judged.

**A TRUNCATED readings window writes NOTHING.** Requirement 14.8 makes truncation reportable
rather
than silent, and a threshold derived from a window that dropped an unknown number of readings is
a
threshold derived from unknown data. That is Requirement 32.4's reasoning — a value stated with
confidence it has not earned is worse than no value — applied to a different cause, so the
outcome
is the same: report the shortfall, write no threshold.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from aqm_ingestion.domain.association import (
    DEFAULT_ASSOCIATION_LIMITS,
    AssociationLimits,
    AssociationReport,
    ExposureObservation,
    compute_association_report,
)
from aqm_ingestion.domain.profile import UserProfile
from aqm_ingestion.domain.symptoms import severity_series
from aqm_ingestion.observability.logging import get_logger, log_handled_error
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import ProfileStore, ReadingsStore, SymptomLogStore

_logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AssociationOutcome:
    """What one user's derivation produced.

    ``report`` is None only when there was nothing to derive from at all — no profile, or no
    diary. That is distinct from a report carrying shortfalls, which means the data was there
    and
    did not meet the bar; conflating the two would lose the difference between "not enough
    history
    yet" and "this user has not started a diary".
    """

    user_id: str
    report: AssociationReport | None
    thresholds_written: int = 0
    readings_truncated: bool = False
    site_codes: tuple[str, ...] = field(default=())


class AssociationJob:
    """Derives Learned_Thresholds from a diary and a Readings history, off the serving path."""

    def __init__(
        self,
        symptoms: SymptomLogStore,
        readings: ReadingsStore,
        profiles: ProfileStore,
        clock: Clock,
        sites_for: Callable[[UserProfile], Sequence[str]],
        limits: AssociationLimits = DEFAULT_ASSOCIATION_LIMITS,
    ) -> None:
        """Hold the two stores, the clock, the site resolver, and the configuration.

        Args:
            symptoms: where the diary is read and the derivation is written.
            readings: the exposure history.
            profiles: read to find the user's locations.
            clock: the instant the retention reach is measured back from (§2).
            sites_for: resolves a profile to the site codes to draw exposure from. INJECTED
                rather than derived here, because selecting sites needs the registry and the
                configured radius, and duplicating that logic would let this job and the serving
                path disagree about which sensors are "the user's".
            limits: the lags, minimums, floor and retention windows.
        """
        self._symptoms = symptoms
        self._readings = readings
        self._profiles = profiles
        self._clock = clock
        self._sites_for = sites_for
        self._limits = limits

    def run_for(self, user_id: str) -> AssociationOutcome:
        """Derive and store this user's Learned_Thresholds.

        Returns:
            The outcome, including the report and how many thresholds were written. A user with
            no profile or no diary yields a report of None and writes nothing.
        """
        profile = self._profiles.get(user_id)
        if profile is None:
            return AssociationOutcome(user_id=user_id, report=None)

        today = self._clock.now().date()
        floor = today - dt.timedelta(days=self._limits.readings_retention_days)
        entries = self._symptoms.query_window(user_id, floor, today)
        if not entries:
            return AssociationOutcome(user_id=user_id, report=None)

        site_codes = tuple(self._sites_for(profile))
        exposures, truncated = self._daily_exposure(site_codes, floor, today)

        report = compute_association_report(
            severity_series(entries), exposures, self._limits, as_of=today
        )

        if truncated:
            # See the module docstring: a window that dropped an unknown number of readings
            # cannot support a threshold. The report is still returned so an operator can see
            # the
            # associations, but nothing is written.
            _logger.warning(
                "association_readings_truncated",
                user_id=user_id,
                sites=len(site_codes),
            )
            return AssociationOutcome(
                user_id=user_id,
                report=report,
                thresholds_written=0,
                readings_truncated=True,
                site_codes=site_codes,
            )

        self._symptoms.put_learned_thresholds(user_id, report.learned_thresholds)
        # Requirement 31.10's logging rule reaches here too: the identity, and counts. No
        # severity, no marker, no note, and no threshold VALUE.
        _logger.info(
            "association_derived",
            user_id=user_id,
            observations=len(entries),
            associations=len(report.associations),
            shortfalls=len(report.shortfalls),
            thresholds=len(report.learned_thresholds),
            reach_days=report.effective_reach_days,
        )
        return AssociationOutcome(
            user_id=user_id,
            report=report,
            thresholds_written=len(report.learned_thresholds),
            site_codes=site_codes,
        )

    def run_for_all(self, user_ids: Sequence[str]) -> tuple[AssociationOutcome, ...]:
        """Derive for many users, isolating a per-user failure.

        One user's failure must not stop the rest, the same rule §5 applies to a per-sensor tick
        failure in the simulator swarm — a batch that aborts on the first bad user silently
        stops
        maintaining everyone after them.
        """
        outcomes: list[AssociationOutcome] = []
        for user_id in sorted(set(user_ids)):
            try:
                outcomes.append(self.run_for(user_id))
            except (ValueError, KeyError, OSError) as failure:
                log_handled_error(
                    _logger, "association_failed", failure, user_id=user_id
                )
                outcomes.append(AssociationOutcome(user_id=user_id, report=None))
        return tuple(outcomes)

    def _daily_exposure(
        self, site_codes: Sequence[str], floor: dt.date, today: dt.date
    ) -> tuple[Mapping[str, tuple[ExposureObservation, ...]], bool]:
        """Reduce the Readings to one Sub_Index per day per species.

        Takes the MAXIMUM across intervals and across sites — see the module docstring. Returns
        the series alongside whether ANY window was truncated, since a partial series must not
        quietly become a threshold.
        """
        start = dt.datetime.combine(floor, dt.time.min, tzinfo=dt.UTC)
        end = dt.datetime.combine(
            today + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC
        )
        peaks: dict[str, dict[dt.date, int]] = {}
        truncated = False

        for site_code in sorted(set(site_codes)):
            window = self._readings.query_window(site_code, None, start, end)
            truncated = truncated or window.truncated
            for reading in window.readings:
                if reading.sub_index is None:
                    continue
                on = reading.key.interval_start.date()
                per_species = peaks.setdefault(reading.key.species, {})
                current = per_species.get(on)
                if current is None or reading.sub_index > current:
                    per_species[on] = reading.sub_index

        return (
            {
                species: tuple(
                    ExposureObservation(on=on, sub_index=by_date[on])
                    for on in sorted(by_date)
                )
                for species, by_date in peaks.items()
            },
            truncated,
        )


__all__ = ["AssociationJob", "AssociationOutcome"]
