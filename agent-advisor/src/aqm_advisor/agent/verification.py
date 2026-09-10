"""The verification ledger and its Strands hook (Requirements 31.5, 34.7).

**A Strands hook cannot veto a response.** `AfterInvocationEvent` carries `result` and `resume`
but no `cancel` field — checked against the installed SDK rather than assumed. So task 10.1's
requirement that "no return path can bypass" the checks cannot be met by a hook that blocks. It
is met a different way:

- the hook RECORDS a verdict at the after-invocation point, which the SDK fires "regardless of
  whether it completed successfully or encountered an error";
- the ledger starts UNVERIFIED and `release` raises rather than returning text;
- so a return path that skips verification has no verdict, and assembly refuses.

Absence of a verdict is treated as failure. That is what makes a deliberately added early return
*safe* rather than merely unlikely: the new path does not have to remember to verify, because it
cannot obtain the text without a verdict already recorded.

Two further rules exist because of how this would otherwise be laundered. A verdict cannot be
overwritten once it has failed — otherwise a retry loop could verify a DIFFERENT text and leave
the ledger positive — and `reset` returns a ledger to unverified between turns, because a stale
positive verdict would publish turn N+1 on turn N's verification, which is the most dangerous
shape available: it looks verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from strands.hooks import AfterInvocationEvent, HookRegistry


@dataclass(frozen=True, slots=True)
class VerificationVerdict:
    """The outcome of the verification checks for one generation.

    `checks` names what actually ran. Req 20.4's audit needs that rather than a bare pass,
    because "verified" with no named check is indistinguishable from a verifier that did nothing
    — and an empty check list is refused at construction for the same reason.
    """

    passed: bool
    checks: tuple[str, ...]
    failures: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse a pass that ran nothing.

        This is the failure that would silently make every other guarantee here vacuous.
        """
        if not self.checks:
            raise ValueError("a verdict must name at least one check that ran")


@dataclass
class VerificationLedger:
    """Per-turn record of whether a generation may be published.

    Mutable and per turn, like the retrieval recorder: the hook writes it from outside the
    pipeline's call stack, which is precisely what makes it unbypassable from inside.
    """

    _verdict: VerificationVerdict | None = field(default=None)

    def record(self, verdict: VerificationVerdict) -> None:
        """Record a verdict, never upgrading a failure into a pass.

        A failed verdict is final for the turn. Allowing a later pass to replace it would let a
        retry verify different text and publish the rejected generation on that verdict.
        """
        if self._verdict is not None and not self._verdict.passed:
            return
        self._verdict = verdict

    def verdict(self) -> VerificationVerdict | None:
        """The recorded verdict, or None when nothing has verified this turn."""
        return self._verdict

    def is_publishable(self) -> bool:
        """True only on a positive recorded verdict. Absence is failure, not neutrality."""
        return self._verdict is not None and self._verdict.passed

    def release(self, text: str) -> str:
        """Return the generated text, or raise if it was never verified.

        The raise is the enforcement point. A caller that forgot to verify does not get
        unverified text back — it gets an exception, which is a bug report rather than a safety
        incident.
        """
        if not self.is_publishable():
            raise RuntimeError(
                "refusing to release a generation that was not verified this turn"
            )
        return text

    def reset(self) -> None:
        """Return to unverified, for the next turn."""
        self._verdict = None


class VerificationHook:
    """Registers the verification checks at the after-invocation point.

    A `HookProvider` in the SDK's sense: `register_hooks` receives the registry and subscribes.
    Registering the checks here rather than calling them from the pipeline is what removes the
    ordinary path's ability to skip them.
    """

    def __init__(
        self,
        ledger: VerificationLedger,
        verify: object,
    ) -> None:
        """Take the ledger to write and the callable that performs the checks.

        `verify` is injected rather than imported so the hook carries no policy of its own — the
        checks live in the domain, and this class only guarantees they run.
        """
        self._ledger = ledger
        self._verify = verify

    def register_hooks(self, registry: HookRegistry) -> None:
        """Subscribe to the after-invocation event."""
        registry.add_callback(AfterInvocationEvent, self._on_after_invocation)

    def _on_after_invocation(self, event: AfterInvocationEvent) -> None:
        """Run the checks and record the verdict.

        Deliberately records a FAILING verdict when the checks themselves raise. A verifier that
        crashed has not established that the text is safe, and the fail-closed ledger then
        withholds it.
        """
        verify = self._verify
        if not callable(verify):
            self._ledger.record(
                VerificationVerdict(
                    passed=False,
                    checks=("verifier_missing",),
                    failures=("no verification callable was configured",),
                )
            )
            return
        try:
            verdict = verify(event)
        except Exception as error:
            self._ledger.record(
                VerificationVerdict(
                    passed=False,
                    checks=("verifier_error",),
                    failures=(type(error).__name__,),
                )
            )
            return
        if isinstance(verdict, VerificationVerdict):
            self._ledger.record(verdict)
            return
        self._ledger.record(
            VerificationVerdict(
                passed=False,
                checks=("verifier_contract",),
                failures=("the verifier did not return a verdict",),
            )
        )


__all__ = ["VerificationHook", "VerificationLedger", "VerificationVerdict"]
