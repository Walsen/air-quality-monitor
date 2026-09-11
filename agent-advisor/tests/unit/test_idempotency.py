"""Tests for idempotent write keys (task 14.4). Validates Req 32.4c.

**The key must not contain a clock reading.** Req 32.4c exists because a re-invoked entrypoint
delivers the same turn twice, and a key built from the wall clock makes the second delivery look
new — which is the very failure it was meant to prevent. An earlier `advice_idempotency_key`
keyed on `user_id | turn_at | route` and had exactly this defect; these tests pin the
replacement, whose identity carries no instant at all.

**The key must not contain the write body either.** A profile write's body is authored by the
MODEL, and a re-invoked entrypoint re-runs the model — the second delivery can carry a patch
meaning the same thing while differing in field order or phrasing.

**What is stable is the `runtimeSessionId`.** Req 33.11 requires it be derived from a stable
property of the triggering execution rather than generated fresh, giving its own reason: that
this "is also what makes Requirement 33 criterion 4's idempotency achievable rather than merely
asserted".

**A key is necessary but not sufficient.** A write built by READ-MODIFY-WRITE computes a
different body on the second delivery — "add salbutamol" against an empty profile writes one
medication, and redelivered against the now-updated profile it would write two. So the body must
be a pure function of the confirmed draft, asserted here.

**The diary write is deliberately NOT keyed**, and that is the interesting case. Req 32.4c says
it is already safe by Service 2's Requirement 31 criterion 7, which stores at most one entry per
user per date and replaces rather than accumulates — verified in Service 2's own requirements,
not taken on faith. But that safety rests on the DATE coming from the confirmed draft. Were the
date taken from a clock at delivery time, a retry crossing midnight would write a second entry
on a second date and the replace-per-date rule would collapse nothing. So the precondition is
asserted rather than assumed, because "already safe" is a claim with a dependency.
"""

from __future__ import annotations

import ast
import datetime as dt
import inspect
import pathlib

import pytest

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

_SESSION = "s" * 33
_IDENTITY = TurnIdentity(user_id="u1", session_id=_SESSION)


def _draft(*, names: tuple[str, ...] = ("salbutamol",)) -> ProfileDraft:
    return ProfileDraft(
        medications=tuple(MedicationEntry(name=name, role="reliever") for name in names),
        confirmed=True,
    )


def _entry(*, date: dt.date = dt.date(2026, 7, 1)) -> SymptomEntryDraft:
    return SymptomEntryDraft(
        date=date,
        severity=3,
        markers=("cough",),
        reliever_used=True,
        confirmed=True,
    )


# --- the identity carries no instant ------------------------------------


def test_the_identity_has_nowhere_to_put_an_instant() -> None:
    # THE structural point. An instant is WHEN a turn happened, not WHICH turn it was, and a
    # field
    # able to hold one is a place a later author would key on it — reintroducing the defect.
    assert set(TurnIdentity.__dataclass_fields__) == {"user_id", "session_id"}


def test_an_empty_component_is_refused() -> None:
    # An empty component would collapse unrelated turns onto one key, suppressing real writes.
    for user_id, session_id in (("", _SESSION), ("u1", "")):
        with pytest.raises(ValueError, match="user id and a session id"):
            TurnIdentity(user_id=user_id, session_id=session_id)


def test_the_identity_is_frozen() -> None:
    with pytest.raises(AttributeError):
        _IDENTITY.session_id = "other"  # type: ignore[misc]


# --- the key collapses a redelivery ------------------------------------


def test_the_same_turn_delivered_twice_yields_the_same_key() -> None:
    # Req 32.4c's whole purpose. Rebuilt from the same components, exactly as a re-invoked
    # entrypoint would, rather than reusing one object.
    first = profile_idempotency_key(
        identity=TurnIdentity(user_id="u1", session_id=_SESSION)
    )
    second = profile_idempotency_key(
        identity=TurnIdentity(user_id="u1", session_id=_SESSION)
    )
    assert first == second


def test_two_sessions_yield_different_keys() -> None:
    # The other direction: a key that never collided would satisfy idempotence by being useless.
    assert profile_idempotency_key(identity=_IDENTITY) != profile_idempotency_key(
        identity=TurnIdentity(user_id="u1", session_id="t" * 33)
    )


def test_two_users_never_share_a_key() -> None:
    assert profile_idempotency_key(identity=_IDENTITY) != profile_idempotency_key(
        identity=TurnIdentity(user_id="u2", session_id=_SESSION)
    )


def test_the_two_write_kinds_never_share_a_key() -> None:
    # Both writes happen in one turn, so keying on identity alone would let the profile write's
    # key
    # suppress the audit row — losing exactly the audit Req 20 requires.
    assert profile_idempotency_key(identity=_IDENTITY) != advice_idempotency_key(
        identity=_IDENTITY
    )


def test_there_is_no_write_kind_for_the_diary() -> None:
    # A member for it would invite somebody to key it "for symmetry" and lose the recorded
    # reason it
    # needs no key.
    assert {kind.value for kind in WriteKind} == {"profile", "advice_record"}


def test_the_key_does_not_embed_the_identity_it_was_built_from() -> None:
    # The key is stored and may be logged. One that concatenated the raw identity would put a
    # user id
    # somewhere Req 5.3 does not sanction.
    key = write_idempotency_key(
        identity=TurnIdentity(user_id="SENTINEL-USER-Q7X", session_id=_SESSION),
        kind=WriteKind.PROFILE,
    )
    assert "SENTINEL-USER-Q7X" not in key
    assert len(key) == 64
    assert all(character in "0123456789abcdef" for character in key)


