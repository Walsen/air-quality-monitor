"""Tests for history summarising and snapshot site reading (tasks 9.2, 9.3).

Two structural invariants carry most of the weight here, and both are stronger than a text
check.

`test_the_history_module_never_reads_a_measurement_value` is how Req 3.4 is discharged. Rather
than trying to detect a trend, an average or an exceedance count after the fact, the module is
built so it never sees a value to compute one FROM: it reads instants, species, units,
confidence and the record count, and never `correctedValue`, `reportedValue` or `subIndex`. A
module that cannot see the numbers cannot average them.

`test_every_sensor_entry_becomes_a_site_view` is Req 2.5. Service 2 includes a stale site
deliberately, so omitting one would hide that the nearest sensor has gone quiet — the failure
mode that looks like a clean answer. The invariant is a count equality over several bodies,
including one where EVERY site is quiet, because a filter that drops empty sites would still
pass a test using a body that happens to have none.
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib

from aqm_advisor.adapters.local import canned_air_quality
from aqm_advisor.domain.history import (
    HistoryView,
    history_view,
)
from aqm_advisor.domain.snapshot import (
    DEFAULT_PROFILE_TEXT,
    NO_CURRENT_READING_TEXT,
    snapshot_sites,
)

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _history_body(reading_count: int = 3, truncated: bool = False) -> dict[str, object]:
    """A history body in Service 2's real shape (HistoryResponse)."""
    return {
        "siteCode": "AQM1",
        "startTime": "2026-06-24T12:00:00Z",
        "endTime": "2026-07-01T12:00:00Z",
        "readings": [
            {
                "dateTime": f"2026-06-{24 + index:02d}T12:00:00Z",
                "species": "PM25",
                "correctedValue": 18.2 + index,
                "units": "ug.m-3",
                "qualityFlag": "calibrated",
                "confidence": "HIGH",
                "subIndex": 60 + index,
                "band": "Moderate",
            }
            for index in range(reading_count)
        ],
        "truncated": truncated,
    }


def _sensor(site_code: str, *, measurements: list[dict[str, object]]) -> dict[str, object]:
    return {
        "siteCode": site_code,
        "siteName": f"Site {site_code}",
        "locationName": "home",
        "distanceKm": 1.2,
        "asOf": "2026-07-01T12:00:00Z",
        "measurements": measurements,
        "overallAqi": 68 if measurements else None,
        "band": "Moderate" if measurements else None,
        "drivingPollutant": "PM25" if measurements else None,
        "confidence": "HIGH" if measurements else None,
    }


_A_MEASUREMENT: dict[str, object] = {
    "species": "PM25",
    "reportedValue": 24.1,
    "correctedValue": 18.2,
    "units": "ug.m-3",
    "qualityFlag": "calibrated",
    "confidence": "HIGH",
    "subIndex": 68,
    "band": "Moderate",
    "method": "nowcast",
    "mixingRatioPpb": None,
}


# --- Req 3.4: a summary is labelled a summary and names its count --------

def test_the_history_view_names_the_reading_count() -> None:
    # Req 3.4 requires the count explicitly. It is the reader's only way to judge how much the
    # summary
    # rests on — "air has been poor" over 2 readings and over 200 are different claims.
    view = history_view(_history_body(reading_count=5))
    assert view.reading_count == 5
    assert "5" in view.text


def test_the_count_is_written_in_digits() -> None:
    # Grounding reads digit forms only, so a spelled-out count would be invisible to the check
    # that has
    # to permit it.
    assert "3" in history_view(_history_body(reading_count=3)).text


def test_the_summary_says_it_is_a_summary() -> None:
    # Req 3.4: describe the summary AS one. Without the label a reader takes a description of a
    # series
    # for a measurement, which is exactly the conflation the requirement forbids.
    text = history_view(_history_body()).text.casefold()
    assert "summar" in text


def test_the_view_states_no_trend_language() -> None:
    # A trend is a computation. Even without arithmetic, wording like "rising" would present a
    # derived
    # direction as a retrieved fact — the requirement's substance rather than its letter.
    text = history_view(_history_body()).text.casefold()
    for word in ("rising", "falling", "improving", "worsening", "trend", "average", "mean"):
        assert word not in text, word


def test_an_empty_series_is_reported_as_no_readings() -> None:
    # Zero readings is an answer, not an error. Returning nothing would leave the model to guess
    # whether
    # retrieval failed or the window was genuinely quiet.
    view = history_view(_history_body(reading_count=0))
    assert view.reading_count == 0
    assert "0" in view.text or "no readings" in view.text.casefold()


def test_a_truncated_series_says_so() -> None:
    # Service 2 sets `truncated` when it capped the window. A summary naming a count without
    # saying it
    # was capped overstates its own coverage.
    assert "truncat" in history_view(_history_body(truncated=True)).text.casefold()


