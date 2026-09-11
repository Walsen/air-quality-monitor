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

import boto3
import botocore.session
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


def session_for_credential_path(
    *, credential_path: str, region: str
) -> boto3.Session:
    """A session that resolves credentials from `credential_path` (Req 6.6).

    Its own function so the wiring is DIRECTLY assertable. Testing it through a built model
    meant
    reaching into `model.client`'s internals, which do not retain the session — a test that
    cannot
    see the thing it claims to check is the shape a review already caught here once.

    THE PATH IS HANDED OVER, NEVER OPENED. botocore resolves it lazily when a call is first
    signed,
    so this function does no I/O and a nonexistent path builds fine. A builder that read the
    file
    would pull secret material into its own frame for no gain, and Req 6.6 forbids logging it.

    THE REGION GOES ON THE SESSION, not alongside it. `BedrockModel.__init__` raises
    `ValueError`
    when handed both `region_name` and `boto_session`, and derives the region from
    `session.region_name` in that case.
    """
    low_level = botocore.session.Session()
    low_level.set_config_variable("credentials_file", credential_path)
    low_level.set_config_variable("region", region)
    return boto3.Session(botocore_session=low_level)


def build_bedrock_model(
    *,
    model_id: str | None,
    model_region: str | None,
    model_temperature: float = 0.0,
    model_max_output_tokens: int | None = None,
    request_timeout_seconds: int = 30,
    model_credential_path: str | None = None,
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

    # `max_tokens` is passed even when None. An earlier comment here claimed omitted and
    # None were different requests; the SDK shows otherwise — `format_request` builds
    # `inferenceConfig` with an `if value is not None` filter, so a None never reaches the wire.
    # That collapses what would be four call forms into two. Req 6.5 still holds: nothing
    # invents a ceiling, and an unconfigured maximum simply is not sent.
    if model_credential_path:
        # Req 6.6's "runtime-supplied path" branch. A review found the loader REQUIRING this key
        # whenever the bedrock adapter is selected while NOTHING consumed it, so an operator had
        # to
        # supply a path to a real file that changed no behaviour.
        #
        # The path is HANDED to botocore, never opened here: the SDK resolves it lazily at call
        # time, and a builder that read the file would pull secret material into its own frame
        # for
        # no gain at all.
        #
        # THE REGION MOVES ONTO THE SESSION. `BedrockModel.__init__` raises `ValueError` when
        # given
        # both `region_name` and `boto_session`, and derives the region from
        # `session.region_name`
        # in that case. Read from the installed strands source; without this, selecting a
        # credential
        # path would raise at construction rather than work.
        return BedrockModel(
            model_id=model_id,
            boto_session=session_for_credential_path(
                credential_path=model_credential_path, region=model_region
            ),
            temperature=model_temperature,
            boto_client_config=client_config,
            max_tokens=model_max_output_tokens,
        )
    # No path configured: boto3's own chain resolves from the environment, which Req 6.6 equally
    # permits. The region is passed directly, there being no session to carry it.
    return BedrockModel(
        model_id=model_id,
        region_name=model_region,
        temperature=model_temperature,
        boto_client_config=client_config,
        max_tokens=model_max_output_tokens,
    )


__all__ = [
    "ModelConfigurationError",
    "build_bedrock_model",
    "session_for_credential_path",
]
