"""CDK app entry point for the AI Advisor deployment.

The environment is set EXPLICITLY from configuration, never left implicit. An unset env makes
CDK
attempt an account lookup during synth (via the bootstrap LookupRole), which needs credentials
and
would break the offline synth gate the steering docs require. Reading account and region from
the
environment keeps synth pure while letting a deploy target a real account.

Every deploy input comes from an environment variable with a documented default, so the same app
synthesizes offline (defaults) and deploys live (real values) with nothing hard-coded.
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from aqm_advisor_infra.advisor_stack import AdvisorRuntimeStack

# The account MUST be explicit. A real deploy sets CDK_DEPLOY_ACCOUNT; the offline synth/test
# path sets AQM_CDK_SYNTH_PLACEHOLDER=1 to opt into a fake account. Neither present is an ERROR,
# so a `cdk deploy` that forgot its env vars fails HERE with a readable message rather than 30
# seconds in with an assume-role error against account 000000000000.
#
# This inversion is deliberate. There is no app-time env var that distinguishes synth from
# deploy -- the CLI sets CDK_OUTDIR for both, and CDK_CLI_ACTION does NOT exist (I guessed it,
# then confirmed against the installed CLI that it is not there). So "real work must name a real
# account; only an explicit opt-in gets the placeholder" is the only guard that cannot silently
# pass a deploy against a fake account.
_SYNTH_PLACEHOLDER_ACCOUNT = "000000000000"
_DEFAULT_REGION = "us-east-1"
_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6"
_PLACEHOLDER_DISCOVERY = "https://example.invalid/.well-known/openid-configuration"
_PLACEHOLDER_CLIENT = "placeholder-client-id"

_explicit_account = os.environ.get("CDK_DEPLOY_ACCOUNT")
if _explicit_account:
    _account = _explicit_account
elif os.environ.get("AQM_CDK_SYNTH_PLACEHOLDER") == "1":
    _account = _SYNTH_PLACEHOLDER_ACCOUNT
else:
    raise SystemExit(
        "no account configured. For a deploy set CDK_DEPLOY_ACCOUNT (and CDK_DEPLOY_REGION, "
        "AQM_COGNITO_CLIENT_ID, AQM_COGNITO_DISCOVERY_URL) -- see infra/README.md. For an "
        "offline synth or the tests, set AQM_CDK_SYNTH_PLACEHOLDER=1."
    )

app = cdk.App()

AdvisorRuntimeStack(
    app,
    "AqmAdvisorRuntime",
    env=cdk.Environment(
        account=_account,
        region=os.environ.get("CDK_DEPLOY_REGION", _DEFAULT_REGION),
    ),
    model_inference_profile=os.environ.get("AQM_ADVISOR_MODEL_ID", _DEFAULT_MODEL),
    cognito_discovery_url=os.environ.get(
        "AQM_COGNITO_DISCOVERY_URL", _PLACEHOLDER_DISCOVERY
    ),
    cognito_client_id=os.environ.get("AQM_COGNITO_CLIENT_ID", _PLACEHOLDER_CLIENT),
)

app.synth()
