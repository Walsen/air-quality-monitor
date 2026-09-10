"""Reading the retrieved snapshot's sites, including the quiet ones (Requirements 2.2 to 2.5).

**Req 2.5 is the reason this module exists.** A `nearestSensors` entry with an empty measurement
set must be described as having no current reading rather than omitted, because Service 2
includes a stale site DELIBERATELY — and omitting it would hide that the user's nearest sensor
has gone quiet. That is a failure which reads as a clean answer, which is the worst kind: the
guidance looks complete and is silently about a sensor further away.

So the reader is total. Every entry becomes a view, including a malformed one, which is read as
quiet rather than dropped — dropping it would breach Req 2.5 by a different route, with the site
vanishing and the shape change that caused it visible to nobody. A test asserts the count
equality over several bodies including one where EVERY site is quiet, because a filter that
drops empty sites still passes a test whose fixture happens to have none.

**Nothing here derives.** Req 2.3 says the driving pollutant, sub-index, band and confidence are
read from the entry, never derived from the raw measurements, and Req 2.2 says the body is
authoritative and no value is modified, rounded or recomputed. Enforced by AST tests for no
arithmetic and no `sorted`/`min`/`max`/`reversed` — the site order is Service 2's nearest-first,
and re-ranking would be this service deciding which site matters, the same derivation Req 9.4
forbids in the basis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

NO_CURRENT_READING_TEXT = "has no current reading"
"""Req 2.5's phrasing. A quiet site is reported with this, never left out."""

DEFAULT_PROFILE_TEXT = (
    "No saved health profile was found for you, so this guidance uses general defaults rather "
    "than anything specific to you."
)
"""Req 2.4's statement, carrying BOTH halves.

That none was found, and that defaults are in use. Either alone leaves the user unable to tell
whether the advice was about them — and a user who believes generic advice was personalised will
trust it further than it deserves.
"""


@dataclass(frozen=True, slots=True)
class SiteView:
    """One `nearestSensors` entry as read, with nothing computed from it."""

    site_code: str | None
    site_name: str | None
    location_name: str | None
    has_reading: bool
    driving_pollutant: str | None
    sub_index: int | None
    band: str | None
    confidence: str | None
    text: str


def _optional_str(entry: dict[str, Any], key: str) -> str | None:
    value = entry.get(key)
    return str(value) if isinstance(value, (str, int)) else None


def _quiet_view(entry: dict[str, Any]) -> SiteView:
    """A site with nothing to report, still named.

    Naming it is the point: "a site has no reading" is useless without saying which, since what
    the user needs to know is that the nearest one went quiet.
    """
    site_code = _optional_str(entry, "siteCode")
    site_name = _optional_str(entry, "siteName")
    label = site_name or site_code or "A nearby site"
    return SiteView(
        site_code=site_code,
        site_name=site_name,
        location_name=_optional_str(entry, "locationName"),
        has_reading=False,
        driving_pollutant=None,
        sub_index=None,
        band=None,
        confidence=None,
        text=(
            f"{label} ({site_code}) {NO_CURRENT_READING_TEXT}, so it cannot be used for "
            "current conditions."
            if site_code
            else f"{label} {NO_CURRENT_READING_TEXT}, so it cannot be used for "
            "current conditions."
        ),
    )


def _sub_index(entry: dict[str, Any]) -> int | None:
    """Read the entry's own overall index. Never derived from the measurements (Req 2.3)."""
    value = entry.get("overallAqi")
    return value if isinstance(value, int) else None


def snapshot_sites(body: object) -> tuple[SiteView, ...]:
    """Read every `nearestSensors` entry into a view, quiet ones included.

    Total by construction: the returned length always equals the number of entries served, which
    is the invariant Req 2.5 reduces to.
    """
    if not isinstance(body, dict):
        return ()
    raw = body.get("nearestSensors")
    if not isinstance(raw, list):
        return ()

    views: list[SiteView] = []
    for entry in raw:
        if not isinstance(entry, dict):
            views.append(_quiet_view({}))
            continue
        measurements = entry.get("measurements")
        if not isinstance(measurements, list) or not measurements:
            views.append(_quiet_view(entry))
            continue
        site_code = _optional_str(entry, "siteCode")
        site_name = _optional_str(entry, "siteName")
        band = _optional_str(entry, "band")
        driving = _optional_str(entry, "drivingPollutant")
        confidence = _optional_str(entry, "confidence")
        label = site_name or site_code or "A nearby site"
        views.append(
            SiteView(
                site_code=site_code,
                site_name=site_name,
                location_name=_optional_str(entry, "locationName"),
                has_reading=True,
                driving_pollutant=driving,
                sub_index=_sub_index(entry),
                band=band,
                confidence=confidence,
                text=(
                    f"{label} reports {band or 'a reading'} with "
                    f"{driving or 'no named'} driving it."
                ),
            )
        )
    return tuple(views)


__all__ = [
    "DEFAULT_PROFILE_TEXT",
    "NO_CURRENT_READING_TEXT",
    "SiteView",
    "snapshot_sites",
]
