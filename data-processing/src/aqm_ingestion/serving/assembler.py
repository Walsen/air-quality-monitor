"""Composing a Serving_Response from every personalization stage.

Requirements 19.1, 19.2, 19.11, and the stages of Requirements 20 through 25.

This module WIRES rather than computes, the way ``ingest/pipeline.py`` does for ingestion: every
value it reports is produced by a module that owns that rule, and its own job is order and
shape.

THREE PLACES REQUIREMENT 19.2's WIRE SHAPE HAS ONE SLOT WHERE A RULE IS PER-SPECIES OR PER-SITE.
Each is resolved by asking which one can actually contribute, and each is recorded because a
future reader will otherwise assume a value was picked arbitrarily:

- ``inhaledDose`` is the PM2.5 dose. Requirement 23.1 computes a dose per species, but its
formula
  is in µg/m³ and NO2 is stored in ppb, so ``dose_concentration_from`` refuses NO2 outright —
  only
  a mass-concentration species can produce one without a conversion this layer must not invent.
- ``forecast.trend`` compares against the NEAREST site's Overall_AQI. Requirement 24.6 says "the
  current Overall_AQI" while a response carries one per site; the nearest is the user's own
  exposure, and it is the site the forecast position was derived from.
- ``escalationSubIndex`` reports the escalation for the DRIVING species where there is one.
  Requirement 22.9 makes the basis of a crossing reviewable, and the crossing that matters is
  the
  one on the species setting the Overall_AQI.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aqm_ingestion.domain.aqi.breakpoints import BreakpointTableRegistry
from aqm_ingestion.domain.aqi.overall import (
    DEFAULT_SPECIES_PRECEDENCE,
    SpeciesContribution,
    compute_overall_aqi,
)
from aqm_ingestion.domain.dose import (
    DEFAULT_BREATHING_RATES,
    MASS_CONCENTRATION_UNIT,
    activity_inputs_from,
    compute_inhaled_dose,
    dose_concentration_from,
)
from aqm_ingestion.domain.escalation import (
    CrossingReport,
    MeasuredSubIndex,
    evaluate_crossings,
)
from aqm_ingestion.domain.models import CalibratedReading
from aqm_ingestion.domain.profile import UserProfile
from aqm_ingestion.domain.weighting import (
    ConditionWeightingRegistry,
    compute_weighted_focus,
    order_species,
)
from aqm_ingestion.ports.clock import Clock
from aqm_ingestion.ports.protocols import VerifiedIdentity
from aqm_ingestion.serving.basis import Basis, assemble_basis
from aqm_ingestion.serving.enrichment import Enricher, primary_position
from aqm_ingestion.serving.geo import GeoSelector, SelectedSite
from aqm_ingestion.serving.guardrails import GuardrailSettings, guardrail_envelope
from aqm_ingestion.serving.models import (
    CrossingOut,
    ForecastOut,
    LocationOut,
    MeasurementOut,
    NearestSensorOut,
    PersonalizedOut,
    ServingResponse,
    basis_out,
)
from aqm_ingestion.serving.profiles import ProfileService

DEFAULT_TABLE_ID = "epa-2024-05-06"
"""The Breakpoint_Table a response is built against, resolved from configuration."""


@dataclass(frozen=True, slots=True)
class AssemblySettings:
    """Configured shape choices for assembly (§1: narrow, not a config blob)."""

    table_id: str = DEFAULT_TABLE_ID
    species_precedence: tuple[str, ...] = DEFAULT_SPECIES_PRECEDENCE
    guardrails: GuardrailSettings | None = None


@dataclass(frozen=True, slots=True)
class AssembledResponse:
    """A response plus what the AUDIT needs, which the body itself cannot supply.

    The basis and the crossing flag travel out beside the response rather than being read back
    off the assembled body: Requirement 25.7's audit record would otherwise depend on the
    response's wire SHAPE, so a later change to the body could quietly change what was audited.
    """

    response: ServingResponse
    basis: Basis
    threshold_crossed: bool


class ResponseAssembler:
    """Builds the Serving_Response for a verified identity."""

    def __init__(
        self,
        profiles: ProfileService,
        selector: GeoSelector,
        enricher: Enricher,
        breakpoints: BreakpointTableRegistry,
        clock: Clock,
        weightings: ConditionWeightingRegistry | None = None,
        settings: AssemblySettings | None = None,
    ) -> None:
        """Hold the stages. Each owns one rule; this object owns only the order."""
        self._profiles = profiles
        self._selector = selector
        self._enricher = enricher
        self._breakpoints = breakpoints
        self._clock = clock
        self._weightings = weightings or ConditionWeightingRegistry.with_defaults()
        self._settings = settings or AssemblySettings()

    def assemble(self, identity: VerifiedIdentity) -> AssembledResponse:
        """Assemble the response, returning it and whether a crossing was reported.

        The crossing flag travels out separately because the AUDIT needs it (Requirement 25.7)
        and reading it back off the assembled body would mean the audit trail depended on the
        response's shape rather than on what actually happened.
        """
        resolved = self._profiles.resolve(identity)
        profile = resolved.profile
        selection = self._selector.select(profile)

        contributing: list[CalibratedReading] = []
        for site in selection.sites:
            contributing.extend(site.measurements)

        weighting = self._weightings.resolve(profile.condition)
        focus = compute_weighted_focus(
            weighting, available={r.key.species for r in contributing}
        )

        sensors = tuple(
            self._sensor_out(site, focus.focus) for site in selection.sites
        )

        crossings = evaluate_crossings(
            [
                MeasuredSubIndex(
                    site_code=reading.key.site_code,
                    species=reading.key.species,
                    sub_index=reading.sub_index,
                    confidence=reading.confidence,
                )
                for reading in contributing
                if reading.sub_index is not None
            ],
            profile,
            registry=self._breakpoints,
            table_id=self._settings.table_id,
        )

        nearest = sensors[0] if sensors else None
        enrichment = self._enricher.enrich(
            primary_position(
                tuple((loc.latitude, loc.longitude) for loc in profile.locations)
            )
            or self._selector_fallback(),
            current_overall_aqi=nearest.overallAqi if nearest else None,
            pollen=weighting.pollen,
        )

        basis = assemble_basis(contributing)
        envelope = guardrail_envelope(self._settings.guardrails)

        response = ServingResponse(
            user=identity.user_id,
            generatedAt=self._clock.now(),
            locations=tuple(
                LocationOut(name=str(loc.name), lat=loc.latitude, lon=loc.longitude)
                for loc in profile.locations
            ),
            nearestSensors=sensors,
            personalized=PersonalizedOut(
                condition=str(profile.condition),
                sensitivity=str(profile.sensitivity_level),
                usedDefaultProfile=resolved.used_default,
                weightedFocus=focus.focus,
                unavailableWeightedSpecies=focus.unavailable,
                escalationSubIndex=self._escalation_for(crossings, nearest),
                thresholdCrossed=crossings.crossed,
                thresholdSource=self._threshold_source(crossings, nearest),
                crossings=tuple(
                    CrossingOut(
                        siteCode=crossing.site_code,
                        species=crossing.species,
                        subIndex=crossing.sub_index,
                        threshold=crossing.threshold,
                    )
                    for crossing in crossings.crossings
                ),
                pollen=(
                    {taxon: str(category) for taxon, category in enrichment.pollen.items()}
                    if enrichment.pollen is not None
                    else None
                ),
                inhaledDose=self._dose_for(profile, contributing),
            ),
            forecast=ForecastOut(
                tomorrowAqi=enrichment.forecast_aqi,
                trend=None if enrichment.trend is None else str(enrichment.trend),
                source=enrichment.provider,
                retrievedAt=enrichment.retrieved_at,
                degraded=enrichment.degraded,
            ),
            basis=basis_out(basis),
            advisoryScope=envelope.advisory_scope,
            emergencyGuidance=envelope.emergency_guidance,
            disclaimer=envelope.disclaimer,
        )
        return AssembledResponse(
            response=response, basis=basis, threshold_crossed=crossings.crossed
        )

    def _selector_fallback(self) -> tuple[float, float]:
        """The enrichment position for a profile with no User_Location.

        Requirement 17.12's default profile has no location, and Requirement 20.6 already gives
        the selection a configured centre — reusing it keeps the forecast and the sites
        describing
        the same place, rather than forecasting somewhere no site was selected from.
        """
        return self._selector.fallback_centre

    def _sensor_out(
        self, site: SelectedSite, focus: tuple[str, ...]
    ) -> NearestSensorOut:
        """Shape one selected site, ordering its measurements by the Weighted_Focus."""
        by_species = {reading.key.species: reading for reading in site.measurements}
        ordered = order_species(
            by_species, focus=focus, precedence=self._settings.species_precedence
        )

        contributions = [
            SpeciesContribution(
                species=reading.key.species,
                sub_index=reading.sub_index,
                method=reading.method or "hourly",
                confidence=reading.confidence,
            )
            for reading in site.measurements
            if reading.sub_index is not None and not _is_index_species(reading)
        ]
        overall = compute_overall_aqi(
            contributions, precedence=self._settings.species_precedence
        )

        return NearestSensorOut(
            siteCode=site.site_code,
            siteName=site.site_name,
            siteClassification=site.site_classification,
            locationName=(
                str(site.location_names[0]) if site.location_names else None
            ),
            distanceKm=site.distance_km,
            asOf=site.as_of,
            measurements=tuple(
                _measurement_out(by_species[species]) for species in ordered
            ),
            overallAqi=overall.value if overall else None,
            band=overall.band if overall else None,
            drivingPollutant=overall.driving_pollutant if overall else None,
            confidence=str(overall.confidence) if overall else None,
        )

    def _escalation_for(
        self, crossings: CrossingReport, nearest: NearestSensorOut | None
    ) -> int | None:
        """The escalation Sub_Index to report (Requirement 22.9). See the module docstring."""
        effective = crossings.effective
        if not effective:
            return None
        driving = nearest.drivingPollutant if nearest else None
        if driving is not None and driving in effective:
            return int(effective[driving].sub_index)
        # No driving species (no Sub_Index anywhere), so report the first by species name so the
        # value is defined rather than incidental (§2).
        return int(effective[sorted(effective)[0]].sub_index)

    def _threshold_source(
        self, crossings: CrossingReport, nearest: NearestSensorOut | None
    ) -> str | None:
        """The source of the reported escalation point (Requirement 22.6)."""
        effective = crossings.effective
        if not effective:
            return None
        driving = nearest.drivingPollutant if nearest else None
        if driving is not None and driving in effective:
            return str(effective[driving].source)
        return str(effective[sorted(effective)[0]].source)

    def _dose_for(
        self, profile: UserProfile, readings: Sequence[CalibratedReading]
    ) -> float | None:
        """The Inhaled_Dose to report (Requirement 23.1). See the module docstring.

        Returns None when the activity inputs are absent (Requirement 23.3) or when no
        mass-concentration reading is available — never a dose derived from a ppb value, which
        ``dose_concentration_from`` refuses outright.
        """
        activity = activity_inputs_from(profile)
        if activity is None:
            return None
        candidate = next(
            (r for r in readings if r.units == MASS_CONCENTRATION_UNIT), None
        )
        if candidate is None:
            return None
        dose = compute_inhaled_dose(
            concentration_ug_m3=dose_concentration_from(candidate),
            concentration_confidence=candidate.confidence,
            activity=activity,
            rates=DEFAULT_BREATHING_RATES,
        )
        return None if dose is None else round(dose.micrograms, 2)


def _measurement_out(reading: CalibratedReading) -> MeasurementOut:
    """Shape one measurement, always with its flag and confidence (Requirement 25.6)."""
    return MeasurementOut(
        species=reading.key.species,
        reportedValue=reading.reported_value,
        correctedValue=reading.corrected_value,
        units=reading.units,
        qualityFlag=str(reading.quality_flag),
        confidence=str(reading.confidence),
        subIndex=reading.sub_index,
        band=reading.band,
        method=None if reading.method is None else str(reading.method),
        mixingRatioPpb=reading.mixing_ratio_ppb,
    )


def _is_index_species(reading: CalibratedReading) -> bool:
    """Whether a reading is a network index species (Requirement 1.7).

    Excluded from the Overall_AQI at the CONTRIBUTION boundary, which refuses one outright —
    this
    keeps the refusal from ever being reached rather than relying on it as control flow.
    """
    return reading.key.species.endswith("Index")


__all__ = ["DEFAULT_TABLE_ID", "AssemblySettings", "ResponseAssembler"]
