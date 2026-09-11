"""The production Model_Port: a configured Strands `BedrockModel` (task 16.2, Req 6.1b).

**THERE IS NO WRAPPER CLASS HERE, AND THAT IS DD2.** The Strands `Model` abstract class IS the
Model_Port, so this module builds a CONFIGURED `BedrockModel` and stops. A wrapper would have to
be
kept in step with the framework's own abstraction — whose real surface is four abstract methods,
not
one — for no gain.

**THE BUILDER REFUSES AN UNCONFIGURED MODEL, AND THAT IS THE POINT OF THIS FILE.**
`strands-agents==1.55.1` bakes in `DEFAULT_BEDROCK_MODEL_ID = "global.anthropic.claude-
sonnet-4-6"`
and `DEFAULT_BEDROCK_REGION = "us-west-2"`, and `BedrockModel.__init__` substitutes them with
only a
`warning` when none is supplied. Req 6.1b says the identifier and region come from configuration
and
"never from a literal in code" — a framework literal is still a literal. Without this refusal an
unconfigured deployment would answer on a model nobody chose, in a region nobody chose, having
logged a warning nobody read. That is a fail-open, so it is refused here instead.

**THE CREDENTIAL IS NOT AN ARGUMENT (Req 6.6).** It is resolved from the environment or a
runtime
path — which is exactly what boto3's own credential chain does — so this module never receives,
holds or logs it. A credential parameter would be a second resolution path and a value that can
reach a repr, a log line or a traceback.
"""

from __future__ import annotations

from botocore.config import Config as BotocoreConfig
from strands.models import BedrockModel

_FRAMEWORK_DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"
"""What `strands-agents==1.55.1` substitutes when no identifier is given.

Recorded so the refusal below can be explained in terms of what it prevents, and so a test can
assert a built model never carries it. If an SDK upgrade changes the value this constant goes
stale
— which is why the refusal is driven by the ABSENCE of configuration rather than by comparing
against this string.
"""


class ModelConfigurationError(Exception):
    """The model could not be configured, naming every missing value.

    Its own type rather than `ValueError` so a composition root can tell "this deployment is not
    configured" from "a value was the wrong shape", and report the first as a startup refusal.
    """


def build_bedrock_model(
    *,
    model_id: str | None,
    model_region: str | None,
    model_temperature: float = 0.0,
    model_max_output_tokens: int | None = None,
    request_timeout_seconds: int = 30,
) -> BedrockModel:
    """Build the production model from configuration (Reqs 6.1b, 6.4, 6.5, 6.6, 25.4).

    Refuses when the identifier or region is missing, rather than letting the framework
    substitute
    its own default. Accumulates BOTH problems before raising, following the configuration
    loader's
    rule: fail fast means never half-start, not stop at the first error — an operator should not
    rediscover a second missing value on the next restart.
    """
    missing = [
        name
        for name, value in (("model_id", model_id), ("model_region", model_region))
        if value is None or not str(value).strip()
    ]
    if missing:
        raise ModelConfigurationError(
            f"the Bedrock model adapter is selected but {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} not configured; Req 6.1b requires "
            "both from configuration, and the Strands default would otherwise be used "
            "silently"
        )
    # Narrowed by the refusal above, restated for the type checker, which cannot see
    # that the comprehension proved both are present.
    assert model_id is not None
    assert model_region is not None

    # The timeout is NOT a BedrockConfig field. It belongs to the botocore client
    # config, verified against the installed strands source. Assuming otherwise would
    # have set a key the SDK ignores, leaving Req 6.4 unenforced while looking
    # configured.
    client_config = BotocoreConfig(
        read_timeout=request_timeout_seconds,
        connect_timeout=request_timeout_seconds,
    )

    # `max_tokens` is OMITTED, not passed as None, when unconfigured. Req 6.5 wants a
    # configured maximum and treats a truncated generation as a failure, so inventing a
    # ceiling would manufacture the very failure the requirement exists to detect. The
    # two call forms are spelled out rather than splatted from a dict because omitted
    # and None are different requests, and a splat hides which one is being made.
    if model_max_output_tokens is not None:
        return BedrockModel(
            model_id=model_id,
            region_name=model_region,
            temperature=model_temperature,
            max_tokens=model_max_output_tokens,
            boto_client_config=client_config,
        )
    return BedrockModel(
        model_id=model_id,
        region_name=model_region,
        temperature=model_temperature,
        boto_client_config=client_config,
    )


__all__ = ["ModelConfigurationError", "build_bedrock_model"]
