"""The wire form for an instant.

Service 2 has a function of the same name. This is a deliberate re-implementation rather than an
import:
the engineering practices forbid importing across service directories until a shared contract
package is
specced, and each service keeps its own copy covered by its own round-trip test. The NAME is
identical
because the form it produces must be — a whole-second UTC instant with a `Z` suffix.

Both functions here are pure functions of their argument. Nothing in this module can reach for
the current
time, which the practices' determinism rule requires of domain code and a test enforces: a
record
identifier that depended on when it was rendered would not be an identifier.
"""

from __future__ import annotations

import datetime as dt

_WIRE_FORM = "%Y-%m-%dT%H:%M:%SZ"


def iso_z(instant: dt.datetime) -> str:
    """Render an aware instant as a whole-second UTC string ending in `Z`.

    Truncates rather than rounds any sub-second component. Truncation is the safer direction for
    a
    reading's identity: rounding could carry an instant into the next second, and a reading's
    identifier
    would then disagree with the instant Service 2 recorded.

    Raises:
        ValueError: if `instant` is naive. A naive datetime names a wall-clock reading rather
        than an
            instant, and assuming UTC would silently shift the value by the real offset.
    """
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise ValueError("an instant must carry a timezone; a naive datetime is ambiguous")
    return instant.astimezone(dt.UTC).replace(microsecond=0).strftime(_WIRE_FORM)


def parse_iso_z(text: str) -> dt.datetime:
    """Read the form `iso_z` produces back into an aware UTC instant.

    Deliberately strict: it accepts only the exact wire form, not the wider set `fromisoformat`
    allows.
    A lenient parser here would let a neighbouring service's differently-shaped timestamp
    through and
    move the disagreement downstream, where it is harder to attribute.

    Raises:
        ValueError: if `text` is not exactly the wire form.
    """
    try:
        parsed = dt.datetime.strptime(text, _WIRE_FORM)
    except ValueError as exc:
        raise ValueError(f"not a wire-form instant: expected {_WIRE_FORM!r}") from exc
    return parsed.replace(tzinfo=dt.UTC)


__all__ = ["iso_z", "parse_iso_z"]
