"""Every resolved setting must reach something that can act on it.

WHY THIS FILE EXISTS. Three settings shipped that nothing read: `model_credential_path`
(task 16.2), `turn_budget_seconds` (task 17.1) and `streaming_enabled` (task 17.4). Each
looked configured, was covered by a loader test asserting it parsed correctly, and changed
no behaviour whatsoever. The first two I found myself; the third a reviewer found in the
same pull request that fixed the second -- which is what makes this a recurring blind spot
rather than three unrelated bugs, and worth a test.

WHAT IT CHECKS, PRECISELY. That each `AdvisorConfig` field is named by a parameter of some
function in `src/` outside `config/`, or is listed in `AWAITING_COMPOSITION` below. That is
narrow, and the narrowness is the point: it catches the ONE shape a static check can see --
a setting no builder even accepts.

WHAT IT DOES NOT CATCH, stated so nobody mistakes a green run for proof:

* A parameter ACCEPTED and then dropped in the body. `ruff`'s `ARG` rules would see this and
  are not enabled here; the 9 sites in `src/` today are all scripted adapters legitimately
  ignoring a `credential` they have no remote to send it to, so switching `ARG` on is a real
  change with a real cost rather than a free win.
* A parameter accepted and then NEUTRALISED for some values. The turn budget was once read
  as `asyncio.timeout(budget or None)`, which honoured every value but `0` -- where
  `0 or None` is `None` and the turn became unbounded. No static check sees that; only a
  test that varies the value does.

So this file narrows the gap, it does not close it. The loader tests assert a setting
PARSES, this asserts it is PLUMBED, and only a behavioural test asserts it MATTERS.

FIELDS ARE DISCOVERED, NEVER LISTED. `dataclasses.fields` is the source of truth, so a new
setting is checked the moment it is added. A hard-coded list would leave the next addition
unexamined and let this file report success over a config it had never seen.
"""

import ast
import dataclasses
import pathlib

from aqm_advisor.config.loader import AdvisorConfig

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "aqm_advisor"

AWAITING_COMPOSITION: dict[str, str] = {
    "serving_base_url": "task 21 -- passed to the HTTP ServingClient",
    "max_utterance_length": "task 21 -- bounds the inbound utterance at the pipeline edge",
    "locale": "task 21 -- selects the response language",
    "bounds": "task 21 -- InvocationBounds reaches the turn pipeline",
    "guardrail_enabled": "task 21 -- decides whether the guardrail is constructed at all",
    "forbidden_patterns": "task 21 -- Req 30.2's local fast path",
    "red_flag_rules": "task 21 -- the emergency detector",
    "emergency_guidance_fallback": "task 21 -- build_app already takes emergency_guidance",
    "system_prompt_path": "task 21 -- loads the prompt the agent is built with",
    "adapters": "task 21 -- selects real adapter over scripted, per port",
    "log_level": "task 21 -- configures the logger at startup, Req 23.1",
    "jwt_discovery_url": "task 20 -- declared on the runtime, not read by this process",
    "jwt_allowed_clients": "task 20 -- declared on the runtime, not read by this process",
    "jwt_allowed_audience": "task 20 -- declared on the runtime, not read by this process",
}
"""Settings with no consumer YET, each naming the task that will wire it.

This is a LEDGER, not an exemption list: `test_no_awaiting_field_is_already_plumbed` fails
the moment a listed field acquires a consumer, so wiring one forces its line to be deleted
here. Without that second direction an allowlist silently becomes permanent, and the test it
guards decays into a rubber stamp.

That nearly every setting sits here is the honest state of the service: there is no
composition root yet, so nothing constructs an `AdvisorConfig` and hands its parts to
anything. Read this as task 21's checklist -- when that task lands and this dict is not
empty, the leftovers are what it forgot.

The three `jwt_*` entries are a DIFFERENT case and do not belong to task 21. AgentCore's own
JWT authorizer validates the token before a request reaches this process, so these are
rendered into the runtime's deployment configuration rather than read by application code.
They stay listed because a future reader would otherwise reasonably wire them into an
in-process check that must not exist.
"""


def _accepted_parameter_names() -> dict[str, str]:
    """Every parameter name declared by a function in `src/`, outside `config/`.

    Excludes `config/` because the loader necessarily names each setting when it builds the
    object; counting that would make every field self-satisfying and this file vacuous.
    """
    found: dict[str, str] = {}
    for path in sorted(_SRC.rglob("*.py")):
        if "config" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                found.setdefault(arg.arg, f"{path.relative_to(_SRC)}:{node.name}")
    return found


def _field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(AdvisorConfig)}


def test_every_setting_either_reaches_a_builder_or_is_recorded_as_awaiting_one() -> None:
    accepted = _accepted_parameter_names()
    orphans = sorted(
        name
        for name in _field_names()
        if name not in accepted and name not in AWAITING_COMPOSITION
    )
    assert not orphans, (
        f"settings that no builder accepts, and that are not recorded as awaiting one: "
        f"{orphans}. Either plumb the setting through to something that acts on it, or add "
        f"it to AWAITING_COMPOSITION naming the task that will. A setting read by nothing "
        f"is not configuration, it is a comment that looks like configuration."
    )


def test_no_awaiting_field_is_already_plumbed() -> None:
    # The staleness direction, which is what stops the ledger above becoming a permanent
    # exemption list. Wiring a listed setting must FAIL here and force its line's removal.
    accepted = _accepted_parameter_names()
    stale = sorted(name for name in AWAITING_COMPOSITION if name in accepted)
    assert not stale, (
        f"these settings now have a consumer and must be deleted from "
        f"AWAITING_COMPOSITION: {stale}"
    )


def test_every_awaiting_entry_names_a_real_setting() -> None:
    # Catches a renamed or removed field leaving a phantom entry behind, which would quietly
    # shrink what the first test examines.
    phantom = sorted(name for name in AWAITING_COMPOSITION if name not in _field_names())
    assert not phantom, (
        f"AWAITING_COMPOSITION names settings that are no longer fields: {phantom}"
    )


def test_the_detector_finds_the_parameters_it_is_supposed_to_find() -> None:
    # A self-check, because every assertion above passes trivially if the AST walk returns
    # nothing. The turn budget is the case this file was written for: a real setting that
    # `build_app` really does accept.
    accepted = _accepted_parameter_names()
    assert len(accepted) > 50, f"the parameter walk found only {len(accepted)} names"
    assert "turn_budget_seconds" in accepted
    assert "run_turn" in accepted, "expected build_app's injected turn runner"


def test_the_awaiting_ledger_does_not_cover_the_whole_config() -> None:
    # If every setting were listed, the first test would assert nothing at all. This does not
    # demand progress, only that SOMETHING is genuinely plumbed -- so the file cannot decay
    # into a ledger that exempts the entire config while still reporting green.
    plumbed = _field_names() - set(AWAITING_COMPOSITION)
    assert plumbed, "every setting is listed as awaiting a consumer; the first test is vacuous"
