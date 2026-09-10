"""Tests for this service's own instant formatter (task 3.2).

Service 2 has a function of the same name. This is deliberately a SEPARATE implementation,
because the engineering practices forbid importing across service directories until a shared
contract package is specced — so what has to be tested is that the two produce the same WIRE
FORM, not that they share code.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_advisor.domain.instants import iso_z, parse_iso_z


def test_a_utc_instant_renders_with_a_z_suffix() -> None:
    assert iso_z(dt.datetime(2026, 7, 1, 12, 0, 0, tzinfo=dt.UTC)) == "2026-07-01T12:00:00Z"


def test_the_rendered_form_carries_no_fractional_second() -> None:
    # The wire form is whole seconds. A microsecond component would make two renderings of the
    # same
    # logical instant differ, which breaks the record identifier's stability (Req 20.2).
    rendered = iso_z(dt.datetime(2026, 7, 1, 12, 0, 0, 123456, tzinfo=dt.UTC))
    assert rendered == "2026-07-01T12:00:00Z"
    assert "." not in rendered


def test_an_offset_instant_is_normalised_to_utc() -> None:
    # Two clients in different zones naming the SAME instant must produce the same identifier,
    # or the
    # audit trail would hold two references to one reading.
    offset = dt.timezone(dt.timedelta(hours=2))
    assert iso_z(dt.datetime(2026, 7, 1, 14, 0, 0, tzinfo=offset)) == "2026-07-01T12:00:00Z"


def test_a_naive_instant_is_refused() -> None:
    # A naive datetime has no instant — only a wall-clock reading. Guessing UTC would silently
    # shift a
    # reading's identity by the offset, so this refuses rather than assuming.
    with pytest.raises(ValueError, match="timezone"):
        iso_z(dt.datetime(2026, 7, 1, 12, 0, 0))


def test_the_rendering_round_trips() -> None:
    original = dt.datetime(2026, 7, 1, 12, 34, 56, tzinfo=dt.UTC)
    assert parse_iso_z(iso_z(original)) == original


@pytest.mark.parametrize(
    "text",
    ["not a date", "2026-07-01T12:00:00", "2026-07-01 12:00:00Z", "", "2026-13-01T00:00:00Z"],
)
def test_a_malformed_wire_form_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="instant"):
        parse_iso_z(text)


def test_the_round_trip_holds_across_a_range_of_instants() -> None:
    # The property behind the single example above, so the pair cannot pass by coincidence.
    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    for hours in range(0, 24 * 400, 137):
        instant = base + dt.timedelta(hours=hours, minutes=hours % 60, seconds=hours % 60)
        assert parse_iso_z(iso_z(instant)) == instant


def test_this_module_never_reads_the_clock() -> None:
    # Determinism (practices §2): formatting is a pure function of its argument. A module that
    # could
    # reach for "now" would make a record identifier depend on when it was rendered.
    import ast
    import pathlib

    from aqm_advisor.domain import instants

    tree = ast.parse(pathlib.Path(instants.__file__).read_text(encoding="utf-8"))
    called = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for forbidden in ("dt.datetime.now", "datetime.now", "dt.date.today", "time.time"):
        assert forbidden not in called
