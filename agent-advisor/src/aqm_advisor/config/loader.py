"""Fail-fast configuration resolution and validation (Requirements 23.1 to 23.6, and 34.9).

**FAIL-FAST MEANS "NEVER HALF-START", NOT "STOP AT THE FIRST ERROR"** (Req 23.2). Every value is
validated and every violation reported, because an operator fixing one setting per deploy is
exactly what accumulation avoids. The structure is deliberately mirrored from Service 2's loader
rather than imported: the practices forbid importing across service directories until a shared
contract package is specced.

**THE SETTINGS OBJECTS VALIDATE THEMSELVES, so this module does NOT duplicate their range
checks.** `InvocationBounds.__post_init__` already refuses a non-positive value and names the
field. Re-checking the same range here produced TWO messages for one invalid value in Service 2
— which Req 23.2's "one message per invalid value" forbids, and which a test caught there. So
the loader CONSTRUCTS the object and surfaces what it raises, verbatim: the settings messages
already name the field and the constraint, and wrapping them adds nothing.

**A SECRET IS RESOLVED BUT NEVER RENDERED** (Req 23.4). The loader reports whether a required
credential resolved, naming the CONFIGURATION KEY that supplies it — never the value. It never
reads a credential file, only asks an injected predicate whether the path exists. A loader that
cannot see a secret cannot log one.

**ZERO IS NOT "UNLIMITED"** (Req 22.1c). `Limits` is a `total=False` TypedDict validating each
PRESENT key as a positive integer, so a zero raises rather than lifting the cap — a bound that
appears configured and is not. An unset ceiling is OMITTED from the mapping, and a configured
zero is rejected here, which is the only place the difference between "no limit" and "invalid"
can be explained to the operator who wrote it.

**RESOLVING IS NOT APPLYING.** This module configures no logging, constructs no adapter, opens
no socket and calls nothing on the Model_Port or the Serving_Client. Req 23.2 requires
validation to complete BEFORE any such call, and a loader with a side effect has half-started
before its own validation finished. `env` is a parameter rather than a read of `os.environ` for
the same reason.

**WHAT IS DELIBERATELY NOT CONFIGURABLE HERE.** Three values look like settings and belong to
somebody else:

- The **history window maximum**. Reqs 3.2 and 3.3 make it Service 2's, learned from that
  service's rejection. A key here would re-declare another service's constant, and the copy that
  drifted would be this one.
- **`advisoryScope` and `disclaimer`**. Assumption A8 and Req 21.9 give them no local copy
  and no fallback. Only `emergencyGuidance` gets a configured fallback, under A8a.
- The **profile and diary write limits** of Req 27.6. That clause reports the limit SERVICE 2
  rejected with; it does not create one here.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aqm_advisor.agent.bounds import (
    DEFAULT_MODEL_INVOCATIONS,
    InvocationBounds,
)
from aqm_advisor.domain.models import DEFAULT_MAX_UTTERANCE_LENGTH
from aqm_advisor.observability.logging import PERMITTED_LOG_LEVELS

DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
"""Req 6.4's stated default."""

DEFAULT_SERVING_CALLS = 4
DEFAULT_PRIOR_TURNS = 6
DEFAULT_LOG_LEVEL = "info"
DEFAULT_LOCALE = "en"
DEFAULT_TURN_BUDGET_SECONDS = 60

_ENV_PREFIX = "AQM_ADVISOR_"


class ConfigError(ValueError):
    """One or more configuration values were rejected."""

    def __init__(self, problems: Sequence[str]) -> None:
        """Carry every problem, not just the first (Req 23.2)."""
        super().__init__("; ".join(problems))
        self.problems = tuple(problems)


@dataclass
class _Problems:
    """Accumulator, so every invalid value is reported (Req 23.2)."""

    messages: list[str] = field(default_factory=list)

    def add(self, value: str, constraint: str) -> None:
        """Record one invalid value and the constraint it violated."""
        self.messages.append(f"{value}: {constraint}")


