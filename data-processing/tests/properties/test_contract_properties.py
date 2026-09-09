"""Contract property tests (tasks 2.4-2.7).

Feature: ingestion-and-serving-service
- Property 1: measurement record round-trip (Req 3.4, 1.1, 1.3, 1.5)
- Property 2: metadata record round-trip (Req 3.4, 2.1, 2.2, 2.3)
- Property 3: parser rejection is total and reasoned (Req 3.3, 3.7)
- Property 4: batch partial acceptance preserves accepted records (Req 3.5, 3.1)

Example counts come from the conftest profile (ci = 100), which is the floor
Requirement 28.11 sets — no test here caps it lower.
"""

from __future__ import annotations

import json
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.contract.parser import (
    RejectionKind,
    parse_data_records,
    parse_metadata_records,
)
from aqm_ingestion.contract.records import DATA_FIELD_ORDER, METADATA_FIELD_ORDER
from aqm_ingestion.contract.serializer import serialize_data, serialize_metadata
from tests.properties.strategies import (
    data_records,
    invalid_data_records,
    metadata_records,
)

_WHOLE_PAYLOAD_KINDS = frozenset(
    {
        RejectionKind.MALFORMED_JSON,
        RejectionKind.PAYLOAD_TOO_LARGE,
        RejectionKind.BATCH_TOO_LARGE,
    }
)


@given(fields=data_records())
def test_property_1_measurement_round_trip(fields: dict[str, Any]) -> None:
    """Feature: ingestion-and-serving-service, Property 1."""
    parsed = parse_data_records(json.dumps(fields))
    assert parsed.rejections == ()
    original = parsed.records[0]

    # render, parse again, and every field must be equal (Req 3.4)
    reparsed = parse_data_records(serialize_data(original))
    assert reparsed.rejections == ()
    returned = reparsed.records[0]

    for field in DATA_FIELD_ORDER:
        assert getattr(returned, field) == getattr(original, field)
    # ScaledValue in particular survives unrounded (Req 1.5)
    assert returned.ScaledValue == fields["ScaledValue"]


@given(fields=metadata_records())
def test_property_2_metadata_round_trip(fields: dict[str, Any]) -> None:
    """Feature: ingestion-and-serving-service, Property 2."""
    parsed = parse_metadata_records(json.dumps(fields))
    assert parsed.rejections == ()
    original = parsed.records[0]

    reparsed = parse_metadata_records(serialize_metadata(original))
    assert reparsed.rejections == ()
    returned = reparsed.records[0]

    for field in METADATA_FIELD_ORDER:
        assert getattr(returned, field) == getattr(original, field)
    # null-valued fields survive as null rather than being dropped (Req 3.4)
    dumped = returned.model_dump(mode="json")
    for field in METADATA_FIELD_ORDER:
        assert field in dumped
    # coordinates stay character-identical to the siblings (Req 2.3)
    assert returned.Location.geometry.coordinates == (
        returned.Latitude,
        returned.Longitude,
    )


@given(fields=invalid_data_records())
def test_property_3_rejection_is_total_and_reasoned(fields: dict[str, Any]) -> None:
    """Feature: ingestion-and-serving-service, Property 3."""
    result = parse_data_records(json.dumps(fields))

    # a broken record is always refused, never partially accepted (Req 3.7)
    assert result.records == ()
    assert len(result.rejections) == 1

    rejection = result.rejections[0]
    # and the refusal is always REASONED: a per-record condition plus a detail
    assert rejection.kind not in _WHOLE_PAYLOAD_KINDS
    assert rejection.detail.strip()
    assert rejection.kind in {
        RejectionKind.MISSING_FIELD,
        RejectionKind.UNKNOWN_FIELD,
        RejectionKind.WRONG_TYPE,
        RejectionKind.INVALID_ENUM,
    }


@given(
    valid=st.lists(data_records(), min_size=0, max_size=4),
    invalid=st.lists(invalid_data_records(), min_size=0, max_size=4),
    seed=st.integers(min_value=0, max_value=10_000),
)
def test_property_4_batch_partial_acceptance(
    valid: list[dict[str, Any]], invalid: list[dict[str, Any]], seed: int
) -> None:
    """Feature: ingestion-and-serving-service, Property 4."""
    # interleave deterministically from the drawn seed, so the ordering under test
    # is reproducible rather than dependent on hypothesis internals
    elements: list[tuple[bool, dict[str, Any]]] = [(True, r) for r in valid]
    elements += [(False, r) for r in invalid]
    if elements:
        rotation = seed % len(elements)
        elements = elements[rotation:] + elements[:rotation]

    result = parse_data_records(json.dumps([record for _, record in elements]))

    expected_valid = [record for is_valid, record in elements if is_valid]
    expected_invalid_positions = [
        position for position, (is_valid, _) in enumerate(elements) if not is_valid
    ]

    # nothing is lost: every element is either accepted or rejected exactly once
    assert len(result.records) + len(result.rejections) == len(elements)
    # accepted records are exactly the valid ones, IN ORDER (Req 3.5, 3.1)
    assert [r.SiteCode for r in result.records] == [
        record["SiteCode"] for record in expected_valid
    ]
    # and each rejection points at the position that actually failed
    assert [r.index for r in result.rejections] == expected_invalid_positions