def test_an_untruncated_series_does_not_claim_truncation() -> None:
    assert "truncat" not in history_view(_history_body()).text.casefold()


def test_the_window_is_read_from_the_body_not_recomputed() -> None:
    # Req 2.2's discipline applied to history: the bounds are Service 2's, echoed rather than
    # derived.
    view = history_view(_history_body())
    assert view.start == "2026-06-24T12:00:00Z"
    assert view.end == "2026-07-01T12:00:00Z"


def test_the_species_are_named_in_first_appearance_order() -> None:
    # Deterministic without sorting. Sorting would impose an order Service 2 did not choose, and
    # Req
    # 12.2's reasoning (order is the server's) applies to any relayed sequence.
    body = _history_body(reading_count=2)
    readings = body["readings"]
    assert isinstance(readings, list)
    readings[0]["species"] = "NO2"
    assert history_view(body).species == ("NO2", "PM25")


def test_a_malformed_readings_block_reads_as_no_readings() -> None:
    # A shape this service does not expect is a Service 2 change. Failing the turn over it would
    # lose
    # the air-quality answer as well, which is the part the user asked for.
    body = _history_body()
    body["readings"] = "not a list"
    assert history_view(body).reading_count == 0


# --- Req 3.4, structurally: it cannot compute what it cannot see --------

_HISTORY_SOURCE = pathlib.Path(
    "src/aqm_advisor/domain/history.py"
).read_text(encoding="utf-8")


