"""The personal-data minimisation sweep (task 13.2). Validates Req 19.1 to 19.6.

**Redaction here is keyed on the KEY NAME, so the leak this sweep exists to catch is a
sensitive
VALUE arriving under an innocuous key.** `SENSITIVE_KEY_MARKERS` cannot see a condition logged
as `detail` or an utterance logged as `context`. So the sweep does not inspect key names at all:
it puts a distinctive sentinel in every field a turn can carry, runs the whole lifecycle at
DEBUG, and asserts no sentinel reaches the log by any route.

**The completeness test is what stops this rotting.** A sweep with a fixed sentinel list
silently stops covering a field the moment somebody adds one — the test would still pass, and
the new field would be the one that leaks. So the sentinel keys are compared against the MODEL
FIELD SETS, and adding a field fails this file until it has a sentinel.

**The self-check is what stops it being vacuous.** A sweep that could not see a leak would pass
on a service that logged everything, so one test plants a sentinel deliberately and asserts the
sweep catches it. Without that, a green sweep says nothing.

**Req 19.3 is asserted positively.** The pseudonymous identity must still be logged where an
entry needs to identify the user, so a sweep that redacted everything would break diagnosability
while looking maximally safe.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import pytest

from aqm_advisor.adapters.local import ScriptedServingClient, canned_air_quality
from aqm_advisor.agent.audit import AuditWriter, build_advice_record
from aqm_advisor.agent.boundary import handle_at_top_level
from aqm_advisor.agent.tools import RetrievalRecorder, build_retrieval_tools
from aqm_advisor.domain.degradation import DriftWatcher, degraded_response
from aqm_advisor.domain.envelope import resolve_envelope
from aqm_advisor.domain.idempotency import TurnIdentity
from aqm_advisor.domain.models import AdvisoryRequest, GuardrailEnvelope, PriorTurn
from aqm_advisor.observability.logging import (
    PERMITTED_IDENTITY_KEYS,
    EventLogger,
    _JsonFormatter,
)
from aqm_advisor.ports.clock import FixedClock

_IDENTITY = TurnIdentity(user_id="u1", session_id="s" * 33)

_AT = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_CONFIGURED = "If you are severely breathless, seek emergency care now."
_USER_ID = "pseudonymous-user-1"

# --- the sentinels ------------------------------------------------------

SENTINELS: dict[str, str] = {
    # AdvisoryRequest
    "utterance": "SENTINEL-UTTERANCE-Q7X",
    "credential": "SENTINEL-CREDENTIAL-Q7X",
    "locale": "SENTINEL-LOCALE-Q7X",
    "prior_turns": "SENTINEL-PRIORTURNS-Q7X",
    # PriorTurn
    "prior_utterance": "SENTINEL-PRIOR-UTTERANCE-Q7X",
    "prior_guidance": "SENTINEL-PRIOR-GUIDANCE-Q7X",
    # the retrieved profile — Service 2's OWN field names, not names I assumed. The completeness
    # test
    # below is data-driven against the canned body, and it caught `sensitivity_level` where I
    # had
    # written `sensitivity`, plus a `routines` field I had not covered at all.
    "condition": "SENTINEL-CONDITION-Q7X",
    "sensitivity_level": "SENTINEL-SENSITIVITY-Q7X",
    "medications": "SENTINEL-MEDICATION-Q7X",
    "routines": "SENTINEL-ROUTINE-Q7X",
    "usedDefaultProfile": "SENTINEL-DEFAULTPROFILE-Q7X",
    # coordinates and the location name, which Req 19.4 treats asymmetrically
    "latitude": "SENTINEL-LATITUDE-Q7X",
    "longitude": "SENTINEL-LONGITUDE-Q7X",
    "locationName": "SENTINEL-LOCATION-Q7X",
    "personalThreshold": "SENTINEL-THRESHOLD-Q7X",
}
"""One distinctive value per field a turn can carry.

