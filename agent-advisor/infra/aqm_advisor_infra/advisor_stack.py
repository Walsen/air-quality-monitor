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

THE JWT AUTHORIZER IS THE SAME COGNITO APP CLIENT SERVICE 2 USES. `allowed_audience` /
`allowed_clients` come from `AQM_COGNITO_CLIENT_ID`, which task 17 already made the advisor read
—
so the token AgentCore validates inbound is the token Service 2 validates on receipt, and there
is one identity, not two that can drift.

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

        bac.CfnRuntime(
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
                custom_jwt_authorizer=bac.CfnRuntime.CustomJWTAuthorizerConfigurationProperty(
                    discovery_url=cognito_discovery_url,
                    allowed_audience=[cognito_client_id],
                    allowed_clients=[cognito_client_id],
                ),
            ),
            environment_variables={
                "AQM_ADVISOR_MODEL_ID": model_inference_profile,
                "AQM_COGNITO_CLIENT_ID": cognito_client_id,
                "AQM_ADVISOR_ADAPTERS": (
                    "serving_client=http,guardrail_checker=bedrock,"
                    "advice_audit_store=memory,model=bedrock,clock=system,"
                    "association_trigger=recording"
                ),
            },
        )

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