REGISTERED_ADAPTERS: Mapping[str, tuple[str, ...]] = {
    "serving_client": ("scripted", "http"),
    "guardrail_checker": ("local", "bedrock"),
    "advice_audit_store": ("memory", "dynamodb"),
    "association_trigger": ("recording",),
    "model": ("scripted", "bedrock"),
    "clock": ("system", "fixed"),
}
"""Req 23.6: each port's registered adapter names, first entry being the default.

The registry is the source of BOTH the default and the validation set, so adding an adapter is
one entry rather than a new branch plus a new default plus a new check that could disagree with
each other.

ONLY NAMES THAT RESOLVE ARE REGISTERED. A first version listed the cloud names for every port
and rejected the unimplemented ones with a second "registered but not implemented" tier, which a
test caught as incoherent: a registry that advertises a name the loader then refuses is telling
the operator two contradictory things, and Req 23.6's whole point is that the registry IS the
answer to "what may I select". Task 16 adds each cloud name when it adds the adapter behind it,
which is the same one-entry change this requirement asks for.

`bedrock` appears for `model` and `guardrail_checker` alone because Reqs 6.6 and 34.9 make those
two selections configuration in their own right, and the loader's credential and identifier
validation for them is live and tested now — the name is doing work at load time even before
task 16 builds what sits behind it.

The first entry is the offline one in every case. That is not a preference: the offline suite
must pass with no credentials and no network, so a cloud default would make the documented test
command fail on a clean checkout.
"""

_ADAPTERS_NEEDING_A_CREDENTIAL: Mapping[str, tuple[str, str]] = {
    "model": ("model_credential_path", "the configured Model_Port adapter"),
}
"""Req 6.6 and 23.4: which adapter selection makes a credential REQUIRED.

Keyed by adapter rather than checked unconditionally, because the scripted model needs none and
demanding one would make the offline suite unstartable. "Validate every value" does not mean
"demand every value".
"""

_CREDENTIAL_EXEMPT_NAMES: Mapping[str, frozenset[str]] = {
    "model": frozenset({"scripted"}),
}

_RECOGNIZED_KEYS: Mapping[str, frozenset[str]] = {
    "turn": frozenset({"max_utterance_length", "locale"}),
    "serving": frozenset({"serving_base_url", "request_timeout_seconds"}),
    "model": frozenset(
        {
            "model_id",
            "model_region",
            "model_temperature",
            "model_max_output_tokens",
            "model_credential_path",
        }
    ),
    "bounds": frozenset(
        {
            "max_model_invocations",
            "max_output_tokens",
            "max_total_tokens",
            "max_serving_calls",
            "max_prior_turns",
        }
    ),
    "guardrail": frozenset(
        {
            "guardrail_enabled",
            "guardrail_identifier",
            "guardrail_version",
            "forbidden_patterns",
        }
    ),
    "escalation": frozenset({"red_flag_rules"}),
    "envelope": frozenset({"emergency_guidance_fallback"}),
    "prompt": frozenset({"system_prompt_path"}),
    "adapters": frozenset(REGISTERED_ADAPTERS),
    "observability": frozenset({"log_level"}),
    "agentcore": frozenset(
        {
            "streaming_enabled",
            "turn_budget_seconds",
            "jwt_discovery_url",
            "jwt_allowed_clients",
            "jwt_allowed_audience",
        }
    ),
}

RECOGNIZED_KEYS: frozenset[str] = frozenset(
    key for keys in _RECOGNIZED_KEYS.values() for key in keys
)
"""Kept DERIVED rather than written twice, so the two cannot drift.

Written twice, the copy that drifted would be the one rejecting a key the other recognises — and
an operator would be told a key is unrecognised by the same loader that resolves it.
"""

