"""Tests for health profile elicitation (task 14.1).

Validates Reqs 4.2, 4.3, 4.4, 4.5, 27.1 to 27.6.

**Reqs 27.3 and 27.4 both end with "SHALL NOT echo the offered value", and that is made
structural.** `decline_message` takes a KIND, not the offered text — so there is no parameter
through which a dose or a date of birth could reach the reply. A function that received the text
and promised not to use it would be a promise; a function that cannot see it is a guarantee.

**A medication entry has nowhere to put a dose.** Req 27.3 says record only the name and the
role, so the model carries exactly those two fields with `extra="forbid"`. An attempt to record
a dose RAISES rather than being silently dropped, because a dropped field looks identical to one
that was never offered — the same reasoning that consolidated `AdviceRecord` onto the strict
model.

**Req 27.2's confirmation is a gate, not a courtesy.** Mapping "a brown inhaler every morning"
onto a preventer role is an interpretation of someone's health, so the draft starts unconfirmed
and the write refuses — the same fail-closed shape as the verification ledger.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aqm_advisor.domain.elicitation import (
    DeclinedKind,
    MedicationEntry,
    ProfileDraft,
    RoutineEntry,
    decline_message,
    limit_rejection_message,
    restate_for_confirmation,
)
from aqm_advisor.domain.grounding import numerals


def _draft(**kwargs: object) -> ProfileDraft:
    defaults: dict[str, object] = {
        "condition": "asthma",
        "sensitivity_level": "standard",
        "medications": (MedicationEntry(name="salbutamol", role="reliever"),),
        "routines": (RoutineEntry(name="morning walk", days=("mon", "wed")),),
    }
    return ProfileDraft(**{**defaults, **kwargs})  # type: ignore[arg-type]


# --- Req 27.3: only the name and the role -------------------------------


def test_a_medication_entry_carries_the_name_and_role() -> None:
    entry = MedicationEntry(name="salbutamol", role="reliever")
    assert entry.name == "salbutamol"
    assert entry.role == "reliever"


@pytest.mark.parametrize(
    "field", ["dose", "frequency", "route", "schedule", "strength", "puffs", "times_per_day"]
)
def test_a_medication_entry_has_nowhere_to_put_a_dose(field: str) -> None:
    # Req 27.3, structurally. An attempt RAISES rather than being silently dropped, because a
    # dropped
    # field looks identical to one that was never offered — and this service must be able to say
    # it did
    # not record the dose.
    with pytest.raises(ValidationError):
        MedicationEntry(name="salbutamol", role="reliever", **{field: "two puffs"})


def test_the_medication_field_set_is_exactly_two() -> None:
    assert set(MedicationEntry.model_fields) == {"name", "role"}


def test_a_medication_name_carrying_a_dose_is_refused() -> None:
    # The smuggling route the field check alone would miss: "salbutamol 100mcg" puts the dose IN
    # the name.
    # Req 27.3 says record only the name, and a name with a strength in it is not only the name.
    with pytest.raises(ValidationError, match="name"):
        MedicationEntry(name="salbutamol 100mcg", role="reliever")


def test_a_plain_medication_name_is_accepted() -> None:
    # Non-vacuity: a check that refused every name would make Req 27.1's elicitation impossible.
    assert MedicationEntry(name="beclometasone", role="preventer").name == "beclometasone"


# --- Reqs 27.3 / 27.4: the decline never echoes -------------------------


@pytest.mark.parametrize("kind", list(DeclinedKind))
def test_the_decline_message_takes_a_kind_not_the_text(kind: DeclinedKind) -> None:
    # THE structural guarantee. Both requirements end with "SHALL NOT echo the offered value",
    # and a
    # function that cannot SEE the value cannot echo it. Quantified over every kind so a new one
    # arrives
    # with a message rather than a KeyError.
    message = decline_message(kind)
    assert message.strip() != ""


def test_the_decline_function_has_no_text_parameter() -> None:
    # What makes the property above hold rather than being tested. A function receiving the
    # offered text
    # and promising not to use it would be a promise; one that cannot see it is a guarantee.
    import inspect

    parameters = set(inspect.signature(decline_message).parameters)
    for forbidden in ("text", "offered", "value", "utterance", "detail"):
        assert forbidden not in parameters, forbidden


@pytest.mark.parametrize("kind", list(DeclinedKind))
def test_every_decline_says_what_the_service_does_keep(kind: DeclinedKind) -> None:
    # Reqs 27.3 and 27.4 both require it. "I cannot record that" leaves the user unsure whether
    # to try a
    # different wording, so the message says what WOULD be kept.
    lowered = decline_message(kind).casefold()
    assert "record" in lowered or "keep" in lowered


@pytest.mark.parametrize("kind", list(DeclinedKind))
def test_no_decline_message_carries_a_numeral(kind: DeclinedKind) -> None:
    # A dose and a date of birth are both numeric, so a numeral in the fixed message would be
    # the echo the
    # requirement forbids arriving by another route.
    assert numerals(decline_message(kind)) == ()


def test_the_dose_decline_names_the_name_and_role_it_keeps() -> None:
    # Req 27.3 is specific: say that it records only the name and the role.
    lowered = decline_message(DeclinedKind.DOSE_OR_FREQUENCY).casefold()
    assert "name" in lowered
    assert "role" in lowered


def test_the_identity_decline_does_not_invite_a_retry() -> None:
    # Req 27.4's values are ones this service must never hold, so the message must not read as
    # "try again
    # differently" — that would elicit the very detail it declined.
    lowered = decline_message(DeclinedKind.IDENTITY_DETAIL).casefold()
    for invitation in ("instead, tell me", "you could give", "try", "resend"):
        assert invitation not in lowered, invitation


# --- Req 27.2 / 4.4: confirmation is a gate -----------------------------


def test_a_draft_starts_unconfirmed() -> None:
    # Req 27.2. Mapping "a brown inhaler every morning" onto a preventer role is an
    # interpretation of
    # someone's health, so it is never assumed silently.
    assert _draft().confirmed is False


def test_an_unconfirmed_draft_refuses_to_produce_a_write_body() -> None:
    # Fail closed, the same shape as the verification ledger: the caller does not get an
    # unconfirmed body
    # back, it gets an exception — a bug report rather than a silent write.
    with pytest.raises(RuntimeError, match="confirm"):
        _draft().write_body()


def test_a_confirmed_draft_produces_a_write_body() -> None:
    # Non-vacuity: a draft that never produced a body would make Req 27.1 impossible.
    body = _draft(confirmed=True).write_body()
    assert body["condition"] == "asthma"


def test_the_write_body_carries_only_name_and_role_per_medication() -> None:
    body = _draft(confirmed=True).write_body()
    medications = body["medications"]
    assert isinstance(medications, list)
    assert set(medications[0]) == {"name", "role"}


def test_the_restatement_names_every_field_being_written() -> None:
    # Req 27.2's substance. A restatement that omitted a field would obtain confirmation for
    # less than the
    # write actually applies.
    restatement = restate_for_confirmation(_draft())
    for expected in ("asthma", "standard", "salbutamol", "reliever", "morning walk"):
        assert expected in restatement, expected


def test_the_restatement_asks_for_confirmation() -> None:
    assert "?" in restate_for_confirmation(_draft())


def test_the_restatement_of_an_empty_draft_is_refused() -> None:
    # Asking a user to confirm nothing would obtain a confirmation that authorises nothing, and
    # the caller
    # has a bug.
    with pytest.raises(ValueError, match="nothing"):
        restate_for_confirmation(ProfileDraft())


# --- Req 27.5: nothing is stored ----------------------------------------


def test_the_draft_is_frozen() -> None:
    # Req 27.5 holds the values for the turn only. A mutable draft is a place to accumulate them
    # across
    # turns, which is how "for the turn" quietly becomes "for the session".
    with pytest.raises(ValidationError):
        _draft().condition = "copd"


def test_confirming_produces_a_new_draft() -> None:
    # The frozen model's consequence: confirmation is a new value rather than a mutation, so
    # nothing
    # accumulates in place.
    draft = _draft()
    confirmed = draft.confirm()
    assert draft.confirmed is False
    assert confirmed.confirmed is True


def test_the_draft_has_nowhere_to_put_an_identity_detail() -> None:
    # Req 27.4's values must not be storable at all, not merely declined at the boundary.
    forbidden_fields = (
        "date_of_birth",
        "dob",
        "name_of_user",
        "email",
        "phone",
        "address",
        "nhs_number",
    )
    for forbidden in forbidden_fields:
        assert forbidden not in ProfileDraft.model_fields, forbidden


# --- Reqs 27.6 / 4.5: a rejection is not an application -----------------


def test_a_limit_rejection_reports_the_limit() -> None:
    # Req 27.6. The user needs to know what the ceiling is, or they cannot tell a transient
    # failure from a
    # permanent one.
    message = limit_rejection_message(field="medications", limit=5)
    assert "medications" in message
    assert "5" in message


def test_a_limit_rejection_never_says_the_change_was_applied() -> None:
    # THE clause. Reporting an unapplied change as applied leaves the user believing their
    # profile is
    # something it is not, which then shapes advice they think was personalised.
    lowered = limit_rejection_message(field="medications", limit=5).casefold()
    for claim in ("applied", "saved", "updated", "recorded", "added"):
        assert claim not in lowered, claim


def test_a_limit_rejection_says_it_was_not_applied() -> None:
    lowered = limit_rejection_message(field="medications", limit=5).casefold()
    assert "not" in lowered


def test_a_rejection_names_the_field_for_req_4_5() -> None:
    assert "sensitivity_level" in limit_rejection_message(
        field="sensitivity_level", limit=1
    )
