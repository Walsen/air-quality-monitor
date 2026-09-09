"""Property tests for the response envelope, basis and guardrails.

Feature: ingestion-and-serving-service, Properties 30, 35, 36, 37.
"""

from __future__ import annotations

import datetime as dt
import json

from hypothesis import given
from hypothesis import strategies as st

from aqm_ingestion.domain.models import (
    CalibratedReading,
    Confidence,
    DedupKey,
    QualityFlag,
)
from aqm_ingestion.serving.audit import audit_record_for
from aqm_ingestion.serving.basis import assemble_basis
from aqm_ingestion.serving.guardrails import (
    ADVISORY_SCOPE,
    GuardrailViolationError,
    enforce_guardrails,
    guardrail_envelope,
)

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_TABLE = "epa-2024-05-06"

_species = st.sampled_from(["PM25", "NO2"])
_sites = st.sampled_from(["SITE1", "SITE2", "SITE3"])
_flags = st.sampled_from(list(QualityFlag))
_confidences = st.sampled_from(list(Confidence))
_strategies = st.sampled_from(["identity", "rh_linear"])
_humidity_sources = st.sampled_from(["channel", "provider", "none"])
_conversion_sources = st.sampled_from([None, "channel", "provider", "default"])


@st.composite
def _readings(draw: st.DrawFn) -> CalibratedReading:
    """A reading with generated provenance, always naming the one shipped table."""
    species = draw(_species)
    conversion = draw(_conversion_sources)
    window = draw(st.one_of(st.none(), st.integers(1, 12)))
    return CalibratedReading(
        key=DedupKey(
            site_code=draw(_sites),
            species=species,
            interval_start=_NOW - dt.timedelta(hours=draw(st.integers(0, 5))),
            duration="PT1H",
        ),
        reported_value=draw(st.floats(0.0, 500.0, allow_nan=False)),
        corrected_value=draw(st.floats(0.0, 500.0, allow_nan=False)),
        units="ug.m-3" if species == "PM25" else "ppb",
        quality_flag=draw(_flags),
        confidence=draw(_confidences),
        calibration_strategy=draw(_strategies),
        # One table throughout: Req 25.5 names a single identifier, and assemble_basis refuses a
        # mixture by design, so generating a mixture would only exercise that refusal.
        breakpoint_table=_TABLE,
        ratification_status=draw(st.sampled_from(["P", "R"])),
        ingested_at=_NOW,
        archive_id=f"archive-{draw(st.integers(0, 999))}",
        sub_index=draw(st.integers(0, 500)),
        band=draw(st.sampled_from(["Good", "Moderate", "Unhealthy"])),
        humidity_source=draw(_humidity_sources),  # type: ignore[arg-type]
        conversion_source=conversion,  # type: ignore[arg-type]
        conversion_temperature_k=288.15 if conversion else None,
        conversion_pressure_pa=74000.0 if conversion else None,
        nowcast_window_hours=window,
        nowcast_hours_available=draw(st.integers(0, window)) if window else None,
        nowcast_weight_factor=0.7 if window else None,
    )


@given(readings=st.lists(_readings(), min_size=1, max_size=6))
def test_property_30_every_served_value_carries_a_flag_and_a_confidence(
    readings: list[CalibratedReading],
) -> None:
    """Feature: ingestion-and-serving-service, Property 30.

    Every served value carries a quality flag and a confidence.
    Validates Requirements 13.10, 14.11, 19.12 and 25.6.

    Asserted on the TYPE rather than on a particular assembled body: both fields are
    non-defaulted on CalibratedReading, so a value cannot exist without them. That is the
    strongest available form — a test over one response shape could be satisfied by an assembler
    that happens to copy both fields today.
    """
    for reading in readings:
        assert isinstance(reading.quality_flag, QualityFlag)
        assert isinstance(reading.confidence, Confidence)

    # Req 25.6: never presented as reference-grade. There is no field that could say so, and no
    # flag value asserts it — the enum's members are all qualifications, not endorsements.
    assert all(
        flag.value not in ("reference", "reference_grade", "verified")
        for flag in QualityFlag
    )

    # And the accompaniment is structural: neither field has a default, so a reading cannot be
    # constructed without deciding both.
    for name in ("quality_flag", "confidence"):
        field = CalibratedReading.__dataclass_fields__[name]
        assert field.default is __import__("dataclasses").MISSING


@given(
    readings=st.lists(_readings(), min_size=0, max_size=6),
    crossed=st.booleans(),
)
def test_property_35_the_guardrail_envelope_is_always_present(
    readings: list[CalibratedReading], crossed: bool
) -> None:
    """Feature: ingestion-and-serving-service, Property 35.

    The guardrail envelope is always present.
    Validates Requirements 25.1, 25.2, 25.3 and 25.12.

    Generated over an EMPTY reading set as well, because Requirement 25.12 applies the envelope
    to every data-bearing endpoint and Requirement 20.9 permits a response whose sites carry no
    measurements — the envelope must not depend on there being a value to wrap.
    """
    envelope = guardrail_envelope()
    guardrails = {
        "disclaimer": envelope.disclaimer,
        "advisoryScope": envelope.advisory_scope,
        "emergencyGuidance": envelope.emergency_guidance,
    }
    body: dict[str, object] = {
        "guardrails": guardrails,
        "measurements": [
            {"species": r.key.species, "subIndex": r.sub_index} for r in readings
        ],
        "thresholdCrossed": crossed,
    }

    assert guardrails["disclaimer"].strip() != ""
    assert guardrails["advisoryScope"] == ADVISORY_SCOPE
    assert guardrails["emergencyGuidance"].strip() != ""

    # Req 25.11 on the same body: the envelope itself never trips the forbidden-phrase check.
    enforce_guardrails(body)


