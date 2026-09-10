"""The condition-keyed exposure-reduction action registry (Requirement 12.4, 12.5).

A REGISTRY keyed by condition, so a new condition is an entry and not a branch (Req 12.5).
`actions_for` resolves by lookup and compares no condition name; a test walks its AST to assert
that, because the moment a conditional appears, adding a condition means editing code rather
than adding data — and a behavioural test cannot see the difference.

**The registry never chooses a weighting.** Which pollutants matter for a condition arrives from
Service 2's `personalized.weightedFocus`, in the order that service returned it (Req 12.1,
12.2). What lives here is only what a person can DO about an exposure, which is this service's
own contribution and the one thing Service 2 does not provide.

**Every template is checked by other tests in the suite**, and that is deliberate rather than
incidental. Templates ship verbatim in guidance, so a single careless one puts a forbidden claim
into the product by construction rather than by an unlucky generation. Tests assert no template
names a medication with an administration verb, and that none states a numeral — a template
carrying its own number could never be grounded, since no retrieval would ever return it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActionTemplate:
    """One exposure-reduction action.

    `species` is `None` for an action that applies whatever the driving pollutant is, or a
    species name for one that only makes sense for that pollutant — closing a roadside window
    helps with traffic NO2 in a way it does not help with regional ozone.
    """

    text: str
    species: str | None = None


_GENERAL: tuple[ActionTemplate, ...] = (
    ActionTemplate(
        text="Consider moving outdoor exertion to a time when levels are usually lower.",
    ),
    ActionTemplate(text="Where you can, choose a route away from busy roads."),
)

_PARTICULATE: tuple[ActionTemplate, ...] = (
    ActionTemplate(
        text="Keeping windows closed on the side facing traffic can reduce what gets indoors.",
        species="PM25",
    ),
    ActionTemplate(
        text="Recirculating cabin air while driving keeps some of it out of the car.",
        species="PM25",
    ),
)

_NITROGEN_DIOXIDE: tuple[ActionTemplate, ...] = (
    ActionTemplate(
        text="Traffic is the usual source, so a quieter street can make a real difference.",
        species="NO2",
    ),
)

_OZONE: tuple[ActionTemplate, ...] = (
    ActionTemplate(
        text="Ozone usually peaks in the afternoon, so earlier outdoor time is often easier.",
        species="O3",
    ),
)

_PREPAREDNESS: tuple[ActionTemplate, ...] = (
    ActionTemplate(
        text="Having your usual reliever with you is sensible on a day like this.",
    ),
)

_POLLEN: tuple[ActionTemplate, ...] = (
    ActionTemplate(
        text="Rinsing your face and hair after being outdoors removes some of what settled.",
    ),
)

CONDITION_ACTIONS: Mapping[str, tuple[ActionTemplate, ...]] = {
    # Every condition Service 2 can serve has an entry. One it can serve but this registry lacks
    # would
    # fall through to no actions at all, which is why a test enumerates them against Service 2's
    # set.
    "asthma": (*_GENERAL, *_PARTICULATE, *_NITROGEN_DIOXIDE, *_OZONE, *_PREPAREDNESS),
    "copd": (*_GENERAL, *_PARTICULATE, *_NITROGEN_DIOXIDE, *_PREPAREDNESS),
    "allergic_rhinitis": (*_GENERAL, *_POLLEN, *_PARTICULATE),
    # Its OWN entry, not a merge of asthma's and copd's. Req 12.6 says apply the weighting
    # Service 2
    # returned rather than choosing a side, and deriving this list by combining the other two
    # would be
    # this service deciding which side dominates.
    "asthma_copd_overlap": (
        *_GENERAL,
        *_PARTICULATE,
        *_NITROGEN_DIOXIDE,
        *_OZONE,
        *_PREPAREDNESS,
        ActionTemplate(
            text="Both irritant and allergic triggers can matter on the same day, so it is "
            "worth watching how you respond rather than assuming one cause.",
        ),
    ),
    # A user who declared no condition still gets exposure advice. Withholding it would make the
    # absence of a declaration a reason to be told less about the air around them.
    "none_declared": _GENERAL,
}


def actions_for(condition: str | None, driving: str | None) -> tuple[ActionTemplate, ...]:
    """The actions for a condition, narrowed to the driving pollutant when there is one.

    Resolution is a LOOKUP. No condition name appears in this function, so a new condition is an
    entry in the registry above and never a branch here (Req 12.5).

    An unknown condition yields an empty tuple rather than raising. Req 12.4 forbids inventing
    an action outside the mapping, so there is nothing to return — and a profile value this
    service does not recognise is a Service 2 change rather than a user error, so it must not
    break the turn.

    With `driving=None` only the general actions come back: a species-specific action makes a
    claim about which pollutant is doing the damage, and without a driving pollutant that claim
    has no basis.
    """
    templates = CONDITION_ACTIONS.get(condition or "", ())
    return tuple(
        template
        for template in templates
        if template.species is None or template.species == driving
    )


__all__ = ["CONDITION_ACTIONS", "ActionTemplate", "actions_for"]
