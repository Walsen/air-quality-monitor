"""Property 17: writes are idempotent under redelivery. Validates Req 32.4c.

**The property is not "the same input gives the same digest".** That would be a property of
`hashlib`, not of this service, and it would pass over a key derived from a clock as long as the
clock were mocked. What has to hold is that a turn REDELIVERED to a re-invoked entrypoint
produces the same write — key and body — while two genuinely different turns produce different
keys. Both directions matter: a key that never collided would satisfy the first half by being
useless.

**The redelivery is simulated the way the fault actually happens.** A re-invoked entrypoint
rebuilds its objects from the request, so the second delivery constructs a FRESH identity and a
FRESH draft from the same content rather than reusing the first objects. A property that reused
one instance would prove only that the methods hold no internal state.
"""

from __future__ import annotations

import datetime as dt

from hypothesis import given
from hypothesis import strategies as st

from aqm_advisor.adapters.local import ScriptedServingClient
from aqm_advisor.domain.elicitation import MedicationEntry, ProfileDraft
from aqm_advisor.domain.idempotency import (
    TurnIdentity,
    WriteKind,
    advice_idempotency_key,
    profile_idempotency_key,
    write_idempotency_key,
)
from aqm_advisor.domain.records import SymptomEntryDraft

_MEDICATION_NAMES = st.sampled_from(
    ["salbutamol", "beclometasone", "montelukast", "formoterol", "prednisolone"]
)
_ROLES = st.sampled_from(["reliever", "preventer", "other"])
_USER_IDS = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=24
)
_SESSION_IDS = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=33, max_size=48
)


@st.composite
def _profile_contents(draw: st.DrawFn) -> tuple[MedicationEntry, ...]:
    """The CONTENT of a confirmed profile, not the object.

    A redelivery rebuilds its objects from the request, so a property must build two drafts from
    one content value rather than reusing a single instance.
    """
    names = draw(st.lists(_MEDICATION_NAMES, min_size=0, max_size=4, unique=True))
    return tuple(
        MedicationEntry(name=name, role=draw(_ROLES)) for name in names
    )


@given(content=_profile_contents(), user_id=_USER_IDS, session_id=_SESSION_IDS)
def test_a_redelivered_profile_write_produces_the_same_key_and_body(
    content: tuple[MedicationEntry, ...], user_id: str, session_id: str
) -> None:
    """Req 32.4c: a duplicate delivery must not double-apply.

    Both the identity and the draft are rebuilt, as a re-invoked entrypoint would rebuild them.
    """
    first_body = ProfileDraft(medications=content, confirmed=True).write_body()
    second_body = ProfileDraft(medications=content, confirmed=True).write_body()
    assert first_body == second_body
    assert profile_idempotency_key(
        identity=TurnIdentity(user_id=user_id, session_id=session_id)
    ) == profile_idempotency_key(
        identity=TurnIdentity(user_id=user_id, session_id=session_id)
    )


@given(content=_profile_contents(), user_id=_USER_IDS, session_id=_SESSION_IDS)
def test_the_local_adapter_applies_a_redelivered_write_only_once(
    content: tuple[MedicationEntry, ...], user_id: str, session_id: str
) -> None:
    """End to end through the port, quantified.

    The keys above could all agree while no adapter honoured them. This asserts the
    deduplication actually happens at the boundary: the second delivery ARRIVES and carries no
    patch through.
    """
    client = ScriptedServingClient()
    identity = TurnIdentity(user_id=user_id, session_id=session_id)
    body = ProfileDraft(medications=content, confirmed=True).write_body()
    for _ in range(2):
        client.profile_put(
            "cred", body, profile_idempotency_key(identity=identity)
        )
    applied = [call for call in client.calls if call[0] == "profile_put" and call[1]]
    assert len(applied) == 1


@given(
    user_id=_USER_IDS,
    left=_SESSION_IDS,
    right=_SESSION_IDS,
    kind=st.sampled_from(list(WriteKind)),
)
def test_distinct_sessions_are_distinct_turns(
    user_id: str, left: str, right: str, kind: WriteKind
) -> None:
    """A key that never collided would satisfy idempotence by being useless."""
    keys_differ = write_idempotency_key(
        identity=TurnIdentity(user_id=user_id, session_id=left), kind=kind
    ) != write_idempotency_key(
        identity=TurnIdentity(user_id=user_id, session_id=right), kind=kind
    )
    assert keys_differ == (left != right)


@given(left=_USER_IDS, right=_USER_IDS, session_id=_SESSION_IDS)
def test_two_users_never_share_a_profile_key(
    left: str, right: str, session_id: str
) -> None:
    """A shared key would let one user's redelivery suppress another user's write."""
    keys_differ = profile_idempotency_key(
        identity=TurnIdentity(user_id=left, session_id=session_id)
    ) != profile_idempotency_key(
        identity=TurnIdentity(user_id=right, session_id=session_id)
    )
    assert keys_differ == (left != right)


@given(user_id=_USER_IDS, session_id=_SESSION_IDS)
def test_the_two_keyed_writes_of_one_turn_never_collide(
    user_id: str, session_id: str
) -> None:
    """Both writes happen in the same turn, under the same identity.

    Keying on identity alone would let the profile write's key suppress the audit row — losing
    exactly the audit Req 20 requires, and losing it silently.
    """
    identity = TurnIdentity(user_id=user_id, session_id=session_id)
    assert profile_idempotency_key(identity=identity) != advice_idempotency_key(
        identity=identity
    )


@given(
    date=st.dates(min_value=dt.date(2020, 1, 1), max_value=dt.date(2030, 1, 1)),
    severity=st.integers(min_value=1, max_value=5),
    markers=st.lists(
        st.sampled_from(["cough", "wheeze", "tight chest", "breathless"]),
        min_size=1,
        max_size=4,
        unique=True,
    ),
    reliever_used=st.booleans(),
)
def test_a_redelivered_diary_write_carries_the_same_date(
    date: dt.date, severity: int, markers: list[str], reliever_used: bool
) -> None:
    """Why the diary write needs no key (Req 32.4c, via Service 2's Req 31.7).

    Service 2 stores at most one entry per user per date and replaces rather than accumulates,
    so the write is already safe — PROVIDED the date comes from the confirmed draft. Were it
    read from a clock at delivery time, a retry crossing midnight would write a second entry on
    a second date and the replace-per-date rule would collapse nothing. This is that
    precondition, quantified.
    """
    first = SymptomEntryDraft(
        date=date,
        severity=severity,
        markers=tuple(markers),
        reliever_used=reliever_used,
        confirmed=True,
    ).write_body()
    second = SymptomEntryDraft(
        date=date,
        severity=severity,
        markers=tuple(markers),
        reliever_used=reliever_used,
        confirmed=True,
    ).write_body()
    assert first == second
    assert first["date"] == date.isoformat()


@given(user_id=_USER_IDS, session_id=_SESSION_IDS, kind=st.sampled_from(list(WriteKind)))
def test_no_key_embeds_the_identity_it_was_built_from(
    user_id: str, session_id: str, kind: WriteKind
) -> None:
    """The key is stored and may be logged, so it must not carry the identity in the clear.

    Quantified over generated ids rather than one sentinel, because a digest that leaked would
    most plausibly leak for a particular shape of input.
    """
    key = write_idempotency_key(
        identity=TurnIdentity(user_id=user_id, session_id=session_id), kind=kind
    )
    assert len(key) == 64
    assert all(character in "0123456789abcdef" for character in key)
    if len(user_id) >= 4:
        assert user_id not in key
    assert session_id not in key
