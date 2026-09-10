"""Unit tests for the Parser (task 2.3).

- Req 3.1: a single object or an array, order preserved, length 0..max batch
  (default 100,000).
- Req 3.2: malformed JSON or an oversize payload RAISES, naming the failure kind,
  and produces no record.
- Req 3.3: a missing required field, a field outside the contract, a wrong JSON
  value type, or an invalid enumeration value is rejected naming the offending
  field AND which of those four conditions it violated.
- Req 3.5: one failing element does not discard its siblings — the accepted
  records come back together with one rejection per failure, identified by ARRAY
  INDEX.
- Req 3.7: never a partially populated record.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from aqm_ingestion.contract.parser import (
    DEFAULT_MAX_BATCH,
    DEFAULT_MAX_PAYLOAD_BYTES,
    ParseError,
    RejectionKind,
    parse_data_records,
)
from tests.unit.test_records import GOLDEN_DATA_PAYLOAD


def _fields() -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD))


def _payload(*records: dict[str, Any]) -> str:
    return json.dumps(list(records))


# --- Req 3.1 single object, arrays, order ---------------------------------

def test_single_object_is_accepted() -> None:
    result = parse_data_records(GOLDEN_DATA_PAYLOAD)
    assert len(result.records) == 1
    assert result.rejections == ()


def test_empty_array_is_accepted() -> None:
    result = parse_data_records("[]")
    assert result.records == ()
    assert result.rejections == ()


def test_array_order_is_preserved() -> None:
    payload = _payload(
        {**_fields(), "SiteCode": "A"},
        {**_fields(), "SiteCode": "B"},
        {**_fields(), "SiteCode": "C"},
    )
    result = parse_data_records(payload)
    assert [r.SiteCode for r in result.records] == ["A", "B", "C"]


def test_batch_above_the_maximum_is_rejected() -> None:
    payload = _payload(*[_fields() for _ in range(3)])
    with pytest.raises(ParseError) as caught:
        parse_data_records(payload, max_batch=2)
    assert caught.value.rejection.kind is RejectionKind.BATCH_TOO_LARGE
    assert "2" in caught.value.rejection.detail


def test_default_maximums_match_the_documented_values() -> None:
    assert DEFAULT_MAX_BATCH == 100_000
    assert DEFAULT_MAX_PAYLOAD_BYTES == 8 * 1024 * 1024


# --- Req 3.2 whole-payload failures --------------------------------------

@pytest.mark.parametrize("text", ["{", "not json", "", "[{,}]", '{"a": }'])
def test_malformed_json_raises_naming_the_kind(text: str) -> None:
    with pytest.raises(ParseError) as caught:
        parse_data_records(text)
    assert caught.value.rejection.kind is RejectionKind.MALFORMED_JSON


def test_oversize_payload_raises_naming_the_kind() -> None:
    with pytest.raises(ParseError) as caught:
        parse_data_records(GOLDEN_DATA_PAYLOAD, max_payload_bytes=10)
    assert caught.value.rejection.kind is RejectionKind.PAYLOAD_TOO_LARGE
    assert "10" in caught.value.rejection.detail


def test_json_that_is_neither_object_nor_array_raises() -> None:
    with pytest.raises(ParseError) as caught:
        parse_data_records("42")
    assert caught.value.rejection.kind is RejectionKind.MALFORMED_JSON


# --- Req 3.3 the four per-record conditions ------------------------------

def test_missing_field_is_named_with_its_condition() -> None:
    fields = _fields()
    del fields["Units"]
    result = parse_data_records(json.dumps(fields))
    assert result.records == ()
    rejection = result.rejections[0]
    assert rejection.kind is RejectionKind.MISSING_FIELD
    assert rejection.field == "Units"


def test_unknown_field_is_named_with_its_condition() -> None:
    result = parse_data_records(json.dumps({**_fields(), "Bogus": 1}))
    rejection = result.rejections[0]
    assert rejection.kind is RejectionKind.UNKNOWN_FIELD
    assert rejection.field == "Bogus"


def test_wrong_value_type_is_named_with_its_condition() -> None:
    result = parse_data_records(json.dumps({**_fields(), "ScaledValue": "text"}))
    rejection = result.rejections[0]
    assert rejection.kind is RejectionKind.WRONG_TYPE
    assert rejection.field == "ScaledValue"


def test_invalid_species_is_named_with_its_condition() -> None:
    result = parse_data_records(json.dumps({**_fields(), "Species": "Ozone"}))
    rejection = result.rejections[0]
    assert rejection.kind is RejectionKind.INVALID_ENUM
    assert rejection.field == "Species"


def test_invalid_ratification_status_is_named() -> None:
    result = parse_data_records(json.dumps({**_fields(), "RatificationStatus": "X"}))
    rejection = result.rejections[0]
    assert rejection.kind is RejectionKind.INVALID_ENUM
    assert rejection.field == "RatificationStatus"


def test_rejection_detail_states_the_condition_in_words() -> None:
    result = parse_data_records(json.dumps({**_fields(), "Species": "Ozone"}))
    assert result.rejections[0].detail  # a diagnosable reason, not just a code


# --- Req 3.5 batch tolerance ---------------------------------------------

def test_a_failing_element_does_not_discard_its_siblings() -> None:
    payload = _payload(
        {**_fields(), "SiteCode": "good-1"},
        {**_fields(), "Species": "Ozone"},          # fails
        {**_fields(), "SiteCode": "good-2"},
    )
    result = parse_data_records(payload)
    assert [r.SiteCode for r in result.records] == ["good-1", "good-2"]
    assert len(result.rejections) == 1


def test_rejection_identifies_the_element_by_array_index() -> None:
    payload = _payload(
        _fields(),
        {**_fields(), "Species": "Ozone"},
        _fields(),
        {**_fields(), "RatificationStatus": "Q"},
    )
    result = parse_data_records(payload)
    assert [r.index for r in result.rejections] == [1, 3]


def test_every_failing_element_yields_exactly_one_rejection() -> None:
    payload = _payload(*[{**_fields(), "Species": "Ozone"} for _ in range(4)])
    result = parse_data_records(payload)
    assert result.records == ()
    assert len(result.rejections) == 4


def test_a_single_object_rejection_has_no_index() -> None:
    result = parse_data_records(json.dumps({**_fields(), "Species": "Ozone"}))
    assert result.rejections[0].index is None


def test_non_object_array_element_is_rejected_not_fatal() -> None:
    payload = json.dumps([_fields(), 42, _fields()])
    result = parse_data_records(payload)
    assert len(result.records) == 2
    assert result.rejections[0].index == 1
    assert result.rejections[0].kind is RejectionKind.WRONG_TYPE


# --- Req 3.7 no partial records ------------------------------------------

def test_a_rejected_record_produces_no_value() -> None:
    fields = _fields()
    del fields["DateTime"]
    result = parse_data_records(json.dumps(fields))
    assert result.records == ()  # never partially populated


def test_multiple_faults_in_one_record_still_reject_once() -> None:
    fields = _fields()
    del fields["Units"]
    fields["Species"] = "Ozone"
    result = parse_data_records(json.dumps(fields))
    assert result.records == ()
    assert len(result.rejections) == 1  # one rejection per failed RECORD
