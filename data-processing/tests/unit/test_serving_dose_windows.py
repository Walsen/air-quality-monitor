"""Per-routine-window Inhaled_Dose on the serving path (Requirements 23.1a, 23.1b).

The domain already computes this; these tests are about whether it REACHES a response and
whether
the basis is reported alongside it. Req 23.1b is the reason the basis matters: a per-window sum
and
a whole-day figure are both truthfully "the dose", so reporting either unlabelled would make two
different numbers indistinguishable.

One aggregation choice is pinned here because it differs deliberately from the association's. A
window's concentration is the MEAN over that window, because a dose is an integral of
concentration
over time. The Req 32 association uses a daily MAXIMUM instead, because it looks for a symptom
response and that tracks the worst part of a day. Two aggregations, two reasons.
"""

from __future__ import annotations

import datetime as dt

from aqm_ingestion.adapters.memory import (
    InMemoryAuditStore,
    InMemoryForecastClient,
    InMemoryProfileStore,
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
)
from aqm_ingestion.contract.records import SensorMetadataRecord
from aqm_ingestion.domain.aqi.breakpoints import BreakpointTableRegistry
from aqm_ingestion.domain.dose import DEFAULT_BREATHING_RATES, DoseBasis
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.domain.profile import (
    RECOGNIZED_CONSENT_VERSIONS,
    ActivityLevel,
    Condition,
    SensitivityLevel,
)
from aqm_ingestion.ports.clock import FixedClock
from aqm_ingestion.ports.protocols import VerifiedIdentity
from aqm_ingestion.serving.assembler import ResponseAssembler
from aqm_ingestion.serving.enrichment import Enricher
from aqm_ingestion.serving.geo import GeoSelector
from aqm_ingestion.serving.models import PersonalizedOut
from aqm_ingestion.serving.profiles import ProfileService

# A Wednesday, so a routine naming "wednesday" falls on it.
_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_USER = "user-1"
_SITE = "AQM1"


def _metadata_record() -> SensorMetadataRecord:
    """Reuse the contract suite's GOLDEN-payload builder rather than hand-writing a record.

    My first draft invented field names and was rejected for a dozen of them at once. The golden
    builder cannot drift from the real Service 1 contract, which is precisely why it exists.
    """
    from tests.contracts.test_port_contracts import _metadata

    return _metadata(site_code=_SITE, lat=51.507, lon=-0.128)


def _reading(at: dt.datetime, corrected: float) -> CalibratedReading:
    return CalibratedReading(
        key=DedupKey(
            site_code=_SITE, species="PM25", interval_start=at, duration="PT1H"
        ),
        reported_value=corrected,
        corrected_value=corrected,
        units="ug.m-3",
        quality_flag=QualityFlag.CALIBRATED,
        confidence=Confidence.HIGH,
        calibration_strategy="rh_linear",
        breakpoint_table="epa-2024-05-06",
        ratification_status="R",
        ingested_at=_NOW,
        archive_id="archive-1",
        sub_index=68,
        band="Moderate",
    )


def _profile_fields(**overrides: object) -> dict[str, object]:
    return {
        "user_id": _USER,
        "condition": Condition.ASTHMA,
        "sensitivity_level": SensitivityLevel.STANDARD,
        "locations": [{"name": "home", "latitude": 51.507, "longitude": -0.128}],
        "consent": {
            "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
            "given_at": _NOW,
        },
        "created_at": _NOW,
        "updated_at": _NOW,
    } | overrides


def _assemble(
    profile_fields: dict[str, object], *, with_readings: bool = True
) -> PersonalizedOut:
    clock = FixedClock(_NOW)
    registry = InMemorySensorRegistryStore()
    registry.upsert(_metadata_record(), _NOW)
    readings = InMemoryReadingsStore(clock=clock)
    # A fresh reading so the site is selected at all, plus the window's own hours.
    readings.put(_reading(_NOW - dt.timedelta(hours=1), 20.0))
    for hour, value in ((7, 10.0), (8, 30.0)):
        readings.put(
            _reading(dt.datetime.combine(_NOW.date(), dt.time(hour), tzinfo=dt.UTC), value)
        )

    profiles_store = InMemoryProfileStore()
    service = ProfileService(profiles=profiles_store, audit=InMemoryAuditStore())
    service.write(VerifiedIdentity(user_id=_USER), profile_fields)

    assembler = ResponseAssembler(
        profiles=service,
        selector=GeoSelector(registry=registry, readings=readings, clock=clock),
        enricher=Enricher(client=InMemoryForecastClient(), clock=clock),
        breakpoints=BreakpointTableRegistry.with_defaults(),
        clock=clock,
        readings=readings if with_readings else None,
    )
    return assembler.assemble(VerifiedIdentity(user_id=_USER)).response.personalized


# --- Req 23.1b: the basis is always named -------------------------------

def test_the_whole_day_basis_is_reported() -> None:
    personalized = _assemble(
        _profile_fields(
            activity_level=ActivityLevel.MODERATE, activity_duration_hours=2.0
        )
    )
    assert personalized.doseBasis == DoseBasis.WHOLE_DAY_ACTIVITY.value
    assert personalized.inhaledDose is not None
    assert personalized.inhaledDoseWindows == ()


