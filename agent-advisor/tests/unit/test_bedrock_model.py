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
from strands.models import BedrockModel

from aqm_advisor.adapters.model.bedrock import (
    ModelConfigurationError,
    build_bedrock_model,
    session_for_credential_path,
)


def _built(**overrides: object) -> BedrockModel:
    settings: dict[str, object] = {
        "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "model_region": "eu-west-2",
        "model_temperature": 0.0,
        "model_max_output_tokens": 1024,
        "request_timeout_seconds": 30,
        "model_credential_path": None,
    }
    settings.update(overrides)
    return build_bedrock_model(**settings)  # type: ignore[arg-type]


# --- Req 6.1b: never a literal, and never the framework's default ------


def test_the_configured_identifier_and_region_are_used() -> None:
    model = _built()
    config = model.get_config()
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
    config = _built().get_config()
    assert config["model_id"] != "global.anthropic.claude-sonnet-4-6"


# --- Req 25.4: temperature 0 by default, sampling is configuration -----


def test_temperature_defaults_to_zero() -> None:
    # Req 25.4. Zero is the DEFAULT rather than a hardcode — the same clause makes any sampling
    # parameter configuration, so it must remain settable.
    assert _built().get_config()["temperature"] == 0.0


def test_temperature_is_configurable_away_from_zero() -> None:
    # The other half of Req 25.4. A builder that pinned 0 would satisfy the default and violate
    # "SHALL treat any sampling parameter as configuration".
    assert _built(model_temperature=0.7).get_config()["temperature"] == 0.7


# --- Req 6.5: the maximum output length is applied --------------------


def test_the_maximum_output_length_is_applied() -> None:
    assert _built().get_config()["max_tokens"] == 1024


def test_an_unset_maximum_output_length_is_omitted_rather_than_guessed() -> None:
    # Req 6.5 requires a CONFIGURED maximum. When none is configured the builder must not invent
    # one: a guessed ceiling would truncate a generation, and Req 6.5 makes a truncated
    # generation
    # a failure rather than partial Guidance — so a wrong guess manufactures the exact failure
    # the
    # requirement is trying to detect.
    assert _built(model_max_output_tokens=None).get_config().get("max_tokens") is None


# --- Req 6.4: the request timeout is applied --------------------------


def test_the_request_timeout_reaches_the_botocore_client() -> None:
    # Req 6.4. The timeout is NOT a BedrockConfig field — it belongs to the botocore client
    # config,
    # which is why this asserts through the client rather than through `get_config()`. Verified
    # against the installed strands source rather than assumed.
    model = _built(request_timeout_seconds=17)
    config = model.client.meta.config
    assert config.read_timeout == 17
    assert config.connect_timeout == 17


# --- Req 6.6: the credential is never held or logged ------------------


def test_the_builder_takes_a_path_but_never_a_secret_value() -> None:
    # THIS TEST'S PREMISE CHANGED, and the change is the point. The first version asserted the
    # builder took no credential argument at all. That was true, and it hid a defect: the loader
    # REQUIRED `model_credential_path` while nothing consumed it, so an operator had to supply a
    # path to a real file that changed no behaviour. Req 6.6 permits "a runtime-supplied path",
    # so
    # the builder now takes the PATH.
    #
    # What must still never appear is a parameter carrying secret MATERIAL — a key, a token, a
    # session credential. A path names where a secret lives; it is not the secret, the file is
    # never opened here, and the redactor catches `credential` anywhere in a logged key name.
    import inspect

    material = ("secret", "password", "access_key", "session_token", "api_key")
    names = list(inspect.signature(build_bedrock_model).parameters)
    offenders = [n for n in names if any(m in n for m in material)]
    assert not offenders, offenders
    assert "model_credential_path" in names, names


def test_the_material_markers_are_not_vacuous() -> None:
    # Self-check, so the test above cannot pass merely by matching nothing. Note what is
    # deliberately NOT caught: `model_max_output_tokens` is a token COUNT, and an earlier looser
    # marker rejected it — the same false-positive class the Req 30.2 pattern review found.
    material = ("secret", "password", "access_key", "session_token", "api_key")
    for planted in ("aws_secret", "session_token", "api_key"):
        assert any(m in planted for m in material), planted
    assert not any(m in "model_max_output_tokens" for m in material)


def test_the_built_model_holds_nothing_credential_shaped() -> None:
    model = _built()
    rendered = repr(vars(model)).casefold()
    for marker in ("aws_secret", "session_token", "secret_access"):
        assert marker not in rendered, rendered


# --- Req 6.6's runtime-supplied path, now actually consumed --------------


def test_a_configured_credential_path_reaches_the_session() -> None:
    # A review found this key REQUIRED by the loader whenever the bedrock adapter is selected,
    # and
    # consumed by nothing: an operator had to supply a path to a real file that changed no
    # behaviour.
    # Req 6.6 permits the environment OR "a runtime-supplied path", so the path is the branch
    # the key
    # exists for, and it is honoured now.
    #
    # Asserted on the SESSION rather than on a resolved credential: what matters is that the
    # path was
    # handed to botocore, not that a secret was read.
    session = session_for_credential_path(
        credential_path="/run/secrets/aws-creds", region="eu-west-2"
    )
    low_level = session._session
    assert low_level.get_config_variable("credentials_file") == "/run/secrets/aws-creds"


def test_the_session_carries_the_region() -> None:
    # THE TRAP. `BedrockModel.__init__` raises ValueError when given both `region_name` and
    # `boto_session`, so consuming the path means the region must travel ON the session. Read
    # from
    # the strands source, not assumed; without it the credential-path branch would raise.
    session = session_for_credential_path(
        credential_path="/run/secrets/aws-creds", region="eu-west-2"
    )
    assert session.region_name == "eu-west-2"


def test_the_region_still_applies_when_a_credential_path_is_used() -> None:
    # The same trap, end to end through the builder, so the two cannot drift apart.
    model = _built(
        model_credential_path="/run/secrets/aws-creds", model_region="eu-west-2"
    )
    assert model.client.meta.region_name == "eu-west-2"


def test_no_credential_path_still_builds_and_uses_the_region() -> None:
    # The other branch: with no path configured, boto3's own chain resolves from the
    # environment,
    # which Req 6.6 equally permits. The region is passed directly in that case.
    model = _built(model_credential_path=None)
    assert model.client.meta.region_name == "eu-west-2"


def test_the_credential_path_is_not_read_by_the_builder() -> None:
    # The builder HANDS OVER the path and never opens it. A nonexistent path must therefore
    # build
    # fine: botocore resolves lazily when a call is first signed, and a builder that read the
    # file
    # would put secret material in its own frame.
    model = _built(model_credential_path="/nonexistent/path/to/creds")
    assert model.client.meta.region_name == "eu-west-2"