_SCALARS: Mapping[str, tuple[str, object]] = {
    # turn
    "max_utterance_length": (
        f"{_ENV_PREFIX}MAX_UTTERANCE_LENGTH",
        DEFAULT_MAX_UTTERANCE_LENGTH,
    ),
    "locale": (f"{_ENV_PREFIX}LOCALE", DEFAULT_LOCALE),
    # serving
    "serving_base_url": (f"{_ENV_PREFIX}SERVING_BASE_URL", None),
    "request_timeout_seconds": (
        f"{_ENV_PREFIX}REQUEST_TIMEOUT_SECONDS",
        DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ),
    # model
    "model_id": (f"{_ENV_PREFIX}MODEL_ID", None),
    "model_region": (f"{_ENV_PREFIX}MODEL_REGION", None),
    "model_temperature": (f"{_ENV_PREFIX}MODEL_TEMPERATURE", 0.0),
    "model_max_output_tokens": (f"{_ENV_PREFIX}MODEL_MAX_OUTPUT_TOKENS", None),
    "model_credential_path": (f"{_ENV_PREFIX}MODEL_CREDENTIAL_PATH", None),
    # bounds
    "max_model_invocations": (
        f"{_ENV_PREFIX}MAX_MODEL_INVOCATIONS",
        DEFAULT_MODEL_INVOCATIONS,
    ),
    "max_output_tokens": (f"{_ENV_PREFIX}MAX_OUTPUT_TOKENS", None),
    "max_total_tokens": (f"{_ENV_PREFIX}MAX_TOTAL_TOKENS", None),
    "max_serving_calls": (f"{_ENV_PREFIX}MAX_SERVING_CALLS", DEFAULT_SERVING_CALLS),
    "max_prior_turns": (f"{_ENV_PREFIX}MAX_PRIOR_TURNS", DEFAULT_PRIOR_TURNS),
    # guardrail
    "guardrail_enabled": (f"{_ENV_PREFIX}GUARDRAIL_ENABLED", False),
    "guardrail_identifier": (f"{_ENV_PREFIX}GUARDRAIL_IDENTIFIER", None),
    "guardrail_version": (f"{_ENV_PREFIX}GUARDRAIL_VERSION", None),
    # envelope
    "emergency_guidance_fallback": (f"{_ENV_PREFIX}EMERGENCY_GUIDANCE_FALLBACK", None),
    # prompt
    "system_prompt_path": (f"{_ENV_PREFIX}SYSTEM_PROMPT_PATH", None),
    # observability
    "log_level": (f"{_ENV_PREFIX}LOG_LEVEL", DEFAULT_LOG_LEVEL),
    # agentcore
    "streaming_enabled": (f"{_ENV_PREFIX}STREAMING_ENABLED", False),
    "turn_budget_seconds": (f"{_ENV_PREFIX}TURN_BUDGET_SECONDS", DEFAULT_TURN_BUDGET_SECONDS),
    "jwt_discovery_url": (f"{_ENV_PREFIX}JWT_DISCOVERY_URL", None),
    # NOT an AQM_ADVISOR_ name, deliberately. Req 32.14 requires this service's inbound
    # authorizer to
    # accept the SAME audience Service 2's does, and for a Cognito JWT that audience IS the app
    # client
    # id. Two separately-named variables for one app client is precisely how they drift apart —
    # and
    # the requirement notes the drift then fails at Service 2, "the hardest place to attribute
    # it".
    # One shared variable makes the disagreement impossible to express rather than merely
    # detectable.
    # `tests/unit/test_audience_agreement.py` reads Service 2's wiring from disk and pins the
    # match.
    "jwt_allowed_audience": ("AQM_COGNITO_CLIENT_ID", None),
}
"""Each key's environment variable and default. `key -> (env_var, default)`.

The environment variable name is WRITTEN, never derived from the key. Deriving it reads as
tidier and then breaks the first time a key and its variable legitimately differ, at which point
half the table follows one rule and half another. The default's TYPE drives coercion, so adding
a scalar needs only a default of the right type.

Sequence-valued keys (`forbidden_patterns`, `red_flag_rules`, `jwt_allowed_clients`) are absent
deliberately: an environment variable would need a separator convention this loader does not
have, and inventing one here would make two settings parse differently. They are file-only.
"""

_SEQUENCES: frozenset[str] = frozenset(
    {"forbidden_patterns", "red_flag_rules", "jwt_allowed_clients"}
)


