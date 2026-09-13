"""The AI Advisor Agent on Amazon Bedrock AgentCore Runtime (CDK).

Every fact below was verified against the installed `aws-cdk-lib==2.263.0` and against AWS docs
before it was written, because this deploy has traps that fail silently or leave a stack wedged.
The load-bearing ones, and how this stack avoids each:

THE IMAGE MUST EXIST BEFORE THE RUNTIME. A `CfnRuntime` pointing at an empty repository does not
come up. So the image is a `DockerImageAsset`, which the CDK deploy builds and pushes to the
bootstrap ECR repo BEFORE the runtime resource is created — the ordering is a dependency, not a
hope. A repository this stack created and referenced in the same deploy would race that
ordering.

ARM64 IS THE PLATFORM, NOT A PREFERENCE. AgentCore requires `linux/arm64`; an amd64 image fails
at container start with `exec format error`. `platform=Platform.LINUX_ARM64` pins it, and the
advisor's own Dockerfile already sets the same platform on its base, so the two agree.

THE EXECUTION ROLE IS THE ONE AGENTCORE ASSUMES, and its trust policy is exact: principal
`bedrock-agentcore.amazonaws.com`, with `aws:SourceAccount` and `aws:SourceArn` conditions that
close the confused-deputy hole (without them, any AgentCore tenant could induce this role). Its
permissions are the documented minimum — pull the image, write logs and traces, and invoke the
one model.

THE MODEL ID CARRIES THE `us.` PREFIX ON PURPOSE. `us.anthropic.claude-sonnet-4-6` is a
cross-region inference profile; the bare `anthropic.claude-sonnet-4-6` has no in-region
on-demand
support in any US region and errors at invoke. The `bedrock:InvokeModel` grant is scoped to both
the profile ARN and the underlying foundation-model ARNs the profile routes to, because the
service checks the resolved model, not the profile alone.

THE JWT AUTHORIZER VALIDATES THE ID TOKEN'S `aud`, NOT `client_id`. The runtime is invoked with
the Cognito ID token the whole system forwards (Service 2 requires token_use=id). That token
carries `aud` (= the app client id from `AQM_COGNITO_CLIENT_ID`, which task 17 already made the
advisor read) but has NO `client_id` claim — that claim is only on the Cognito access token. So
the authorizer sets `allowed_audience` ONLY: adding `allowed_clients` would make AgentCore
verify the absent `client_id` and 401 every real turn. Validating `aud` still binds the token to
the SAME Cognito app client Service 2 validates on receipt — one identity, not two that drift.

WHAT THIS STACK DOES NOT DO. It does not enable the Anthropic model (a one-time console form,
not
an API this can call), and it does not `cdk bootstrap` (that is an admin, cloud-touching step
run
once per environment). Both are recorded in the README as operator prerequisites.
"""

from __future__ import annotations

import pathlib

import aws_cdk as cdk
from aws_cdk import aws_bedrockagentcore as bac
from aws_cdk import aws_iam as iam
from aws_cdk.aws_ecr_assets import DockerImageAsset, Platform
from constructs import Construct

# The advisor package root (where the Dockerfile lives), resolved from THIS file rather than the
# CWD: infra/aqm_advisor_infra/advisor_stack.py -> agent-advisor/. Resolving from the CWD landed
# at
# the repo root during synth and DockerImageAsset failed to find the Dockerfile — a test caught
# it.
_ADVISOR_ROOT = str(pathlib.Path(__file__).resolve().parents[2])


