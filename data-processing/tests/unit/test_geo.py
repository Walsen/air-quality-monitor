"""Unit tests for the Geo_Selector (task 21.1).

Requirements 20.1 through 20.10.

TWO CLAUSES WHERE THE OBVIOUS IMPLEMENTATION IS THE WRONG ONE, and both are the point of this
task:

- Requirement 20.9: a selected site with no Reading in the freshness window is INCLUDED, with an
  empty measurement set and no Overall_AQI. The obvious filter drops it — which would hide the
  fact that the sensor nearest the user has gone quiet, exactly what they would want to know.
- Requirement 20.5: a User_Location with no site inside the radius yields no site and a DECLARED
  gap, never a silently widened radius. Widening looks helpful and would report a sensor 40 km
  away as though it described the user's street.

Requirement 20.4 is the structural one: a site nearest to two User_Locations appears ONCE naming
BOTH. A per-location list is the natural shape and duplicates it.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.adapters.memory import (
    InMemoryReadingsStore,
    InMemorySensorRegistryStore,
)
from aqm_ingestion.contract.records import SensorMetadataRecord
from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.domain.profile import (
    RECOGNIZED_CONSENT_VERSIONS,
    Condition,
    LocationName,
    SensitivityLevel,
    UserProfile,
    build_profile,
)
from aqm_ingestion.ports.clock import FixedClock
from aqm_ingestion.serving.geo import (
    DEFAULT_DISTANCE_DECIMALS,
    DEFAULT_FRESHNESS_HOURS,
    DEFAULT_GEOGRAPHIC_CENTRE,
    DEFAULT_NEAREST_N,
    DEFAULT_RADIUS_KM,
    GeoSelector,
    SelectionSettings,
)
from tests.unit.test_records import GOLDEN_METADATA_PAYLOAD

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_LONDON = (51.507, -0.128)
_PARIS = (48.857, 2.352)


def _metadata(site_code: str, lat: float, lon: float) -> SensorMetadataRecord:
    """A metadata record positioned at a given point.

    Built from the GOLDEN Service 1 payload rather than field by field, so the test cannot drift
    from the real contract and so only the fields under test are stated here.
    """
    fields = cast("dict[str, Any]", json.loads(GOLDEN_METADATA_PAYLOAD))
    coordinates = [f"{lat:.7f}", f"{lon:.7f}"]
    # Location is a GeoJSON Feature wrapping a Point, so only the nested coordinates move —
    # replacing the whole object would drop the type/geometry envelope the contract requires.
    location = cast("dict[str, Any]", fields["Location"])
    geometry = cast("dict[str, Any]", location["geometry"])
    geometry["coordinates"] = coordinates
    fields |= {
        "SiteCode": site_code,
        "Latitude": f"{lat:.7f}",
        "Longitude": f"{lon:.7f}",
    }
    return SensorMetadataRecord(**fields)


def _reading(site_code: str, species: str, instant: dt.datetime) -> CalibratedReading:
    return CalibratedReading(
        key=DedupKey(
            site_code=site_code, species=species, interval_start=instant, duration="PT1H"
        ),
        reported_value=20.0,
        corrected_value=18.0,
        units="ug.m-3" if species == "PM25" else "ppb",
        quality_flag=QualityFlag.CALIBRATED,
        confidence=Confidence.HIGH,
        calibration_strategy="rh_linear",
        breakpoint_table="epa-2024-05-06",
        ratification_status="R",
        ingested_at=instant,
        archive_id=f"archive-{site_code}-{species}",
    )


def _profile(
    *locations: tuple[LocationName, float, float], user_id: str = "user-1"
) -> UserProfile:
    return build_profile(
        {
            "user_id": user_id,
            "condition": Condition.ASTHMA,
            "sensitivity_level": SensitivityLevel.STANDARD,
            "locations": [
                {"name": name, "latitude": lat, "longitude": lon}
                for name, lat, lon in locations
            ],
            "consent": {
                "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
                "given_at": _NOW,
            },
            "created_at": _NOW,
            "updated_at": _NOW,
        }
    )


def _selector(
    sites: dict[str, tuple[float, float]],
    readings: list[CalibratedReading] | None = None,
    settings: SelectionSettings | None = None,
) -> GeoSelector:
    clock = FixedClock(_NOW)
    registry = InMemorySensorRegistryStore()
    for site_code, (lat, lon) in sites.items():
        registry.upsert(_metadata(site_code, lat, lon), at=_NOW)
    store = InMemoryReadingsStore(clock=clock)
    for reading in readings or []:
        store.put(reading)
    return GeoSelector(
        registry=registry,
        readings=store,
        clock=clock,
        settings=settings or SelectionSettings(),
    )


# --- Req 20.1, 20.2, 20.3: the configured defaults ----------------------

def test_the_defaults_are_the_requirements_defaults() -> None:
    assert DEFAULT_NEAREST_N == 3
    assert DEFAULT_RADIUS_KM == 10.0
    assert DEFAULT_DISTANCE_DECIMALS == 2
    assert DEFAULT_FRESHNESS_HOURS == 3


def test_at_most_n_sites_are_selected_per_location() -> None:
    # Five sites all within radius; only the configured 3 nearest are taken.
    sites = {
        f"S{index}": (51.507 + index * 0.005, -0.128) for index in range(5)
    }
    selector = _selector(sites)
    selection = selector.select(_profile((LocationName.HOME, *_LONDON)))
    assert len(selection.sites) == 3


def test_n_is_configurable() -> None:
    sites = {f"S{index}": (51.507 + index * 0.005, -0.128) for index in range(5)}
    selector = _selector(sites, settings=SelectionSettings(nearest_n=2))
    selection = selector.select(_profile((LocationName.HOME, *_LONDON)))
    assert len(selection.sites) == 2


def test_a_site_at_exactly_the_radius_is_included() -> None:
    # Req 20.2 is explicit that the boundary is inclusive. Rather than hoping a hand-computed
    # offset lands exactly on 10 km — it does not, and a float a hair over would silently make
    # this an "outside the radius" test — the radius is set to the distance the SAME
    # great-circle helper reports. That is the P14 lesson: never ask a float comparison to
    # land precisely.
    from aqm_ingestion.adapters.memory.adapters import _great_circle_km

    site = (51.607, -0.128)
    exact = _great_circle_km(51.507, -0.128, *site)
    selector = _selector({"EDGE": site}, settings=SelectionSettings(radius_km=exact))
    selection = selector.select(_profile((LocationName.HOME, 51.507, -0.128)))
    assert [s.site_code for s in selection.sites] == ["EDGE"]


def test_a_site_beyond_the_radius_is_excluded() -> None:
    selector = _selector({"FAR": _PARIS})
    selection = selector.select(_profile((LocationName.HOME, *_LONDON)))
    assert selection.sites == ()


def test_the_radius_is_configurable() -> None:
    selector = _selector({"FAR": (51.607, -0.128)}, settings=SelectionSettings(radius_km=1.0))
    assert selector.select(_profile((LocationName.HOME, *_LONDON))).sites == ()


def test_the_distance_is_rounded_to_the_configured_precision() -> None:
    selector = _selector({"NEAR": (51.512, -0.128)})
    site = selector.select(_profile((LocationName.HOME, *_LONDON))).sites[0]
    assert site.distance_km == round(site.distance_km, DEFAULT_DISTANCE_DECIMALS)


def test_the_distance_precision_is_configurable() -> None:
    selector = _selector(
        {"NEAR": (51.512, -0.128)}, settings=SelectionSettings(distance_decimals=4)
    )
    site = selector.select(_profile((LocationName.HOME, *_LONDON))).sites[0]
    assert site.distance_km == round(site.distance_km, 4)


def test_a_selected_site_names_the_location_it_was_selected_for() -> None:
    selector = _selector({"NEAR": (51.512, -0.128)})
    site = selector.select(_profile((LocationName.WORK, *_LONDON))).sites[0]
    assert site.location_names == (LocationName.WORK,)


# --- Req 20.4: a shared site appears once, naming every location --------

def test_a_site_serving_two_locations_appears_once() -> None:
    # Both locations are close to the one site, so a per-location list would list it twice.
    selector = _selector({"SHARED": (51.5075, -0.1285)})
    selection = selector.select(
        _profile(
            (LocationName.HOME, 51.507, -0.128),
            (LocationName.WORK, 51.508, -0.129),
        )
    )
    assert [site.site_code for site in selection.sites] == ["SHARED"]


def test_a_shared_site_names_every_location_it_serves() -> None:
    selector = _selector({"SHARED": (51.5075, -0.1285)})
    selection = selector.select(
        _profile(
            (LocationName.HOME, 51.507, -0.128),
            (LocationName.WORK, 51.508, -0.129),
        )
    )
    assert set(selection.sites[0].location_names) == {
        LocationName.HOME,
        LocationName.WORK,
    }


def test_a_shared_sites_location_names_are_ordered() -> None:
    # §2: the names reach the response, so their order must be defined rather than incidental.
    selector = _selector({"SHARED": (51.5075, -0.1285)})
    forwards = selector.select(
        _profile((LocationName.HOME, 51.507, -0.128), (LocationName.WORK, 51.508, -0.129))
    )
    backwards = selector.select(
        _profile((LocationName.WORK, 51.508, -0.129), (LocationName.HOME, 51.507, -0.128))
    )
    assert forwards.sites[0].location_names == backwards.sites[0].location_names


def test_a_shared_site_reports_its_nearest_distance() -> None:
    # Serving two locations, the site has two distances; the reported one is the smaller, since
    # a site "3 km away" that is actually 1 km from the user's home understates its relevance.
    selector = _selector({"SHARED": (51.507, -0.128)})
    selection = selector.select(
        _profile((LocationName.HOME, 51.507, -0.128), (LocationName.WORK, 51.550, -0.128))
    )
    assert selection.sites[0].distance_km == 0.0


# --- Req 20.5: an unserved location is declared, not widened -----------

def test_a_location_with_no_site_in_radius_is_declared() -> None:
    selector = _selector({"FAR": _PARIS})
    selection = selector.select(_profile((LocationName.HOME, *_LONDON)))
    assert selection.unserved_locations == (LocationName.HOME,)


def test_the_radius_is_not_widened_to_reach_a_site() -> None:
    # The declaration must come WITH no site, not alongside a site found by relaxing the rule.
    selector = _selector({"FAR": _PARIS})
    selection = selector.select(_profile((LocationName.HOME, *_LONDON)))
    assert selection.sites == ()
    assert selection.unserved_locations == (LocationName.HOME,)


def test_one_unserved_location_does_not_suppress_a_served_one() -> None:
    # §5's isolation applied geographically: a gap at one location must not cost the user the
    # other location's data.
    selector = _selector({"NEAR": (51.512, -0.128)})
    selection = selector.select(
        _profile((LocationName.HOME, *_LONDON), (LocationName.WORK, *_PARIS))
    )
    assert [site.site_code for site in selection.sites] == ["NEAR"]
    assert selection.unserved_locations == (LocationName.WORK,)


def test_a_served_location_is_not_declared_unserved() -> None:
    selector = _selector({"NEAR": (51.512, -0.128)})
    selection = selector.select(_profile((LocationName.HOME, *_LONDON)))
    assert selection.unserved_locations == ()


# --- Req 20.6: the fallback for a profile with no location -------------

def test_a_profile_with_no_location_uses_the_fallback_centre() -> None:
    selector = _selector({"CENTRE": DEFAULT_GEOGRAPHIC_CENTRE})
    selection = selector.select(_profile())
    assert [site.site_code for site in selection.sites] == ["CENTRE"]


def test_the_fallback_is_declared_as_used() -> None:
    selector = _selector({"CENTRE": DEFAULT_GEOGRAPHIC_CENTRE})
    assert selector.select(_profile()).used_fallback is True


def test_the_fallback_is_not_declared_when_a_location_exists() -> None:
    selector = _selector({"NEAR": (51.512, -0.128)})
    assert selector.select(_profile((LocationName.HOME, *_LONDON))).used_fallback is False


def test_the_fallback_centre_is_configurable() -> None:
    selector = _selector(
        {"PARIS": _PARIS}, settings=SelectionSettings(fallback_centre=_PARIS)
    )
    assert [site.site_code for site in selector.select(_profile()).sites] == ["PARIS"]


def test_a_fallback_site_names_no_user_location() -> None:
    # There is no User_Location to name, and inventing one would misreport where the user is.
    selector = _selector({"CENTRE": DEFAULT_GEOGRAPHIC_CENTRE})
    assert selector.select(_profile()).sites[0].location_names == ()


def test_an_absent_profile_also_uses_the_fallback() -> None:
    # Req 17.12's default profile carries no locations, so it takes this path too.
    selector = _selector({"CENTRE": DEFAULT_GEOGRAPHIC_CENTRE})
    assert selector.select(None).used_fallback is True


# --- Req 20.7: ordering ------------------------------------------------

def test_sites_are_ordered_by_ascending_distance() -> None:
    selector = _selector(
        {"FARTHER": (51.530, -0.128), "NEARER": (51.510, -0.128)}
    )
    selection = selector.select(_profile((LocationName.HOME, *_LONDON)))
    assert [site.site_code for site in selection.sites] == ["NEARER", "FARTHER"]


def test_a_distance_tie_is_broken_by_ascending_site_code() -> None:
    # Two sites at identical distance, inserted in the order that would fail a naive sort.
    selector = _selector({"ZZZ": (51.512, -0.128), "AAA": (51.512, -0.128)})
    selection = selector.select(_profile((LocationName.HOME, *_LONDON)))
    assert [site.site_code for site in selection.sites] == ["AAA", "ZZZ"]


def test_the_ordering_is_independent_of_insertion_order() -> None:
    # A stable sort can pass for real ordering if only one insertion order is tried.
    first = _selector({"AAA": (51.512, -0.128), "ZZZ": (51.512, -0.128)}).select(
        _profile((LocationName.HOME, *_LONDON))
    )
    second = _selector({"ZZZ": (51.512, -0.128), "AAA": (51.512, -0.128)}).select(
        _profile((LocationName.HOME, *_LONDON))
    )
    assert [s.site_code for s in first.sites] == [s.site_code for s in second.sites]


# --- Req 20.8, 20.9: freshness ----------------------------------------

def test_a_fresh_reading_is_selected_and_reports_as_of() -> None:
    fresh = _NOW - dt.timedelta(hours=1)
    selector = _selector({"NEAR": (51.512, -0.128)}, [_reading("NEAR", "PM25", fresh)])
    site = selector.select(_profile((LocationName.HOME, *_LONDON))).sites[0]
    assert [m.key.species for m in site.measurements] == ["PM25"]
    assert site.as_of == fresh


def test_a_stale_reading_is_not_selected() -> None:
    stale = _NOW - dt.timedelta(hours=5)
    selector = _selector({"NEAR": (51.512, -0.128)}, [_reading("NEAR", "PM25", stale)])
    site = selector.select(_profile((LocationName.HOME, *_LONDON))).sites[0]
    assert site.measurements == ()


def test_a_site_with_no_fresh_reading_is_still_included() -> None:
    # Req 20.9, and the whole point of the clause: dropping the site would hide that the sensor
    # nearest the user has gone quiet.
    stale = _NOW - dt.timedelta(hours=5)
    selector = _selector({"NEAR": (51.512, -0.128)}, [_reading("NEAR", "PM25", stale)])
    selection = selector.select(_profile((LocationName.HOME, *_LONDON)))
    assert [site.site_code for site in selection.sites] == ["NEAR"]


def test_a_site_with_no_fresh_reading_reports_no_overall_aqi() -> None:
    # Req 20.9 forbids "reporting an older value as current", so there is nothing to index.
    stale = _NOW - dt.timedelta(hours=5)
    selector = _selector({"NEAR": (51.512, -0.128)}, [_reading("NEAR", "PM25", stale)])
    site = selector.select(_profile((LocationName.HOME, *_LONDON))).sites[0]
    assert site.measurements == ()
    assert site.as_of is None


def test_a_site_with_no_reading_at_all_is_included() -> None:
    selector = _selector({"SILENT": (51.512, -0.128)})
    selection = selector.select(_profile((LocationName.HOME, *_LONDON)))
    assert [site.site_code for site in selection.sites] == ["SILENT"]
    assert selection.sites[0].measurements == ()


def test_the_freshness_boundary_is_measured_from_the_clock() -> None:
    # §2: the window moves with the injected Clock, so the same data ages out with no timer.
    at_edge = _NOW - dt.timedelta(hours=3)
    selector = _selector({"NEAR": (51.512, -0.128)}, [_reading("NEAR", "PM25", at_edge)])
    site = selector.select(_profile((LocationName.HOME, *_LONDON))).sites[0]
    # Exactly at the window edge is still WITHIN it: Req 20.8 says "within the configured
    # freshness window", and the window exists to admit precisely that much age.
    assert [m.key.species for m in site.measurements] == ["PM25"]


def test_the_freshness_window_is_configurable() -> None:
    older = _NOW - dt.timedelta(hours=5)
    selector = _selector(
        {"NEAR": (51.512, -0.128)},
        [_reading("NEAR", "PM25", older)],
        settings=SelectionSettings(freshness_hours=6),
    )
    site = selector.select(_profile((LocationName.HOME, *_LONDON))).sites[0]
    assert [m.key.species for m in site.measurements] == ["PM25"]


def test_as_of_is_the_readings_interval_start() -> None:
    fresh = _NOW - dt.timedelta(minutes=30)
    selector = _selector({"NEAR": (51.512, -0.128)}, [_reading("NEAR", "PM25", fresh)])
    site = selector.select(_profile((LocationName.HOME, *_LONDON))).sites[0]
    assert site.as_of == fresh


def test_as_of_is_the_newest_interval_when_species_differ() -> None:
    # asOf makes STALENESS visible (Req 20.8), so with two species the reported instant must be
    # the one a reader would check — the newest present.
    older = _NOW - dt.timedelta(hours=2)
    newer = _NOW - dt.timedelta(minutes=15)
    selector = _selector(
        {"NEAR": (51.512, -0.128)},
        [_reading("NEAR", "NO2", older), _reading("NEAR", "PM25", newer)],
    )
    site = selector.select(_profile((LocationName.HOME, *_LONDON))).sites[0]
    assert site.as_of == newer


# --- Req 20.10: distances from stored coordinates ---------------------

def test_the_distance_uses_the_stored_metadata_coordinates() -> None:
    # One degree of latitude is 111.195 km (the Requirement 15.4 constant), so this pins the
    # distance independently of the implementation's own arithmetic.
    selector = _selector(
        {"NORTH": (52.507, -0.128)}, settings=SelectionSettings(radius_km=200.0)
    )
    site = selector.select(_profile((LocationName.HOME, 51.507, -0.128))).sites[0]
    assert 111.0 <= site.distance_km <= 111.4


def test_the_distance_uses_the_reduced_precision_user_coordinates() -> None:
    # Req 20.10 says the REDUCED-precision coordinates, which is what the profile stores
    # (Req 17.5). A selector reaching for a more precise value would have nowhere to get one.
    profile = _profile((LocationName.HOME, 51.5074999, -0.1278999))
    assert profile.locations[0].latitude == 51.507
    selector = _selector({"NEAR": (51.507, -0.128)})
    site = selector.select(profile).sites[0]
    assert site.distance_km < 0.1


# --- Req 17.10: no cross-user leakage, now assertable ------------------

def test_one_users_locations_do_not_select_another_users_sites() -> None:
    # The response-level half of Req 17.10, deferred from task 17.3 until selection existed.
    selector = _selector({"LONDON": (51.507, -0.128), "PARIS": (48.857, 2.352)})
    london_user = _profile((LocationName.HOME, *_LONDON), user_id="user-a")
    paris_user = _profile((LocationName.HOME, *_PARIS), user_id="user-b")

    a = selector.select(london_user)
    b = selector.select(paris_user)

    assert [site.site_code for site in a.sites] == ["LONDON"]
    assert [site.site_code for site in b.sites] == ["PARIS"]


def test_neither_user_sees_the_others_readings() -> None:
    fresh = _NOW - dt.timedelta(minutes=10)
    selector = _selector(
        {"LONDON": (51.507, -0.128), "PARIS": (48.857, 2.352)},
        [_reading("LONDON", "PM25", fresh), _reading("PARIS", "NO2", fresh)],
    )
    a = selector.select(_profile((LocationName.HOME, *_LONDON), user_id="user-a"))
    species_served = {m.key.species for site in a.sites for m in site.measurements}
    assert species_served == {"PM25"}


# --- shape ------------------------------------------------------------

def test_the_selector_takes_one_profile_and_nothing_wider() -> None:
    # Keeps Req 17.10's isolation structural, as query_positions does in serving/profiles.py.
    import inspect

    assert set(inspect.signature(GeoSelector.select).parameters) == {"self", "profile"}


def test_a_non_positive_n_is_refused() -> None:
    with pytest.raises(ValueError, match="nearest_n"):
        SelectionSettings(nearest_n=0)


def test_a_non_positive_radius_is_refused() -> None:
    with pytest.raises(ValueError, match="radius"):
        SelectionSettings(radius_km=0.0)


def test_a_non_positive_freshness_window_is_refused() -> None:
    # A zero window would admit no reading at all, silently emptying every response.
    with pytest.raises(ValueError, match="freshness"):
        SelectionSettings(freshness_hours=0)