def read_config_file(path: str | None) -> dict[str, Any]:
    """Read the JSON configuration file, or return an empty mapping when there is none.

    Raises IMMEDIATELY rather than accumulating, because a file that cannot be parsed yields no
    values to accumulate over. There is deliberately no fallback to defaults: falling back would
    come up with settings nobody chose, which is worse than not coming up, and a named file that
    is absent is a deploy error rather than an absence.

    Names the PATH but never the CONTENTS. The path is the operator's own and naming it is how
    they find the file; its contents may hold a credential, so a parse error never quotes the
    text it failed on.

    Raises:
        ConfigError: when the path is unreadable, unparseable, or does not hold a JSON object.
    """
    if path is None:
        return {}
    candidate = Path(path)
    try:
        text = candidate.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(
            [f"config file {path!r}: unreadable ({type(error).__name__})"]
        ) from error
    try:
        parsed = json.loads(text)
    except ValueError as error:
        raise ConfigError(
            [f"config file {path!r}: unparseable ({type(error).__name__})"]
        ) from error
    if not isinstance(parsed, dict):
        raise ConfigError([f"config file {path!r}: unparseable (expected a JSON object)"])
    return parsed


def _coerce(raw: object, default: object) -> object:
    """Coerce a raw value to the default's type.

    `bool` is checked BEFORE `int` because `bool` is a subclass of `int` in Python: the other
    order would send every flag through `int()` and turn "true" into a coercion error.

    A non-finite float is rejected. `float("nan")` and `float("inf")` parse without complaint
    and are never valid configuration — a NaN temperature would make every comparison against it
    false.

    Raises:
        ValueError: on a value the default's type cannot accept.
    """
    if default is None:
        return str(raw)
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        lowered = str(raw).strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
        raise ValueError(f"not a boolean: {raw!r}")
    if isinstance(default, int):
        return int(str(raw).strip())
    if isinstance(default, float):
        value = float(str(raw).strip())
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"not finite: {raw!r}")
        return value
    return str(raw)


def _expected(default: object) -> str:
    """The type name a rejection message should name."""
    if default is None:
        return "a string"
    return type(default).__name__


def _category_of(key: str) -> str | None:
    """The category a recognised key belongs to."""
    for category, keys in sorted(_RECOGNIZED_KEYS.items()):
        if key in keys:
            return category
    return None


def _nearest_category(key: str) -> str:
    """The recognised keys of the category a key most plausibly belongs to (Req 23.3).

    Resolves by CLOSEST WHOLE KEY first, then by the first underscore-delimited token, then by
    giving up and listing everything.

    The whole-key step is not decoration. A first version matched on the token head alone and
    sent `model_regoin` to the `adapters` category, because that category holds a port literally
    named `model` and sorted ahead of the `model` category — so an operator who mistyped
    `model_region` was shown the adapter names and not the key they wanted. `guardrail_checker`
    versus the `guardrail_*` settings is the same collision, and any port named after a settings
    prefix would join them. Comparing the whole key resolves it the way a reader would:
    `model_regoin` is one letter from `model_region`.

    Falls back to every category when the key resembles nothing, which is honest — somebody who
    invented a key wholesale needs to see the shape of what exists, where one confidently wrong
    category sends them looking in the wrong place.

    Sorted throughout, so the message is reproducible and a test can assert on it.
    """
    close = difflib.get_close_matches(key, sorted(RECOGNIZED_KEYS), n=1, cutoff=0.6)
    if close:
        category = _category_of(close[0])
        if category is not None:
            return f"{category}: {', '.join(sorted(_RECOGNIZED_KEYS[category]))}"
    head = key.split("_")[0]
    for category, keys in sorted(_RECOGNIZED_KEYS.items()):
        if any(head == known.split("_")[0] for known in keys):
            return f"{category}: {', '.join(sorted(keys))}"
    return "; ".join(
        f"{category}: {', '.join(sorted(keys))}"
        for category, keys in sorted(_RECOGNIZED_KEYS.items())
    )