class AdvisorRuntimeStack(cdk.Stack):
    """The advisor's AgentCore Runtime, its execution role, and its container image."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        model_inference_profile: str,
        cognito_discovery_url: str,
        cognito_client_id: str,
        serving_base_url: str,
        env: cdk.Environment,
    ) -> None:
        """Build the stack from values a deployment supplies, never from literals in code.

        Args:
            scope: the CDK app.
            construct_id: the stack id.
            model_inference_profile: e.g. ``us.anthropic.claude-sonnet-4-6`` — the `us.` prefix
            is
                required for on-demand use in a US region.
            cognito_discovery_url: the OIDC discovery URL of the Cognito user pool.
            cognito_client_id: the Cognito app client id — the SAME one Service 2 validates
            against.
            serving_base_url: the Service 2 serving base URL the advisor's HTTP ServingClient
                calls; supplied at deploy, never a literal in code.
            env: the target account and region, set EXPLICITLY so synth performs no account
            lookup.
        """
        super().__init__(scope, construct_id, env=env)

        image = DockerImageAsset(
            self,
            "AdvisorImage",
            directory=_ADVISOR_ROOT,
            platform=Platform.LINUX_ARM64,
        )

        execution_role = self._execution_role(image, model_inference_profile)

        runtime = bac.CfnRuntime(
            self,
            "AdvisorRuntime",
            agent_runtime_name="aqm_advisor",
            agent_runtime_artifact=bac.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=bac.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=image.image_uri,
                ),
            ),
            role_arn=execution_role.role_arn,
            # HTTP, not MCP/A2A/AGUI: the advisor serves the Req 32.1 invocation contract, not a
            # tool-server protocol.
            protocol_configuration="HTTP",
            network_configuration=bac.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC",
            ),
            authorizer_configuration=bac.CfnRuntime.AuthorizerConfigurationProperty(
                # Validate the `aud` claim ONLY, via allowed_audience. The runtime is
                # invoked with the Cognito ID token the whole system forwards (Service 2's
                # authenticator requires token_use=id). An ID token carries `aud` (= the app
                # client id) but has NO `client_id` claim — that claim is only on the Cognito
                # access token. When both allowed_audience and allowed_clients are set the
                # authorizer verifies ALL of them (allowed_clients validates `client_id`), so
                # an allowed_clients entry would reject every forwarded ID token with a 401
                # ("Claim 'client_id' value mismatch"). Validating `aud` alone still binds the
                # token to the SAME Cognito app client Service 2 validates — one identity, not
                # two that can drift.
                custom_jwt_authorizer=bac.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=cognito_discovery_url,
                    allowed_audience=[cognito_client_id],
                ),
            ),
            # ALLOWLIST THE INBOUND `Authorization` HEADER OR THE ADVISOR NEVER SEES THE JWT.
            # AgentCore STRIPS every inbound request header that is NOT on this allowlist before
            # the container receives it — and it does so EVEN WHEN the JWT authorizer above has
            # already accepted the token. The two are separate gates: the authorizer decides
            # whether to admit the request, the allowlist decides which headers survive into the
            # container. With no allowlist configured, `Authorization` is stripped, and the
            # advisor entrypoint (`_credential_from` in
            # agent-advisor/src/aqm_advisor/agentcore/app.py, which reads
            # `context.request_headers["Authorization"]`) sees no credential — so every turn
            # fails fast with `IdentityUnavailableError`, before any model or serving call. The
            # entrypoint's own docstring predicts exactly this: "arriving here without a usable
            # credential means the request-header allowlist is misconfigured."
            #
            # `Authorization` is explicitly PERMITTED for JWT auth per the AWS "Pass custom
            # headers to Amazon Bedrock AgentCore Runtime" docs — it is NOT one of the
            # restricted headers when a custom JWT authorizer is configured. Only
            # `Authorization` is listed; nothing else is forwarded.
            request_header_configuration=bac.CfnRuntime.RequestHeaderConfigurationProperty(
                request_header_allowlist=["Authorization"],
            ),
            # THE LOADER READS PER-PORT VARS, NOT A COMBINED STRING. The advisor's config
            # loader (agent-advisor/src/aqm_advisor/config/loader.py) resolves each adapter from
            # its own `AQM_ADVISOR_<PORT>` variable, falling back to the first registered
            # adapter — the scripted/local/memory OFFLINE defaults. It NEVER parses a combined
            # `AQM_ADVISOR_ADAPTERS` string. An earlier version set exactly that combined
            # string, which the loader silently ignored, so the runtime answered FULLY SCRIPTED
            # (CloudWatch: `advisor_composed model=scripted serving=scripted`) while the wiring
            # read as correct. So each selection that must change is its own variable here.
            #
            # Only the two ports that MUST leave their offline default are set:
            #   - serving_client=http + AQM_ADVISOR_SERVING_BASE_URL makes the advisor call the
            #     real Service 2 (forwarding the same Cognito JWT, so one identity end to end).
            #   - model=bedrock + AQM_ADVISOR_MODEL_ID + AQM_ADVISOR_MODEL_REGION makes the turn
            #     call the real Bedrock model (both the bedrock model and guardrail clients read
            #     config.model_region).
            # guardrail_checker, advice_audit_store, clock and association_trigger are LEFT to
            # their safe defaults (local / memory / system / recording): the local guardrail
            # needs no provisioned Bedrock Guardrail resource (we have none) and the domain
            # forbidden-claims rules run regardless, memory audit is the POC default, and
            # selecting the bedrock guardrail would demand a guardrail identifier we have not
            # created and would fail every turn.
            #
            # self.region is a concrete string at synth because app.py sets an explicit
            # cdk.Environment(region=...), so AQM_ADVISOR_MODEL_REGION renders as the literal
            # region (e.g. "us-east-1"), not an unresolved token.
            environment_variables={
                "AQM_ADVISOR_MODEL_ID": model_inference_profile,
                "AQM_COGNITO_CLIENT_ID": cognito_client_id,
                "AQM_ADVISOR_SERVING_BASE_URL": serving_base_url,
                "AQM_ADVISOR_SERVING_CLIENT": "http",
                "AQM_ADVISOR_MODEL": "bedrock",
                "AQM_ADVISOR_MODEL_REGION": self.region,
            },
        )

        # ORDER THE POLICY BEFORE THE RUNTIME. AgentCore validates the ECR image URI
        # SYNCHRONOUSLY during runtime creation, using the execution role — so the role's pull
        # permissions must already be attached when that validation runs. Those permissions live
        # on the role's inline DefaultPolicy, a SEPARATE `AWS::IAM::Policy` resource. Passing
        # `role_arn=execution_role.role_arn` (a string attribute) makes CloudFormation capture a
        # dependency on the Role but NOT on that policy, so the runtime could be created — and
        # its ECR URI validated — before the policy is attached, failing with an ECR access-
        # denied on an otherwise correctly-permissioned role. Make the dependency explicit so
        # CloudFormation creates and attaches the DefaultPolicy first.
        default_policy = execution_role.node.find_child("DefaultPolicy")
        runtime.node.add_dependency(default_policy)

    def _execution_role(
        self, image: DockerImageAsset, model_inference_profile: str
    ) -> iam.Role:
        """The role AgentCore assumes to run the advisor.

        The trust policy is the crux: principal ``bedrock-agentcore.amazonaws.com`` with the two
        confused-deputy conditions. `iam.ServicePrincipal` with `conditions` renders exactly the
        `aws:SourceAccount` / `aws:SourceArn` guards AWS documents.
        """
        role = iam.Role(
            self,
            "AdvisorExecutionRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:*"
                        )
                    },
                },
            ),
            description="Execution role for the AQM advisor AgentCore runtime.",
        )

        # Pull the image. GetAuthorizationToken is account-scoped by the service; the layer
        # reads
        # are scoped to the asset's own repository.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        image.repository.grant_pull(role)

        # Structured logs (Req 23) and OTel traces (Req 32.10) — the runtime's own log group.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:aws:logs:{self.region}:{self.account}:log-group:"
                    "/aws/bedrock-agentcore/runtimes/*"
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "cloudwatch:PutMetricData",
                ],
                resources=["*"],
            )
        )

        # Invoke the one model. Scoped to the inference profile AND the foundation models it
        # routes to, because the service authorizes the resolved model, not just the profile.
        model_name = model_inference_profile.split(".", 1)[-1]
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:"
                    f"inference-profile/{model_inference_profile}",
                    f"arn:aws:bedrock:*::foundation-model/{model_name}*",
                ],
            )
        )
        return role