# --- a key is not sufficient: the body must be a pure function ----------


def test_the_profile_body_is_a_pure_function_of_the_confirmed_draft() -> None:
    # THE clause behind Req 32.4c. The hazard is not duplicate bytes but a READ-MODIFY-WRITE
    # that
    # computes a different body on the second delivery.
    assert _draft().write_body() == _draft().write_body()


def test_the_profile_body_takes_no_argument_and_so_cannot_read_current_state() -> None:
    # Structural, not behavioural. `write_body()` has nowhere to receive the stored profile, so
    # a
    # read-modify-write cannot be introduced inside it without changing its signature — a
    # reviewable
    # act rather than a silent one.
    assert list(inspect.signature(ProfileDraft.write_body).parameters) == ["self"]
    assert list(inspect.signature(SymptomEntryDraft.write_body).parameters) == ["self"]


# --- the adapter actually collapses, rather than accepting the key -------


def test_the_local_adapter_collapses_a_repeated_profile_key() -> None:
    # An adapter that took the key and ignored it would let every idempotency test above pass
    # while
    # the real protection existed nowhere. So the offline fake performs the deduplication and a
    # test
    # observes it.
    client = ScriptedServingClient()
    key = profile_idempotency_key(identity=_IDENTITY)
    patch = _draft().write_body()
    client.profile_put("cred", patch, key)
    client.profile_put("cred", patch, key)
    applied = [call for call in client.calls if call[0] == "profile_put"]
    assert len(applied) == 2, "the second delivery must be seen, not silently dropped"
    assert applied[1][1] == (), "the second delivery must carry no patch through"


def test_a_different_key_is_applied_normally() -> None:
    client = ScriptedServingClient()
    patch = _draft().write_body()
    client.profile_put("cred", patch, profile_idempotency_key(identity=_IDENTITY))
    client.profile_put(
        "cred",
        patch,
        profile_idempotency_key(
            identity=TurnIdentity(user_id="u1", session_id="t" * 33)
        ),
    )
    applied = [call for call in client.calls if call[0] == "profile_put" and call[1]]
    assert len(applied) == 2


# --- the model must never author the key --------------------------------


def test_the_profile_tool_does_not_expose_an_idempotency_key() -> None:
    # A tool's `inputSchema` IS part of the prompt. A key the model authored would be freshly
    # invented
    # on every delivery, so a re-invoked entrypoint would defeat Req 32.4c entirely — the same
    # reason
    # the credential is closure-captured, plus one of its own.
    from aqm_advisor.agent.tools import RetrievalRecorder, build_retrieval_tools
    from aqm_advisor.ports.clock import FixedClock

    tools = build_retrieval_tools(
        client=ScriptedServingClient(),
        credential="cred",
        recorder=RetrievalRecorder(),
        clock=FixedClock(dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.UTC)),
        identity=_IDENTITY,
    )
    for tool in tools:
        properties = tool.tool_spec["inputSchema"]["json"].get("properties", {})
        assert "idempotency_key" not in properties, tool.tool_spec["name"]
        assert "credential" not in properties, tool.tool_spec["name"]


# --- why the diary write is NOT keyed ----------------------------------


def test_the_diary_body_carries_the_drafts_date_not_todays() -> None:
    # Req 32.4c calls the diary write already safe by Service 2's Req 31.7, which stores at most
    # one
    # entry per user per date and replaces rather than accumulates. That safety rests entirely
    # on the
    # DATE coming from the confirmed draft: were it read from a clock at delivery time, a retry
    # crossing midnight would write a second entry on a second date and the replace-per-date
    # rule
    # would collapse nothing. So the precondition is asserted rather than assumed.
    assert _entry(date=dt.date(2020, 2, 29)).write_body()["date"] == "2020-02-29"


def test_the_diary_body_is_stable_across_deliveries() -> None:
    assert _entry().write_body() == _entry().write_body()


@pytest.mark.parametrize(
    "module",
    [
        "src/aqm_advisor/domain/idempotency.py",
        "src/aqm_advisor/domain/records.py",
        "src/aqm_advisor/domain/elicitation.py",
        "src/aqm_advisor/domain/diary.py",
    ],
)
def test_no_write_path_module_reads_a_clock_or_randomness(module: str) -> None:
    # The determinism this all rests on, enforced structurally across every module on the write
    # path.
    # A clock read anywhere here makes a redelivered turn look new.
    tree = ast.parse(pathlib.Path(module).read_text(encoding="utf-8"))
    assert _called_names(tree).isdisjoint(_FORBIDDEN_CALLS), (
        f"{module} reads: {_called_names(tree) & _FORBIDDEN_CALLS}"
    )


_FORBIDDEN_CALLS = frozenset(
    {"now", "utcnow", "today", "time", "random", "uuid4", "monotonic"}
)


def _called_names(tree: ast.AST) -> set[str]:
    """Every called name, by attribute and by bare name."""
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_the_clock_detector_actually_fires() -> None:
    # The guardrail-property lesson: a sweep that passes while testing nothing is worse than no
    # sweep,
    # because it reports safety. Planting a clock read must be caught in both call forms.
    for planted in ("x = dt.datetime.now()", "x = today()", "x = random()"):
        assert not _called_names(ast.parse(planted)).isdisjoint(_FORBIDDEN_CALLS), planted