def _resolve_scalars(
    env: Mapping[str, str], data: Mapping[str, Any], problems: _Problems
) -> dict[str, Any]:
    """Resolve every scalar as environment, then file, then default (Req 23.1)."""
    resolved: dict[str, Any] = {}
    for key, (env_var, default) in _SCALARS.items():
        raw: object
        if env_var in env:
            raw = env[env_var]
        elif key in data:
            raw = data[key]
        else:
            resolved[key] = default
            continue
        try:
            resolved[key] = _coerce(raw, default)
        except (TypeError, ValueError):
            problems.add(f"{key}={raw!r}", f"expected {_expected(default)}")
            resolved[key] = default
    for key in _SEQUENCES:
        supplied = data.get(key)
        if supplied is None:
            resolved[key] = ()
        elif isinstance(supplied, list) and all(isinstance(item, str) for item in supplied):
            resolved[key] = tuple(supplied)
        else:
            problems.add(f"{key}={supplied!r}", "expected a list of strings")
            resolved[key] = ()
    resolved["adapters"] = {
        port: str(
            env.get(f"{_ENV_PREFIX}{port.upper()}")
            or data.get(port)
            or REGISTERED_ADAPTERS[port][0]
        )
        for port in REGISTERED_ADAPTERS
    }
    return resolved


def _validate_url(
    key: str, supplied: object, problems: _Problems, *, required: bool
) -> None:
    """Validate one URL setting (Reqs 23.5, 32.7).

    REJECTS EMBEDDED CREDENTIALS. `urlparse` accepts `https://user:pass@host` happily, and a
    review found that such a URL then reached `redacted()` verbatim — so the moment the
    composition root logs the startup line, the operator's basic-auth secret is in the log.
    Refusing the whole shape is better than stripping it for the log: this service authenticates
    to Service 2 by forwarding the caller's credential (assumption A4a), so basic-auth in the
    base URL is not a configuration it has any use for, and accepting it silently would leave a
    credential somewhere no test looks.
    """
    if supplied is None or not str(supplied).strip():
        if required:
            problems.add(
                key,
                "required; the service cannot retrieve anything without Service 2's base URL",
            )
        return
    text = str(supplied).strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        problems.add(f"{key}={text!r}", "expected an absolute http or https URL")
        return
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        problems.add(
            key,
            "must not embed a credential; this service forwards the caller's credential "
            "instead, and an embedded one would reach the startup log",
        )


def _validate_serving(resolved: Mapping[str, Any], problems: _Problems) -> None:
    """Req 23.5: the Service 2 base URL is required and must be usable."""
    _validate_url("serving_base_url", resolved.get("serving_base_url"), problems, required=True)
    _validate_url(
        "jwt_discovery_url", resolved.get("jwt_discovery_url"), problems, required=False
    )


_POSITIVE_SCALARS: tuple[str, ...] = (
    "request_timeout_seconds",
    "turn_budget_seconds",
    "max_utterance_length",
    "model_max_output_tokens",
)
"""Scalars this loader owns outright, which nothing downstream range-checks.

A review found every one of these resolving clean at zero or negative. They are NOT in
`InvocationBounds`, so nothing else refuses them, and each has a concrete failure: a zero
`max_utterance_length` rejects every request (Req 1.5), and a non-positive timeout or turn
budget is not a duration. A service that starts healthy and answers nothing is the outcome Req
23.2 exists to prevent, so these are validated HERE — which is also Service 2's rule that
validation lives wherever the value lives.

`model_max_output_tokens` is the sharp one: its sibling `max_output_tokens` is refused at zero
by `InvocationBounds`, so the asymmetry made one of two adjacent ceilings checked and the other
not.
"""


