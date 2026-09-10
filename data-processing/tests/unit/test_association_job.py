"""Tests for the scheduled Exposure_Association derivation (Requirement 32.12).

The job is deliberately thin: every rule about lags, minimums, strength and the floor lives in
the
pure `domain/association.py` function, and this module only gathers two series and hands them
over.
So these tests are about the GATHERING and the WRITING, not about the arithmetic — that is
already
covered by `test_association.py` at 100+ examples per property.

Three decisions the requirement leaves open are pinned here because a plausible alternative
would
pass a weaker test:

- a day's exposure is that day's MAXIMUM Sub_Index, not its mean;
- across sites, the maximum again;
- a TRUNCATED readings window writes nothing at all.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_ingestion.adapters.memory import (
    InMemoryProfileStore,
    InMemoryReadingsStore,
    InMemorySymptomLogStore,
)
from aqm_ingestion.domain.association import AssociationLimits
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.domain.profile import (
    RECOGNIZED_CONSENT_VERSIONS,
    Condition,
    SensitivityLevel,
    UserProfile,
    build_profile,
)
from aqm_ingestion.domain.symptoms import build_symptom_entry
from aqm_ingestion.jobs.association import AssociationJob
from aqm_ingestion.ports.clock import FixedClock

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_TODAY = _NOW.date()
_USER = "user-1"


def _profile(user_id: str = _USER) -> UserProfile:
    return build_profile(
        {
            "user_id": user_id,
            "condition": Condition.ASTHMA,
            "sensitivity_level": SensitivityLevel.STANDARD,
            "locations": [{"name": "home", "latitude": 51.507, "longitude": -0.128}],
            "consent": {
                "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
                "given_at": _NOW,
            },
            "created_at": _NOW,
            "updated_at": _NOW,
        }
    )


def _reading(
    site_code: str,
    species: str,
    at: dt.datetime,
    sub_index: int | None,
) -> CalibratedReading:
    """Built from the contract suite's own builder rather than guessed at.

    My first draft named `QualityFlag.VALID`, which does not exist — the real members are
    `calibrated`, `calibrated_extrapolated`, `uncalibrated`, `suspect_fault`,
    `suspect_conflict`.
    """
    return CalibratedReading(
        key=DedupKey(
            site_code=site_code,
            species=species,
            interval_start=at,
            duration="PT1H",
        ),
        reported_value=float(sub_index or 0),
        corrected_value=float(sub_index or 0),
        units="ug.m-3" if species == "PM25" else "ppb",
        quality_flag=QualityFlag.CALIBRATED,
        confidence=Confidence.HIGH,
        calibration_strategy="rh_linear" if species == "PM25" else "identity",
        breakpoint_table="epa-2024-05-06",
        ratification_status="R",
        ingested_at=_NOW,
        archive_id=f"archive-{site_code}-{species}",
        sub_index=sub_index,
        band="Moderate",
    )


def _stack(
    *,
    limits: AssociationLimits | None = None,
    window_cap: int = 10_000,
    sites: tuple[str, ...] = ("AQM1",),
) -> tuple[AssociationJob, InMemorySymptomLogStore, InMemoryReadingsStore]:
    clock = FixedClock(_NOW)
    symptoms = InMemorySymptomLogStore(clock=clock)
    readings = InMemoryReadingsStore(max_window_readings=window_cap, clock=clock)
    profiles = InMemoryProfileStore()
    profiles.put(_profile())
    job = AssociationJob(
        symptoms=symptoms,
        readings=readings,
        profiles=profiles,
        clock=clock,
        sites_for=lambda _profile: sites,
        limits=limits or AssociationLimits(lags=(0,), min_observations=5),
    )
    return job, symptoms, readings


def _seed(
    symptoms: InMemorySymptomLogStore,
    readings: InMemoryReadingsStore,
    days: int = 20,
    site: str = "AQM1",
) -> None:
    """A clean same-day relationship: exposure and severity rise together."""
    for offset in range(days):
        on = _TODAY - dt.timedelta(days=offset)
        severity = 1 + (offset % 5)
        symptoms.put(
            build_symptom_entry(
                {
                    "user_id": _USER,
                    "entry_date": on,
                    "severity": severity,
                    "markers": ["cough"],
                    "reliever_used": False,
                },
                now=_NOW,
            )
        )
        readings.put(
            _reading(
                site,
                "PM25",
                dt.datetime.combine(on, dt.time(9), tzinfo=dt.UTC),
                60 + 30 * (offset % 5),
            )
        )


# --- nothing to derive from ---------------------------------------------

def test_a_user_with_no_profile_yields_no_report() -> None:
    clock = FixedClock(_NOW)
    job = AssociationJob(
        symptoms=InMemorySymptomLogStore(clock=clock),
        readings=InMemoryReadingsStore(clock=clock),
        profiles=InMemoryProfileStore(),
        clock=clock,
        sites_for=lambda _p: ("AQM1",),
    )
    outcome = job.run_for("nobody")
    assert outcome.report is None
    assert outcome.thresholds_written == 0


def test_a_user_with_no_diary_yields_no_report() -> None:
    job, _symptoms, _readings = _stack()
    outcome = job.run_for(_USER)
    assert outcome.report is None, "no diary is distinct from a diary that misses the bar"


def test_no_diary_is_distinct_from_a_shortfall() -> None:
    # A report with shortfalls means the data was there and did not meet the bar; None means
    # there was nothing to derive from. Conflating them loses "has not started a diary".
    job, symptoms, readings = _stack(
        limits=AssociationLimits(lags=(0,), min_observations=14)
    )
    _seed(symptoms, readings, days=4)
    outcome = job.run_for(_USER)
    assert outcome.report is not None
    assert outcome.report.shortfalls != ()
    assert outcome.thresholds_written == 0


# --- the derivation is written ------------------------------------------

def test_a_derivation_is_written_for_the_serving_path_to_read() -> None:
    # Req 32.12's whole point: the job stores, the serving path reads.
    job, symptoms, readings = _stack()
    _seed(symptoms, readings)
    outcome = job.run_for(_USER)
    assert outcome.thresholds_written >= 1
    assert symptoms.learned_thresholds(_USER) != {}
    assert "PM25" in symptoms.learned_thresholds(_USER)


def test_a_rerun_replaces_rather_than_accumulating() -> None:
    job, symptoms, readings = _stack()
    _seed(symptoms, readings)
    job.run_for(_USER)
    first = symptoms.learned_thresholds(_USER)
    job.run_for(_USER)
    assert symptoms.learned_thresholds(_USER) == first


def test_the_job_writes_nothing_for_another_user() -> None:
    job, symptoms, readings = _stack()
    _seed(symptoms, readings)
    job.run_for(_USER)
    assert symptoms.learned_thresholds("someone-else") == {}


# --- a day's exposure is its maximum ------------------------------------

def test_a_days_exposure_is_its_maximum_not_its_mean() -> None:
    # A clean evening must not dilute a bad morning. With 200 and 0 in one day, a mean would
    # give
    # 100 and a maximum gives 200 — so the derived threshold differs and the choice is visible.
    job, symptoms, readings = _stack(
        limits=AssociationLimits(lags=(0,), min_observations=5, min_strength=0.1)
    )
    for offset in range(10):
        on = _TODAY - dt.timedelta(days=offset)
        symptoms.put(
            build_symptom_entry(
                {
                    "user_id": _USER,
                    "entry_date": on,
                    "severity": 5 if offset % 2 == 0 else 1,
                    "markers": [],
                    "reliever_used": False,
                },
                now=_NOW,
            )
        )
        peak = 200 if offset % 2 == 0 else 60
        readings.put(
            _reading(
                "AQM1", "PM25", dt.datetime.combine(on, dt.time(8), tzinfo=dt.UTC), peak
            )
        )
        readings.put(
            _reading(
                "AQM1", "PM25", dt.datetime.combine(on, dt.time(20), tzinfo=dt.UTC), 1
            )
        )

    outcome = job.run_for(_USER)
    assert outcome.report is not None
    learned = symptoms.learned_thresholds(_USER)
    assert learned["PM25"].sub_index >= 200, (
        "the elevated days peaked at 200, so a mean-based aggregation would site the "
        "threshold far lower"
    )


def test_the_exposure_across_sites_is_the_maximum() -> None:
    # AQM1 sits permanently at 10, BELOW the threshold floor of 51, so a threshold derived from
    # AQM1 alone could never be emitted at all. AQM2 carries the real signal. A learned
    # threshold
    # existing therefore proves the cross-site MAXIMUM was taken rather than the first site, the
    # last one, or a mean.
    #
    # My first draft of this test gave every day severity 5 — which is zero variance, so the
    # correlation was correctly undefined and nothing was learned. The test was wrong, not the
    # code; the severity has to VARY for there to be anything to correlate.
    job, symptoms, readings = _stack(
        sites=("AQM1", "AQM2"),
        limits=AssociationLimits(lags=(0,), min_observations=5, min_strength=0.1),
    )
    for offset in range(15):
        on = _TODAY - dt.timedelta(days=offset)
        symptoms.put(
            build_symptom_entry(
                {
                    "user_id": _USER,
                    "entry_date": on,
                    "severity": 1 + (offset % 5),
                    "markers": [],
                    "reliever_used": False,
                },
                now=_NOW,
            )
        )
        at = dt.datetime.combine(on, dt.time(9), tzinfo=dt.UTC)
        readings.put(_reading("AQM1", "PM25", at, 10))
        readings.put(_reading("AQM2", "PM25", at, 60 + 30 * (offset % 5)))

    outcome = job.run_for(_USER)
    assert outcome.report is not None
    learned = symptoms.learned_thresholds(_USER)
    assert "PM25" in learned, (
        "no threshold means AQM1's sub-floor series was used, so the cross-site maximum "
        "was not taken"
    )
    assert learned["PM25"].sub_index >= 60, "the threshold must come from AQM2's scale"


def test_a_reading_with_no_sub_index_is_skipped_rather_than_counted_as_zero() -> None:
    job, symptoms, readings = _stack()
    _seed(symptoms, readings)
    on = _TODAY - dt.timedelta(days=1)
    readings.put(
        _reading("AQM1", "NO2", dt.datetime.combine(on, dt.time(10), tzinfo=dt.UTC), None)
    )
    outcome = job.run_for(_USER)
    assert outcome.report is not None
    # NO2 contributed no indexable value, so it appears nowhere — not as a zero series, which
    # would be a fabricated observation of clean air.
    assert all(a.species != "NO2" for a in outcome.report.associations)
    assert all(s.species != "NO2" for s in outcome.report.shortfalls)


# --- Req 14.8: a truncated window writes nothing ------------------------

def test_a_truncated_readings_window_writes_no_threshold() -> None:
    # Req 14.8 makes truncation reportable rather than silent, and a threshold derived from a
    # window that dropped an unknown number of readings is derived from unknown data. Same
    # reasoning as Req 32.4's shortfall, different cause, same outcome.
    job, symptoms, readings = _stack(window_cap=3)
    _seed(symptoms, readings)
    outcome = job.run_for(_USER)
    assert outcome.readings_truncated is True
    assert outcome.thresholds_written == 0
    assert symptoms.learned_thresholds(_USER) == {}


def test_a_truncated_window_still_returns_the_report_for_an_operator() -> None:
    # Withholding the WRITE is the safety rule; withholding the report would hide why.
    job, symptoms, readings = _stack(window_cap=3)
    _seed(symptoms, readings)
    assert job.run_for(_USER).report is not None


# --- Req 32.12 / §5: batching and isolation -----------------------------

def test_a_batch_isolates_one_users_failure() -> None:
    clock = FixedClock(_NOW)
    symptoms = InMemorySymptomLogStore(clock=clock)
    readings = InMemoryReadingsStore(clock=clock)
    profiles = InMemoryProfileStore()
    profiles.put(_profile("good-user"))
    profiles.put(_profile("bad-user"))

    def _sites(profile: UserProfile) -> tuple[str, ...]:
        if profile.user_id == "bad-user":
            raise ValueError("site resolution failed for this user")
        return ("AQM1",)

    for offset in range(10):
        on = _TODAY - dt.timedelta(days=offset)
        for user in ("good-user", "bad-user"):
            symptoms.put(
                build_symptom_entry(
                    {
                        "user_id": user,
                        "entry_date": on,
                        "severity": 1 + (offset % 5),
                        "markers": [],
                        "reliever_used": False,
                    },
                    now=_NOW,
                )
            )
        readings.put(
            _reading(
                "AQM1",
                "PM25",
                dt.datetime.combine(on, dt.time(9), tzinfo=dt.UTC),
                60 + 30 * (offset % 5),
            )
        )

    job = AssociationJob(
        symptoms=symptoms,
        readings=readings,
        profiles=profiles,
        clock=clock,
        sites_for=_sites,
        limits=AssociationLimits(lags=(0,), min_observations=5),
    )
    outcomes = {o.user_id: o for o in job.run_for_all(["bad-user", "good-user"])}
    assert outcomes["bad-user"].report is None
    assert outcomes["good-user"].thresholds_written >= 1, (
        "one user's failure must not stop the rest of the batch"
    )


def test_a_batch_is_ordered_and_deduplicated() -> None:
    job, symptoms, readings = _stack()
    _seed(symptoms, readings)
    outcomes = job.run_for_all([_USER, _USER])
    assert [o.user_id for o in outcomes] == [_USER]


# --- Req 32.10 / 31.10: purity and logging ------------------------------

def test_the_job_computes_no_association_itself() -> None:
    # Every rule lives in the pure domain function. Asserted over the module's AST so a later
    # inlined correlation cannot slip in — a text search would trip on the docstring that says
    # so.
    import ast
    import pathlib

    import aqm_ingestion.jobs.association as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    called = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "compute_association_report" in called, "the job must delegate the derivation"
    for forbidden in ("math.sqrt", "math.fsum", "statistics.correlation"):
        assert forbidden not in called, f"the job is computing {forbidden} itself"


def test_no_clinical_value_reaches_a_log(capsys: pytest.CaptureFixture[str]) -> None:
    from aqm_ingestion.observability.logging import configure_logging

    configure_logging("debug")
    job, symptoms, readings = _stack()
    _seed(symptoms, readings)
    symptoms.put(
        build_symptom_entry(
            {
                "user_id": _USER,
                "entry_date": _TODAY,
                "severity": 5,
                "markers": ["wheeze"],
                "reliever_used": True,
                "note": "worst night in months",
            },
            now=_NOW,
        )
    )
    job.run_for(_USER)
    output = capsys.readouterr().out
    for leaked in ("worst night in months", "wheeze"):
        assert leaked not in output
    assert _USER in output, "the pseudonymous identity IS permitted, so this is not vacuous"