def test_no_basis_is_reported_when_the_profile_supplies_neither() -> None:
    personalized = _assemble(_profile_fields())
    assert personalized.doseBasis == DoseBasis.NONE.value
    assert personalized.inhaledDose is None


def test_the_routine_basis_wins_over_the_whole_day_inputs() -> None:
    # Req 23.1b: WHERE both are present the routine records are used. The whole-day inputs here
    # are deliberately REST for 24 hours, which would give a very different number — so the
    # reported basis is what tells the two apart.
    personalized = _assemble(
        _profile_fields(
            activity_level=ActivityLevel.REST,
            activity_duration_hours=24.0,
            routines=[
                {
                    "days": ["wednesday"],
                    "start_time": "07:00:00",
                    "duration_hours": 2.0,
                    "activity_level": "vigorous",
                }
            ],
        )
    )
    assert personalized.doseBasis == DoseBasis.ROUTINE_WINDOWS.value


# --- Req 23.1a: a dose per window, from that window's concentration ------

def test_a_routine_window_is_priced_from_its_own_hours() -> None:
    # The window is 07:00-09:00 and the two readings inside it are 10 and 30 µg/m³, so the mean
    # is 20. A dose is an integral over time, so the MEAN is what the formula wants — the daily
    # maximum the association uses would give 30 here and overstate the dose.
    personalized = _assemble(
        _profile_fields(
            routines=[
                {
                    "days": ["wednesday"],
                    "start_time": "07:00:00",
                    "duration_hours": 2.0,
                    "activity_level": "vigorous",
                }
            ]
        )
    )
    assert len(personalized.inhaledDoseWindows) == 1
    window = personalized.inhaledDoseWindows[0]
    assert window.concentrationUgM3 == 20.0
    assert window.breathingRateM3PerH == DEFAULT_BREATHING_RATES[ActivityLevel.VIGOROUS]
    # 20 ug/m3 * 3.2 m3/h * 2 h = 128 ug
    assert window.micrograms == 128.0
    assert personalized.inhaledDose == 128.0


def test_the_reported_total_is_the_sum_of_the_windows() -> None:
    personalized = _assemble(
        _profile_fields(
            routines=[
                {
                    "days": ["wednesday"],
                    "start_time": "07:00:00",
                    "duration_hours": 2.0,
                    "activity_level": "vigorous",
                },
                {
                    "days": ["wednesday"],
                    "start_time": "08:00:00",
                    "duration_hours": 1.0,
                    "activity_level": "light",
                },
            ]
        )
    )
    assert len(personalized.inhaledDoseWindows) == 2
    assert personalized.inhaledDose == sum(
        w.micrograms for w in personalized.inhaledDoseWindows
    )


def test_a_window_on_another_day_is_not_counted() -> None:
    personalized = _assemble(
        _profile_fields(
            routines=[
                {
                    "days": ["sunday"],
                    "start_time": "07:00:00",
                    "duration_hours": 1.0,
                    "activity_level": "vigorous",
                }
            ]
        )
    )
    assert personalized.inhaledDoseWindows == ()
    assert personalized.inhaledDose is None, (
        "a day with no routine window must not report zero, which would claim the user "
        "breathed nothing in"
    )


def test_a_window_with_no_reading_is_reported_unavailable_not_guessed() -> None:
    # Req 23.3's reasoning: an assumed concentration is not a measured one.
    personalized = _assemble(
        _profile_fields(
            routines=[
                {
                    "days": ["wednesday"],
                    "start_time": "03:00:00",
                    "duration_hours": 1.0,
                    "activity_level": "vigorous",
                }
            ]
        )
    )
    assert personalized.inhaledDoseWindows == ()
    assert personalized.unavailableDoseWindows == 1
    assert personalized.inhaledDose is None


def test_without_a_readings_port_the_basis_falls_back_and_says_so() -> None:
    # The per-window path needs its own look at the history, because the selector holds only the
    # LATEST reading per species. With no readings port the service must fall back to the
    # whole-day basis and REPORT that basis rather than pretend to a precision it does not have.
    personalized = _assemble(
        _profile_fields(
            activity_level=ActivityLevel.MODERATE,
            activity_duration_hours=2.0,
            routines=[
                {
                    "days": ["wednesday"],
                    "start_time": "07:00:00",
                    "duration_hours": 2.0,
                    "activity_level": "vigorous",
                }
            ],
        ),
        with_readings=False,
    )
    assert personalized.doseBasis == DoseBasis.WHOLE_DAY_ACTIVITY.value
    assert personalized.inhaledDoseWindows == ()


def test_a_dose_window_carries_no_clinical_interpretation() -> None:
    # Req 23.8, inherited into the wire shape as well as the domain type.
    from aqm_ingestion.serving.models import DoseWindowOut

    for forbidden in ("band", "severity", "risk", "advice", "interpretation"):
        assert forbidden not in DoseWindowOut.model_fields
