"""Unit tests for the Exposure_Association and Learned_Threshold (Requirement 32).

Three decisions here are worth stating, because a looser choice would look equally reasonable
and
would be wrong.

**Lag 3 is why this feature is worth building** (Req 32.2). The evidence base puts the gaseous
effect on the same day and the particulate effect about three days later, so a same-day-only
correlation would systematically miss the PM signal — the signal that matters most for the very
user this feature exists to serve. A lag of L pairs the symptom on day D with the exposure on
day
D-L, since the exposure comes FIRST.

**A refusal is a result** (Req 32.4). Below the minimum paired observation count the association
reports a shortfall rather than a number. A threshold learned from four days is worse than no
learned threshold, because it is stated with the same confidence and acted on the same way.

**The reach is the SHORTER of the two retention windows** (Req 32.5). A diary entry
whose exposure
data has already aged out cannot be paired, so counting it would inflate the sample the
association claims to rest on. With the shipped defaults — 365 days of diary against 90 of
readings — the effective reach is 90.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_ingestion.domain.association import (
    DEFAULT_ASSOCIATION_LAGS,
    DEFAULT_ELEVATED_SEVERITY,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_MIN_STRENGTH,
    DEFAULT_READINGS_RETENTION_DAYS,
    DEFAULT_THRESHOLD_FLOOR,
    AssociationLimits,
    ExposureObservation,
    LearnedThreshold,
    compute_association_report,
)
from aqm_ingestion.domain.escalation import ThresholdSource, resolve_escalation
from aqm_ingestion.domain.symptoms import SeverityObservation

_START = dt.date(2026, 4, 1)


def _days(count: int) -> list[dt.date]:
    return [_START + dt.timedelta(days=n) for n in range(count)]


def _severities(values: list[int], start: dt.date = _START) -> tuple[SeverityObservation, ...]:
    return tuple(
        SeverityObservation(on=start + dt.timedelta(days=n), severity=v)
        for n, v in enumerate(values)
    )


def _exposures(
    values: list[int], start: dt.date = _START
) -> tuple[ExposureObservation, ...]:
    return tuple(
        ExposureObservation(on=start + dt.timedelta(days=n), sub_index=v)
        for n, v in enumerate(values)
    )


def _rising_pair(
    n: int = 20,
) -> tuple[tuple[SeverityObservation, ...], dict[str, tuple[ExposureObservation, ...]]]:
    """A clean same-day relationship: exposure and severity move together."""
    severities = [1 + (i % 5) for i in range(n)]
    exposures = [40 + 20 * (i % 5) for i in range(n)]
    return _severities(severities), {"PM25": _exposures(exposures)}


# --- Req 32.1 / 32.3: what an association reports ------------------------

def test_the_default_lags_are_zero_and_three() -> None:
    assert DEFAULT_ASSOCIATION_LAGS == (0, 3)


def test_an_association_reports_species_lag_count_and_strength() -> None:
    severities, exposures = _rising_pair()
    report = compute_association_report(severities, exposures)
    same_day = [a for a in report.associations if a.species == "PM25" and a.lag_days == 0]
    assert len(same_day) == 1
    association = same_day[0]
    assert association.species == "PM25"
    assert association.lag_days == 0
    assert association.observations >= DEFAULT_MIN_OBSERVATIONS
    assert association.strength == pytest.approx(1.0, abs=1e-9)


def test_every_configured_lag_is_evaluated() -> None:
    severities, exposures = _rising_pair(30)
    report = compute_association_report(severities, exposures)
    evaluated = {(a.species, a.lag_days) for a in report.associations} | {
        (s.species, s.lag_days) for s in report.shortfalls
    }
    assert evaluated == {("PM25", 0), ("PM25", 3)}


# --- Req 32.2: the lag pairs exposure BEFORE symptom --------------------

def test_a_lag_pairs_the_symptom_with_the_earlier_exposure() -> None:
    # A spike of exposure on day 0 and of symptoms on day 3 is a lag-3 relationship, not lag 0.
    # Getting the sign wrong here would look plausible and invert the entire feature.
    n = 24
    severity_values = [1] * n
    exposure_values = [40] * n
    for day in range(3, n, 6):
        severity_values[day] = 5
        exposure_values[day - 3] = 180

    report = compute_association_report(
        _severities(severity_values),
        {"PM25": _exposures(exposure_values)},
        AssociationLimits(lags=(0, 3), min_observations=5),
    )
    by_lag = {a.lag_days: a.strength for a in report.associations}
    assert by_lag[3] > by_lag[0], (
        "a delayed relationship must score higher at lag 3 than at lag 0, "
        "or the lag is being applied in the wrong direction"
    )


def test_the_lag_reduces_the_pairable_observation_count() -> None:
    # At lag 3 the first three symptom days have no exposure partner, so the count must drop.
    severities, exposures = _rising_pair(20)
    report = compute_association_report(
        severities, exposures, AssociationLimits(lags=(0, 3), min_observations=1)
    )
    counts = {a.lag_days: a.observations for a in report.associations}
    assert counts[0] - counts[3] == 3


# --- Req 32.4: a refusal is a result ------------------------------------

def test_the_default_minimum_observation_count() -> None:
    assert DEFAULT_MIN_OBSERVATIONS == 14


def test_too_few_observations_reports_a_shortfall_rather_than_a_value() -> None:
    severities = _severities([1, 3, 5, 2])
    exposures = {"PM25": _exposures([40, 90, 160, 60])}
    report = compute_association_report(severities, exposures)
    assert report.associations == ()
    shortfall = [s for s in report.shortfalls if s.lag_days == 0]
    assert len(shortfall) == 1
    assert shortfall[0].observations == 4
    assert shortfall[0].required == DEFAULT_MIN_OBSERVATIONS


def test_exactly_the_minimum_is_enough() -> None:
    # "fewer than" excludes the boundary, so the minimum itself must produce an association.
    limits = AssociationLimits(lags=(0,), min_observations=14)
    severities, exposures = _rising_pair(14)
    report = compute_association_report(severities, exposures, limits)
    assert len(report.associations) == 1
    assert report.associations[0].observations == 14


def test_a_shortfall_yields_no_learned_threshold() -> None:
    severities = _severities([1, 5, 1, 5])
    exposures = {"PM25": _exposures([40, 200, 40, 200])}
    assert compute_association_report(severities, exposures).learned_thresholds == ()


# --- Req 32.5: the reach is the shorter window --------------------------

def test_the_effective_reach_is_the_shorter_of_the_two_windows() -> None:
    limits = AssociationLimits(symptom_retention_days=365, readings_retention_days=90)
    severities, exposures = _rising_pair(20)
    report = compute_association_report(severities, exposures, limits)
    assert report.effective_reach_days == 90


def test_an_observation_outside_the_reach_is_not_counted() -> None:
    # A diary entry whose exposure data has aged out must not be counted as an observation.
    limits = AssociationLimits(
        lags=(0,), min_observations=1, symptom_retention_days=365, readings_retention_days=10
    )
    severities = _severities([1 + (i % 5) for i in range(20)])
    exposures = {"PM25": _exposures([40 + 20 * (i % 5) for i in range(20)])}
    report = compute_association_report(
        severities, exposures, limits, as_of=_START + dt.timedelta(days=19)
    )
    assert report.associations[0].observations <= 11


# --- Req 32.6 / 32.8: deriving a Learned_Threshold ----------------------

def test_the_documented_defaults() -> None:
    assert DEFAULT_THRESHOLD_FLOOR == 51
    assert DEFAULT_ELEVATED_SEVERITY == 3
    assert 0.0 < DEFAULT_MIN_STRENGTH < 1.0


def test_a_learned_threshold_records_its_derivation() -> None:
    severities, exposures = _rising_pair(25)
    report = compute_association_report(severities, exposures)
    assert report.learned_thresholds != ()
    learned = report.learned_thresholds[0]
    assert learned.species == "PM25"
    assert learned.lag_days in DEFAULT_ASSOCIATION_LAGS
    assert learned.observations >= DEFAULT_MIN_OBSERVATIONS
    assert DEFAULT_THRESHOLD_FLOOR <= learned.sub_index <= 500


def test_a_weak_association_yields_no_learned_threshold() -> None:
    # Severity unrelated to exposure: no threshold may be learned from it.
    severities = _severities([3, 1, 4, 1, 5, 2, 3, 1, 4, 1, 5, 2, 3, 1, 4, 1, 5, 2])
    exposures = {"PM25": _exposures([50] * 18)}
    report = compute_association_report(severities, exposures)
    assert report.learned_thresholds == ()


def test_a_threshold_below_the_floor_is_not_emitted() -> None:
    # Req 32.8: NOT emitted, not clamped up. Learning an escalation point inside the Good band
    # would alert continuously and teach the user to ignore the alerts.
    severities = _severities([1 + (i % 5) for i in range(20)])
    exposures = {"PM25": _exposures([2 + 3 * (i % 5) for i in range(20)])}
    report = compute_association_report(severities, exposures)
    assert all(t.sub_index >= DEFAULT_THRESHOLD_FLOOR for t in report.learned_thresholds)
    assert report.learned_thresholds == (), "a sub-floor derivation must be dropped entirely"


def test_the_floor_is_configuration() -> None:
    severities, exposures = _rising_pair(20)
    high = AssociationLimits(threshold_floor=499)
    assert compute_association_report(severities, exposures, high).learned_thresholds == ()


def test_at_most_one_learned_threshold_per_species() -> None:
    # Two lags can both qualify; emitting both would make the precedence of Req 22.1 ambiguous.
    severities, exposures = _rising_pair(30)
    report = compute_association_report(severities, exposures)
    species = [t.species for t in report.learned_thresholds]
    assert len(species) == len(set(species))


# --- Req 32.10 / 32.11: purity and ordering -----------------------------

def test_the_same_stored_data_yields_the_same_report() -> None:
    severities, exposures = _rising_pair(20)
    first = compute_association_report(severities, exposures)
    second = compute_association_report(severities, exposures)
    assert first == second


def test_the_report_is_independent_of_input_order() -> None:
    severities, exposures = _rising_pair(20)
    series = exposures["PM25"]
    assert isinstance(series, tuple)
    shuffled: dict[str, tuple[ExposureObservation, ...]] = {"PM25": series[::-1]}
    assert compute_association_report(severities[::-1], shuffled) == (
        compute_association_report(severities, exposures)
    )


def test_species_are_ordered_by_the_configured_precedence_not_alphabetically() -> None:
    # Req 32.11 defers to Req 14.3's precedence, whose default is PM25 before NO2 — the REVERSE
    # of alphabetical. Sorting by name would look correct and be wrong.
    severities = _severities([1 + (i % 5) for i in range(20)])
    rising = _exposures([40 + 20 * (i % 5) for i in range(20)])
    report = compute_association_report(
        severities,
        {"NO2": rising, "PM25": rising},
        AssociationLimits(lags=(0,), species_precedence=("PM25", "NO2")),
    )
    assert [a.species for a in report.associations] == ["PM25", "NO2"]


def test_an_unlisted_species_sorts_last_rather_than_being_dropped() -> None:
    severities = _severities([1 + (i % 5) for i in range(20)])
    rising = _exposures([40 + 20 * (i % 5) for i in range(20)])
    report = compute_association_report(
        severities,
        {"O3": rising, "PM25": rising},
        AssociationLimits(lags=(0,), species_precedence=("PM25",)),
    )
    assert [a.species for a in report.associations] == ["PM25", "O3"]


def test_the_module_reads_no_clock_and_no_random() -> None:
    # Req 32.10 requires purity. An import-level check, since a wall-clock read inside the
    # computation would make the derivation unreproducible from an audit.
    import ast
    import pathlib

    import aqm_ingestion.domain.association as module

    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "random" not in imported
    calls = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    for forbidden in ("dt.datetime.now", "datetime.now", "dt.date.today", "date.today"):
        assert forbidden not in calls


def test_a_zero_variance_series_does_not_raise() -> None:
    # Every severity identical means the correlation is undefined rather than zero. Dividing by
    # a zero standard deviation would raise; the association must simply not be derivable.
    severities = _severities([3] * 20)
    exposures = {"PM25": _exposures([40 + 20 * (i % 5) for i in range(20)])}
    report = compute_association_report(severities, exposures)
    assert report.learned_thresholds == ()


def test_the_readings_retention_default_does_not_drift_from_its_owner() -> None:
    # DEFAULT_READINGS_RETENTION_DAYS duplicates a fact the ingestion configuration owns. The
    # association is a pure function and must not reach for a config object (§1), so the
    # constant
    # is duplicated deliberately — which makes this guard the thing that keeps the copy honest.
    # The association's docstring PROMISES this test exists; without it that promise is a lie.
    from aqm_ingestion.config.loader import DEFAULT_RETENTION_DAYS

    assert DEFAULT_READINGS_RETENTION_DAYS == DEFAULT_RETENTION_DAYS


def test_the_symptom_retention_default_does_not_drift_from_its_owner() -> None:
    from aqm_ingestion.domain.symptoms import DEFAULT_SYMPTOM_RETENTION_DAYS

    assert AssociationLimits().symptom_retention_days == DEFAULT_SYMPTOM_RETENTION_DAYS


def test_the_shipped_defaults_make_the_reach_clamp_live() -> None:
    # With 365 days of diary against 90 of readings the clamp is exercised by the DEFAULT
    # configuration, rather than lying dormant until somebody reconfigures. If these
    # two defaults
    # ever coincide, Req 32.5's branch stops being covered by every other test in this file.
    limits = AssociationLimits()
    assert limits.symptom_retention_days > limits.readings_retention_days


# --- Req 32.7 / 22.1: the precedence tier -------------------------------

def test_the_learned_source_exists_and_is_named_as_the_spec_spells_it() -> None:
    assert ThresholdSource.LEARNED.value == "learned"


def test_the_precedence_has_exactly_four_tiers() -> None:
    assert len(ThresholdSource) == 4


def test_a_learned_threshold_is_used_when_no_personal_threshold_exists() -> None:
    from aqm_ingestion.domain.aqi.breakpoints import (
        DEFAULT_TABLE_ID,
        BreakpointTableRegistry,
    )

    learned = {
        "PM25": LearnedThreshold(
            species="PM25", sub_index=88, lag_days=3, observations=20
        )
    }
    effective = resolve_escalation(
        None,
        species="PM25",
        registry=BreakpointTableRegistry.with_defaults(),
        table_id=DEFAULT_TABLE_ID,
        learned=learned,
    )
    assert effective.sub_index == 88
    assert effective.source is ThresholdSource.LEARNED
