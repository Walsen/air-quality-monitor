"""Tests for the real transport adapters (task 27.4).

Requirements 4.1, 5.1, 24.9, 24.10, 8.4, 5.2.

WHY THESE RUN OFFLINE AT ALL. The three HTTP adapters take an INJECTED httpx transport, so the
adapter's real request-building and response-parsing code runs against a canned transport with
no
socket. That is Dependency Inversion (§1) doing exactly the job the practices claim for it: the
boundary that gets swapped (a live feed for a canned one) is the boundary that makes the thing
testable. Only the MQTT adapter needs a broker, and its tests are marked ``integration``.

THE CREDENTIAL TESTS ARE THE POINT OF THIS MODULE. Requirements 5.2 and 24.10 both say a
credential
is resolved only at runtime and never written to a log, a body, or the archive. That is not
something an adapter can be inspected for once and trusted about, so the assertions here capture
ALL output and search it, rather than checking one field of one event.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import httpx
import pytest

from aqm_ingestion.observability.logging import configure_logging, get_logger
from aqm_ingestion.ports.protocols import (
    FeedClient,
    ForecastClient,
    MeteorologyProvider,
    PollenCategory,
)

_SECRET = "sk-live-do-not-log-me-4a7f"
_T0 = dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC)


def _events(captured: str) -> list[dict[str, Any]]:
    """Parse the JSON log lines the configured logger writes to stdout.

    THE STRUCTURED CONTEXT IS ONLY VISIBLE HERE. ``caplog.text`` renders the message alone, so a
    credential passed as a context kwarg would NOT appear in it — an assertion over
    ``caplog.text``
    would have passed vacuously while the secret sat in the emitted JSON. This module's first
    draft
    made exactly that mistake; the tests below read the real output instead, the same surface
    the
    task-16.2 feed tests settled on.
    """
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


def _json_transport(
    body: object, status: int = 200, capture: list[httpx.Request] | None = None
) -> httpx.MockTransport:
    """A transport answering every request with one canned JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        if capture is not None:
            capture.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def _failing_transport(error: Exception) -> httpx.MockTransport:
    """A transport that always fails, for the Req 24.4 / 5.6 degradation paths."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------------------
# The HTTP feed adapter (Requirements 5.1, 5.2)
# --------------------------------------------------------------------------------------


def test_the_feed_adapter_satisfies_the_feed_client_port() -> None:
    from aqm_ingestion.adapters.http_feed import HttpFeedClient

    client = HttpFeedClient(
        base_url="https://feed.example/api",
        credential=_SECRET,
        transport=_json_transport([]),
    )
    assert isinstance(client, FeedClient)


def test_the_feed_adapter_requests_sensordata_with_the_species_set() -> None:
    # Req 5.1: /SensorData for the CONFIGURED species set and the derived window.
    from aqm_ingestion.adapters.http_feed import HttpFeedClient

    seen: list[httpx.Request] = []
    client = HttpFeedClient(
        base_url="https://feed.example/api",
        credential=_SECRET,
        transport=_json_transport([], capture=seen),
    )
    client.fetch_data(_T0, _T0 + dt.timedelta(hours=1), frozenset({"PM25", "NO2"}))

    assert len(seen) == 1
    assert seen[0].url.path.endswith("/SensorData")
    species = seen[0].url.params["species"]
    # Sorted, so the request is reproducible rather than depending on set iteration (§2).
    assert species == "NO2,PM25"


def test_the_feed_adapter_sends_the_credential_in_the_api_key_header() -> None:
    # Req 5.2 names the header exactly.
    from aqm_ingestion.adapters.http_feed import HttpFeedClient

    seen: list[httpx.Request] = []
    client = HttpFeedClient(
        base_url="https://feed.example/api",
        credential=_SECRET,
        transport=_json_transport([], capture=seen),
    )
    client.fetch_sensors()

    assert seen[0].headers["X-API-KEY"] == _SECRET


def test_the_feed_adapter_requests_listsensors_for_the_sensor_call() -> None:
    # Req 5.7's second endpoint, which the port gained in task 16.2.
    from aqm_ingestion.adapters.http_feed import HttpFeedClient

    seen: list[httpx.Request] = []
    client = HttpFeedClient(
        base_url="https://feed.example/api",
        credential=_SECRET,
        transport=_json_transport([], capture=seen),
    )
    client.fetch_sensors()

    assert seen[0].url.path.endswith("/ListSensors")


def test_the_feed_adapter_returns_the_body_bytes_verbatim() -> None:
    # The port returns BYTES because Req 16.1 archives before parsing: an adapter that parsed
    # would deprive the archive of the bytes that actually arrived.
    from aqm_ingestion.adapters.http_feed import HttpFeedClient

    body = [{"SiteCode": "AQM1", "Species": "PM25"}]
    client = HttpFeedClient(
        base_url="https://feed.example/api",
        credential=_SECRET,
        transport=_json_transport(body),
    )
    returned = client.fetch_data(_T0, _T0 + dt.timedelta(hours=1), frozenset({"PM25"}))

    assert json.loads(returned) == body


def test_a_feed_error_status_raises_rather_than_returning_an_empty_body() -> None:
    # Req 5.6 lists a transport error beside an unparseable body precisely so a failure cannot
    # be
    # mistaken for "the feed had nothing", which would advance the high-water mark past real
    # data.
    from aqm_ingestion.adapters.http_feed import FeedRequestError, HttpFeedClient

    client = HttpFeedClient(
        base_url="https://feed.example/api",
        credential=_SECRET,
        transport=_json_transport({"message": "nope"}, status=503),
    )
    with pytest.raises(FeedRequestError):
        client.fetch_sensors()


def test_a_feed_failure_never_names_the_credential(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 5.2: not in any log entry. Asserted over the WHOLE emitted JSON at DEBUG, so a
    # credential in a structured context field fails this too — which caplog.text would miss.
    from aqm_ingestion.adapters.http_feed import FeedRequestError, HttpFeedClient

    configure_logging("debug")
    client = HttpFeedClient(
        base_url="https://feed.example/api",
        credential=_SECRET,
        transport=_failing_transport(httpx.ConnectError("refused")),
    )
    with pytest.raises(FeedRequestError) as raised:
        client.fetch_sensors()

    captured = capsys.readouterr().out
    assert _SECRET not in captured
    assert _SECRET not in str(raised.value)
    # The guard against passing for the wrong reason: an EMPTY log also contains no secret, so
    # assert the failure really was reported (§6 — a handled error must still be logged).
    assert any(e.get("event") == "feed_request_failed" for e in _events(captured))


def test_the_feed_adapter_does_not_expose_its_credential_as_an_attribute() -> None:
    # §7 structurally: a credential reachable through the object is a credential a future
    # repr, log call, or debug dump can reach. It is held in the request headers alone.
    from aqm_ingestion.adapters.http_feed import HttpFeedClient

    client = HttpFeedClient(
        base_url="https://feed.example/api",
        credential=_SECRET,
        transport=_json_transport([]),
    )
    rendered = f"{client!r} {client!s} {vars(client)}"
    assert _SECRET not in rendered


# --------------------------------------------------------------------------------------
# The HTTP forecast adapter (Requirements 24.3, 24.4, 24.9, 24.10)
# --------------------------------------------------------------------------------------


def test_the_forecast_adapter_satisfies_the_forecast_client_port() -> None:
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    client = HttpForecastClient(
        base_url="https://forecast.example",
        credential=_SECRET,
        transport=_json_transport({"values": {}, "pollen": {}}),
    )
    assert isinstance(client, ForecastClient)


def test_the_forecast_adapter_reports_its_provider() -> None:
    # Req 24.3: the provider identifier travels with EVERY forecast.
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    client = HttpForecastClient(
        base_url="https://forecast.example",
        credential=_SECRET,
        provider="example-met",
        transport=_json_transport({"values": {"PM25": 42.0}}),
    )
    result = client.forecast(51.507, -0.128)

    assert result.provider == "example-met"
    assert result.values["PM25"] == 42.0
    assert result.degraded is False


def test_a_forecast_failure_degrades_rather_than_raising() -> None:
    # Req 24.4: serve WITHOUT forecast values, degraded true, and do NOT fail the request. The
    # adapter must not raise, because the enricher above it has no way to answer a raise without
    # failing a request the requirement says must succeed.
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    client = HttpForecastClient(
        base_url="https://forecast.example",
        credential=_SECRET,
        transport=_failing_transport(httpx.ConnectError("refused")),
    )
    result = client.forecast(51.507, -0.128)

    assert result.degraded is True
    assert result.values == {}


def test_an_unusable_forecast_body_degrades() -> None:
    # Req 24.4 names an unusable body beside a failure and a timeout: a 200 carrying the wrong
    # shape is exactly as unusable as a refused connection, and is the case a naive adapter
    # turns into a 500 by indexing into it.
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    client = HttpForecastClient(
        base_url="https://forecast.example",
        credential=_SECRET,
        transport=_json_transport(["not", "an", "object"]),
    )
    result = client.forecast(51.507, -0.128)

    assert result.degraded is True


def test_a_forecast_failure_logs_one_warning_naming_the_failure_kind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 24.4: exactly one warning, naming the KIND — and §6 requires the handled error be
    # logged rather than swallowed, since degradation is otherwise invisible.
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    configure_logging("info")
    client = HttpForecastClient(
        base_url="https://forecast.example",
        credential=_SECRET,
        transport=_failing_transport(httpx.ConnectError("refused")),
    )
    client.forecast(51.507, -0.128)

    events = _events(capsys.readouterr().out)
    warnings = [e for e in events if e["level"] == "warning"]
    assert len(warnings) == 1
    # The KIND is a structured field, not part of the message — the distinction this module's
    # first draft got wrong.
    assert warnings[0]["failure_kind"] == "ConnectError"


def test_the_forecast_adapter_never_logs_its_credential(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 24.10, the forecast counterpart of the feed rule. Over the emitted JSON at DEBUG.
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    configure_logging("debug")
    client = HttpForecastClient(
        base_url="https://forecast.example",
        credential=_SECRET,
        transport=_failing_transport(httpx.ConnectError("refused")),
    )
    client.forecast(51.507, -0.128)
    client.pollen(51.507, -0.128)

    captured = capsys.readouterr().out
    assert _SECRET not in captured
    # Two failures were driven, so two events must be present: without this the assertion above
    # would also pass against an empty log.
    assert len(_events(captured)) == 2


def test_the_forecast_adapter_reads_pollen_categories_from_the_provider() -> None:
    # Req 24.7 + the task-22 finding: the spec defines pollen thresholds NOWHERE, so the
    # provider supplies the CATEGORY and the adapter must not derive one from a count.
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    client = HttpForecastClient(
        base_url="https://forecast.example",
        credential=_SECRET,
        transport=_json_transport({"pollen": {"grass": "high", "tree": "low"}}),
    )
    result = client.pollen(51.507, -0.128)

    assert result.values["grass"] is PollenCategory.HIGH
    assert result.values["tree"] is PollenCategory.LOW


def test_an_unrecognized_pollen_category_degrades_rather_than_inventing_one() -> None:
    # A category outside Req 24.7's closed set cannot be mapped, and guessing the nearest one
    # would report a clinical judgement the provider never made.
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    client = HttpForecastClient(
        base_url="https://forecast.example",
        credential=_SECRET,
        transport=_json_transport({"pollen": {"grass": "catastrophic"}}),
    )
    result = client.pollen(51.507, -0.128)

    assert result.degraded is True


def test_the_forecast_adapter_sends_only_the_coordinates() -> None:
    # Req 24.8 structurally: the port takes lat/lon only, so this asserts the REQUEST carries
    # nothing else that could have come from a profile.
    from aqm_ingestion.adapters.forecast import HttpForecastClient

    seen: list[httpx.Request] = []
    client = HttpForecastClient(
        base_url="https://forecast.example",
        credential=_SECRET,
        transport=_json_transport({"values": {}}, capture=seen),
    )
    client.forecast(51.507, -0.128)

    assert set(seen[0].url.params.keys()) == {"lat", "lon"}


def test_the_forecast_timeout_defaults_to_two_seconds() -> None:
    # Req 24.4 names the default; pinned so a later edit cannot quietly lengthen it and make a
    # serving request wait past what the requirement allows.
    from aqm_ingestion.adapters.forecast import DEFAULT_FORECAST_TIMEOUT_SECONDS

    assert DEFAULT_FORECAST_TIMEOUT_SECONDS == 2.0


# --------------------------------------------------------------------------------------
# The HTTP meteorology adapter (Requirement 8.4)
# --------------------------------------------------------------------------------------


def test_the_meteorology_adapter_satisfies_the_provider_port() -> None:
    from aqm_ingestion.adapters.forecast import HttpMeteorologyProvider

    provider = HttpMeteorologyProvider(
        base_url="https://met.example",
        credential=_SECRET,
        transport=_json_transport({}),
    )
    assert isinstance(provider, MeteorologyProvider)


def test_the_meteorology_adapter_returns_an_observation() -> None:
    from aqm_ingestion.adapters.forecast import HttpMeteorologyProvider

    provider = HttpMeteorologyProvider(
        base_url="https://met.example",
        credential=_SECRET,
        transport=_json_transport(
            {"temperature_k": 288.15, "pressure_pa": 74000.0, "relative_humidity_pct": 55.0}
        ),
    )
    observation = provider.observation("AQM1", _T0)

    assert observation is not None
    assert observation.relative_humidity_pct == 55.0


def test_a_missing_meteorology_field_stays_none_rather_than_defaulting() -> None:
    # Req 8.4's third source is "no RH AT ALL", and the task-7 finding is that a substituted
    # value fabricates a correction and reports it as calibrated. Absent must stay absent.
    from aqm_ingestion.adapters.forecast import HttpMeteorologyProvider

    provider = HttpMeteorologyProvider(
        base_url="https://met.example",
        credential=_SECRET,
        transport=_json_transport({"temperature_k": 288.15, "pressure_pa": 74000.0}),
    )
    observation = provider.observation("AQM1", _T0)

    assert observation is not None
    assert observation.relative_humidity_pct is None


def test_a_meteorology_failure_returns_none_so_the_precedence_falls_through() -> None:
    # Req 8.4's precedence ENDS in "no RH at all", so an unreachable provider must return None
    # rather than raise: a raise would fail an ingestion the requirement says continues
    # uncalibrated (Req 8.6).
    from aqm_ingestion.adapters.forecast import HttpMeteorologyProvider

    provider = HttpMeteorologyProvider(
        base_url="https://met.example",
        credential=_SECRET,
        transport=_failing_transport(httpx.ConnectError("refused")),
    )
    assert provider.observation("AQM1", _T0) is None


def test_a_non_numeric_meteorology_value_returns_none() -> None:
    # A string where a number belongs is an unusable body; coercing it would carry a wrong
    # temperature into the Req 9 conversion, which is worse than having none.
    from aqm_ingestion.adapters.forecast import HttpMeteorologyProvider

    provider = HttpMeteorologyProvider(
        base_url="https://met.example",
        credential=_SECRET,
        transport=_json_transport({"temperature_k": "warm"}),
    )
    assert provider.observation("AQM1", _T0) is None


def test_the_credential_sweep_can_actually_fail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Prove the sweep detects a value in a context field, so the tests above prove something.

    The detector-detects self-check from task 3.4 and 17.3. Without it, the two sweeps above
    would
    pass identically if the logger emitted nothing — and this module's first draft DID pass
    while
    watching a surface (``caplog.text``) that never carries context fields at all.

    THE KEY IS DELIBERATELY BENIGN. Writing the secret under ``credential_value`` first,
    the central redaction in the formatter replaced it with ``[redacted]`` and this
    self-check failed — the redactor working exactly as task 1 built it. That is worth
    knowing but is NOT what this test is for: redaction is keyed by NAME, so it cannot
    save a value logged under an innocent key, which is precisely the limit task 17.3
    recorded and precisely what the sweeps above exist to catch. So the self-check uses a
    key the redactor does not recognise.
    """
    configure_logging("debug")
    get_logger("adapters.test").warning("deliberate_leak", detail=_SECRET)

    captured = capsys.readouterr().out
    assert _SECRET in captured, "the sweep cannot see a context field, so it proves nothing"


