"""Calibration strategies and the registry that resolves them.

Requirement 8.2 selects a strategy BY NAME from a registry. That is a genuine Strategy
pattern rather than a pattern applied for its own sake (§4): the `rh_linear` correction,
the `identity` fallback, and the per-species mapping of Requirement 8.12 are three
independent reasons for the correction to differ, and the coefficients are configuration
rather than a pinned constant. Selecting by name from a registry keeps each one
independently testable and keeps a fourth from becoming another arm in a conditional
(§1 open/closed) — a test asserts the resolution path compares against no strategy
name at all.

Every strategy clamps its result below at 0 (Requirement 8.9), including `identity`,
because that bound is a property of a corrected concentration rather than of one
correction formula.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

DEFAULT_NO_HUMIDITY_STRATEGY = "identity"
"""Requirement 8.6: what to apply when no RH value can be resolved."""

DEFAULT_SPECIES_STRATEGY: dict[str, str] = {"NO2": "identity", "PM25": "rh_linear"}
"""Requirement 8.12 / 8.2: the per-species default mapping.

NO2 gets `identity` because the humidity confounding that motivates the PM2.5
correction is specific to optical particle counting — an electrochemical NO2 cell does
not share it.
"""


class UnknownStrategyError(LookupError):
    """A configured strategy name is not registered (Requirement 8.2)."""

    def __init__(self, requested: str, registered: tuple[str, ...]) -> None:
        """Keep the parts addressable so a caller need not parse the message (§5)."""
        self.requested = requested
        self.registered = registered
        super().__init__(
            f"calibration strategy {requested!r} is not registered; "
            f"available strategies are: {', '.join(registered)}"
        )


@dataclass(frozen=True, slots=True)
class CalibrationDomain:
    """The input range a strategy's coefficients were fitted over.

    Outside it the strategy is still applied — Requirement 8.7 flags rather than
    refuses, because a refused reading serves nothing while an extrapolated one at
    least carries a value and a caveat.

    Bounds are INCLUSIVE: a reading exactly at a published bound is within the fitted
    range, not beyond it.
    """

    reported_min: float
    reported_max: float
    rh_min: float
    rh_max: float

    def __post_init__(self) -> None:
        """Validate the bounds up front (Requirement 8.13)."""
        for label, value in (
            ("reported_min", self.reported_min),
            ("reported_max", self.reported_max),
            ("rh_min", self.rh_min),
            ("rh_max", self.rh_max),
        ):
            if not math.isfinite(value):
                raise ValueError(
                    f"Calibration_Domain bound {label} must be finite, got {value}"
                )
        if self.reported_min >= self.reported_max:
            raise ValueError(
                f"Calibration_Domain reported minimum {self.reported_min} must be "
                f"below its maximum {self.reported_max}"
            )
        if self.rh_min >= self.rh_max:
            raise ValueError(
                f"Calibration_Domain RH minimum {self.rh_min} must be below its "
                f"maximum {self.rh_max}"
            )

    def breaches(self, *, reported: float, rh: float | None) -> tuple[str, ...]:
        """Describe each out-of-domain input and the bound it passed.

        Requirement 8.7's warning must name both, so they are produced together here
        rather than left to a caller to reassemble.

        An ABSENT rh is not a breach: no RH at all is Requirement 8.6's uncalibrated
        path, a different condition from extrapolation, and conflating the two would
        mislabel the reading.
        """
        found: list[str] = []
        if reported < self.reported_min:
            found.append(
                f"reported concentration {reported} below domain minimum "
                f"{self.reported_min}"
            )
        elif reported > self.reported_max:
            found.append(
                f"reported concentration {reported} above domain maximum "
                f"{self.reported_max}"
            )
        if rh is not None:
            if rh < self.rh_min:
                found.append(f"RH {rh} below domain minimum {self.rh_min}")
            elif rh > self.rh_max:
                found.append(f"RH {rh} above domain maximum {self.rh_max}")
        return tuple(found)

    def contains(self, *, reported: float, rh: float | None) -> bool:
        """Whether both inputs sit inside the fitted range."""
        return not self.breaches(reported=reported, rh=rh)


@dataclass(frozen=True, slots=True)
class RhLinearCoefficients:
    """Coefficients for the `rh_linear` correction (Requirement 8.3).

    Configuration rather than constants: the defaults are the form and published
    coefficients of a United States-wide humidity-compensated correction derived for a
    widely deployed class of low-cost optical PM2.5 sensor, adopted as a documented
    STARTING POINT and not as a validated fit for this fleet.
    """

    a: float
    b: float
    c: float

    def __post_init__(self) -> None:
        """Reject non-finite coefficients up front (Requirement 8.13)."""
        for label, value in (("a", self.a), ("b", self.b), ("c", self.c)):
            if not math.isfinite(value):
                raise ValueError(
                    f"rh_linear coefficient {label} must be finite, got {value}"
                )

    @property
    def is_humidity_monotonic(self) -> bool:
        """Whether these coefficients satisfy Requirement 8.8.

        Requirement 8.8 requires a more humid reading to be corrected at least as far
        DOWNWARD, which holds exactly while ``b <= 0``. Requirement 8.13 lists the
        configuration rejections and a positive ``b`` is NOT among them, so this is
        exposed for a caller (a Config_Loader, or a test) to check rather than being
        enforced here — enforcing it would refuse a configuration the spec permits.
        """
        return self.b <= 0.0


DEFAULT_RH_LINEAR_COEFFICIENTS = RhLinearCoefficients(a=0.524, b=-0.0862, c=5.75)
"""Requirement 8.3's published defaults."""