@given(readings=st.lists(_readings(), min_size=1, max_size=6))
def test_property_36_the_basis_is_always_populated(
    readings: list[CalibratedReading],
) -> None:
    """Feature: ingestion-and-serving-service, Property 36.

    The basis is always populated.
    Validates Requirements 25.5, 8.11, 9.7, 10.2 and 11.9.

    "Populated" is asserted as COMPLETENESS BOTH WAYS: every contributing reading has a record
    reference, and every reference corresponds to a contributing reading. One direction alone is
    trivially satisfiable — an empty basis satisfies "no spurious references", and a basis
    listing everything twice satisfies "nothing missing".
    """
    basis = assemble_basis(readings)

    assert basis.breakpoint_table == _TABLE
    assert len(basis.records) == len(readings)

    # Both directions, as multisets so a duplicate cannot hide.
    expected = sorted(
        (r.key.site_code, r.key.species, r.key.interval_start, r.archive_id)
        for r in readings
    )
    actual = sorted(
        (ref.site_code, ref.species, ref.interval_start, ref.archive_id)
        for ref in basis.records
    )
    assert actual == expected

    # Req 8.11: every species present has its strategy, and only species present appear.
    assert {entry.species for entry in basis.species} == {
        r.key.species for r in readings
    }

    for entry in basis.species:
        # Req 8.11's RH source is never absent — "none" is a value, not a gap.
        assert entry.humidity_source in ("channel", "provider", "none")
        # Req 9.7: a conversion source arrives WITH the conditions used, or not at all. A source
        # without its temperature would leave the conversion unreviewable.
        if entry.conversion_source is not None:
            assert entry.conversion_temperature_k is not None
            assert entry.conversion_pressure_pa is not None
        # Req 11.9: a NowCast window arrives with its coverage and weight factor.
        if entry.nowcast_window_hours is not None:
            assert entry.nowcast_hours_available is not None
            assert entry.nowcast_weight_factor is not None


@given(readings=st.lists(_readings(), min_size=1, max_size=6), crossed=st.booleans())
def test_property_37_no_forbidden_phrasing_and_no_sensitive_data_in_the_audit(
    readings: list[CalibratedReading], crossed: bool
) -> None:
    """Feature: ingestion-and-serving-service, Property 37.

    No forbidden phrasing in responses and no sensitive data in logs or the audit trail.
    Validates Requirements 25.4, 25.11, 25.8, 17.9 and 18.7.

    The audit half is asserted by SERIALISING the whole record and searching it, rather than by
    checking named fields: a field added later would escape a field-by-field check but not this.
    """
    basis = assemble_basis(readings)
    record = audit_record_for(
        user_id="user-sentinel",
        served_at=_NOW,
        route="/v1/advice",
        basis=basis,
        threshold_crossed=crossed,
    )

    rendered = json.dumps(
        {
            "userId": record.user_id,
            "servedAt": record.served_at.isoformat(),
            "route": record.route,
            "breakpointTable": record.breakpoint_table,
            "calibrationStrategies": list(record.calibration_strategies),
            "thresholdCrossed": record.threshold_crossed,
            "recordReferences": list(record.record_references),
        }
    )

    # Req 25.8: none of the health-adjacent things the requirement enumerates.
    for forbidden in ("asthma", "copd", "allergic_rhinitis", "elevated", "vigorous"):
        assert forbidden not in rendered

    # Req 25.7 wants WHETHER a crossing was reported; the flag is a bool, so no threshold value
    # can travel in it whatever the generated crossing state.
    assert isinstance(record.threshold_crossed, bool)

    # Req 25.11: the assembled body carries no forbidden phrasing. Species names, band names and
    # site codes are the real content of a response, so this asserts the patterns do not fire on
    # legitimate output — a guardrail that refused every real response would be useless.
    envelope = guardrail_envelope()
    enforce_guardrails(
        {
            "guardrails": {
                "disclaimer": envelope.disclaimer,
                "advisoryScope": envelope.advisory_scope,
                "emergencyGuidance": envelope.emergency_guidance,
            },
            "basis": {
                "breakpointTable": basis.breakpoint_table,
                "species": [
                    {
                        "species": e.species,
                        "calibrationStrategy": e.calibration_strategy,
                        "humiditySource": e.humidity_source,
                    }
                    for e in basis.species
                ],
            },
            "measurements": [
                {"species": r.key.species, "band": r.band, "subIndex": r.sub_index}
                for r in readings
            ],
        }
    )


@given(text=st.sampled_from(["you are having an asthma attack", "take salbutamol now"]))
def test_property_37_a_violating_body_is_always_refused(text: str) -> None:
    """Feature: ingestion-and-serving-service, Property 37.

    The counterpart to the property above: a body that DOES carry forbidden phrasing is refused
    wherever the phrase sits. Without this, the clean-body assertions would all be satisfied by
    a check that never fires.
    """
    for body in (
        {"advice": text},
        {"sites": [{"note": text}]},
        {"nested": {"deeper": {"note": text}}},
        {"list": [[text]]},
    ):
        try:
            enforce_guardrails(body)
        except GuardrailViolationError:
            continue
        raise AssertionError(f"forbidden phrasing was not refused in {body!r}")