# --------------------------------------------------------------------------------------
# The MQTT adapter (Requirement 4.1) — construction and shape offline, behaviour fenced
# --------------------------------------------------------------------------------------


def test_the_mqtt_adapter_exposes_exactly_the_transport_port_surface() -> None:
    # The port gained acknowledge() at task 16.1 because (topic, payload) could not express
    # Req 4.6 at all. This asserts the real adapter carries that corrected surface.
    from aqm_ingestion.adapters.mqtt import PahoMqttTransport

    for name in ("subscribe", "messages", "acknowledge", "close"):
        assert callable(getattr(PahoMqttTransport, name))


def test_the_mqtt_adapter_takes_no_credential_material_in_its_signature() -> None:
    # §7: certificate PATHS are configuration, key CONTENTS are not. The adapter takes paths and
    # loads them at connect time, so no key material is ever an argument that could be logged.
    import inspect

    from aqm_ingestion.adapters.mqtt import PahoMqttTransport

    parameters = set(inspect.signature(PahoMqttTransport.__init__).parameters)
    parameters.discard("self")
    assert parameters == {
        "host",
        "port",
        "client_id",
        "ca_cert_path",
        "client_cert_path",
        "client_key_path",
        "keepalive_seconds",
    }


def test_the_default_topic_filter_matches_the_requirement() -> None:
    # Req 4.1 names `aqm/sensors/+/data` as the default.
    from aqm_ingestion.adapters.mqtt import DEFAULT_TOPIC_FILTER

    assert DEFAULT_TOPIC_FILTER == "aqm/sensors/+/data"