DEFAULT_RH_LINEAR_DOMAIN = CalibrationDomain(
    reported_min=0.0, reported_max=250.0, rh_min=20.0, rh_max=90.0
)
"""Requirement 8.3's declared Calibration_Domain: 0-250 µg/m³, 20-90 percent RH."""


@dataclass(frozen=True, slots=True)
class RhLinearStrategy:
    """The default humidity-aware PM2.5 correction (Requirement 8.3).

    ``corrected = a * reported + b * RH + c``, clamped below at 0 because the linear
    form goes negative for a small concentration at high humidity, and a negative
    concentration is not a physical value.
    """

    coefficients: RhLinearCoefficients = DEFAULT_RH_LINEAR_COEFFICIENTS
    calibration_domain: CalibrationDomain = DEFAULT_RH_LINEAR_DOMAIN
    name: str = "rh_linear"

    @property
    def domain(self) -> CalibrationDomain:
        """The range the coefficients were fitted over."""
        return self.calibration_domain

    def correct(self, *, reported: float, rh: float | None) -> float:
        """Apply the humidity-compensated correction.

        Raises:
            ValueError: if ``rh`` is absent. Requirement 8.6 routes a reading with no
                RH to the configured fallback strategy instead; substituting a zero
                here would fabricate a correction from data this strategy does not
                have, and would silently report it as calibrated.
        """
        if rh is None:
            raise ValueError(
                "rh_linear requires an RH value; a reading without one belongs to the "
                "configured no-humidity fallback strategy (Requirement 8.6)"
            )
        corrected = self.coefficients.a * reported + self.coefficients.b * rh
        return max(0.0, corrected + self.coefficients.c)


@runtime_checkable
class CalibrationStrategy(Protocol):
    """A named correction from a reported concentration to a corrected one."""

    @property
    def name(self) -> str:
        """The registry key configuration refers to this strategy by."""
        ...

    @property
    def domain(self) -> CalibrationDomain:
        """The input range the strategy's coefficients were fitted over."""
        ...

    def correct(self, *, reported: float, rh: float | None) -> float:
        """Return the corrected concentration, never below 0 (Requirement 8.9)."""
        ...


@dataclass(frozen=True, slots=True)
class IdentityStrategy:
    """Passes the reported value through unchanged (Requirements 8.6, 8.12).

    Its domain admits every finite input, because applying no humidity correction
    cannot be an extrapolation of one — flagging `calibrated_extrapolated` here would
    claim a correction that was never made.
    """

    name: str = "identity"

    @property
    def domain(self) -> CalibrationDomain:
        """Every finite input, for the reason in the class docstring."""
        return _IDENTITY_DOMAIN

    def correct(self, *, reported: float, rh: float | None) -> float:
        """Return the reported value, clamped below at 0 (Requirement 8.9)."""
        del rh  # identity is by definition humidity-independent
        return max(0.0, reported)


# Wide enough that no real concentration or RH reads as extrapolated, while staying
# finite so the Requirement 8.13 bound checks still apply to it.
_IDENTITY_DOMAIN = CalibrationDomain(
    reported_min=-1e308, reported_max=1e308, rh_min=-1e308, rh_max=1e308
)


class CalibrationRegistry:
    """Resolves calibration strategies by name (Requirement 8.2).

    Deliberately a plain mapping rather than a module-level global: two configurations
    in one test session must be able to hold different strategy sets without leaking
    into each other.
    """

    def __init__(self) -> None:
        """Start empty; use `with_defaults` for the shipped set."""
        self._strategies: dict[str, CalibrationStrategy] = {}

    @classmethod
    def with_defaults(cls) -> CalibrationRegistry:
        """A registry holding the strategies this service ships."""
        registry = cls()
        registry.register(IdentityStrategy())
        registry.register(RhLinearStrategy())
        return registry

    def register(self, strategy: CalibrationStrategy) -> None:
        """Add a strategy under its own name.

        Raises:
            ValueError: if the name is taken. Replacing silently would make the
                resolved strategy depend on import order, which §2 forbids.
        """
        if strategy.name in self._strategies:
            raise ValueError(
                f"calibration strategy {strategy.name!r} is already registered"
            )
        self._strategies[strategy.name] = strategy

    def resolve(self, name: str) -> CalibrationStrategy:
        """Return the strategy registered under a name.

        Raises:
            UnknownStrategyError: naming the requested value and every registered
                name, which is what Requirement 8.2 asks the Config_Loader to report.
        """
        try:
            return self._strategies[name]
        except KeyError:
            raise UnknownStrategyError(name, self.names()) from None

    def names(self) -> tuple[str, ...]:
        """Every registered name, sorted so error messages are reproducible (§2)."""
        return tuple(sorted(self._strategies))
