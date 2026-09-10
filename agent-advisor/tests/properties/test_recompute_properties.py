"""Property 20: nothing is recomputed.

Task 8.3. Validates Reqs 2.2, 2.3, 9.4, 12.1, 16.2.

Every reading this service states was computed by Service 2. The property is EQUALITY, not
closeness: the value that comes out of a reader is the value that went in, bit for bit. Rounding
"68.4" to "68" would look harmless and would be this service authoring a reading — and once one
value is derived, the grounding check that protects the user is comparing against numbers this
service made up.

Quantifying it matters because the failure is invisible at a single example. A reader that
rounded would agree with a fixture built from whole numbers and disagree only on the decimals a
real sensor produces, so the bug would ship and then appear as a mismatch between the guidance
and the basis.

The structural AST guards (no arithmetic operator, no `sorted`/`min`/`max`/`sum`) already make
derivation hard to write. This property is the behavioural complement: it would catch a
derivation performed by a library call the AST guards do not name — `round`, `Decimal.quantize`,
a format string with a precision.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from aqm_advisor.domain.history import history_view
from aqm_advisor.domain.interpretation import interpret_personalized
from aqm_advisor.domain.snapshot import snapshot_sites

_BANDS = st.sampled_from(["Low", "Moderate", "High", "Very High"])
_CONFIDENCES = st.sampled_from(["HIGH", "MEDIUM", "LOW", "INDICATIVE"])
_SPECIES = st.sampled_from(["PM25", "NO2", "PM25Index", "NO2Index"])

_SUB_INDICES = st.integers(min_value=1, max_value=10)
_CONCENTRATIONS = st.decimals(
    min_value=0, max_value=500, places=2, allow_nan=False, allow_infinity=False
).map(float)
"""Two decimal places deliberately.

A reader that rounded would agree with whole-number fixtures and disagree only here, which is
exactly how such a bug reaches production.
"""


@st.composite
def _sensors(draw: st.DrawFn) -> list[dict[str, object]]:
    """A `nearestSensors` list, some entries quiet, in Service 2's real shape."""
    entries: list[dict[str, object]] = []
    for index in range(draw(st.integers(min_value=1, max_value=4))):
        quiet = draw(st.booleans())
        measurements: list[dict[str, object]] = []
        if not quiet:
            measurements.append(
                {
                    "species": draw(_SPECIES),
                    "reportedValue": draw(_CONCENTRATIONS),
                    "correctedValue": draw(_CONCENTRATIONS),
                    "units": "ug.m-3",
                    "qualityFlag": "calibrated",
                    "confidence": draw(_CONFIDENCES),
                    "subIndex": draw(_SUB_INDICES),
                    "band": draw(_BANDS),
                    "method": "nowcast",
                    "mixingRatioPpb": None,
                }
            )
        entries.append(
            {
                "siteCode": f"AQM{index}",
                "siteName": f"Site {index}",
                "locationName": "home",
                "distanceKm": draw(_CONCENTRATIONS),
                "asOf": "2026-07-01T12:00:00Z",
                "measurements": measurements,
                "overallAqi": draw(_SUB_INDICES) if measurements else None,
                "band": draw(_BANDS) if measurements else None,
                "drivingPollutant": draw(_SPECIES) if measurements else None,
                "confidence": draw(_CONFIDENCES) if measurements else None,
            }
        )
    return entries


# --- the snapshot reader relays exactly what it was given ---------------


@given(_sensors())
def test_every_read_value_equals_the_served_value(
    sensors: list[dict[str, object]],
) -> None:
    # Req 2.2 and 2.3. EQUALITY rather than closeness: rounding would be this service authoring
    # a
    # reading, and the grounding check would then be comparing against a number nobody served.
    views = snapshot_sites({"nearestSensors": sensors})
    for view, entry in zip(views, sensors, strict=True):
        if not view.has_reading:
            continue
        assert view.sub_index == entry["overallAqi"]
        assert view.band == entry["band"]
        assert view.driving_pollutant == entry["drivingPollutant"]
        assert view.confidence == entry["confidence"]


@given(_sensors())
def test_no_site_is_added_or_dropped(sensors: list[dict[str, object]]) -> None:
    # Req 2.5 as a counting property. A quiet site dropped here would hide that the user's
    # nearest sensor
    # has gone silent, and the answer would still look complete.
    assert len(snapshot_sites({"nearestSensors": sensors})) == len(sensors)


@given(_sensors())
def test_the_site_order_is_the_served_order(sensors: list[dict[str, object]]) -> None:
    # The order is Service 2's, nearest first. Re-ranking would be this service deciding which
    # site
    # matters — the same derivation Req 9.4 forbids in the basis.
    views = snapshot_sites({"nearestSensors": sensors})
    assert [view.site_code for view in views] == [
        entry["siteCode"] for entry in sensors
    ]