Distinctive on purpose: a sentinel like `"asthma"` would collide with the service's own
vocabulary and the sweep would report a leak that was really the module's prose. These cannot
occur by accident.
"""


def _all_sentinels() -> tuple[str, ...]:
    return tuple(SENTINELS.values())


# --- Req 19.1 / 19.2: the completeness test -----------------------------


def test_every_request_field_has_a_sentinel() -> None:
    # THE anti-rot test. A sweep with a fixed sentinel list silently stops covering a field the
    # moment
    # somebody adds one — the sweep would still pass, and the new field would be the one that
    # leaks.
    for field in AdvisoryRequest.model_fields:
        assert field in SENTINELS, field


def test_every_prior_turn_field_has_a_sentinel() -> None:
    # Prefixed so they cannot collide with the request's own `utterance`, which is a different
    # value in a
    # different place and must be distinguishable in a failure.
    for field in PriorTurn.model_fields:
        assert f"prior_{field}" in SENTINELS, field


def test_every_profile_field_in_the_canned_body_has_a_sentinel() -> None:
    # Data-driven against what Service 2 actually serves, so a new profile field it starts
    # returning fails
    # here until it is covered. The canned body is this service's record of that contract.
    body = ScriptedServingClient().profile_body
    assert isinstance(body, dict)
    for field in body:
        assert field in SENTINELS, field


def test_no_sentinel_value_is_reused() -> None:
    # A shared sentinel would make a failure ambiguous about which field leaked, which is most
    # of the
    # diagnostic value.
    assert len(set(SENTINELS.values())) == len(SENTINELS)


# --- the lifecycle run --------------------------------------------------


def _sentinel_profile() -> dict[str, object]:
    return {
        "condition": SENTINELS["condition"],
        "sensitivity_level": SENTINELS["sensitivity_level"],
        "medications": [{"name": SENTINELS["medications"], "role": "reliever"}],
        "routines": [{"name": SENTINELS["routines"]}],
        "usedDefaultProfile": False,
        "personalThreshold": SENTINELS["personalThreshold"],
        "latitude": SENTINELS["latitude"],
        "longitude": SENTINELS["longitude"],
        "locationName": SENTINELS["locationName"],
    }


def _sentinel_request() -> AdvisoryRequest:
    return AdvisoryRequest(
        utterance=SENTINELS["utterance"],
        credential=SENTINELS["credential"],  # type: ignore[arg-type]
        locale=SENTINELS["locale"],
        prior_turns=(
            PriorTurn(
                utterance=SENTINELS["prior_utterance"],  # type: ignore[arg-type]
                guidance=SENTINELS["prior_guidance"],  # type: ignore[arg-type]
            ),
        ),
    )


def _run_lifecycle(logger: EventLogger) -> None:
    """Exercise every stage that logs, with sentinels in every field.

    Deliberately covers the paths a happy-path test would skip — the drift watcher, the degraded
    response, the top-level handler and the audit writer — because those are where an author
    reaches for "just log the context so we can debug it".
    """
    request = _sentinel_request()
    client = ScriptedServingClient(profile_body=_sentinel_profile())
    recorder = RetrievalRecorder()
    tools = build_retrieval_tools(
        identity=_IDENTITY,
        client=client,
        credential=request.credential.get_secret_value(),
        recorder=recorder,
        clock=FixedClock(_AT),
    )
    by_name = {tool.tool_name: tool for tool in tools}
    by_name["profile_get"]()
    by_name["air_quality"]()

    envelope = resolve_envelope(
        served=GuardrailEnvelope(emergency_guidance=_CONFIGURED),
        cached=None,
        configured_emergency_guidance=_CONFIGURED,
    ).envelope

    DriftWatcher(configured=_CONFIGURED, logger=logger).observe_served(
        "a different emergency wording so the warning fires"
    )

    degraded_response(
        envelope=envelope,
        answered_at=_AT,
        missing=("current conditions",),
    )
    handle_at_top_level(
        RuntimeError(SENTINELS["utterance"]),
        logger=logger,
        envelope=envelope,
        answered_at=_AT,
    )

    class _FailingStore:
        def append(self, record: object) -> None:
            raise OSError(SENTINELS["condition"])

        def forget_user(self, user_id: str) -> int:
            return 0

    AuditWriter(store=_FailingStore(), logger=logger).write(
        build_advice_record(
            identity=TurnIdentity(user_id=_USER_ID, session_id="s" * 33),
            turn_at=_AT,
            route="/invocations",
            escalation=None,
            threshold_crossed=False,
            driving_pollutant="PM25",
            basis=None,
            guardrail_rejected=False,
            rejection_category=None,
        )
    )
    logger.info("advisory_turn_completed", user_id=_USER_ID)


def _captured_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Every record as the FORMATTER would emit it.

    Formatted rather than raw, because the formatter is what reaches stdout and is where
    redaction happens. A sweep over `getMessage()` would miss a value carried in `extra` — the
    very place structured context lives.
    """
    formatter = _JsonFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


