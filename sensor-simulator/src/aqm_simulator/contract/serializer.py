"""The Serializer.

Renders the canonical record models as JSON text in declared field order with
the JSON value types the contract requires (Requirement 3.1). ``ScaledValue`` is
emitted as a JSON *number* rounded to exactly two decimal places using
half-away-from-zero rounding (Requirement 2.7) — never banker's rounding, so
12.345 → 12.35 and 2.675 → 2.68.

The Serializer only renders; reading JSON back into records is the Parser's job
(engineering-practices §1, single responsibility). Push mode and pull mode both
render through here, so the same record produces byte-identical text
(Requirement 3.6).
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_UP, Decimal

from aqm_simulator.contract.records import SensorDataRecord, SensorMetadataRecord

_CENTS = Decimal("0.01")


def round_half_away_from_zero(value: float) -> float:
    """Round ``value`` to 2 decimal places, halves going away from zero."""
    quantized = Decimal(str(value)).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return float(quantized)


def serialize_data(record: SensorDataRecord) -> str:
    """Render a SensorDataRecord as compact JSON text in declared order."""
    payload = record.model_dump(mode="json")
    payload["ScaledValue"] = round_half_away_from_zero(record.ScaledValue)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def serialize_metadata(record: SensorMetadataRecord) -> str:
    """Render a SensorMetadataRecord as compact JSON text in declared order."""
    payload = record.model_dump(mode="json")
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