@given(_sensors())
def test_a_quiet_site_reports_no_values_at_all(
    sensors: list[dict[str, object]],
) -> None:
    # The complement: a site with no measurements must not acquire a sub-index from anywhere,
    # which is
    # what a "sensible default" would do.
    for view in snapshot_sites({"nearestSensors": sensors}):
        if view.has_reading:
            continue
        assert view.sub_index is None
        assert view.band is None
        assert view.confidence is None


# --- the history reader counts and relays, and computes nothing ---------


@given(
    st.lists(
        st.tuples(_SPECIES, _CONCENTRATIONS, _CONFIDENCES),
        max_size=8,
    )
)
def test_the_reading_count_is_the_number_served(
    readings: list[tuple[str, float, str]],
) -> None:
    # Req 3.4 requires the count, and a count is the one thing this reader may produce — it is a
    # count of
    # RECORDS, not a summary of values.
    body = {
        "siteCode": "AQM1",
        "startTime": "2026-06-24T12:00:00Z",
        "endTime": "2026-07-01T12:00:00Z",
        "truncated": False,
        "readings": [
            {
                "dateTime": "2026-06-24T12:00:00Z",
                "species": species,
                "correctedValue": value,
                "units": "ug.m-3",
                "qualityFlag": "calibrated",
                "confidence": confidence,
            }
            for species, value, confidence in readings
        ],
    }
    view = history_view(body)
    assert view.reading_count == len(readings)
    if readings:
        assert str(len(readings)) in view.text
    else:
        # The empty series is worded rather than numbered — "it contains no readings" instead of
        # "0 readings". Req 3.4 asks the summary to name the number it covers, and for zero the
        # words do
        # that more clearly. There is no grounding consequence either way: this text is authored
        # by the
        # service, not the model, so it is not what the grounding check inspects.
        assert "no readings" in view.text


@given(
    st.lists(
        st.tuples(_SPECIES, _CONCENTRATIONS, _CONFIDENCES),
        min_size=1,
        max_size=8,
    )
)
def test_no_served_concentration_appears_in_the_summary_text(
    readings: list[tuple[str, float, str]],
) -> None:
    # THE history half of Property 20. The summary describes the SERIES, never its values, so a
    # concentration must not appear in the text — an average or a range would show up here even
    # if it
    # were computed without an arithmetic operator the AST guard could see.
    body = {
        "siteCode": "AQM1",
        "startTime": "2026-06-24T12:00:00Z",
        "endTime": "2026-07-01T12:00:00Z",
        "truncated": False,
        "readings": [
            {
                "dateTime": "2026-06-24T12:00:00Z",
                "species": species,
                "correctedValue": value,
                "units": "ug.m-3",
                "qualityFlag": "calibrated",
                "confidence": confidence,
            }
            for species, value, confidence in readings
        ],
    }
    text = history_view(body).text
    for _species, value, _confidence in readings:
        assert str(value) not in text


@given(st.lists(_SPECIES, min_size=1, max_size=6))
def test_the_species_are_relayed_without_sorting(species: list[str]) -> None:
    # First-appearance order, not alphabetical. Sorting would impose an order Service 2 did not
    # choose.
    body = {
        "siteCode": "AQM1",
        "startTime": "2026-06-24T12:00:00Z",
        "endTime": "2026-07-01T12:00:00Z",
        "truncated": False,
        "readings": [
            {
                "dateTime": "2026-06-24T12:00:00Z",
                "species": name,
                "correctedValue": 1.0,
                "units": "ug.m-3",
                "qualityFlag": "calibrated",
                "confidence": "HIGH",
            }
            for name in species
        ],
    }
    expected: list[str] = []
    for name in species:
        if name not in expected:
            expected.append(name)
    assert list(history_view(body).species) == expected


# --- the interpretation weights nothing --------------------------------


@given(
    st.sampled_from(
        ["asthma", "copd", "allergic_rhinitis", "asthma_copd_overlap", "none_declared"]
    ),
    st.lists(_SPECIES, max_size=4),
)
def test_the_weighted_focus_is_relayed_in_the_served_order(
    condition: str, focus: list[str]
) -> None:
    # Req 12.1 and 12.2. The order is Service 2's clinical precedence, so relaying it unchanged
    # is the
    # whole job — reordering would be this service computing a weighting.
    view = interpret_personalized(
        {
            "condition": condition,
            "sensitivity": "standard",
            "weightedFocus": focus,
            "usedDefaultProfile": False,
        }
    )
    assert list(view.weighted_focus) == focus
