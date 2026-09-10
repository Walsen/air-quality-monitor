"""Parsing and validating the history window parameters.

Requirements 19.6, 19.7, 19.9.

EVERY FAULT ACCUMULATES rather than the first winning. Requirement 19.6 says the body names
"each offending parameter", and §5 says the same of a documented failure: a caller fixing one
parameter per round trip is exactly what accumulation avoids. Validation lives here rather than
in the handler so the rule is testable without an HTTP client (§1).

The unknown-site case is kept SEPARATE from the parameter problems because it is a different
status: Requirement 19.7 makes it a 404 while 19.6 makes a bad parameter a 400, and folding them
together would force the handler to guess which it was looking at.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from aqm_ingestion.ports.protocols import SensorRegistryStore

DEFAULT_MAX_HISTORY_SPAN_DAYS = 30
"""Requirement 19.9's default maximum history span."""

PERMITTED_HISTORY_SPECIES: tuple[str, ...] = ("PM25", "NO2")
"""Species a history request may name (Requirement 19.6).

Mirrors the contract's mass-concentration species. A drift guard in the tests ties this to the
Breakpoint_Table registry, the same protection the Personal_Threshold species list carries.
"""

_INSTANT_FORM = "an ISO-8601 UTC instant at whole-second precision ending in Z"


@dataclass(frozen=True, slots=True)
class HistoryWindow:
    """A parsed window, or the reasons it could not be parsed."""

    start: dt.datetime
    end: dt.datetime
    problems: tuple[str, ...] = ()
    unknown_site: bool = False
    bounds_supplied: bool = False
    """Whether the caller gave both bounds.

    Requirement 19.6 names "one of them without the other" as a fault, which implies NEITHER is
    not one — so a request with no bounds gets the default window rather than a rejection. The
    default is applied by the HANDLER, not here, because it is measured from the Clock and this
    module stays pure (§2).
    """


@dataclass
class _Problems:
    """Accumulator, so every fault is reported in one response."""

    messages: list[str] = field(default_factory=list)

    def add(self, parameter: str, expected: str) -> None:
        """Record one offending parameter and its permitted form."""
        self.messages.append(f"{parameter}: expected {expected}")


def parse_instant(raw: str) -> dt.datetime | None:
    """Parse an ISO-8601 UTC instant, or None when it is unparseable.

    Accepts the ``Z`` suffix the contract emits (Requirement 19.13) and normalises an
    offset-aware instant to UTC. A NAIVE instant is REFUSED rather than assumed UTC: it compares
    as local time and would silently shift the window, the same rule the Clock port applies.
    """
    candidate = raw.strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.UTC)


def parse_history_window(
    site_code: str,
    start: str | None,
    end: str | None,
    species: str | None,
    registry: SensorRegistryStore,
    max_span_days: int = DEFAULT_MAX_HISTORY_SPAN_DAYS,
) -> HistoryWindow:
    """Validate the history parameters, accumulating faults (Req 19.6, 19.7, 19.9).

    ``start`` and ``end`` are OPTIONAL at this boundary on purpose. Requirement 19.6 lists "one
    of them without the other" among the faults that must answer 400 NAMING the offending
    parameter — but a REQUIRED FastAPI query parameter is missing before any handler runs, so
    the
    framework answers 422 with its own body and 19.6's own case could never be met. Accepting
    them as optional and validating the PAIRING here is what makes that case reachable.

    Returns a window whose ``problems`` is empty only when every parameter is acceptable. The
    returned ``start``/``end`` are meaningless when problems exist and the caller must not use
    them — which is why the handler checks ``problems`` before reading either.
    """
    problems = _Problems()
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)

    # Requirement 19.6's pairing rule, named as the ABSENT parameter since that is the one the
    # caller has to add.
    if start is None and end is not None:
        problems.add("startTime", "an instant, because endTime was supplied")
    if end is None and start is not None:
        problems.add("endTime", "an instant, because startTime was supplied")

    parsed_start = parse_instant(start) if start is not None else None
    parsed_end = parse_instant(end) if end is not None else None
    if start is not None and parsed_start is None:
        problems.add("startTime", _INSTANT_FORM)
    if end is not None and parsed_end is None:
        problems.add("endTime", _INSTANT_FORM)

    if species is not None and species not in PERMITTED_HISTORY_SPECIES:
        problems.add(
            "species", f"one of {', '.join(PERMITTED_HISTORY_SPECIES)}"
        )

    if parsed_start is not None and parsed_end is not None:
        if parsed_start > parsed_end:
            problems.add("startTime", "an instant no later than endTime")
        else:
            span = parsed_end - parsed_start
            limit = dt.timedelta(days=max_span_days)
            if span > limit:
                # Requirement 19.9 names the requested span AND the maximum, so an operator can
                # see by how much the request overshot rather than only that it did.
                problems.add(
                    "endTime",
                    f"a window no longer than {max_span_days} days "
                    f"(requested {span.days} days)",
                )

    # Requirement 19.7's unknown site is a 404, not one of the 400 problems above — checked only
    # when the parameters are otherwise sound, so a request with a bad instant AND an unknown
    # site
    # gets the 400 that describes what the caller can fix first.
    unknown_site = False
    if not problems.messages:
        unknown_site = registry.get(site_code) is None

    return HistoryWindow(
        start=parsed_start or epoch,
        end=parsed_end or epoch,
        problems=tuple(problems.messages),
        unknown_site=unknown_site,
        bounds_supplied=parsed_start is not None and parsed_end is not None,
    )


__all__ = [
    "DEFAULT_MAX_HISTORY_SPAN_DAYS",
    "PERMITTED_HISTORY_SPECIES",
    "HistoryWindow",
    "parse_history_window",
    "parse_instant",
]
