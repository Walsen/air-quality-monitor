"""Tests for the fail-fast configuration loader (task 15.1).

Validates Reqs 23.1 to 23.6 and 34.9.

**Fail-fast means "never half-start", NOT "stop at the first error".** Req 23.2 requires one
message per invalid value, so every validator runs and every violation is reported before
anything raises. An operator fixing one setting per deploy is exactly what that avoids. Service
2's loader states the same rule and this mirrors its shape deliberately — mirrored rather than
imported, because the practices forbid importing across service directories until a shared
contract package is specced.

**The loader does NOT re-check bounds an object already owns.** `InvocationBounds.__post_init__`
refuses a non-positive value and names the field. Checking the same range here produced TWO
messages for one invalid value in Service 2 — which Req 23.2's "one message per invalid value"
forbids, and which a test caught there. The rule lives wherever the value lives, so the loader
CONSTRUCTS these objects and surfaces what they raise.

**A secret is resolved but never rendered.** Req 23.4 wants a credential reported as whether it
resolved. The loader never reads a credential file — it asks an injected predicate whether the
path exists. A loader that cannot see a secret cannot log one.

**Zero is not "unlimited".** Req 22.1c: `Limits` validates each present key as a positive
integer, so a zero raises rather than lifting the cap — a bound that appears configured and is
not. An unset ceiling is OMITTED.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from aqm_advisor.config.loader import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    DEFAULT_TURN_BUDGET_SECONDS,
    RECOGNIZED_KEYS,
    REGISTERED_ADAPTERS,
    AdvisorConfig,
    ConfigError,
    read_config_file,
    resolve_and_validate,
)

_BASE_URL = "https://serving.example.test"


def _env(**overrides: str) -> dict[str, str]:
    """The minimum environment that starts, plus overrides."""
    return {"AQM_ADVISOR_SERVING_BASE_URL": _BASE_URL, **overrides}


def _resolved(**overrides: str) -> AdvisorConfig:
    return resolve_and_validate(_env(**overrides), {}, credential_exists=lambda _: True)


# --- Req 23.5: the base URL is required --------------------------------


def test_a_base_url_embedding_a_credential_is_refused() -> None:
    # A review found `urlparse` accepting `https://user:pass@host` and `redacted()` then
    # emitting
    # it verbatim, so the operator's basic-auth secret would land in Req 23.1's startup line.
    # Refusing the shape beats stripping it for the log: this service authenticates to Service 2
    # by forwarding the caller's credential (A4a), so basic-auth in the base URL is a
    # configuration it has no use for, and accepting it silently leaves a secret where no test
    # looks.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            {"AQM_ADVISOR_SERVING_BASE_URL": "https://svcuser:SENTINEL-PW-Q7X@serving.test/api"},
            {},
            credential_exists=lambda _: True,
        )
    joined = "; ".join(caught.value.problems)
    assert "serving_base_url" in joined
    assert "SENTINEL-PW-Q7X" not in joined, "the refusal echoed the credential it refused"


def test_a_jwt_discovery_url_embedding_a_credential_is_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            _env(
                AQM_ADVISOR_JWT_DISCOVERY_URL="https://u:SENTINEL-PW-Q7X@idp.test/.well"
            ),
            {},
            credential_exists=lambda _: True,
        )
    joined = "; ".join(caught.value.problems)
    assert "jwt_discovery_url" in joined
    assert "SENTINEL-PW-Q7X" not in joined


def test_no_url_in_the_redacted_view_can_carry_a_credential() -> None:
    # The other half of the same fix: even for a URL that passed validation, nothing
    # credential-shaped reaches the startup line. Asserted over the whole rendered payload
    # rather
    # than key by key, so a URL field added later is covered without touching this test.
    config = _resolved(AQM_ADVISOR_JWT_DISCOVERY_URL="https://idp.test/.well-known")
    rendered = json.dumps(config.redacted())
    assert "@" not in rendered, rendered


@pytest.mark.parametrize(
    "variable",
    [
        "AQM_ADVISOR_REQUEST_TIMEOUT_SECONDS",
        "AQM_ADVISOR_TURN_BUDGET_SECONDS",
        "AQM_ADVISOR_MAX_UTTERANCE_LENGTH",
        "AQM_ADVISOR_MODEL_MAX_OUTPUT_TOKENS",
    ],
)
@pytest.mark.parametrize("raw", ["0", "-1"])
def test_a_scalar_this_loader_owns_must_be_positive(variable: str, raw: str) -> None:
    # Req 23.2 requires validating EVERY resolved value. A review found all four of these
    # resolving clean at zero and negative, because none is an `InvocationBounds` field so
    # nothing
    # downstream refused them. Each has a concrete failure: a zero `max_utterance_length`
    # rejects
    # every request, and a negative timeout or turn budget is not a duration. A service that
    # starts healthy and answers nothing is exactly what this requirement exists to prevent.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(_env(**{variable: raw}), {}, credential_exists=lambda _: True)
    assert caught.value.problems, (variable, raw)


def test_the_two_adjacent_output_ceilings_are_both_checked() -> None:
    # The asymmetry the review called sharp: `max_output_tokens` is refused at zero by
    # `InvocationBounds`, so before this fix one of two adjacent ceilings was checked and the
    # other was not — the kind of gap that reads as deliberate.
    for variable in ("AQM_ADVISOR_MAX_OUTPUT_TOKENS", "AQM_ADVISOR_MODEL_MAX_OUTPUT_TOKENS"):
        with pytest.raises(ConfigError):
            resolve_and_validate(_env(**{variable: "0"}), {}, credential_exists=lambda _: True)


def test_a_blank_locale_is_refused() -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            _env(AQM_ADVISOR_LOCALE="  "), {}, credential_exists=lambda _: True
        )
    assert any("locale" in problem for problem in caught.value.problems)


def test_a_temperature_outside_the_sampling_range_is_refused() -> None:
    # Finite is not the same as usable. A negative temperature is not a sampling setting.
    for raw in ("-0.5", "3.0"):
        with pytest.raises(ConfigError) as caught:
            resolve_and_validate(
                _env(AQM_ADVISOR_MODEL_TEMPERATURE=raw), {}, credential_exists=lambda _: True
            )
        assert any("model_temperature" in problem for problem in caught.value.problems), raw


def test_the_service_2_base_url_is_required() -> None:
    # Req 23.5 names this as its own requirement rather than folding it into general validation,
    # because a service that starts without it answers every turn with a retrieval failure —
    # healthy by its own health check and useless.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate({}, {}, credential_exists=lambda _: True)
    assert any("serving_base_url" in problem for problem in caught.value.problems)


def test_a_blank_base_url_is_refused_like_a_missing_one() -> None:
    # An empty string is "configured" to a naive presence check while being no more usable than
    # nothing. Set-but-blank is how a broken deploy template presents.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            {"AQM_ADVISOR_SERVING_BASE_URL": "   "}, {}, credential_exists=lambda _: True
        )
    assert any("serving_base_url" in problem for problem in caught.value.problems)


def test_the_base_url_must_be_http_or_https() -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            {"AQM_ADVISOR_SERVING_BASE_URL": "ftp://nope.example.test"},
            {},
            credential_exists=lambda _: True,
        )
    assert any("serving_base_url" in problem for problem in caught.value.problems)


def test_the_minimum_configuration_resolves() -> None:
    config = _resolved()
    assert config.serving_base_url == _BASE_URL


# --- Req 23.1: precedence is env, then file, then default -------------


def test_an_environment_value_beats_a_file_value() -> None:
    config = resolve_and_validate(
        _env(AQM_ADVISOR_LOG_LEVEL="warning"),
        {"log_level": "debug"},
        credential_exists=lambda _: True,
    )
    assert config.log_level == "warning"


def test_a_file_value_beats_the_default() -> None:
    config = resolve_and_validate(
        _env(), {"log_level": "debug"}, credential_exists=lambda _: True
    )
    assert config.log_level == "debug"


def test_the_default_applies_when_neither_supplies_a_value() -> None:
    assert _resolved().log_level == "info"


def test_the_documented_defaults_are_the_requirements_defaults() -> None:
    # Reqs 1.5, 22.1 and 6.4 each name a default in the requirement text. A default that drifted
    # from its requirement would be a silent spec violation, so they are pinned here.
    config = _resolved()
    assert config.max_utterance_length == 4000, "Req 1.5"
    assert config.bounds.model_invocations == 2, "Req 22.1"
    assert config.request_timeout_seconds == 30, "Req 6.4"


# --- Req 23.3: an unrecognised key names its category's keys ----------


def test_an_unrecognised_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(_env(), {"nonsense": 1}, credential_exists=lambda _: True)
    assert any("nonsense" in problem for problem in caught.value.problems)


def test_the_rejection_lists_the_keys_of_the_nearest_category() -> None:
    # Req 23.3 says "in the same category", not "every key". An operator who typed
    # `model_regoin`
    # needs the model keys, and burying them in a list of forty is how a helpful message becomes
    # an unread one.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(_env(), {"model_regoin": "x"}, credential_exists=lambda _: True)
    message = "; ".join(caught.value.problems)
    assert "model" in message
    assert "model_region" in message


def test_a_key_resembling_nothing_gets_every_category() -> None:
    # Honest rather than unhelpful: an operator who invented a key wholesale needs to see the
    # shape of what exists.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(_env(), {"zzz": 1}, credential_exists=lambda _: True)
    message = "; ".join(caught.value.problems)
    assert "serving" in message
    assert "guardrail" in message


def test_the_flat_key_set_is_derived_from_the_categories() -> None:
    # Written twice, the two would drift, and the copy that drifted would be the one rejecting a
    # key the other recognises.
    from aqm_advisor.config.loader import _RECOGNIZED_KEYS

    flattened = {key for keys in _RECOGNIZED_KEYS.values() for key in keys}
    assert flattened == RECOGNIZED_KEYS


def test_every_scalar_key_is_recognised() -> None:
    # A scalar the loader resolves but does not list would be rejected as unrecognised when set
    # in
    # a file while working from the environment — the least discoverable failure there is.
    from aqm_advisor.config.loader import _SCALARS

    assert set(_SCALARS).issubset(RECOGNIZED_KEYS), set(_SCALARS) - RECOGNIZED_KEYS


def test_every_adapter_key_is_recognised() -> None:
    assert set(REGISTERED_ADAPTERS).issubset(RECOGNIZED_KEYS)


# --- Req 23.2: every invalid value gets its own message ---------------


def test_every_invalid_value_is_reported_not_just_the_first() -> None:
    # THE clause. Fail-fast means never half-start, not stop at the first error — an operator
    # fixing one setting per deploy is what accumulation avoids.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            _env(
                AQM_ADVISOR_LOG_LEVEL="shouting",
                AQM_ADVISOR_MAX_UTTERANCE_LENGTH="not-a-number",
                AQM_ADVISOR_SERVING_CLIENT="nonexistent",
            ),
            {},
            credential_exists=lambda _: True,
        )
    joined = "; ".join(caught.value.problems)
    assert len(caught.value.problems) >= 3, caught.value.problems
    assert "log_level" in joined
    assert "max_utterance_length" in joined
    assert "serving_client" in joined


def test_one_invalid_value_yields_exactly_one_message() -> None:
    # The double-validation trap Service 2's loader records: `InvocationBounds` already refuses
    # a
    # non-positive value and names the field, so re-checking the range here produced TWO
    # messages
    # for one value. The rule lives wherever the value lives.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            _env(AQM_ADVISOR_MAX_MODEL_INVOCATIONS="0"), {}, credential_exists=lambda _: True
        )
    invocation_problems = [
        problem for problem in caught.value.problems if "model_invocations" in problem
    ]
    assert len(invocation_problems) == 1, caught.value.problems


def test_the_error_carries_every_problem_and_joins_them_for_a_human() -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate({}, {}, credential_exists=lambda _: True)
    assert isinstance(caught.value.problems, tuple)
    assert str(caught.value) == "; ".join(caught.value.problems)


def test_nothing_is_returned_when_anything_is_invalid() -> None:
    # "Without half-starting". A loader that returned a partly valid config would hand the
    # service
    # settings nobody chose.
    with pytest.raises(ConfigError):
        resolve_and_validate(
            _env(AQM_ADVISOR_LOG_LEVEL="shouting"), {}, credential_exists=lambda _: True
        )


# --- Req 22.1c: zero is not "unlimited" -------------------------------


def test_an_unset_token_ceiling_is_omitted_rather_than_zeroed() -> None:
    # Req 22.1c. `Limits` is a `total=False` TypedDict validating each PRESENT key as a positive
    # integer, so a zero raises rather than lifting the cap — a bound that appears configured
    # and
    # is not.
    limits = _resolved().bounds.as_limits()
    assert "turns" in limits
    assert "output_tokens" not in limits
    assert "total_tokens" not in limits


def test_a_configured_token_ceiling_is_present() -> None:
    config = _resolved(AQM_ADVISOR_MAX_OUTPUT_TOKENS="2048")
    limits = config.bounds.as_limits()
    assert limits["output_tokens"] == 2048


def test_a_zero_ceiling_is_rejected_rather_than_read_as_unlimited() -> None:
    # An operator writing 0 means "no limit". The SDK means "invalid". Rejecting at load is the
    # only place that difference can be explained to them.
    for variable in (
        "AQM_ADVISOR_MAX_MODEL_INVOCATIONS",
        "AQM_ADVISOR_MAX_OUTPUT_TOKENS",
        "AQM_ADVISOR_MAX_TOTAL_TOKENS",
    ):
        with pytest.raises(ConfigError) as caught:
            resolve_and_validate(_env(**{variable: "0"}), {}, credential_exists=lambda _: True)
        assert caught.value.problems, variable


# --- Req 23.6: adapters come from a registry --------------------------


def test_every_port_has_a_registry_entry() -> None:
    # Req 23.6's point is that adding an adapter is a registry entry rather than a new branch.
    assert set(REGISTERED_ADAPTERS) >= {
        "serving_client",
        "guardrail_checker",
        "advice_audit_store",
        "association_trigger",
        "model",
        "clock",
    }


def test_an_unregistered_adapter_name_is_rejected_and_lists_the_registered_ones() -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            _env(AQM_ADVISOR_GUARDRAIL_CHECKER="imaginary"),
            {},
            credential_exists=lambda _: True,
        )
    message = "; ".join(caught.value.problems)
    assert "guardrail_checker" in message
    assert "local" in message


def test_each_registry_default_is_itself_registered() -> None:
    # A default outside its own registry would make the minimum configuration unstartable, and
    # the
    # failure would read as the operator's fault.
    config = _resolved()
    for port, selected in config.adapters.items():
        assert selected in REGISTERED_ADAPTERS[port], (port, selected)


def test_every_registered_name_is_selectable() -> None:
    # The registry is only honest if each name it advertises actually resolves. A first version
    # of
    # this test supplied nothing but the base URL and failed on `model=bedrock`, which was the
    # LOADER being right: Req 6.6 makes a credential required for a model adapter that needs
    # one.
    # So the test supplies what each selection legitimately requires, which is also the closest
    # thing here to a smoke test of a real deployment's configuration.
    for port, names in REGISTERED_ADAPTERS.items():
        for name in names:
            overrides = {f"AQM_ADVISOR_{port.upper()}": name}
            if port == "model" and name != "scripted":
                overrides["AQM_ADVISOR_MODEL_CREDENTIAL_PATH"] = "/secrets/key"
            config = _resolved(**overrides)
            assert config.adapters[port] == name, (port, name)


# --- Req 34.9: the guardrail identifier gates startup ----------------


def test_enabling_the_guardrail_without_an_identifier_refuses_to_start() -> None:
    # Req 34.9. Enforcement enabled with nothing to enforce against is the worst of the three
    # states: the operator believes output is checked and it is not.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            _env(AQM_ADVISOR_GUARDRAIL_ENABLED="true"), {}, credential_exists=lambda _: True
        )
    assert any("guardrail_identifier" in problem for problem in caught.value.problems)


def test_enabling_the_guardrail_with_an_identifier_starts() -> None:
    config = _resolved(
        AQM_ADVISOR_GUARDRAIL_ENABLED="true", AQM_ADVISOR_GUARDRAIL_IDENTIFIER="gr-1"
    )
    assert config.guardrail_enabled is True
    assert config.guardrail_identifier == "gr-1"


def test_the_guardrail_is_disabled_by_default_and_then_needs_no_identifier() -> None:
    # Disabled is the honest default for a service whose offline suite must pass with no cloud
    # access — and Req 34.5 keeps the LOCAL forbidden-claim check running either way, so
    # disabled
    # does not mean unchecked.
    config = _resolved()
    assert config.guardrail_enabled is False
    assert config.guardrail_identifier is None


# --- Req 23.4: a credential is reported, never rendered --------------


def test_a_missing_model_credential_path_is_reported_by_key_not_value() -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            _env(
                AQM_ADVISOR_MODEL="bedrock",
                AQM_ADVISOR_MODEL_CREDENTIAL_PATH="/secrets/SENTINEL-KEY-Q7X",
            ),
            {},
            credential_exists=lambda _: False,
        )
    joined = "; ".join(caught.value.problems)
    assert "model_credential_path" in joined
    assert "SENTINEL-KEY-Q7X" not in joined, "the loader rendered a credential path"


def test_the_loader_never_reads_a_credential_file() -> None:
    # A loader that cannot see a secret cannot log one. The predicate is asked whether the path
    # exists; nothing opens it.
    asked: list[str] = []

    def _exists(path: object) -> bool:
        asked.append(str(path))
        return True

    resolve_and_validate(
        _env(
            AQM_ADVISOR_MODEL="bedrock",
            AQM_ADVISOR_MODEL_CREDENTIAL_PATH="/secrets/key",
        ),
        {},
        credential_exists=_exists,
    )
    assert asked == ["/secrets/key"]


def test_a_credential_is_only_required_by_the_adapter_that_needs_one() -> None:
    # The scripted model needs no credential, so requiring one would make the offline suite
    # unstartable — and Req 23.2's "validate every value" does not mean "demand every value".
    config = resolve_and_validate(
        _env(AQM_ADVISOR_MODEL="scripted"), {}, credential_exists=lambda _: False
    )
    assert config.model_credential_path is None


# --- Req 23.1: the resolved non-secret configuration is loggable ------


def test_the_redacted_view_omits_every_credential_path() -> None:
    config = _resolved(
        AQM_ADVISOR_MODEL="bedrock",
        AQM_ADVISOR_MODEL_CREDENTIAL_PATH="/secrets/SENTINEL-KEY-Q7X",
    )
    rendered = json.dumps(config.redacted())
    assert "SENTINEL-KEY-Q7X" not in rendered
    assert "/secrets" not in rendered


def test_the_redacted_view_reports_whether_the_credential_resolved() -> None:
    # Req 23.4: as WHETHER it resolved, not as a path. A boolean is the whole of what an
    # operator
    # needs and the most a log should carry.
    config = _resolved(
        AQM_ADVISOR_MODEL="bedrock", AQM_ADVISOR_MODEL_CREDENTIAL_PATH="/secrets/key"
    )
    assert config.redacted()["modelCredentialConfigured"] is True
    assert _resolved().redacted()["modelCredentialConfigured"] is False


def test_the_redacted_view_survives_the_log_redactor() -> None:
    # The loader's own output has to be publishable by the logger that will publish it. A key
    # the
    # redactor considers sensitive would reach the log as REDACTED, so the one startup line Req
    # 23.1 requires would say nothing.
    from aqm_advisor.observability.logging import is_sensitive

    offending = sorted(key for key in _resolved().redacted() if is_sensitive(key))
    assert offending == [], offending


def test_the_redacted_view_is_json_serialisable() -> None:
    # It is logged as one line of JSON. A value the encoder refuses would take the startup line
    # with it.
    json.dumps(_resolved().redacted())


def test_the_config_is_frozen() -> None:
    with pytest.raises(AttributeError):
        _resolved().serving_base_url = "https://elsewhere.test"  # type: ignore[misc]


# --- the file reader ---------------------------------------------------


def test_no_configuration_file_is_legal() -> None:
    assert read_config_file(None) == {}


def test_a_missing_file_raises_rather_than_falling_back_to_defaults() -> None:
    # Falling back would come up with settings nobody chose, which is worse than not coming up.
    # A named file that is not there is a deploy error, not an absence.
    with pytest.raises(ConfigError) as caught:
        read_config_file("/nonexistent/advisor.json")
    assert any("unreadable" in problem for problem in caught.value.problems)


def test_an_unparseable_file_raises(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "advisor.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        read_config_file(str(path))
    assert any("unparseable" in problem for problem in caught.value.problems)


def test_a_file_that_is_not_an_object_raises(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "advisor.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        read_config_file(str(path))
    assert any("unparseable" in problem for problem in caught.value.problems)


def test_a_readable_object_file_is_returned(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "advisor.json"
    path.write_text(json.dumps({"log_level": "debug"}), encoding="utf-8")
    assert read_config_file(str(path)) == {"log_level": "debug"}


def test_the_file_reader_names_the_path_but_not_its_contents(tmp_path: pathlib.Path) -> None:
    # The path is the operator's own and naming it is how they find the file. Its CONTENTS may
    # hold a credential, so a parse error never quotes the text it failed on.
    path = tmp_path / "advisor.json"
    path.write_text('{"secret": "SENTINEL-KEY-Q7X"', encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        read_config_file(str(path))
    joined = "; ".join(caught.value.problems)
    assert str(path) in joined
    assert "SENTINEL-KEY-Q7X" not in joined


# --- coercion ---------------------------------------------------------


@pytest.mark.parametrize("raw", ["true", "TRUE", "1", "yes", "  true  "])
def test_a_boolean_accepts_the_usual_spellings(raw: str) -> None:
    assert _resolved(AQM_ADVISOR_STREAMING_ENABLED=raw).streaming_enabled is True


@pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no"])
def test_a_boolean_accepts_the_usual_negatives(raw: str) -> None:
    assert _resolved(AQM_ADVISOR_STREAMING_ENABLED=raw).streaming_enabled is False


def test_a_boolean_rejects_anything_else() -> None:
    # "maybe" is not false. A flag that silently read as false would disable a feature the
    # operator believes they enabled.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            _env(AQM_ADVISOR_STREAMING_ENABLED="maybe"), {}, credential_exists=lambda _: True
        )
    assert any("streaming_enabled" in problem for problem in caught.value.problems)


def test_a_boolean_is_coerced_before_an_integer() -> None:
    # `bool` is a subclass of `int` in Python, so checking `int` first would send every flag
    # through `int()` and turn "true" into a coercion error.
    assert _resolved(AQM_ADVISOR_STREAMING_ENABLED="true").streaming_enabled is True


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "NaN"])
def test_a_non_finite_float_is_rejected(raw: str) -> None:
    # `float("nan")` and `float("inf")` parse without error and are never valid configuration. A
    # NaN temperature would make every comparison against it false.
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            _env(AQM_ADVISOR_MODEL_TEMPERATURE=raw), {}, credential_exists=lambda _: True
        )
    assert any("model_temperature" in problem for problem in caught.value.problems)


def test_a_valid_temperature_resolves() -> None:
    assert _resolved(AQM_ADVISOR_MODEL_TEMPERATURE="0.2").model_temperature == 0.2


def test_the_default_temperature_is_zero() -> None:
    # Req 25.4: temperature 0 by default. Any sampling parameter is configuration, but the
    # DEFAULT is the reproducible one.
    assert _resolved().model_temperature == 0.0


# --- the log level is owned by the logging module --------------------


def test_an_invalid_log_level_is_reported_once_with_the_permitted_set() -> None:
    with pytest.raises(ConfigError) as caught:
        resolve_and_validate(
            _env(AQM_ADVISOR_LOG_LEVEL="shouting"), {}, credential_exists=lambda _: True
        )
    matching = [problem for problem in caught.value.problems if "log_level" in problem]
    assert len(matching) == 1
    assert "info" in matching[0]


def _loader_tree() -> ast.AST:
    """The loader's AST, parsed once per call site."""
    return ast.parse(
        pathlib.Path("src/aqm_advisor/config/loader.py").read_text(encoding="utf-8")
    )