def _validate_budget_covers_a_request(
    resolved: Mapping[str, Any], problems: _Problems
) -> None:
    """The turn budget must be at least one request timeout (Reqs 32.13, 32.4a).

    A CROSS-FIELD RULE, and the only one here. Both values validate fine alone, which is exactly
    why
    this was missing: nothing looked at their RELATIONSHIP. A turn budget SHORTER than a single
    downstream timeout guarantees the budget expires while a retrieval is still in flight — and
    the
    entrypoint runs the turn on a worker thread that `asyncio.timeout` cannot kill, because
    Python
    cannot kill a thread. So every such turn abandons a running thread.

    Repeated abandonment fills the executor, and a saturated pool makes later turns answer
    degraded
    WITHOUT EVER EXECUTING while the container still reports healthy — a silent liveness
    collapse
    that no health check notices. A review found the accumulation; this refuses the
    configuration
    that makes it routine instead of rare.
    """
    budget = resolved.get("turn_budget_seconds")
    timeout = resolved.get("request_timeout_seconds")
    if not isinstance(budget, int) or not isinstance(timeout, int):
        return  # each is reported on its own by _validate_scalars
    if budget < timeout:
        problems.add(
            "turn_budget_seconds",
            f"must be at least request_timeout_seconds ({timeout}); a budget of {budget} "
            "would expire while a retrieval is still running, abandoning a worker thread "
            "that cannot be cancelled",
        )


def _validate_scalars(resolved: Mapping[str, Any], problems: _Problems) -> None:
    """Req 23.2: validate every resolved value this loader owns."""
    for key in _POSITIVE_SCALARS:
        value = resolved.get(key)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            problems.add(f"{key}={value!r}", "expected a positive integer")
    if not str(resolved.get("locale") or "").strip():
        problems.add("locale", "must not be blank")
    temperature = resolved.get("model_temperature")
    if isinstance(temperature, float) and not 0.0 <= temperature <= 2.0:
        problems.add(
            f"model_temperature={temperature!r}",
            "expected a value between 0.0 and 2.0",
        )


def _validate_registries(resolved: Mapping[str, Any], problems: _Problems) -> None:
    """Req 23.6: every adapter name must be registered.

    One tier, not two. The rejection lists the registered names inline, because an operator who
    mistyped one needs the alternatives in the same breath as the refusal.
    """
    for port, supplied in sorted(resolved["adapters"].items()):
        registered = REGISTERED_ADAPTERS[port]
        if supplied not in registered:
            problems.add(
                f"{port}={supplied!r}",
                f"not registered; registered adapters are {', '.join(registered)}",
            )


def _validate_log_level(resolved: Mapping[str, Any], problems: _Problems) -> None:
    """Req 24.2's level bands. The permitted set is the logging module's, not a second copy."""
    level = str(resolved["log_level"]).strip().lower()
    if level not in PERMITTED_LOG_LEVELS:
        problems.add(
            f"log_level={resolved['log_level']!r}",
            f"expected one of {', '.join(PERMITTED_LOG_LEVELS)}",
        )


def _validate_guardrail(resolved: Mapping[str, Any], problems: _Problems) -> None:
    """Req 34.9: enforcement enabled with no identifier must refuse to start.

    Of the three states this can be in, enabled-without-an-identifier is the worst: the operator
    believes output is checked and it is not. Disabled is honest, and Req 34.5 keeps the local
    Forbidden_Claim check running either way, so disabled does not mean unchecked.
    """
    if resolved["guardrail_enabled"] and not resolved.get("guardrail_identifier"):
        problems.add(
            "guardrail_identifier",
            "required when guardrail_enabled is true (Requirement 34.9)",
        )


def _validate_credentials(
    resolved: Mapping[str, Any], problems: _Problems, exists: object
) -> None:
    """Req 6.6 and 23.4: a credential the SELECTED adapter needs must resolve.

    Reports the configuration KEY, never the value, and never reads the file — it only asks
    whether the path resolves, so no key material enters this process.
    """
    checker = exists if callable(exists) else Path.exists
    for port, (key, needed_by) in sorted(_ADAPTERS_NEEDING_A_CREDENTIAL.items()):
        if resolved["adapters"][port] in _CREDENTIAL_EXEMPT_NAMES.get(port, frozenset()):
            continue
        supplied = resolved.get(key)
        if not supplied:
            problems.add(key, f"required by {needed_by} but not configured")
        elif not checker(Path(str(supplied))):
            problems.add(key, f"required by {needed_by} but does not resolve")


