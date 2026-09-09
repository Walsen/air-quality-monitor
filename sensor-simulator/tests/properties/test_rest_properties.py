"""REST property tests (tasks 18.4, 18.5, 18.7).

Feature: sensor-simulator-service
- Property 7: list filtering matches supplied parameters (Req 1.8, 1.9, 1.10, 1.14)
- Property 8: SensorData query windowing and filtering (Req 2.10-2.13, 14.7, 14.11)
- Property 4: push and pull byte-identity (Req 3.6)
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json

import httpx
from hypothesis import given, settings
from hypothesis import strategies as st

from aqm_simulator.contract.serializer import serialize_data
from aqm_simulator.interfaces.mqtt import MqttPublisher, PublishedMessage
from aqm_simulator.interfaces.rest import build_app
from aqm_simulator.pipeline.determinism import build_pipeline

_KEY = "a-development-api-key-value"
_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_AUTH = {"X-API-KEY": _KEY}
_SPECIES = ("PM25", "NO2", "PM25Index", "NO2Index")


def _request(
    path: str, params: dict[str, str], seed: int, size: int
) -> httpx.Response:
    app = build_app(
        pipeline=build_pipeline(seed=seed, size=size), api_key=_KEY, now=lambda: _NOW
    )

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://simulator"
        ) as client:
            return await client.get(path, params=params, headers=_AUTH)

    return asyncio.run(call())


@given(
    seed=st.integers(min_value=0, max_value=2_000),
    size=st.integers(min_value=2, max_value=6),
    junk=st.sampled_from(["", "Nonsense", "OrderBy", "limit"]),
)
@settings(deadline=None)
def test_property_7_list_filtering(seed: int, size: int, junk: str) -> None:
    """Feature: sensor-simulator-service, Property 7."""
    everything = _request("/ListSensors", {}, seed, size).json()
    assert len(everything) == size
    codes = [r["SiteCode"] for r in everything]
    assert codes == sorted(codes)  # ascending SiteCode (Req 1.8)

    # an unrecognized parameter never changes the result (Req 1.14)
    if junk:
        with_junk = _request("/ListSensors", {junk: "x"}, seed, size).json()
        assert with_junk == everything

    # every supplied filter is honoured, whole-field and case-insensitively (1.9)
    target = everything[0]
    for name, field in (("SiteCode", "SiteCode"), ("Borough", "Borough"),
                        ("Sponsor", "SponsorName")):
        value = str(target[field])
        filtered = _request("/ListSensors", {name: value.swapcase()}, seed, size).json()
        assert filtered  # case-insensitive match still finds it
        assert all(str(r[field]).casefold() == value.casefold() for r in filtered)
        assert target["SiteCode"] in {r["SiteCode"] for r in filtered}


@given(
    seed=st.integers(min_value=0, max_value=2_000),
    start_h=st.integers(min_value=0, max_value=9),
    span=st.integers(min_value=0, max_value=6),
    species=st.sampled_from(_SPECIES),
)
@settings(deadline=None)
def test_property_8_sensordata_windowing(
    seed: int, start_h: int, span: int, species: str
) -> None:
    """Feature: sensor-simulator-service, Property 8."""
    start = _NOW - dt.timedelta(hours=start_h + span)
    end = start + dt.timedelta(hours=span)
    params = {
        "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Species": species,
    }
    body = _request("/SensorData", params, seed, 3).json()

    times = [r["DateTime"] for r in body]
    assert times == sorted(times)  # ascending DateTime (Req 2.11)
    for record in body:
        assert record["Species"] == species  # case-sensitive filter (Req 2.12)
        # half-open window, and never beyond simulated now (Req 2.11, 14.7)
        assert params["startTime"] <= record["DateTime"] < params["endTime"]
        assert record["DateTime"] < "2026-07-01T12:00:00Z"
    if span == 0:
        assert body == []  # startTime == endTime gives an empty array


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[PublishedMessage] = []

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def publish(self, topic: str, payload: str) -> None:
        self.messages.append(PublishedMessage(topic=topic, payload=payload))


@given(
    seed=st.integers(min_value=0, max_value=2_000),
    size=st.integers(min_value=1, max_value=4),
)
@settings(deadline=None)
def test_property_4_push_pull_byte_identity(seed: int, size: int) -> None:
    """Feature: sensor-simulator-service, Property 4."""
    # the SAME interval delivered by push and by pull must agree field for field
    interval_start = _NOW - dt.timedelta(hours=1)
    pipeline = build_pipeline(seed=seed, size=size)
    pushed_records = pipeline.run_interval(interval_start, reference_time=_NOW)

    transport = _Recorder()
    asyncio.run(MqttPublisher(transport).publish_all(pushed_records))

    def _identity(record: dict[str, object]) -> tuple[str, str, str]:
        return (str(record["SiteCode"]), str(record["Species"]), str(record["DateTime"]))

    pushed = sorted(
        (json.loads(m.payload) for m in transport.messages), key=_identity
    )
    pulled = sorted(_request("/SensorData", {}, seed, size).json(), key=_identity)

    assert pushed == pulled  # identical records via both interfaces
    # and the push payload is exactly the canonical serializer output
    for message, record in zip(transport.messages, pushed_records, strict=True):
        assert message.payload == serialize_data(record)
