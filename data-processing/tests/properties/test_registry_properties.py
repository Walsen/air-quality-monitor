"""Registry property tests (tasks 14.3, 14.4).

Feature: ingestion-and-serving-service
- Property 28: nearest-N ordering and radius inclusion
  (Requirements 15.5, 15.6, 15.7, 15.8, 20.2)
- Property 29: great-circle distance is symmetric and zero on identity
  (Requirements 15.4, 20.10)

Property 28 asserts the SELECTION as well as the ordering. A nearest-N that returns the N
sites it happens to encounter first, sorted, would satisfy an ordering-only property while
being wrong — so the returned set is checked against the truly-nearest set computed
independently.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.adapters.memory import InMemorySensorRegistryStore
from aqm_ingestion.adapters.memory.adapters import _great_circle_km
from aqm_ingestion.contract.records import SensorMetadataRecord
from tests.unit.test_records import GOLDEN_METADATA_PAYLOAD

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)

# Latitudes stop short of the poles: the requirement is about a sensor fleet, and a
# generated pole crossing would test the haversine's edge behaviour rather than Req 15.
_latitude = st.floats(min_value=-85.0, max_value=85.0, allow_nan=False)
_longitude = st.floats(min_value=-180.0, max_value=180.0, allow_nan=False)


def _record(**overrides: object) -> SensorMetadataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_METADATA_PAYLOAD)) | overrides
    return SensorMetadataRecord(**fields)


@st.composite
def _sites(draw: st.DrawFn) -> list[tuple[str, float, float, bool]]:
    """Distinct site codes with positions and an activity flag."""
    codes = draw(
        st.lists(
            st.text(alphabet="ABCDEFGH", min_size=4, max_size=4),
            min_size=1,
            max_size=8,
            unique=True,
        )
    )
    return [
        (code, draw(_latitude), draw(_longitude), draw(st.booleans()))
        for code in codes
    ]


def _registry(sites: list[tuple[str, float, float, bool]]) -> InMemorySensorRegistryStore:
    registry = InMemorySensorRegistryStore()
    for code, lat, lon, active in sites:
        registry.upsert(
            _record(
                SiteCode=code,
                Latitude=f"{lat:.7f}",
                Longitude=f"{lon:.7f}",
                # a past EndDate is how Req 2.8 marks a site inactive
                EndDate=None if active else "2026-01-01T00:00:00Z",
            ),
            _NOW,
        )
    return registry


@given(
    sites=_sites(),
    query=st.tuples(_latitude, _longitude),
    n=st.integers(min_value=1, max_value=10),
    radius=st.one_of(st.none(), st.floats(min_value=0.0, max_value=20_000.0)),
)
def test_property_28_nearest_n_ordering_and_radius_inclusion(
    sites: list[tuple[str, float, float, bool]],
    query: tuple[float, float],
    n: int,
    radius: float | None,
) -> None:
    """Feature: ingestion-and-serving-service, Property 28."""
    lat, lon = query
    registry = _registry(sites)
    result = registry.nearest(lat=lat, lon=lon, n=n, max_km=radius)

    # Req 15.7: distances are non-decreasing
    distances = [site.distance_km for site in result]
    assert distances == sorted(distances)

    # Req 15.5: never more than N
    assert len(result) <= n

    # Req 15.8: no inactive site appears, though get() still resolves it
    for site in result:
        assert site.entry.active is True
    for code, _lat, _lon, active in sites:
        if not active:
            assert registry.get(code) is not None  # still resolvable

    # Req 15.6/15.7: the radius bound is inclusive and nothing beyond it is returned
    if radius is not None:
        assert all(site.distance_km <= radius for site in result)

    # Req 15.5 SELECTION, not just ordering: computed independently, so a nearest-N that
    # returned the first N it encountered and sorted them would fail here
    eligible = sorted(
        (
            (_great_circle_km(lat, lon, site_lat, site_lon), code)
            for code, site_lat, site_lon, active in sites
            if active
        ),
        key=lambda pair: (pair[0], pair[1]),  # distance then SiteCode (Req 15.5)
    )
    if radius is not None:
        eligible = [pair for pair in eligible if pair[0] <= radius]
    expected = [code for _distance, code in eligible[:n]]
    assert [site.entry.record.SiteCode for site in result] == expected


@given(sites=_sites(), query=st.tuples(_latitude, _longitude))
def test_property_28_a_tie_is_broken_by_site_code(
    sites: list[tuple[str, float, float, bool]], query: tuple[float, float]
) -> None:
    """Feature: ingestion-and-serving-service, Property 28 (deterministic ties).

    Requirement 15.5 breaks a distance tie by ascending `SiteCode` so the order is
    deterministic. Asserted over the RESULT rather than by constructing a tie, so it holds
    for the incidental ties the generator produces as well as contrived ones.
    """
    lat, lon = query
    result = _registry(sites).nearest(lat=lat, lon=lon, n=len(sites), max_km=None)
    keys = [(site.distance_km, site.entry.record.SiteCode) for site in result]
    assert keys == sorted(keys)


@given(
    first=st.tuples(_latitude, _longitude),
    second=st.tuples(_latitude, _longitude),
)
def test_property_29_great_circle_distance_is_symmetric_and_zero_on_identity(
    first: tuple[float, float], second: tuple[float, float]
) -> None:
    """Feature: ingestion-and-serving-service, Property 29."""
    lat1, lon1 = first
    lat2, lon2 = second

    there = _great_circle_km(lat1, lon1, lat2, lon2)
    back = _great_circle_km(lat2, lon2, lat1, lon1)

    # Req 15.4: symmetric. Compared with a tolerance rather than exactly, because the two
    # evaluations differ in floating-point rounding order even though the formula is
    # symmetric in exact arithmetic.
    assert there == pytest.approx(back, rel=1e-12, abs=1e-9)

    # zero on identity, in both directions
    assert _great_circle_km(lat1, lon1, lat1, lon1) == pytest.approx(0.0, abs=1e-9)
    assert _great_circle_km(lat2, lon2, lat2, lon2) == pytest.approx(0.0, abs=1e-9)

    # and non-negative and bounded by half the Earth's circumference
    assert there >= 0.0
    assert there <= 20_100.0


@given(
    origin=st.tuples(_latitude, _longitude),
    middle=st.tuples(_latitude, _longitude),
    far=st.tuples(_latitude, _longitude),
)
def test_property_29_distance_satisfies_the_triangle_inequality(
    origin: tuple[float, float],
    middle: tuple[float, float],
    far: tuple[float, float],
) -> None:
    """Feature: ingestion-and-serving-service, Property 29 (a metric, not just symmetric).

    Symmetry and zero-on-identity alone are satisfied by some plainly wrong functions —
    the constant zero satisfies both. The triangle inequality is what makes this a metric,
    and it is what nearest-N's ordering implicitly relies on.
    """
    direct = _great_circle_km(*origin, *far)
    via = _great_circle_km(*origin, *middle) + _great_circle_km(*middle, *far)
    assert direct <= via + 1e-6