def _accessed_field_names(source: str) -> set[str]:
    """Every string literal the CODE uses, excluding docstrings.

    A raw substring sweep cannot serve here: the history module's own docstring NAMES the fields
    it must never read, in order to explain why. Scanning the source text fails on that prose —
    the same self-reference trap as a line-reflow tool that cannot process its own source.
    Comments are absent from the AST already, so only docstrings need excluding.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if not body or not isinstance(body[0], ast.Expr):
                continue
            if isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def test_the_history_module_never_reads_a_measurement_value() -> None:
    # THE structural discharge of Req 3.4. Rather than detecting a computed trend after the
    # fact, the
    # module is built so it never sees a value to compute one from: a module that cannot read
    # the
    # corrected value cannot average it, cannot compare two into a direction, and cannot count
    # exceedances of a threshold.
    accessed = _accessed_field_names(_HISTORY_SOURCE)
    for field_name in ("correctedValue", "reportedValue", "subIndex", "overallAqi"):
        assert field_name not in accessed, field_name


def test_the_field_name_detector_sees_a_real_access() -> None:
    # Self-check: a detector that found nothing anywhere would report this guarantee for free.
    planted = "def read(entry):\n    return entry.get('correctedValue')\n"
    assert "correctedValue" in _accessed_field_names(planted)


def test_the_field_name_detector_ignores_prose() -> None:
    # The other half: it must NOT fire on a docstring that names the field in order to forbid
    # it.
    planted = '"""This module never reads correctedValue."""\n'
    assert "correctedValue" not in _accessed_field_names(planted)


def test_the_history_module_performs_no_arithmetic() -> None:
    # Belt to the braces above: no operator, so no derivation even over a field added later.
    # `BitOr` is excluded deliberately: `str | None` is a type union, not a computation. Naming
    # the
    # arithmetic operators rather than sweeping every BinOp is the same choice the reporting
    # guard made,
    # and sweeping broadly is what made this test fail on its own type annotations first time
    # round.
    tree = ast.parse(_HISTORY_SOURCE)
    operators = [
        type(node.op).__name__
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult | ast.Div | ast.Add | ast.Sub | ast.Pow | ast.FloorDiv)
    ]
    assert operators == [], f"the history module computes: {operators}"


def test_the_history_module_aggregates_nothing() -> None:
    # `sum`, `min`, `max` and `sorted` are how an average, a range or a ranking would arrive
    # without a
    # single arithmetic operator appearing.
    tree = ast.parse(_HISTORY_SOURCE)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called.isdisjoint({"sum", "min", "max", "sorted", "mean", "median"})


def test_the_value_detector_would_catch_a_computation() -> None:
    # Self-check: a detector aimed at the wrong node reports a guarantee it is not making.
    planted = ast.parse("def trend(readings):\n    return readings[-1] - readings[0]\n")
    found = [node for node in ast.walk(planted) if isinstance(node, ast.BinOp)]
    assert found != []


# --- Req 2.5: a quiet site is described, never omitted -----------------

def test_every_sensor_entry_becomes_a_site_view() -> None:
    # Req 2.5's invariant, over bodies that include an ALL-quiet one — a filter dropping empty
    # sites
    # would still pass a test whose body happens to have none.
    bodies = [
        [_sensor("AQM1", measurements=[_A_MEASUREMENT])],
        [_sensor("AQM1", measurements=[])],
        [_sensor("AQM1", measurements=[_A_MEASUREMENT]), _sensor("AQM2", measurements=[])],
        [_sensor("AQM1", measurements=[]), _sensor("AQM2", measurements=[])],
    ]
    for sensors in bodies:
        assert len(snapshot_sites({"nearestSensors": sensors})) == len(sensors)


def test_a_quiet_site_is_described_as_having_no_current_reading() -> None:
    # Service 2 includes a stale site deliberately. Omitting it would hide that the nearest
    # sensor has
    # gone quiet — a failure that reads as a clean answer.
    views = snapshot_sites({"nearestSensors": [_sensor("AQM1", measurements=[])]})
    assert views[0].has_reading is False
    assert NO_CURRENT_READING_TEXT.casefold() in views[0].text.casefold()


def test_a_quiet_site_still_names_the_site() -> None:
    # "A site has no reading" is useless without saying WHICH, since the point is that the
    # user's
    # nearest sensor is the one that went quiet.
    views = snapshot_sites({"nearestSensors": [_sensor("AQM7", measurements=[])]})
    assert "AQM7" in views[0].text or "Site AQM7" in views[0].text


def test_a_reading_site_is_not_described_as_quiet() -> None:
    # Non-vacuity: text applied to every site would make the quiet marker meaningless.
    views = snapshot_sites({"nearestSensors": [_sensor("AQM1", measurements=[_A_MEASUREMENT])]})
    assert views[0].has_reading is True
    assert NO_CURRENT_READING_TEXT.casefold() not in views[0].text.casefold()


def test_the_reading_site_values_are_read_not_derived() -> None:
    # Req 2.3: driving pollutant, sub-index, band and confidence come from the ENTRY, never from
    # the
    # raw measurements. Reading them is the whole job.
    views = snapshot_sites({"nearestSensors": [_sensor("AQM1", measurements=[_A_MEASUREMENT])]})
    view = views[0]
    assert view.driving_pollutant == "PM25"
    assert view.sub_index == 68
    assert view.band == "Moderate"
    assert view.confidence == "HIGH"


def test_the_real_canned_snapshot_reads_cleanly() -> None:
    # Guards the reader against the shape Service 2 actually serves, not only the local fixture.
    views = snapshot_sites(canned_air_quality())
    assert len(views) == 1
    assert views[0].has_reading is True


def test_a_missing_sensor_list_yields_no_sites_rather_than_raising() -> None:
    assert snapshot_sites({}) == ()


def test_a_malformed_sensor_entry_is_read_as_quiet_rather_than_dropped() -> None:
    # Dropping it would breach Req 2.5 by a different route: the site vanishes, and the reason
    # it
    # vanished is a shape change nobody sees.
    views = snapshot_sites({"nearestSensors": ["not a mapping"]})
    assert len(views) == 1
    assert views[0].has_reading is False


# --- Req 2.4: say when the defaults were used --------------------------

def test_the_default_profile_text_says_no_profile_was_found_and_defaults_used() -> None:
    # Req 2.4 requires BOTH halves: that none was found, and that defaults are in use. Either
    # alone
    # leaves the user unable to tell whether the advice was about them.
    lowered = DEFAULT_PROFILE_TEXT.casefold()
    assert "default" in lowered
    assert "profile" in lowered


def test_the_default_profile_text_promises_no_personalisation() -> None:
    # It must not imply the guidance was tailored, which is the thing it exists to deny.
    lowered = DEFAULT_PROFILE_TEXT.casefold()
    for claim in ("your condition", "personalised for you", "tailored"):
        assert claim not in lowered, claim


# --- the structural discipline the snapshot reader shares --------------

_SNAPSHOT_SOURCE = pathlib.Path(
    "src/aqm_advisor/domain/snapshot.py"
).read_text(encoding="utf-8")


def test_the_snapshot_module_performs_no_arithmetic() -> None:
    # Req 2.2: treat the body as authoritative and modify, round or recompute nothing.
    tree = ast.parse(_SNAPSHOT_SOURCE)
    operators = [
        type(node.op).__name__
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Mult | ast.Div | ast.Add | ast.Sub | ast.Pow | ast.FloorDiv)
    ]
    assert operators == [], f"the snapshot module computes: {operators}"


def test_the_snapshot_module_does_not_reorder_the_sites() -> None:
    # The order is Service 2's, which serves them nearest-first. Re-ranking would be this
    # service
    # choosing which site matters — the same derivation Req 9.4 forbids in the basis.
    tree = ast.parse(_SNAPSHOT_SOURCE)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called.isdisjoint({"sorted", "min", "max", "reversed"})


def test_the_view_is_immutable() -> None:
    # A view a caller can edit is a body this service modified, one indirection later.
    view = history_view(_history_body())
    assert isinstance(view, HistoryView)
    try:
        view.reading_count = 99  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("HistoryView must be frozen")
