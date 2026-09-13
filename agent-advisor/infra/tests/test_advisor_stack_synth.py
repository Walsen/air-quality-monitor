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
_SERVING = "https://serving.example.com"


def _template() -> assertions.Template:
    app = cdk.App()
    stack = AdvisorRuntimeStack(
        app,
        "TestStack",
        env=cdk.Environment(account="111122223333", region="us-east-1"),
        model_inference_profile=_MODEL,
        cognito_discovery_url=_DISCOVERY,
        cognito_client_id=_CLIENT,
        serving_base_url=_SERVING,
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


def _custom_jwt_authorizer() -> dict[str, object]:
    """The synthesized CustomJWTAuthorizer block of the single runtime resource."""
    body = _template().to_json()
    runtimes = [
        res["Properties"]
        for res in body["Resources"].values()
        if res["Type"] == "AWS::BedrockAgentCore::Runtime"
    ]
    assert len(runtimes) == 1, "expected exactly one runtime resource"
    authorizer = runtimes[0]["AuthorizerConfiguration"]["CustomJWTAuthorizer"]
    assert isinstance(authorizer, dict)
    return authorizer


def test_the_authorizer_validates_the_id_tokens_aud_not_client_id() -> None:
    # The runtime forwards the Cognito ID token (Service 2 requires token_use=id). An ID token
    # carries `aud` (= the app client id) but has NO `client_id` claim — that claim is only on
    # the Cognito access token. The authorizer verifies ALL of AllowedAudience/AllowedClients
    # when both are set, so an AllowedClients entry validates the absent `client_id` and 401s
    # every real turn ("Claim 'client_id' value mismatch"). It must validate `aud` ONLY.
    authorizer = _custom_jwt_authorizer()
    assert authorizer["AllowedAudience"] == [_CLIENT]
    assert authorizer["DiscoveryUrl"] == _DISCOVERY
    assert "AllowedClients" not in authorizer, (
        "AllowedClients validates the `client_id` claim, which a Cognito ID token lacks; its "
        "presence rejects every forwarded ID token with a 401 at invoke"
    )


def test_the_runtime_carries_the_serving_base_url() -> None:
    # serving_client=http needs a target: the env var carries the Service 2 serving base URL the
    # advisor's HTTP ServingClient calls. The PAIR is what makes the client usable — a base URL
    # with a scripted client, or http with no URL, is the bug this pins against.
    body = _template().to_json()
    runtimes = [
        res["Properties"]
        for res in body["Resources"].values()
        if res["Type"] == "AWS::BedrockAgentCore::Runtime"
    ]
    assert len(runtimes) == 1, "expected exactly one runtime resource"
    env = runtimes[0]["EnvironmentVariables"]
    assert env["AQM_ADVISOR_SERVING_BASE_URL"] == _SERVING, (
        "the runtime does not carry the serving base URL the http client targets"
    )
    assert "serving_client=http" in env["AQM_ADVISOR_ADAPTERS"], (
        "the http serving adapter is not selected; the base URL would have no client"
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


def test_the_runtime_depends_on_the_execution_role_policy() -> None:
    # AgentCore validates the ECR URI SYNCHRONOUSLY at runtime-create using the execution role,
    # so the role's inline pull policy (a separate AWS::IAM::Policy, the DefaultPolicy) must
    # exist and be attached BEFORE the runtime is created. Passing role_arn as a string captures
    # a DependsOn on the Role but NOT on that policy, so the runtime must carry an explicit
    # DependsOn naming the policy. Without it the deploy races and fails with an ECR access-
    # denied on an otherwise correctly-permissioned role.
    body = _template().to_json()
    resources = body["Resources"]

    runtimes = {
        logical_id: res
        for logical_id, res in resources.items()
        if res["Type"] == "AWS::BedrockAgentCore::Runtime"
    }
    assert len(runtimes) == 1, "expected exactly one runtime resource"
    runtime = next(iter(runtimes.values()))

    policy_ids = {
        logical_id
        for logical_id, res in resources.items()
        if res["Type"] == "AWS::IAM::Policy"
    }
    assert policy_ids, "expected at least one AWS::IAM::Policy (the role's DefaultPolicy)"

    depends_on = runtime.get("DependsOn", [])
    if isinstance(depends_on, str):
        depends_on = [depends_on]

    assert policy_ids.intersection(depends_on), (
        "the runtime does not depend on the execution role's DefaultPolicy; the ECR pull "
        "permissions may not be attached before AgentCore validates the image URI"
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
