"""End-to-end determinism from the composition root (task 29.2).

Requirements 27.2, 27.3, 27.5, 27.6.

WHY THIS COULD NOT BE WRITTEN BEFORE TASK 29.1. Requirement 27.2 is about the SERVICE, not a
component: two independent instances built from the same configuration and the same Clock
instant
must produce a byte-identical Serving_Response. Until the composition root existed there was no
way
to build the service twice — every earlier determinism test could only construct a stage and
compare
it with itself, which cannot catch shared mutable state, an ambient clock read, or an identifier
derived from something other than the inputs.

TWO INSTANCES, NOT ONE CALLED TWICE. Calling one instance twice would pass even if it cached the
first answer, and would share every object inside it. Building two from the same configuration
is
what makes the assertion meaningful: they have separate stores, separate caches, and separate
adapters, so agreement can only come from the inputs.

BODIES ARE COMPARED AS BYTES. Two dicts differing only in member ORDER compare EQUAL, and
Requirement 27.2 says byte-identical — which is also what a client actually receives. The same
reasoning Property 38 recorded at task 25.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.composition import Runtime, build_runtime
from aqm_ingestion.config.loader import ServiceConfig, resolve_and_validate
from aqm_ingestion.ports.clock import FixedClock

_T0 = dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC)
_USER = "user-determinism"
_TOKEN = "dev-token-not-a-real-credential"


def _config() -> ServiceConfig:
    """One configuration, resolved once and reused, so both instances get identical settings."""
    return resolve_and_validate(
        {
            "AQM_ENABLE_SERVING": "true",
            "AQM_ENABLE_PUSH": "false",
            "AQM_ENABLE_PULL": "false",
        },
        {},
        credential_exists=lambda _path: True,
    )


def _instance(config: ServiceConfig, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    """Build one whole service instance at the fixed instant."""
    monkeypatch.setenv("AQM_LOCAL_CREDENTIALS", f"{_TOKEN}={_USER}")
    return build_runtime(config, clock=FixedClock(_T0))


def _ingest_identical_payload(runtime: Runtime) -> object:
    """Feed one instance the same bytes, through the same pipeline.

    BUILT FROM THE GOLDEN PAYLOAD, not hand-written. My first version invented three field names
    (``Provisional_Or_Ratified``, ``InstrumentType``, ``Value``) where the contract says
    ``Source``,
    ``RatificationStatus`` and ``SensorContract``; the record was rejected and nothing was
    stored.
    The vacuity guard in the comparison below is what reported it rather than the test quietly
    comparing two empty results — and reusing the golden payload is the standing lesson for
    exactly this.
    """
    from aqm_ingestion.contract.records import SensorDataRecord
    from aqm_ingestion.contract.serializer import serialize_data
    from aqm_ingestion.ports.protocols import ArchiveMeta
    from tests.unit.test_records import GOLDEN_DATA_PAYLOAD

    fields = cast("dict[str, Any]", json.loads(GOLDEN_DATA_PAYLOAD))
    fields["SiteCode"] = "DET1"
    fields["DateTime"] = "2026-03-01T11:00:00Z"
    payload = serialize_data(SensorDataRecord(**fields)).encode("utf-8")
    meta = ArchiveMeta(ingested_at=_T0, transport="mqtt", source="determinism")
    return cast("Any", runtime.pipeline).ingest(payload, meta)


def _serving_body(runtime: Runtime) -> bytes:
    """The raw response bytes for the same authenticated request."""
    import asyncio

    import httpx

    async def request() -> bytes:
        transport = httpx.ASGITransport(app=cast("Any", runtime.app))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/v1/air-quality/me", headers={"Authorization": f"Bearer {_TOKEN}"}
            )
            return response.content

    return asyncio.run(request())


def test_two_instances_produce_a_byte_identical_serving_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 27.2, asserted across two independently built services."""
    config = _config()

    first = _instance(config, monkeypatch)
    _ingest_identical_payload(first)
    first_body = _serving_body(first)

    second = _instance(config, monkeypatch)
    _ingest_identical_payload(second)
    second_body = _serving_body(second)

    assert first_body == second_body


def test_the_two_instances_are_genuinely_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard without which the test above proves nothing.

    If ``build_runtime`` returned a shared store — a module-level singleton, a default argument
    evaluated once — the comparison would pass for the wrong reason. Ingesting into ONE instance
    must leave the other empty.
    """
    config = _config()
    first = _instance(config, monkeypatch)
    second = _instance(config, monkeypatch)

    assert first.pipeline is not second.pipeline
    _ingest_identical_payload(first)

    window = cast("Any", second.ports["readings_store"]).query_window(
        "DET1", None, _T0 - dt.timedelta(days=1), _T0 + dt.timedelta(days=1)
    )
    assert window.readings == (), "the second instance saw the first instance's data"


def test_two_instances_store_identical_readings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 27.3: identical Calibrated_Reading values from identical inputs.

    Compared field by field over the whole reading — corrected value, Quality_Flag, Confidence,
    Sub_Index, Band — because 27.3 enumerates those and a dataclass comparison covers each
    without
    a list here going stale as fields are added.
    """
    config = _config()

    first = _instance(config, monkeypatch)
    _ingest_identical_payload(first)
    second = _instance(config, monkeypatch)
    _ingest_identical_payload(second)

    span = (_T0 - dt.timedelta(days=1), _T0 + dt.timedelta(days=1))
    first_store = cast("Any", first.ports["readings_store"])
    second_store = cast("Any", second.ports["readings_store"])
    first_readings = first_store.query_window("DET1", None, *span)
    second_readings = second_store.query_window("DET1", None, *span)

    assert first_readings.readings == second_readings.readings
    assert first_readings.readings, "nothing was stored, so this comparison would be vacuous"


def test_the_archive_identifier_is_reproducible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 27.6: a generated identifier reaching stored data is DERIVED, not ambient.

    A uuid would satisfy every other assertion in this file — the readings would match, the body
    would match — and quietly break replay. So the identifier is compared directly across the
    two
    instances.
    """
    config = _config()

    first_summary = _ingest_identical_payload(_instance(config, monkeypatch))
    second_summary = _ingest_identical_payload(_instance(config, monkeypatch))

    assert cast("Any", first_summary).archive_id == cast("Any", second_summary).archive_id


def test_no_randomness_reaches_a_stored_reading_or_a_response() -> None:
    """Requirement 27.5, asserted over the source rather than observed.

    A random value that happened to agree twice would pass the comparisons above, so this walks
    the
    AST of every domain and serving module for an import of ``random``. Transport scheduling is
    permitted to jitter, which is why ``adapters/`` is not scanned — 27.5 confines randomness to
    exactly there.
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "aqm_ingestion"
    offenders: list[str] = []
    scanned = 0
    for area in ("domain", "serving", "ingest"):
        for path in (root / area).rglob("*.py"):
            scanned += 1
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name.split(".")[0] in ("random", "secrets") for name in names):
                    offenders.append(str(path.relative_to(root)))

    assert scanned, "no modules were scanned, so this check would be vacuous"
    assert not offenders, f"randomness reaches a computed value in: {sorted(set(offenders))}"