def _called_names(tree: ast.AST) -> set[str]:
    """Every called name, by bare name AND by attribute.

    The attribute half is what a review found missing: the first version inspected only
    `ast.Call` whose `func` was an `ast.Name`, so `logging.configure_logging()` — the form a
    caller would most naturally write — was never examined.
    """
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    } | {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _environment_reads(tree: ast.AST) -> set[str]:
    """Every spelling that reaches the process environment.

    Covers the attribute form, the `from os import environ` form and a bare `import os`. A
    review found the attribute-only version letting the import form through, which reaches the
    same global by another name.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv"):
            found.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            found |= {a.name for a in node.names} & {"environ", "getenv"}
        elif isinstance(node, ast.Import):
            found |= {a.name for a in node.names} & {"os"}
    return found


def test_the_loader_does_not_configure_logging_itself() -> None:
    # Resolving is not applying. A loader that configured logging as a side effect would have
    # half-started before its own validation finished — and Req 23.2 forbids half-starting.
    assert "configure_logging" not in _called_names(_loader_tree())


def test_the_loader_reads_no_environment_of_its_own() -> None:
    # `env` is a parameter, so the loader is pure and a test can supply any environment. A
    # module
    # reaching for `os.environ` would make the process's real environment leak into every test.
    assert _environment_reads(_loader_tree()) == set()


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param("configure_logging('info')", id="bare-name"),
        pytest.param("logging.configure_logging('info')", id="attribute"),
    ],
)
def test_the_logging_guard_catches_both_call_forms(planted: str) -> None:
    # Self-check over the forms that matter rather than the one the guard obviously catches. The
    # attribute form is the one that slipped past the first version.

    assert "configure_logging" in _called_names(ast.parse(planted)), planted


@pytest.mark.parametrize(
    "planted",
    [
        pytest.param("import os\nx = os.environ['A']", id="attribute"),
        pytest.param("from os import environ\nx = environ['A']", id="import-from"),
        pytest.param("import os\nx = os.getenv('A')", id="getenv"),
    ],
)
def test_the_environment_guard_catches_every_spelling(planted: str) -> None:

    assert _environment_reads(ast.parse(planted)) != set(), planted


def test_the_loader_calls_nothing_on_the_serving_client_or_model() -> None:
    # Req 23.2: validate every value BEFORE invoking the Model_Port or the Serving_Client. The
    # loader names adapters; it does not construct or call them.
    source = pathlib.Path("src/aqm_advisor/config/loader.py").read_text(encoding="utf-8")
    for forbidden in ("air_quality(", "profile_get(", "invoke(", "stream(", "boto3"):
        assert forbidden not in source, forbidden


def test_a_turn_budget_shorter_than_a_request_timeout_is_refused() -> None:
    # A CROSS-FIELD rule, and the only one here. Both values validate fine alone, which is
    # exactly why
    # this was missing: nothing looked at their relationship. A budget shorter than one
    # downstream
    # timeout guarantees the budget expires while a retrieval is still running — and the
    # entrypoint
    # runs the turn on a worker thread that `asyncio.timeout` cannot kill. Repeated expiries
    # fill the
    # executor, and a saturated pool makes later turns answer degraded WITHOUT EVER EXECUTING
    # while the
    # container still reports healthy.
    with pytest.raises(ConfigError) as caught:
        _resolved(
            AQM_ADVISOR_TURN_BUDGET_SECONDS="10",
            AQM_ADVISOR_REQUEST_TIMEOUT_SECONDS="30",
        )
    message = str(caught.value)
    assert "turn_budget_seconds" in message
    assert "request_timeout_seconds" in message


def test_a_budget_equal_to_the_timeout_is_accepted() -> None:
    # The boundary is >=, not >. Equal is the tightest configuration that still lets one
    # retrieval
    # finish inside the budget, so refusing it would be over-strict.
    config = _resolved(
        AQM_ADVISOR_TURN_BUDGET_SECONDS="30",
        AQM_ADVISOR_REQUEST_TIMEOUT_SECONDS="30",
    )
    assert config.turn_budget_seconds == 30


def test_the_shipped_defaults_satisfy_the_relationship() -> None:
    # Non-vacuity for the rule: if the defaults violated it, every unconfigured deployment would
    # refuse
    # to start and the rule would be discovered as an outage rather than as a guard.
    assert DEFAULT_TURN_BUDGET_SECONDS >= DEFAULT_REQUEST_TIMEOUT_SECONDS
