"""Deduplication property tests (tasks 6.2, 6.3).

Feature: ingestion-and-serving-service
- Property 8: deduplication is order-independent (Requirements 7.7, 7.6)
- Property 9: deduplication resolution follows the stated precedence
  (Requirements 7.4, 7.5, 7.6)

Property 8 is checked two ways, because each catches what the other cannot:

1. Against a CLOSED FORM. The converged outcome should be fully determined by the
   highest ratification status present, the maximum value among records at that
   status, and whether two distinct values exist at that status — none of which
   depends on order. Comparing the fold against that formula catches a rule that is
   self-consistent but wrong.
2. Against a SHUFFLE of the same records. Folding a permutation must reach the same
   state, which catches order dependence even if the closed form were also wrong.
"""

from __future__ import annotations

import json
from typing import Any, cast

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.contract.records import SensorDataRecord
from aqm_ingestion.domain.dedup import DedupAction, resolve_duplicate
from aqm_ingestion.domain.models import QualityFlag
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

_MOMENT = "2026-07-01T11:00:00Z"


def _record(status: str, value: float, *, source: str = "Measurement") -> SensorDataRecord:
    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD))
    fields["DateTime"] = _MOMENT
    fields["RatificationStatus"] = status
    fields["ScaledValue"] = value
    fields["Source"] = source
    return SensorDataRecord(**fields)


def _converge(
    records: list[SensorDataRecord],
) -> tuple[SensorDataRecord | None, QualityFlag | None]:
    """Fold the records through resolution in the order given."""
    stored: SensorDataRecord | None = None
    flag: QualityFlag | None = None
    for record in records:
        resolution = resolve_duplicate(
            stored=stored, incoming=record, stored_flag=flag
        )
        if resolution.action in (DedupAction.STORE, DedupAction.REPLACE):
            stored = resolution.winner
        flag = resolution.quality_flag
    return stored, flag


# statuses and values kept to a small set so collisions, ties, and conflicts all
# occur often rather than being drowned out by a wide numeric range
_candidates = st.lists(
    st.tuples(st.sampled_from(["P", "R"]), st.sampled_from([1.0, 5.0, 25.0])),
    min_size=1,
    max_size=6,
)


@given(candidates=_candidates, seed=st.integers(min_value=0, max_value=10_000))
def test_property_8_deduplication_is_order_independent(
    candidates: list[tuple[str, float]], seed: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 8."""
    records = [_record(status, value) for status, value in candidates]

    stored, flag = _converge(records)
    assert stored is not None

    # (1) the closed form: the winning status tier, the max value in it, and whether
    # that tier holds two distinct values
    best_status = "R" if any(status == "R" for status, _ in candidates) else "P"
    tier_values = [value for status, value in candidates if status == best_status]
    assert stored.RatificationStatus == best_status
    assert stored.ScaledValue == max(tier_values)
    expected_conflict = len(set(tier_values)) > 1
    assert (flag is QualityFlag.SUSPECT_CONFLICT) == expected_conflict

    # (2) a permutation of the same records converges identically
    rotated = records[seed % len(records) :] + records[: seed % len(records)]
    reversed_order = list(reversed(records))
    for permutation in (rotated, reversed_order):
        other_stored, other_flag = _converge(permutation)
        assert other_stored is not None
        assert other_stored.RatificationStatus == stored.RatificationStatus
        assert other_stored.ScaledValue == stored.ScaledValue
        assert other_flag == flag


@given(candidates=_candidates)
def test_property_8_reingesting_changes_nothing(
    candidates: list[tuple[str, float]]
) -> None:
    """Feature: ingestion-and-serving-service, Property 8 (idempotence, Req 7.8)."""
    records = [_record(status, value) for status, value in candidates]
    once_stored, once_flag = _converge(records)
    twice_stored, twice_flag = _converge(records + records)
    assert once_stored == twice_stored
    assert once_flag == twice_flag


@given(
    stored_status=st.sampled_from(["P", "R"]),
    incoming_status=st.sampled_from(["P", "R"]),
    stored_value=st.floats(min_value=0.0, max_value=900.0, allow_nan=False),
    incoming_value=st.floats(min_value=0.0, max_value=900.0, allow_nan=False),
)
def test_property_9_resolution_follows_the_stated_precedence(
    stored_status: str,
    incoming_status: str,
    stored_value: float,
    incoming_value: float,
) -> None:
    """Feature: ingestion-and-serving-service, Property 9."""
    stored = _record(stored_status, stored_value)
    incoming = _record(incoming_status, incoming_value)
    resolution = resolve_duplicate(stored=stored, incoming=incoming)

    if stored_status == incoming_status:
        if stored_value == incoming_value:
            # identical records: Req 7.3 writes nothing and declares no conflict
            assert resolution.action is DedupAction.NO_WRITE
            assert resolution.quality_flag is not QualityFlag.SUSPECT_CONFLICT
        else:
            # Req 7.6: the GREATER value prevails, and the reading is disputed
            assert resolution.winner.ScaledValue == max(stored_value, incoming_value)
            assert resolution.quality_flag is QualityFlag.SUSPECT_CONFLICT
            assert resolution.warning is not None
    elif incoming_status == "R":
        # Req 7.4: status outranks magnitude — the ratified value wins even when
        # it is the smaller of the two
        assert resolution.action is DedupAction.REPLACE
        assert resolution.winner is incoming
        assert resolution.quality_flag is not QualityFlag.SUSPECT_CONFLICT
    else:
        # Req 7.5: a provisional value never displaces a ratified one
        assert resolution.action is DedupAction.NO_WRITE
        assert resolution.winner is stored
        assert resolution.warning is not None

    # whichever branch ran, resolution never invents a third value
    assert resolution.winner.ScaledValue in (stored_value, incoming_value)


@given(
    status=st.sampled_from(["P", "R"]),
    stored_value=st.floats(min_value=0.0, max_value=900.0, allow_nan=False),
    incoming_value=st.floats(min_value=0.0, max_value=900.0, allow_nan=False),
)
def test_property_9_a_conflict_is_never_resolved_downward(
    status: str, stored_value: float, incoming_value: float
) -> None:
    """Feature: ingestion-and-serving-service, Property 9 (conservative choice).

    Requirement 7.6 chooses the greater value because the more conservative of two
    irreconcilable readings is the safer one to serve in a health advisory. So the
    prevailing value is never below either candidate.
    """
    resolution = resolve_duplicate(
        stored=_record(status, stored_value), incoming=_record(status, incoming_value)
    )
    assert resolution.winner.ScaledValue >= min(stored_value, incoming_value)
    assert resolution.winner.ScaledValue == max(stored_value, incoming_value)