# --- Reqs 19.2, 19.4, 19.6: no sentinel reaches a log -------------------


def test_no_sentinel_reaches_a_log_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    # THE sweep. At DEBUG, because that is the level where an author adds context "just for now"
    # and the
    # level a production incident gets switched to.
    caplog.set_level(logging.DEBUG)
    _run_lifecycle(EventLogger(logging.getLogger("aqm_advisor.test.sweep")))
    text = _captured_log_text(caplog)
    assert caplog.records, "the lifecycle logged nothing, so the sweep proved nothing"
    for sentinel in _all_sentinels():
        assert sentinel not in text, sentinel


def test_the_sweep_can_see_a_planted_value(caplog: pytest.LogCaptureFixture) -> None:
    # THE self-check. A sweep that could not see a leak would pass on a service that logged
    # everything, so
    # a green sweep would say nothing at all.
    caplog.set_level(logging.DEBUG)
    logger = EventLogger(logging.getLogger("aqm_advisor.test.sweep_selfcheck"))
    logger.info("planted", detail=SENTINELS["condition"])
    text = _captured_log_text(caplog)
    assert SENTINELS["condition"] in text


def test_a_sensitive_value_under_an_innocuous_key_is_what_the_sweep_catches(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Naming the gap the sweep exists for. Redaction is keyed on the KEY NAME, so a condition
    # logged as
    # `detail` is invisible to `SENSITIVE_KEY_MARKERS` — and the sweep is the only thing that
    # would notice.
    caplog.set_level(logging.DEBUG)
    logger = EventLogger(logging.getLogger("aqm_advisor.test.sweep_gap"))
    logger.info("leak", condition=SENTINELS["condition"])
    logger.info("leak", detail=SENTINELS["sensitivity_level"])
    text = _captured_log_text(caplog)
    assert SENTINELS["condition"] not in text
    assert SENTINELS["sensitivity_level"] in text


# --- Req 19.3: the pseudonymous identity IS logged ----------------------


def test_the_pseudonymous_identity_is_still_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Req 19.3, asserted POSITIVELY. A sweep that redacted everything would look maximally safe
    # while
    # making an incident undiagnosable, so the identity must survive.
    caplog.set_level(logging.DEBUG)
    logger = EventLogger(logging.getLogger("aqm_advisor.test.sweep_identity"))
    logger.info("advisory_turn_completed", user_id=_USER_ID)
    assert _USER_ID in _captured_log_text(caplog)


def test_the_identity_keys_are_the_documented_ones() -> None:
    # Pins the exception, so widening it is a reviewable act rather than a quiet one.
    assert frozenset({"user_id", "sub", "subject"}) == PERMITTED_IDENTITY_KEYS


# --- Req 19.4: no coordinate, ever --------------------------------------


def test_a_coordinate_never_reaches_a_log(caplog: pytest.LogCaptureFixture) -> None:
    # Req 19.4 singles coordinates out because a location is the one personal datum that is
    # useful to an
    # attacker on its own — it says where the person is.
    caplog.set_level(logging.DEBUG)
    logger = EventLogger(logging.getLogger("aqm_advisor.test.sweep_coords"))
    logger.info("site", latitude=SENTINELS["latitude"], longitude=SENTINELS["longitude"])
    text = _captured_log_text(caplog)
    assert SENTINELS["latitude"] not in text
    assert SENTINELS["longitude"] not in text


def test_the_location_name_is_permitted(caplog: pytest.LogCaptureFixture) -> None:
    # Req 19.4's other half: where a location must be identified, use the NAME Service 2
    # returned. A sweep
    # that also redacted the name would leave no way to say which of a user's sites an entry was
    # about.
    caplog.set_level(logging.DEBUG)
    logger = EventLogger(logging.getLogger("aqm_advisor.test.sweep_locname"))
    logger.info("site", location_name="home")
    assert "home" in _captured_log_text(caplog)


# --- Req 19.6: the tool results are the only place the data goes --------


def test_the_retrieved_profile_is_never_logged_by_the_tools(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Req 19.6: personal data goes to the Serving_Client and the Model_Port, and nowhere else. A
    # tool that
    # logged its own result "for debugging" would send the profile to a third destination.
    caplog.set_level(logging.DEBUG)
    client = ScriptedServingClient(profile_body=_sentinel_profile())
    tools = build_retrieval_tools(
        identity=_IDENTITY,
        client=client,
        credential="a-credential",
        recorder=RetrievalRecorder(),
        clock=FixedClock(_AT),
    )
    by_name = {tool.tool_name: tool for tool in tools}
    result = str(by_name["profile_get"]())
    assert SENTINELS["condition"] in result, "the tool must still return the profile"
    assert SENTINELS["condition"] not in _captured_log_text(caplog)


def test_the_snapshot_body_is_never_logged_by_the_tools(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    body = canned_air_quality()
    personalized = body["personalized"]
    assert isinstance(personalized, dict)
    personalized["condition"] = SENTINELS["condition"]
    tools = build_retrieval_tools(
        identity=_IDENTITY,
        client=ScriptedServingClient(air_quality_body=body),
        credential="a-credential",
        recorder=RetrievalRecorder(),
        clock=FixedClock(_AT),
    )
    by_name = {tool.tool_name: tool for tool in tools}
    by_name["air_quality"]()
    assert SENTINELS["condition"] not in _captured_log_text(caplog)


def test_the_credential_never_reaches_a_log_from_the_lifecycle(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Called out separately from the sweep because it is the one value whose disclosure is
    # immediately
    # exploitable rather than merely private.
    caplog.set_level(logging.DEBUG)
    _run_lifecycle(EventLogger(logging.getLogger("aqm_advisor.test.sweep_cred")))
    assert SENTINELS["credential"] not in _captured_log_text(caplog)


def test_a_secret_field_does_not_render_even_when_interpolated() -> None:
    # Req 19.2's structural backstop: `SecretStr` refuses to render, so a call site that
    # interpolated a
    # prior turn into a message would emit the mask rather than the words.
    request = _sentinel_request()
    assert SENTINELS["prior_utterance"] not in str(request.prior_turns[0])
    assert SENTINELS["credential"] not in str(request.credential)
    assert SENTINELS["credential"] not in json.dumps(
        request.model_dump(mode="json"), default=str
    )


# --- the location exception, scoped exactly -----------------------------


@pytest.mark.parametrize("key", ["location_name", "site_name", "site_code"])
def test_the_permitted_location_keys_are_not_redacted(key: str) -> None:
    # Req 19.4's prescribed alternative to a coordinate. Before this, the broad `location`
    # marker redacted
    # `location_name` too — satisfying the requirement's first clause while making its second
    # impossible,
    # so an operator had no way to say which of a user's sites an entry was about.
    from aqm_advisor.observability.logging import is_sensitive

    assert is_sensitive(key) is False


@pytest.mark.parametrize(
    "key",
    [
        "location",
        "locationName",
        "location_coordinates",
        "site_location",
        "user_location",
        "latitude",
        "longitude",
        "coordinate",
    ],
)
def test_the_exception_does_not_widen_the_location_marker(key: str) -> None:
    # THE non-vacuity half. Exact names only, exactly as `PERMITTED_COUNT_KEYS` is: a substring
    # exception
    # would re-open the marker it exists to narrow, and `location_coordinates` would pass.
    #
    # `locationName` stays redacted deliberately, even though that is the name Service 2
    # returns. This
    # service writes its own log keys in snake_case, so the camelCase form only ever appears
    # when a
    # RETRIEVED BODY is passed wholesale — and a whole retrieved body is exactly what must not
    # be logged.
    from aqm_advisor.observability.logging import is_sensitive

    assert is_sensitive(key) is True


def test_the_permitted_sets_do_not_overlap() -> None:
    # Three exact-match exceptions now exist. A key appearing in two would make the reason for
    # its
    # exemption ambiguous, and narrowing one later would silently leave it exempt via the other.
    from aqm_advisor.observability.logging import (
        PERMITTED_COUNT_KEYS,
        PERMITTED_LOCATION_KEYS,
    )

    assert PERMITTED_IDENTITY_KEYS.isdisjoint(PERMITTED_COUNT_KEYS)
    assert PERMITTED_IDENTITY_KEYS.isdisjoint(PERMITTED_LOCATION_KEYS)
    assert PERMITTED_COUNT_KEYS.isdisjoint(PERMITTED_LOCATION_KEYS)
