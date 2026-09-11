"""Write keys that survive a re-invoked entrypoint (Requirement 32.4c).

**One place answers "what are our write keys".** Req 32.4c names three writes and gives them
three different answers, so they are documented together rather than discovered one adapter at a
time.

| Write | Keyed? | Why |
|---|---|---|
| Advice_Record | yes | an append; a duplicate delivery adds a second audit row for one turn |
| profile | yes | a replace is not automatically safe — see below |
| Symptom_Entry | **no** | already safe by Service 2's Req 31.7 — one entry per date, replaced |

Both keyed writes go through `write_idempotency_key`, so there is one derivation rather than two
that must be kept in step.

**The key is derived from the turn's STABLE IDENTITY, not from a clock and not from the write
body.** This is the load-bearing decision and the first two attempts at this module got it
wrong.

Including the *body* fails because a profile write's body is authored by the MODEL, and a
re-invoked entrypoint re-runs the model — the second delivery can carry a patch meaning the same
thing while differing in field order or phrasing, so a body-derived key changes and the
duplicate applies.

Including a *wall-clock instant* fails for a plainer reason: a re-invoked entrypoint reads a
later clock, so every delivery looks new. This defect was live in
`records.advice_idempotency_key`, which keyed on `user_id | turn_at | route` and therefore could
not survive the redelivery it existed for.

What IS stable is the `runtimeSessionId`. Req 33.11 requires it be derived from a stable
property of the triggering execution rather than generated fresh, and gives its own reason: that
this "is also what makes Requirement 33 criterion 4's idempotency achievable rather than merely
asserted". So `TurnIdentity` is `user_id` plus that session id, and carries no instant at all.

**A key is still not sufficient on its own.** `ProfileDraft.write_body()` takes no argument
beyond `self`, so it has nowhere to receive current state and a read-modify-write cannot appear
inside it without changing a signature — which is a reviewable act rather than a silent one.

**Why the Symptom_Entry write is deliberately unkeyed.** Service 2's Requirement 31 criterion 7
stores at most one entry per user per calendar date and replaces rather than accumulates —
checked in Service 2's own requirements rather than taken on faith. Adding a key would be
ceremony the requirement does not ask for. But "already safe" is a claim with a DEPENDENCY: it
rests on the date coming from the confirmed draft. Were the date read from a clock at delivery
time, a retry crossing midnight would write a second entry on a second date and the
replace-per-date rule would collapse nothing. That precondition is asserted by test, so the
safety cannot evaporate silently.

**Nothing here reads a clock or generates randomness**, enforced by an AST test across every
module on the write path. A key derived from the current time makes every delivery look new,
which defeats the deduplication this module exists for.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class WriteKind(StrEnum):
    """Which write a key belongs to.

    Present so two keyed writes in the same turn cannot collide on the turn identity alone.
    `SYMPTOM_ENTRY` is deliberately absent: that write is unkeyed, and a member for it would
    invite somebody to key it "for symmetry" and lose the reason it does not need one.
    """

    PROFILE = "profile"
    ADVICE_RECORD = "advice_record"


@dataclass(frozen=True, slots=True)
class TurnIdentity:
    """What makes two deliveries the SAME turn (Req 32.4c, via Req 33.11).

    `session_id` is the `runtimeSessionId`, which Req 33.11 requires be derived from a stable
    property of the triggering execution rather than generated fresh — its own stated reason
    being that this "is also what makes Requirement 33 criterion 4's idempotency achievable
    rather than merely asserted". So it is the identity every write key rests on.

    There is deliberately NO instant here. An instant is when a turn happened; it is not which
    turn it was, and a re-invoked entrypoint reads a later clock. A key built from a wall-clock
    reading makes every delivery look new, which is the whole failure Req 32.4c describes — see
    `advice_idempotency_key` for the defect this replaced.
    """

    user_id: str
    session_id: str

    def __post_init__(self) -> None:
        """Reject an empty component, which would collapse unrelated turns onto one key."""
        if not self.user_id or not self.session_id:
            raise ValueError("a turn identity needs both a user id and a session id")


def write_idempotency_key(*, identity: TurnIdentity, kind: WriteKind) -> str:
    """Derive the key that collapses a duplicate write (Req 32.4c).

    Derived from the turn's stable identity and the KIND of write. Nothing else participates —
    in particular not the body, which for a profile write is authored by the MODEL: a re-invoked
    entrypoint re-runs the model, so a redelivery can carry a patch meaning the same thing while
    differing in field order or phrasing. A body-derived key would change with it and the
    duplicate would apply, which is the exact failure this guards.

    Each component is LENGTH-PREFIXED rather than joined on a separator. Joining on `|` was
    ambiguous: `user_id="alice", session_id="|b…"` and `user_id="alice|", session_id="b…"` both
    render `alice||b…`, so two DIFFERENT turns collided onto one key — and a colliding key reads
    as an already-applied duplicate, silently discarding a real write to someone's health
    profile. Neither component is constrained to exclude `|`: a `runtimeSessionId` may arrive
    from the platform, where validation checks only length and non-blankness, and `user_id`
    comes from a federated identity whose character set this service does not choose.
    Length-prefixing removes the whole class rather than banning one character.

    Returns a digest rather than a concatenation. The key is stored and may be logged, and one
    embedding the raw identity would put a user id somewhere Req 5.3 does not sanction.
    """
    digest = hashlib.sha256()
    for component in (identity.user_id, identity.session_id, kind.value):
        encoded = component.encode()
        digest.update(f"{len(encoded)}:".encode())
        digest.update(encoded)
    return digest.hexdigest()


def profile_idempotency_key(*, identity: TurnIdentity) -> str:
    """The key for a profile write (Req 32.4c).

    The consequence of excluding the body is deliberate: two profile writes in ONE turn collapse
    to the first. That is the correct shape anyway — `profile_put` replaces the confirmed state
    rather than appending to it, and `ProfileDraft` already holds the whole of that state, so a
    turn needs exactly one write. Collapsing a second also removes the read-modify-write
    accumulation hazard outright: "add salbutamol" cannot be applied twice, because the second
    application never reaches the store.
    """
    return write_idempotency_key(identity=identity, kind=WriteKind.PROFILE)


def advice_idempotency_key(*, identity: TurnIdentity) -> str:
    """The key for an Advice_Record write (Req 32.4c).

    REPLACES an earlier version that derived the key from `user_id | turn_at | route`. That
    version could not survive the redelivery it existed for: `turn_at` was a clock reading taken
    when the turn was handled, so a re-invoked entrypoint produced a different key and wrote a
    second audit row for one turn — which Req 20 would then present as two separate pieces of
    advice.

    The record still carries `turn_at` as DATA. When a turn happened is a legitimate audit fact;
    it is simply not what identifies the turn.
    """
    return write_idempotency_key(identity=identity, kind=WriteKind.ADVICE_RECORD)


__all__ = [
    "TurnIdentity",
    "WriteKind",
    "advice_idempotency_key",
    "profile_idempotency_key",
    "write_idempotency_key",
]