def _build_bounds(resolved: Mapping[str, Any], problems: _Problems) -> InvocationBounds | None:
    """Construct the bounds, surfacing their own messages verbatim (Req 22.1, 22.1c).

    `InvocationBounds` already refuses a non-positive value and names the field, so the range is
    NOT re-checked here: doing so produced two messages for one value in Service 2's loader. Its
    messages already name the field and the constraint, so they are surfaced as they are rather
    than wrapped in a prefix that adds nothing.
    """
    try:
        return InvocationBounds(
            model_invocations=int(resolved["max_model_invocations"]),
            output_tokens=_optional_int(resolved["max_output_tokens"]),
            total_tokens=_optional_int(resolved["max_total_tokens"]),
            serving_calls=int(resolved["max_serving_calls"]),
            prior_turns=int(resolved["max_prior_turns"]),
        )
    except (TypeError, ValueError) as error:
        problems.add("bounds", str(error))
        return None


def _optional_int(raw: object) -> int | None:
    """An absent ceiling stays absent (Req 22.1c).

    Returning 0 for an unset ceiling would put a zero into `Limits`, where each present key is
    validated as a positive integer — so the zero raises rather than lifting the cap, and the
    bound appears configured while being nothing of the kind.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise ValueError(f"not an integer: {raw!r}")
    if isinstance(raw, int):
        return raw
    return int(str(raw).strip())


@dataclass(frozen=True, slots=True)
class AdvisorConfig:
    """Every resolved, validated setting.

    Frozen, because configuration is decided once at startup.
    """

    serving_base_url: str
    request_timeout_seconds: int
    max_utterance_length: int
    locale: str
    model_id: str | None
    model_region: str | None
    model_temperature: float
    model_max_output_tokens: int | None
    model_credential_path: str | None
    bounds: InvocationBounds
    guardrail_enabled: bool
    guardrail_identifier: str | None
    guardrail_version: str | None
    forbidden_patterns: tuple[str, ...]
    red_flag_rules: tuple[str, ...]
    emergency_guidance_fallback: str | None
    system_prompt_path: str | None
    adapters: Mapping[str, str]
    log_level: str
    streaming_enabled: bool
    turn_budget_seconds: int
    jwt_discovery_url: str | None
    jwt_allowed_clients: tuple[str, ...]
    jwt_allowed_audience: str | None

    def redacted(self) -> dict[str, object]:
        """The one startup log line's payload (Req 23.1), with no credential in it.

        A credential PATH is not itself a secret, but it is a map to one, and Req 23.4's "report
        a credential path as whether it resolved" is cheapest to honour by never rendering
        anything credential-shaped. So the path appears only as a boolean.

        Every key here is one the logger's own redactor considers non-sensitive, asserted by
        test: a key it redacted would reach the log as REDACTED, and the single startup line Req
        23.1 requires would say nothing.
        """
        return {
            "servingBaseUrl": self.serving_base_url,
            "requestTimeoutSeconds": self.request_timeout_seconds,
            "maxUtteranceLength": self.max_utterance_length,
            "locale": self.locale,
            "modelId": self.model_id,
            "modelRegion": self.model_region,
            "modelTemperature": self.model_temperature,
            "modelMaxOutputTokens": self.model_max_output_tokens,
            "modelCredentialConfigured": self.model_credential_path is not None,
            "maxModelInvocations": self.bounds.model_invocations,
            "maxOutputTokens": self.bounds.output_tokens,
            "maxTotalTokens": self.bounds.total_tokens,
            "maxServingCalls": self.bounds.serving_calls,
            "maxPriorTurns": self.bounds.prior_turns,
            "enforcedCeilings": sorted(self.bounds.as_limits()),
            "guardrailEnabled": self.guardrail_enabled,
            "guardrailIdentifier": self.guardrail_identifier,
            "guardrailVersion": self.guardrail_version,
            "forbiddenPatternsConfigured": len(self.forbidden_patterns),
            "redFlagRulesConfigured": len(self.red_flag_rules),
            "emergencyGuidanceFallbackConfigured": self.emergency_guidance_fallback is not None,
            "systemPromptPath": self.system_prompt_path,
            "adapters": dict(sorted(self.adapters.items())),
            "logLevel": self.log_level,
            "streamingEnabled": self.streaming_enabled,
            "turnBudgetSeconds": self.turn_budget_seconds,
            "jwtDiscoveryUrl": self.jwt_discovery_url,
            "jwtAllowedClients": list(self.jwt_allowed_clients),
            "jwtAllowedAudience": self.jwt_allowed_audience,
        }


def resolve_and_validate(
    env: Mapping[str, str],
    file_data: Mapping[str, Any] | None = None,
    *,
    credential_exists: object = None,
) -> AdvisorConfig:
    """Resolve every value, validate all, and return the config or raise (Reqs 23.1 to 23.6).

    `env` and `file_data` are injected rather than read here, which keeps this pure: a test can
    supply any environment, and the process's real one cannot leak into one. `credential_exists`
    is injected for the same reason and defaults to `Path.exists`.

    Raises:
        ConfigError: carrying EVERY problem found, not the first. Nothing is returned when
        anything is
            invalid — a partly valid config would hand the service settings nobody chose.
    """
    data = dict(file_data or {})
    problems = _Problems()

    for key in sorted(set(data) - RECOGNIZED_KEYS):
        problems.add(
            f"configuration key {key!r}",
            f"not recognized; recognized keys are {_nearest_category(key)}",
        )

    resolved = _resolve_scalars(env, data, problems)

    _validate_serving(resolved, problems)
    _validate_scalars(resolved, problems)
    _validate_budget_covers_a_request(resolved, problems)
    _validate_registries(resolved, problems)
    _validate_log_level(resolved, problems)
    _validate_guardrail(resolved, problems)
    _validate_credentials(resolved, problems, credential_exists)
    bounds = _build_bounds(resolved, problems)

    if problems.messages:
        raise ConfigError(problems.messages)
    assert bounds is not None
    return AdvisorConfig(
        serving_base_url=str(resolved["serving_base_url"]).strip(),
        request_timeout_seconds=int(resolved["request_timeout_seconds"]),
        max_utterance_length=int(resolved["max_utterance_length"]),
        locale=str(resolved["locale"]),
        model_id=resolved["model_id"],
        model_region=resolved["model_region"],
        model_temperature=float(resolved["model_temperature"]),
        model_max_output_tokens=_optional_int(resolved["model_max_output_tokens"]),
        model_credential_path=resolved["model_credential_path"],
        bounds=bounds,
        guardrail_enabled=bool(resolved["guardrail_enabled"]),
        guardrail_identifier=resolved["guardrail_identifier"],
        guardrail_version=resolved["guardrail_version"],
        forbidden_patterns=tuple(resolved["forbidden_patterns"]),
        red_flag_rules=tuple(resolved["red_flag_rules"]),
        emergency_guidance_fallback=resolved["emergency_guidance_fallback"],
        system_prompt_path=resolved["system_prompt_path"],
        adapters=dict(resolved["adapters"]),
        log_level=str(resolved["log_level"]).strip().lower(),
        streaming_enabled=bool(resolved["streaming_enabled"]),
        turn_budget_seconds=int(resolved["turn_budget_seconds"]),
        jwt_discovery_url=resolved["jwt_discovery_url"],
        jwt_allowed_clients=tuple(resolved["jwt_allowed_clients"]),
        jwt_allowed_audience=resolved["jwt_allowed_audience"],
    )


__all__ = [
    "DEFAULT_REQUEST_TIMEOUT_SECONDS",
    "RECOGNIZED_KEYS",
    "REGISTERED_ADAPTERS",
    "AdvisorConfig",
    "ConfigError",
    "read_config_file",
    "resolve_and_validate",
]
