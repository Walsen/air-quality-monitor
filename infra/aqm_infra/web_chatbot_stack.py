"""The web chatbot: a Lambda serving the chat page + /chat, behind an HTTP API.

The Lambda proxies to the advisor runtime on Bedrock AgentCore, so its execution
role is granted `bedrock-agentcore:InvokeAgentRuntime` on exactly that runtime ARN
— nothing wider (least privilege, per the practices). The shared access key and
the runtime ARN arrive as deploy-time context, never committed; a synth with no
key still succeeds so `cdk synth` stays credential-free for CI, and the deploy is
refused without one.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct


class WebChatbotStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        service_dir: str,
        runtime_arn: str | None,
        region: str,
        access_key: str | None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Both the runtime ARN and the access key are required at DEPLOY, not synth.
        if not runtime_arn:
            cdk.Annotations.of(self).add_error(
                f"{construct_id}: no advisor runtime ARN. Deploy with "
                "-c chatbot_runtime_arn=<arn> (synth succeeds without it; deploy does not)."
            )
        if not access_key:
            cdk.Annotations.of(self).add_error(
                f"{construct_id}: no access key. Deploy with -c chatbot_access_key=<value>; "
                "the chatbot must not run as an open proxy to the advisor."
            )

        env = {
            "AQM_CHATBOT_RUNTIME_ARN": runtime_arn or "",
            "AQM_CHATBOT_REGION": region,
            "AQM_CHATBOT_ACCESS_KEY": access_key or "",
        }

        # Bundle the service source + pinned deps with uv, including its static/
        # directory (cp -r src/* carries src/aqm_chatbot/static/ along).
        code = lambda_.Code.from_asset(
            service_dir,
            bundling=BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                command=[
                    "bash",
                    "-c",
                    " && ".join(
                        [
                            "pip install uv -q",
                            "uv export --frozen --no-dev --no-emit-project "
                            "-o /tmp/req.txt 2>/dev/null || uv pip compile pyproject.toml -o /tmp/req.txt",
                            "pip install -r /tmp/req.txt -t /asset-output -q",
                            "cp -r src/* /asset-output/",
                        ]
                    ),
                ],
            ),
        )

        fn = lambda_.Function(
            self,
            "Fn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="aqm_chatbot.lambda_handler.handler",
            code=code,
            timeout=Duration.seconds(29),  # HTTP API integration ceiling
            memory_size=512,
            environment=env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # Least privilege: invoke ONLY the advisor runtime, nothing else on AgentCore.
        if runtime_arn:
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=[runtime_arn, f"{runtime_arn}/*"],
                )
            )

        http_api = apigw.HttpApi(
            self,
            "HttpApi",
            default_integration=integrations.HttpLambdaIntegration("Integration", fn),
        )

        CfnOutput(self, "ChatUrl", value=http_api.api_endpoint)
        CfnOutput(self, "FunctionName", value=fn.function_name)
