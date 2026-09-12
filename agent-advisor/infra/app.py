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

# Defaults let `cdk synth` run offline with no account configured. A real deploy overrides them.
_DEFAULT_REGION = "us-east-1"
_DEFAULT_ACCOUNT = "000000000000"
_DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6"
_PLACEHOLDER_DISCOVERY = "https://example.invalid/.well-known/openid-configuration"
_PLACEHOLDER_CLIENT = "placeholder-client-id"

app = cdk.App()

AdvisorRuntimeStack(
    app,
    "AqmAdvisorRuntime",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEPLOY_ACCOUNT", _DEFAULT_ACCOUNT),
        region=os.environ.get("CDK_DEPLOY_REGION", _DEFAULT_REGION),
    ),
    model_inference_profile=os.environ.get("AQM_ADVISOR_MODEL_ID", _DEFAULT_MODEL),
    cognito_discovery_url=os.environ.get(
        "AQM_COGNITO_DISCOVERY_URL", _PLACEHOLDER_DISCOVERY
    ),
    cognito_client_id=os.environ.get("AQM_COGNITO_CLIENT_ID", _PLACEHOLDER_CLIENT),
)

app.synth()
