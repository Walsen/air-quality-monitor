"""`identity_from_snapshot` — the lawful source of the pseudonymous user identity.

Req 20.2 needs it for the audit record; Req 5.6 forbids getting it from the credential. It comes
from the `user` field of the Air_Quality_Snapshot, which a live call to Service 2 confirmed is
present (the field is `user`, not `userId` — a grep for the latter is what first suggested,
wrongly, that Service 2 returned no identity at all).
"""

from __future__ import annotations

import pytest

from aqm_advisor.composition import IdentityUnavailableError, identity_from_snapshot
from aqm_advisor.domain.models import AdvisoryRequest
from aqm_advisor.observability.correlation import SESSION_ID_MIN_LENGTH


def _request() -> AdvisoryRequest:
    return AdvisoryRequest(utterance="how is the air?", credential="c")  # type: ignore[arg-type]


def test_the_identity_is_the_snapshot_user() -> None:
    identity = identity_from_snapshot(_request(), {"user": "u-demo-1", "nearestSensors": []})
    assert identity.user_id == "u-demo-1"


def test_a_missing_user_is_refused_rather_than_invented() -> None:
    # Failing is correct: an audit record with an invented subject is one `forget_user` could
    # never erase. Silence here would be the worst outcome.
    with pytest.raises(IdentityUnavailableError):
        identity_from_snapshot(_request(), {"nearestSensors": []})


def test_a_degraded_turn_with_no_snapshot_is_refused() -> None:
    with pytest.raises(IdentityUnavailableError):
        identity_from_snapshot(_request(), None)


def test_a_blank_user_is_refused() -> None:
    with pytest.raises(IdentityUnavailableError):
        identity_from_snapshot(_request(), {"user": "   "})


def test_the_originated_session_id_clears_the_platform_floor() -> None:
    # Outside a session scope no correlation id is set, so one is originated — and it must
    # clear AgentCore's 33-char minimum, or `TurnIdentity` would carry an id it rejects.
    identity = identity_from_snapshot(_request(), {"user": "u-demo-1"})
    assert len(identity.session_id) >= SESSION_ID_MIN_LENGTH
