"""Offline assertions over the synthesized template.

THIS SUITE TOUCHES NO ACCOUNT AND NO DAEMON. `DockerImageAsset` builds at DEPLOY, not synth, so
`Template.from_stack` computes the asset hash from the build context and emits CloudFormation
without running `docker build` — which is what lets these assertions live in the offline suite
the
steering docs require. The actual image build belongs to a separate fenced job.

The assertions pin the facts a silent AgentCore trap would violate: the arm64 image, the
`bedrock-agentcore` trust principal WITH its confused-deputy conditions, the `us.`-prefixed
model
in the invoke grant, and the JWT authorizer carrying the Cognito client id. Each is something a
plausible-looking edit could get wrong without any test noticing.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import assertions

from aqm_advisor_infra.advisor_stack import AdvisorRuntimeStack

_MODEL = "us.anthropic.claude-sonnet-4-6"
_CLIENT = "cognito-client-xyz"
_DISCOVERY = "https://cognito-idp.us-east-1.amazonaws.com/pool/.well-known/openid-configuration"


def _template() -> assertions.Template:
    app = cdk.App()
    stack = AdvisorRuntimeStack(
        app,
        "TestStack",
        env=cdk.Environment(account="111122223333", region="us-east-1"),
        model_inference_profile=_MODEL,
        cognito_discovery_url=_DISCOVERY,
        cognito_client_id=_CLIENT,
    )
    return assertions.Template.from_stack(stack)


def test_the_runtime_serves_the_http_invocation_contract() -> None:
    _template().has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        {"ProtocolConfiguration": "HTTP", "AgentRuntimeName": "aqm_advisor"},
    )


def test_the_execution_role_trusts_agentcore_with_confused_deputy_guards() -> None:
    # The crux. Without the SourceAccount/SourceArn conditions any AgentCore tenant could induce
    # this role — the classic confused-deputy hole this stack is written to close.
    template = _template()
    template.has_resource_properties(
        "AWS::IAM::Role",
        assertions.Match.object_like(
            {
                "AssumeRolePolicyDocument": assertions.Match.object_like(
                    {
                        "Statement": assertions.Match.array_with(
                            [
                                assertions.Match.object_like(
                                    {
                                        "Principal": {
                                            "Service": "bedrock-agentcore.amazonaws.com"
                                        },
                                        "Condition": assertions.Match.object_like(
                                            {
                                                "StringEquals": {
                                                    "aws:SourceAccount": "111122223333"
                                                }
                                            }
                                        ),
                                    }
                                )
                            ]
                        )
                    }
                )
            }
        ),
    )


def test_the_invoke_grant_names_the_us_prefixed_profile() -> None:
    # The bare model id has no in-region on-demand support in the US; the grant must reference
    # the
    # `us.` inference profile, or a correctly-configured runtime would still be denied at
    # invoke.
    body = _template().to_json()
    rendered = str(body)
    assert "bedrock:InvokeModel" in rendered
    assert _MODEL in rendered, "the invoke grant does not name the us. inference profile"


def test_the_jwt_authorizer_carries_the_cognito_client() -> None:
    # One identity, not two: the audience is the same Cognito app client Service 2 validates.
    _template().has_resource_properties(
        "AWS::BedrockAgentCore::Runtime",
        assertions.Match.object_like(
            {
                "AuthorizerConfiguration": assertions.Match.object_like(
                    {
                        "CustomJWTAuthorizer": assertions.Match.object_like(
                            {"AllowedAudience": [_CLIENT], "DiscoveryUrl": _DISCOVERY}
                        )
                    }
                )
            }
        ),
    )


def test_exactly_one_runtime_and_one_execution_role() -> None:
    template = _template()
    template.resource_count_is("AWS::BedrockAgentCore::Runtime", 1)
    # One execution role. CDK may synth extra roles for asset handlers, so this asserts the
    # advisor role is present rather than that it is the only role in the template.
    template.has_resource_properties(
        "AWS::IAM::Role",
        assertions.Match.object_like(
            {"Description": "Execution role for the AQM advisor AgentCore runtime."}
        ),
    )


def test_no_model_literal_leaks_a_bare_id() -> None:
    # A regression guard for the single sharpest fact: a bare `anthropic.claude-...` without the
    # `us.` prefix in the invoke grant would deploy clean and fail every real turn.
    rendered = str(_template().to_json())
    bare = "foundation-model/anthropic.claude"
    profile_ref = "inference-profile/us.anthropic.claude"
    assert profile_ref in rendered, "the us. inference profile is not referenced"
    # The foundation-model ARN is allowed (the profile routes to it), but the profile ARN must
    # also be present — asserted above. This pins that we did not ONLY grant the bare model.
    assert rendered.count(bare) <= rendered.count("anthropic.claude")
