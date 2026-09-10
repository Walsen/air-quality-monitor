"""Unit tests for the User_Profile model (task 17.1).

Health-adjacent data, so §7's minimisation governs every decision here.

- 17.2: EXACTLY the declared fields — an allowlist, not a suggestion.
- 17.3: the Condition and Sensitivity_Level enumerations.
- 17.4: no free-text clinical field, medication, symptom narrative, diagnosis code, date of
  birth, name or contact detail; a write carrying a field outside the set is REJECTED naming
  the offending field.
- 17.5: each location is `home`/`work`/`commute` with coordinates rounded to the configured
  precision (default 3 dp, about 110 m) — precise enough to pick nearby sensors, too coarse to
  identify a dwelling.
- 17.6: at most the configured number of locations (default 5), rejected naming the limit.
- 17.7: a Consent_Record is REQUIRED and its version must be recognized.
- 17.9: no profile field reaches a log, the archive, or an error message.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest
from pydantic import ValidationError

from aqm_ingestion.domain.profile import (
    DEFAULT_LOCATION_LIMIT,
    DEFAULT_LOCATION_PRECISION,
    DEFAULT_PROFILE_CONDITION,
    DEFAULT_PROFILE_SENSITIVITY,
    RECOGNIZED_CONSENT_VERSIONS,
    ActivityLevel,
    Condition,
    ConsentRecord,
    LocationName,
    ProfileLimits,
    SensitivityLevel,
    UserLocation,
    UserProfile,
    build_profile,
)
from aqm_ingestion.observability.logging import configure_logging, get_logger

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _consent() -> dict[str, object]:
    return {"version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))), "given_at": _NOW}


def _fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "user_id": "user-123",
        "condition": Condition.ASTHMA,
        "sensitivity_level": SensitivityLevel.ELEVATED,
        "personal_thresholds": {"PM25": {"kind": "sub_index", "value": 35.0}},
        "locations": [
            {"name": "home", "latitude": 51.5074321, "longitude": -0.1278456}
        ],
        "consent": _consent(),
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return base | overrides


def _profile(**overrides: object) -> UserProfile:
    return build_profile(_fields(**overrides))


# --- Req 17.2 the exact field set ---------------------------------------

def test_the_field_set_is_exactly_the_documented_one() -> None:
    assert set(UserProfile.model_fields) == {
        "user_id",
        "condition",
        "sensitivity_level",
        "personal_thresholds",
        "locations",
        "activity_level",
        "activity_duration_hours",
        "consent",
        "created_at",
        "updated_at",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "medication",
        "symptoms",
        "diagnosis_code",
        "date_of_birth",
        "full_name",
        "email",
        "phone",
        "notes",
    ],
)
def test_a_field_outside_the_allowlist_is_rejected(forbidden: str) -> None:
    # Req 17.4 names each of these explicitly; an allowlist REJECTS rather than ignores,
    # because silently dropping a clinical narrative would still have accepted the request
    with pytest.raises(ValidationError) as caught:
        build_profile(_fields(**{forbidden: "anything"}))
    assert forbidden in str(caught.value)


def test_the_rejection_names_the_offending_field() -> None:
    with pytest.raises(ValidationError) as caught:
        build_profile(_fields(medication="salbutamol"))
    message = str(caught.value)
    assert "medication" in message
    # §7: naming the FIELD is required; echoing its VALUE is not, and would defeat the point
    assert "salbutamol" not in message


# --- Req 17.3 the enumerations ------------------------------------------

def test_the_conditions_are_exactly_the_documented_five() -> None:
    assert {c.value for c in Condition} == {
        "asthma",
        "copd",
        "allergic_rhinitis",
        "asthma_copd_overlap",
        "none_declared",
    }


def test_the_sensitivity_levels_are_exactly_the_documented_three() -> None:
    assert {s.value for s in SensitivityLevel} == {"standard", "elevated", "high"}


def test_an_unknown_condition_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(_fields(condition="emphysema"))


def test_an_unknown_sensitivity_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(_fields(sensitivity_level="extreme"))


# --- Req 17.5 location minimisation -------------------------------------

def test_the_default_precision_is_three_decimals() -> None:
    assert DEFAULT_LOCATION_PRECISION == 3


def test_a_location_is_rounded_to_the_configured_precision() -> None:
    profile = _profile()
    location = profile.locations[0]
    assert location.latitude == pytest.approx(51.507)
    assert location.longitude == pytest.approx(-0.128)


def test_rounding_actually_discards_precision() -> None:
    # the point is MINIMISATION, so assert the stored value is genuinely coarser than the
    # input rather than merely that a round happened
    supplied = 51.5074321
    stored = _profile().locations[0].latitude
    assert stored != supplied
    assert len(str(stored).split(".")[1]) <= DEFAULT_LOCATION_PRECISION


def test_the_stored_precision_cannot_identify_a_dwelling() -> None:
    # 3 dp is about 110 m; a test that pins the ORDER of magnitude protects the intent even
    # if the constant is edited
    from aqm_ingestion.adapters.memory.adapters import _great_circle_km

    metres = _great_circle_km(51.507, 0.0, 51.508, 0.0) * 1000
    assert 50 < metres < 200


def test_the_precision_is_configurable() -> None:
    profile = build_profile(
        _fields(), limits=ProfileLimits(location_precision=1)
    )
    assert profile.locations[0].latitude == pytest.approx(51.5)


def test_the_location_names_are_exactly_the_documented_three() -> None:
    assert {name.value for name in LocationName} == {"home", "work", "commute"}


def test_an_unknown_location_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(
            _fields(locations=[{"name": "gym", "latitude": 51.5, "longitude": 0.0}])
        )


def test_an_out_of_range_coordinate_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(
            _fields(locations=[{"name": "home", "latitude": 91.0, "longitude": 0.0}])
        )


# --- Req 17.6 the location cap ------------------------------------------

def test_the_default_location_limit_is_five() -> None:
    assert DEFAULT_LOCATION_LIMIT == 5


def test_five_locations_are_accepted() -> None:
    locations = [
        {"name": "home", "latitude": 51.5 + index / 100, "longitude": 0.0}
        for index in range(5)
    ]
    assert len(build_profile(_fields(locations=locations)).locations) == 5


def test_six_locations_are_rejected_naming_the_limit() -> None:
    locations = [
        {"name": "home", "latitude": 51.5 + index / 100, "longitude": 0.0}
        for index in range(6)
    ]
    with pytest.raises(ValidationError, match="5"):
        build_profile(_fields(locations=locations))


def test_the_limit_is_configurable() -> None:
    locations = [
        {"name": "home", "latitude": 51.5 + index / 100, "longitude": 0.0}
        for index in range(3)
    ]
    with pytest.raises(ValidationError, match="2"):
        build_profile(_fields(locations=locations), limits=ProfileLimits(location_limit=2))


def test_no_locations_at_all_is_accepted() -> None:
    # Req 17.12's default profile has none, so an empty set must be valid
    assert build_profile(_fields(locations=[])).locations == ()


# --- Req 17.7 the consent record ----------------------------------------

def test_a_missing_consent_record_is_rejected() -> None:
    fields = _fields()
    del fields["consent"]
    with pytest.raises(ValidationError, match="consent"):
        build_profile(fields)


def test_an_unrecognized_consent_version_is_rejected_naming_the_field() -> None:
    with pytest.raises(ValidationError) as caught:
        build_profile(_fields(consent={"version": "made-up", "given_at": _NOW}))
    assert "consent" in str(caught.value).lower()


def test_a_recognized_consent_version_is_accepted() -> None:
    assert _profile().consent.version in RECOGNIZED_CONSENT_VERSIONS


def test_the_consent_record_carries_the_instant_it_was_given() -> None:
    assert _profile().consent.given_at == _NOW


def test_a_naive_consent_instant_is_refused() -> None:
    # §2 / Req 27.8: every internal instant is timezone-aware UTC
    with pytest.raises(ValidationError):
        build_profile(
            _fields(consent={"version": "2026-01-01", "given_at": dt.datetime(2026, 1, 1)})
        )


# --- Req 23 the optional activity inputs --------------------------------

def test_the_activity_inputs_are_optional() -> None:
    profile = _profile()
    assert profile.activity_level is None
    assert profile.activity_duration_hours is None


def test_an_activity_level_is_accepted_from_the_documented_set() -> None:
    assert {level.value for level in ActivityLevel} == {
        "rest",
        "light",
        "moderate",
        "vigorous",
    }


def test_an_unknown_activity_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(_fields(activity_level="sprinting"))


def test_a_negative_activity_duration_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_profile(_fields(activity_level="light", activity_duration_hours=-1.0))


# --- Req 17.12 the default profile --------------------------------------

def test_the_documented_defaults_are_declared() -> None:
    assert DEFAULT_PROFILE_CONDITION is Condition.NONE_DECLARED
    assert DEFAULT_PROFILE_SENSITIVITY is SensitivityLevel.STANDARD


# --- Req 17.9 nothing reaches a log -------------------------------------

def test_the_repr_reveals_only_the_pseudonymous_identity() -> None:
    # THE structural guarantee. Req 17.9 forbids a profile field reaching any log or error
    # message, and a key-name redactor cannot help when a profile is interpolated into an
    # f-string — so the object itself refuses to render its contents.
    rendered = repr(_profile())
    assert "user-123" in rendered  # the pseudonymous identity is permitted (Req 17.9)
    assert "asthma" not in rendered
    assert "51.507" not in rendered
    assert "elevated" not in rendered


def test_str_is_redacted_too() -> None:
    assert "asthma" not in str(_profile())


def test_logging_a_whole_profile_leaks_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info")
    get_logger("test").info("profile_touched", profile=_profile())
    output = capsys.readouterr().out
    assert "asthma" not in output
    assert "51.507" not in output
    assert "elevated" not in output


def test_logging_individual_profile_fields_is_redacted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # belt and braces: the central redactor must also catch the field names, so a caller
    # passing them individually cannot leak them either
    configure_logging("info")
    profile = _profile()
    get_logger("test").info(
        "profile_touched",
        condition=profile.condition.value,
        sensitivity_level=profile.sensitivity_level.value,
        user_location=profile.locations[0].latitude,
        personal_thresholds=dict(profile.personal_thresholds),
        consent_version=profile.consent.version,
    )
    output = capsys.readouterr().out
    for leaked in ("asthma", "elevated", "51.507", "35.0"):
        assert leaked not in output


def test_the_site_position_warning_is_still_loggable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 15.9 REQUIRES logging a SITE's latitude and longitude, while Req 17.9 forbids
    # logging a USER's. Same shape, different subject — so the redactor must not blanket
    # "latitude", and this test is what stops a well-meaning marker from breaking Req 15.9.
    configure_logging("info")
    get_logger("test").warning(
        "site_position_changed",
        SiteCode="CB0001",
        previous_latitude="51.5074000",
        latitude="51.6000000",
    )
    output = capsys.readouterr().out
    assert "51.6000000" in output


def test_an_error_message_names_no_profile_value() -> None:
    # Req 17.9 includes error messages
    with pytest.raises(ValidationError) as caught:
        build_profile(_fields(condition="emphysema", diagnosis_code="J45"))
    assert "J45" not in str(caught.value)


def test_the_profile_is_frozen() -> None:
    with pytest.raises(ValidationError):
        _profile().user_id = "someone-else"


def test_dumping_the_profile_still_yields_the_data() -> None:
    # the redaction is about ACCIDENTAL rendering; a deliberate dump for the store must work
    dumped = cast("dict[str, Any]", json.loads(_profile().model_dump_json()))
    assert dumped["condition"] == "asthma"


def test_locations_are_stored_in_a_defined_order() -> None:
    # §2: order that reaches output must be defined
    locations = [
        {"name": "work", "latitude": 51.6, "longitude": 0.0},
        {"name": "home", "latitude": 51.5, "longitude": 0.0},
    ]
    stored = build_profile(_fields(locations=locations)).locations
    assert [entry.name for entry in stored] == [LocationName.WORK, LocationName.HOME]


def test_a_user_location_is_frozen() -> None:
    with pytest.raises(ValidationError):
        _profile().locations[0].latitude = 0.0


def test_a_consent_record_is_frozen() -> None:
    with pytest.raises(ValidationError):
        _profile().consent.version = "other"


def test_consent_record_fields_are_minimal() -> None:
    assert set(ConsentRecord.model_fields) == {"version", "given_at"}


def test_user_location_fields_are_minimal() -> None:
    assert set(UserLocation.model_fields) == {"name", "latitude", "longitude"}
