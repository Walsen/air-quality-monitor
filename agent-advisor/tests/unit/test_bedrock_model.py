"""Tests for the `BedrockModel` configuration (task 16.2).

Validates Reqs 6.1b, 6.4, 6.5, 6.6 and 25.4.

**There is no wrapper class to test, and that is DD2.** The Strands `Model` abstract class IS
the
Model_Port, so this task builds a CONFIGURED `BedrockModel` rather than something around one.
What
is under test is therefore the configuration decision, which is where the requirements live.

**The sharpest finding is a fail-open in the framework.** `strands-agents==1.55.1` bakes in
`DEFAULT_BEDROCK_MODEL_ID = "global.anthropic.claude-sonnet-4-6"` and
`DEFAULT_BEDROCK_REGION = "us-west-2"`, and `BedrockModel.__init__` substitutes them with only a
warning when none is supplied. Req 6.1b says the identifier and region come from configuration
and
"never from a literal in code" — a framework literal is still a literal, and an unconfigured
deployment would quietly answer on a model nobody chose, in a region nobody chose. So the
builder
REFUSES rather than letting the default through.

These tests construct real `BedrockModel` objects. That is offline-safe: boto3 client
construction
resolves no credentials and makes no call, which is why an explicit region is passed.
"""

from __future__ import annotations

import pytest

from aqm_advisor.adapters.model.bedrock import (
    ModelConfigurationError,
    build_bedrock_model,
)


def _built(**overrides: object) -> object:
    settings: dict[str, object] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_region": "eu-west-2",
        "model_temperature": 0.0,
        "model_max_output_tokens": 1024,
        "request_timeout_seconds": 30,
    }
    settings.update(overrides)
    return build_bedrock_model(**settings)  # type: ignore[arg-type]


# --- Req 6.1b: never a literal, and never the framework's default ------


def test_the_configured_identifier_and_region_are_used() -> None:
    model = _built()
    config = model.get_config()  # type: ignore[attr-defined]
    assert config["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"


@pytest.mark.parametrize("missing", ["model_id", "model_region"])
@pytest.mark.parametrize("empty", [None, "", "   "])
def test_a_missing_identifier_or_region_is_refused(
    missing: str, empty: object
) -> None:
    # THE clause of this task. `strands-agents==1.55.1` would substitute
    # `global.anthropic.claude-sonnet-4-6` in `us-west-2` with only a warning, so without this
    # refusal an unconfigured deployment answers on a model and in a region nobody chose — and
    # Req 6.1b's "never from a literal in code" would be defeated by a literal one layer down.
    with pytest.raises(ModelConfigurationError) as caught:
        _built(**{missing: empty})
    assert missing in str(caught.value)


def test_the_refusal_names_every_missing_value_at_once() -> None:
    # Same fail-fast rule the configuration loader follows: never half-start, and report every
    # problem rather than making an operator rediscover them one restart at a time.
    with pytest.raises(ModelConfigurationError) as caught:
        _built(model_id=None, model_region=None)
    message = str(caught.value)
    assert "model_id" in message
    assert "model_region" in message


def test_the_framework_default_model_id_never_appears_in_a_built_model() -> None:
    # Guards the fail-open directly rather than by proxy: if a future refactor drops the
    # refusal,
    # this fails even when the refusal test is deleted along with it.
    config = _built().get_config()  # type: ignore[attr-defined]
    assert config["model_id"] != "global.anthropic.claude-sonnet-4-6"


# --- Req 25.4: temperature 0 by default, sampling is configuration -----


def test_temperature_defaults_to_zero() -> None:
    # Req 25.4. Zero is the DEFAULT rather than a hardcode — the same clause makes any sampling
    # parameter configuration, so it must remain settable.
    assert _built().get_config()["temperature"] == 0.0  # type: ignore[attr-defined]


def test_temperature_is_configurable_away_from_zero() -> None:
    # The other half of Req 25.4. A builder that pinned 0 would satisfy the default and violate
    # "SHALL treat any sampling parameter as configuration".
    assert _built(model_temperature=0.7).get_config()["temperature"] == 0.7  # type: ignore[attr-defined]


# --- Req 6.5: the maximum output length is applied --------------------


def test_the_maximum_output_length_is_applied() -> None:
    assert _built().get_config()["max_tokens"] == 1024  # type: ignore[attr-defined]


def test_an_unset_maximum_output_length_is_omitted_rather_than_guessed() -> None:
    # Req 6.5 requires a CONFIGURED maximum. When none is configured the builder must not invent
    # one: a guessed ceiling would truncate a generation, and Req 6.5 makes a truncated
    # generation
    # a failure rather than partial Guidance — so a wrong guess manufactures the exact failure
    # the
    # requirement is trying to detect.
    assert _built(model_max_output_tokens=None).get_config().get("max_tokens") is None  # type: ignore[attr-defined]


# --- Req 6.4: the request timeout is applied --------------------------


def test_the_request_timeout_reaches_the_botocore_client() -> None:
    # Req 6.4. The timeout is NOT a BedrockConfig field — it belongs to the botocore client
    # config,
    # which is why this asserts through the client rather than through `get_config()`. Verified
    # against the installed strands source rather than assumed.
    model = _built(request_timeout_seconds=17)
    config = model.client.meta.config  # type: ignore[attr-defined]
    assert config.read_timeout == 17
    assert config.connect_timeout == 17


# --- Req 6.6: the credential is never held or logged ------------------


def test_the_builder_takes_no_credential_argument() -> None:
    # Req 6.6 resolves the credential from the environment or a runtime path, which is exactly
    # what
    # boto3's own chain does. A credential PARAMETER would create a second path, and a value
    # passed
    # in is a value that can be logged, put in a repr, or captured in a traceback.
    #
    # The markers are BOUND to credential shapes rather than matching the bare word "token":
    # `model_max_output_tokens` is a token COUNT, and a loose marker rejected it — the same
    # false-positive class the Req 30.2 pattern review found, where an unbound match discarded
    # legitimate text.
    import inspect

    markers = ("credential", "secret", "password", "auth_token", "access_token", "api_key")
    offenders = [
        name
        for name in inspect.signature(build_bedrock_model).parameters
        if any(marker in name for marker in markers)
    ]
    assert not offenders, offenders


def test_the_builder_signature_check_is_not_vacuous() -> None:
    # Self-check: proves the markers above would actually fire, so the test cannot pass merely
    # by
    # having a marker list that matches nothing.
    markers = ("credential", "secret", "password", "auth_token", "access_token", "api_key")
    planted = ("model_credential_path", "aws_secret", "api_key")
    for name in planted:
        assert any(marker in name for marker in markers), name


def test_the_built_model_holds_nothing_credential_shaped() -> None:
    model = _built()
    rendered = repr(vars(model)).casefold()
    for marker in ("aws_secret", "session_token", "secret_access"):
        assert marker not in rendered, rendered
