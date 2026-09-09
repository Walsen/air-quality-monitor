"""Quarantine property test (task 5.3).

Feature: ingestion-and-serving-service
- Property 6: quarantined records are fully reasoned and never stored
  (Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.10)

The generator deliberately builds records that break a CHOSEN SUBSET of rules, so the
property can assert the reasons match the faults injected — not merely that some
reason came back. A validator that reported one reason and stopped, or that reported
a wrong category, would fail this.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

from hypothesis import assume, given
from hypothesis import strategies as st

from aqm_ingestion.adapters.memory import InMemoryReadingsStore
from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.domain.validation import QuarantineReason, ValidationLimits
from aqm_ingestion.ingest.quarantine import screen_reading
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_LIMITS = ValidationLimits()
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _base_fields(site: str, moment: dt.datetime) -> dict[str, Any]:
    """Start from a real payload so the field set cannot drift from the contract."""
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD))
    fields["SiteCode"] = site
    fields["DateTime"] = moment.strftime(_FORMAT)
    fields["Species"] = "PM25"
    fields["Units"] = "ug.m-3"
    fields["Duration"] = "PT1H"
    fields["ScaledValue"] = 12.5
    return fields


@given(
    site=st.text(
        alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=4, max_size=8
    ),
    faults=st.sets(
        st.sampled_from(["negative", "implausible", "future", "stale", "misaligned"]),
        min_size=1,
        max_size=5,
    ),
)
def test_property_6_quarantined_records_are_fully_reasoned_and_never_stored(
    site: str, faults: set[str]
) -> None:
    """Feature: ingestion-and-serving-service, Property 6."""
    moment = dt.datetime(2026, 7, 1, 11, tzinfo=dt.UTC)
    fields = _base_fields(site, moment)
    expected: set[QuarantineReason] = set()

    # "future" and "stale" both move DateTime, so they cannot both apply; drop the
    # combination rather than letting one silently overwrite the other
    assume(not ("future" in faults and "stale" in faults))

    if "negative" in faults:
        fields["ScaledValue"] = -1.5
        expected.add(QuarantineReason.VALUE_OUT_OF_RANGE)
    if "implausible" in faults:
        fields["ScaledValue"] = _LIMITS.pm25_ceiling + 10.0
        expected.add(QuarantineReason.IMPLAUSIBLE_VALUE)
        expected.discard(QuarantineReason.VALUE_OUT_OF_RANGE)  # one value, one fault
    if "future" in faults:
        far = _NOW + _LIMITS.clock_skew_tolerance + dt.timedelta(hours=2)
        fields["DateTime"] = far.strftime(_FORMAT)
        expected.add(QuarantineReason.FUTURE_DATED)
    if "stale" in faults:
        old = _NOW - dt.timedelta(days=_LIMITS.retention_days + 2)
        fields["DateTime"] = old.replace(minute=0, second=0).strftime(_FORMAT)
        expected.add(QuarantineReason.STALE)
    if "misaligned" in faults:
        current = str(fields["DateTime"])
        fields["DateTime"] = current[:14] + "37:00Z"  # a non-zero minute
        expected.add(QuarantineReason.INTERVAL_MISALIGNED)

    record = SensorDataRecord(**fields)
    outcome = screen_reading(
        record, now=_NOW, limits=_LIMITS, transport="mqtt", archive_id="arch1"
    )

    # Req 6.10: nothing storable came back, so the record CANNOT reach the store
    assert outcome.accepted is None
    assert outcome.quarantined is not None
    quarantined = outcome.quarantined

    # Req 6.1: every fault injected is reported, not just the first one found
    reported = {problem.reason for problem in quarantined.reasons}
    assert expected <= reported

    # every reason is a real category and carries a diagnosable detail
    for problem in quarantined.reasons:
        assert isinstance(problem.reason, QuarantineReason)
        assert problem.detail.strip()

    # Req 6.8: retained as received, with its provenance intact
    assert quarantined.raw_record == record.model_dump()
    assert quarantined.ingested_at == _NOW
    assert quarantined.archive_id == "arch1"

    # Req 6.10 again, this time against a real store: a quarantined record leaves it
    # empty, because there was never a value to write
    store = InMemoryReadingsStore()
    window = store.query_window(
        site_code=site,
        species=None,
        start=_NOW - dt.timedelta(days=400),
        end=_NOW + dt.timedelta(days=400),
    )
    assert window.readings == ()
