"""Unit tests for forecast and pollen enrichment (task 22.1).

Requirements 24.1-24.8 and 24.10.

TWO PORT GAPS FOUND BY READING REQUIREMENT 24 AGAINST THE TASK-3 CODE, both of which made a
requirement inexpressible rather than merely awkward:

- `ForecastResult` had no provider field, so Requirement 24.3 — report the provider identifier
  with EVERY forecast — could not be honoured at all: the serving layer would have had to name
  a provider nobody had told it about.
- `PollenResult.values` was `Mapping[str, float]`, but Requirement 24.7 wants a category from a
  CLOSED set. The deciding evidence is what the spec does NOT contain: it defines
  Breakpoint_Tables for PM2.5 and NO2 in full detail and gives no pollen thresholds anywhere,
  while Requirement 24.2 forbids this service deriving or modelling either value. Classifying a
  raw count would mean inventing bands the spec deliberately withholds, so the PROVIDER supplies
  the category.

A SPEC GAP, resolved and recorded: Requirements 24.1 and 24.8 both say "the primary
User_Location" and nothing in the spec defines which one that is. Resolved as the FIRST location
in the profile's declared order, because that is TOTAL — defined for every lawful profile —
whereas keying on the name `home` would leave "primary" undefined for a user who saved only work
and commute.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from aqm_ingestion.domain.weighting import PollenRelevance
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.ports.clock import AdvanceableClock
from aqm_ingestion.ports.protocols import (
    ForecastResult,
    PollenCategory,
    PollenResult,
)
from aqm_ingestion.serving.enrichment import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_TREND_BAND_POINTS,
    DEFAULT_TTL_MINUTES,
    Enricher,
    EnrichmentSettings,
    TrendDirection,
    primary_position,
)

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_HERE = (51.507, -0.128)


class _StubForecastClient:
    """A forecast client whose answers and failures are scripted."""

    def __init__(
        self,
        forecast: ForecastResult | None = None,
        pollen: PollenResult | None = None,
        raises: Exception | None = None,
    ) -> None:
        """Hold the scripted answers."""
        self._forecast = forecast
        self._pollen = pollen
        self._raises = raises
        self.forecast_calls: list[tuple[float, float]] = []
        self.pollen_calls: list[tuple[float, float]] = []

    def forecast(self, lat: float, lon: float) -> ForecastResult:
        """Return the scripted forecast, recording the call."""
        self.forecast_calls.append((lat, lon))
        if self._raises is not None:
            raise self._raises
        return self._forecast or ForecastResult(values={}, degraded=True)

    def pollen(self, lat: float, lon: float) -> PollenResult:
        """Return the scripted pollen, recording the call."""
        self.pollen_calls.append((lat, lon))
        if self._raises is not None:
            raise self._raises
        return self._pollen or PollenResult(values={}, degraded=True)


def _forecast(aqi: float, provider: str = "test-provider") -> ForecastResult:
    return ForecastResult(
        values={"aqi": aqi}, provider=provider, issued_at=_NOW, degraded=False
    )


def _pollen(provider: str = "test-provider") -> PollenResult:
    return PollenResult(
        values={"grass": PollenCategory.HIGH, "birch": PollenCategory.LOW},
        provider=provider,
        issued_at=_NOW,
        degraded=False,
    )


def _enricher(
    client: _StubForecastClient, settings: EnrichmentSettings | None = None
) -> tuple[Enricher, AdvanceableClock]:
    clock = AdvanceableClock(_NOW)
    return (
        Enricher(client=client, clock=clock, settings=settings or EnrichmentSettings()),
        clock,
    )


# --- the configured defaults --------------------------------------------

def test_the_defaults_are_the_requirements_defaults() -> None:
    assert DEFAULT_TTL_MINUTES == 60
    assert DEFAULT_TREND_BAND_POINTS == 5
    assert DEFAULT_TIMEOUT_SECONDS == 2.0


# --- the spec gap: which location is "primary" -------------------------

def test_the_primary_location_is_the_first_declared() -> None:
    assert primary_position(((51.5, -0.1), (48.8, 2.3))) == (51.5, -0.1)


def test_there_is_no_primary_position_without_a_location() -> None:
    # Req 17.12's default profile carries no locations, so this path is reachable.
    assert primary_position(()) is None


# --- Req 24.1, 24.3: retrieve and report the provider ------------------

def test_the_forecast_aqi_is_attached() -> None:
    enricher, _clock = _enricher(_StubForecastClient(_forecast(80.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.forecast_aqi == 80.0


def test_the_provider_identifier_is_reported() -> None:
    enricher, _clock = _enricher(
        _StubForecastClient(_forecast(80.0, provider="acme-air"), _pollen("acme-air"))
    )
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.provider == "acme-air"


def test_the_retrieval_instant_is_reported_from_the_clock() -> None:
    # Req 24.3 asks for the instant the value was RETRIEVED, which is this service's own
    # observation — distinct from the provider's issued_at, which is the provider's claim.
    enricher, clock = _enricher(_StubForecastClient(_forecast(80.0), _pollen()))
    clock.advance(dt.timedelta(minutes=7))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.retrieved_at == _NOW + dt.timedelta(minutes=7)


def test_a_degraded_provider_answer_is_treated_as_no_forecast() -> None:
    enricher, _clock = _enricher(
        _StubForecastClient(ForecastResult(values={}, degraded=True), _pollen())
    )
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.forecast_aqi is None
    assert result.degraded is True


# --- Req 24.6: the trend --------------------------------------------------

def test_a_forecast_above_the_band_is_rising() -> None:
    enricher, _clock = _enricher(_StubForecastClient(_forecast(80.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.trend is TrendDirection.RISING


def test_a_forecast_below_the_band_is_falling() -> None:
    enricher, _clock = _enricher(_StubForecastClient(_forecast(60.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.trend is TrendDirection.FALLING


def test_a_forecast_inside_the_band_is_steady() -> None:
    enricher, _clock = _enricher(_StubForecastClient(_forecast(73.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.trend is TrendDirection.STEADY


def test_a_forecast_exactly_at_the_band_edge_is_steady() -> None:
    # The band is what counts as "no meaningful change", so its own width is inside it. A
    # strict comparison here would call a 5-point move rising AND make the band 4 points wide.
    enricher, _clock = _enricher(_StubForecastClient(_forecast(75.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.trend is TrendDirection.STEADY


def test_the_band_is_symmetric() -> None:
    enricher, _clock = _enricher(_StubForecastClient(_forecast(65.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.trend is TrendDirection.STEADY


def test_the_band_width_is_configurable() -> None:
    enricher, _clock = _enricher(
        _StubForecastClient(_forecast(80.0), _pollen()),
        EnrichmentSettings(trend_band_points=20),
    )
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.trend is TrendDirection.STEADY


def test_there_is_no_trend_without_a_current_overall_aqi() -> None:
    # Req 12.6 allows no Overall_AQI when no species is available; a trend against nothing would
    # be a comparison with a value that does not exist.
    enricher, _clock = _enricher(_StubForecastClient(_forecast(80.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=None, pollen=PollenRelevance.RELEVANT)
    assert result.forecast_aqi == 80.0
    assert result.trend is None


def test_there_is_no_trend_without_a_forecast() -> None:
    enricher, _clock = _enricher(
        _StubForecastClient(ForecastResult(values={}, degraded=True), _pollen())
    )
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.trend is None


# --- Req 24.7: pollen only when the weighting says so -----------------

def test_pollen_is_included_when_the_weighting_marks_it_relevant() -> None:
    enricher, _clock = _enricher(_StubForecastClient(_forecast(80.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.pollen == {"birch": PollenCategory.LOW, "grass": PollenCategory.HIGH}


def test_pollen_is_included_when_it_is_primary() -> None:
    enricher, _clock = _enricher(_StubForecastClient(_forecast(80.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.PRIMARY)
    assert result.pollen is not None


def test_pollen_is_omitted_when_the_weighting_says_it_is_irrelevant() -> None:
    enricher, _clock = _enricher(_StubForecastClient(_forecast(80.0), _pollen()))
    result = enricher.enrich(
        _HERE, current_overall_aqi=70, pollen=PollenRelevance.NOT_RELEVANT
    )
    assert result.pollen is None


def test_pollen_is_not_even_requested_when_irrelevant() -> None:
    # §7 data minimisation: not asking is stronger than asking and discarding, and it also
    # spares the provider a call whose answer would be thrown away.
    client = _StubForecastClient(_forecast(80.0), _pollen())
    enricher, _clock = _enricher(client)
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.NOT_RELEVANT)
    assert client.pollen_calls == []


def test_every_pollen_category_is_from_the_requirements_set() -> None:
    assert {member.value for member in PollenCategory} == {
        "none",
        "low",
        "moderate",
        "high",
        "very_high",
    }


def test_the_pollen_taxa_are_ordered() -> None:
    # §2: the taxa reach the response, so their order must be defined rather than incidental.
    enricher, _clock = _enricher(_StubForecastClient(_forecast(80.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.pollen is not None
    assert list(result.pollen) == sorted(result.pollen)


# --- Req 24.4: graceful degradation -----------------------------------

def test_a_client_failure_does_not_fail_the_request() -> None:
    enricher, _clock = _enricher(_StubForecastClient(raises=TimeoutError("slow")))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.forecast_aqi is None
    assert result.degraded is True


def test_a_failure_names_its_kind() -> None:
    enricher, _clock = _enricher(_StubForecastClient(raises=TimeoutError("slow")))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.failure_kind == "timeout"


def test_a_connection_failure_is_a_distinct_kind() -> None:
    enricher, _clock = _enricher(_StubForecastClient(raises=OSError("refused")))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.failure_kind == "transport"


def test_an_unusable_body_is_a_distinct_kind() -> None:
    enricher, _clock = _enricher(_StubForecastClient(raises=ValueError("garbage")))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.failure_kind == "unusable_body"


def test_exactly_one_warning_is_logged_for_a_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 24.4 says ONE warning. Two calls failing inside one enrichment must not become two
    # lines, or the rate of provider trouble is misreported.
    configure_logging("info")
    enricher, _clock = _enricher(_StubForecastClient(raises=TimeoutError("slow")))
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    warnings = [
        line
        for line in capsys.readouterr().out.strip().splitlines()
        if line and json.loads(line).get("level") == "warning"
    ]
    assert len(warnings) == 1


def test_the_warning_names_the_failure_kind(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    enricher, _clock = _enricher(_StubForecastClient(raises=TimeoutError("slow")))
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert "timeout" in capsys.readouterr().out


def test_a_pollen_failure_still_serves_the_forecast() -> None:
    # Per-item isolation (§5): losing pollen must not cost the user the forecast.
    class _PollenOnlyFailure(_StubForecastClient):
        def pollen(self, lat: float, lon: float) -> PollenResult:
            """Fail only the pollen call."""
            raise TimeoutError("slow")

    enricher, _clock = _enricher(_PollenOnlyFailure(_forecast(80.0)))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.forecast_aqi == 80.0
    assert result.pollen is None
    assert result.degraded is True


def test_a_successful_enrichment_is_not_degraded() -> None:
    # The counterpart, without which every "degraded is True" assertion above could pass on an
    # implementation that always degrades.
    enricher, _clock = _enricher(_StubForecastClient(_forecast(80.0), _pollen()))
    result = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert result.degraded is False
    assert result.failure_kind is None


# --- Req 24.5: caching -------------------------------------------------

def test_a_second_call_within_the_ttl_is_served_from_cache() -> None:
    client = _StubForecastClient(_forecast(80.0), _pollen())
    enricher, clock = _enricher(client)
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    clock.advance(dt.timedelta(minutes=30))
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert len(client.forecast_calls) == 1


def test_a_call_after_the_ttl_refetches() -> None:
    client = _StubForecastClient(_forecast(80.0), _pollen())
    enricher, clock = _enricher(client)
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    clock.advance(dt.timedelta(minutes=61))
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert len(client.forecast_calls) == 2


def test_the_cache_is_keyed_by_position() -> None:
    client = _StubForecastClient(_forecast(80.0), _pollen())
    enricher, _clock = _enricher(client)
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    enricher.enrich((48.857, 2.352), current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert len(client.forecast_calls) == 2


def test_the_cache_serves_the_same_values() -> None:
    client = _StubForecastClient(_forecast(80.0), _pollen())
    enricher, clock = _enricher(client)
    first = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    clock.advance(dt.timedelta(minutes=10))
    second = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert second.forecast_aqi == first.forecast_aqi
    assert second.provider == first.provider


def test_a_cached_answer_reports_its_original_retrieval_instant() -> None:
    # Req 24.3's retrieval instant must stay the moment the value was FETCHED. Restamping it on
    # a cache hit would present an hour-old value as just-retrieved, defeating the disclosure.
    client = _StubForecastClient(_forecast(80.0), _pollen())
    enricher, clock = _enricher(client)
    first = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    clock.advance(dt.timedelta(minutes=30))
    second = enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert second.retrieved_at == first.retrieved_at


def test_the_trend_is_recomputed_on_a_cache_hit() -> None:
    # The forecast is cached; the CURRENT AQI is not. A cached trend would go stale the moment a
    # new reading arrived, reporting a rise that had already happened.
    client = _StubForecastClient(_forecast(80.0), _pollen())
    enricher, clock = _enricher(client)
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    clock.advance(dt.timedelta(minutes=10))
    second = enricher.enrich(_HERE, current_overall_aqi=95, pollen=PollenRelevance.RELEVANT)
    assert second.trend is TrendDirection.FALLING


def test_a_failure_is_not_cached() -> None:
    # Caching a failure would extend a transient outage to the full time-to-live.
    client = _StubForecastClient(raises=TimeoutError("slow"))
    enricher, clock = _enricher(client)
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    clock.advance(dt.timedelta(minutes=1))
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert len(client.forecast_calls) == 2


def test_the_ttl_is_configurable() -> None:
    client = _StubForecastClient(_forecast(80.0), _pollen())
    enricher, clock = _enricher(client, EnrichmentSettings(ttl_minutes=5))
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    clock.advance(dt.timedelta(minutes=6))
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert len(client.forecast_calls) == 2


# --- Req 24.2, 24.8, 24.10: what the enricher may not touch -----------

def test_the_enricher_cannot_reach_stored_readings() -> None:
    # Req 24.2 forbids deriving a forecast from stored Readings. Structural: there is no
    # ReadingsStore in scope, so there is nothing to derive one from.
    import inspect

    params = set(inspect.signature(Enricher.__init__).parameters)
    assert params == {"self", "client", "clock", "settings"}


def test_the_enricher_is_passed_only_a_position() -> None:
    # Req 24.8: never a Condition, never the user identity, never any other profile field. The
    # pollen relevance is a BOOLEAN-ish flag derived from the weighting, not the Condition
    # itself, so the Condition never crosses this boundary.
    import inspect

    assert set(inspect.signature(Enricher.enrich).parameters) == {
        "self",
        "position",
        "current_overall_aqi",
        "pollen",
    }


def test_only_the_coordinates_reach_the_client() -> None:
    client = _StubForecastClient(_forecast(80.0), _pollen())
    enricher, _clock = _enricher(client)
    enricher.enrich(_HERE, current_overall_aqi=70, pollen=PollenRelevance.RELEVANT)
    assert client.forecast_calls == [_HERE]
    assert client.pollen_calls == [_HERE]


def test_the_port_takes_no_credential() -> None:
    # Req 24.10, the same structural argument the feed poller uses: an enricher that CANNOT
    # obtain a credential cannot leak one, so resolving it is the adapter's business.
    import inspect

    from aqm_ingestion.ports.protocols import ForecastClient

    for method in (ForecastClient.forecast, ForecastClient.pollen):
        assert set(inspect.signature(method).parameters) == {"self", "lat", "lon"}


def test_no_enrichment_field_names_a_profile_attribute() -> None:
    from aqm_ingestion.serving.enrichment import Enrichment

    forbidden = ("condition", "user", "identity", "sensitivity", "threshold")
    for field in Enrichment.__dataclass_fields__:
        assert not any(word in field.lower() for word in forbidden)


# --- shape ------------------------------------------------------------

def test_a_non_positive_ttl_is_refused() -> None:
    with pytest.raises(ValueError, match="ttl"):
        EnrichmentSettings(ttl_minutes=0)


def test_a_negative_band_is_refused() -> None:
    with pytest.raises(ValueError, match="band"):
        EnrichmentSettings(trend_band_points=-1)


def test_a_non_positive_timeout_is_refused() -> None:
    with pytest.raises(ValueError, match="timeout"):
        EnrichmentSettings(timeout_seconds=0.0)
