"""A FastAPI service packaged as a Lambda behind an API Gateway HTTP API.

Deliberately minimal for a POC. The Lambda is built from the service's committed
source with its dependencies bundled by CDK inside a Python build image, so no
local Docker image or ECR repository is needed. The service's own `build_app` /
`build_runtime` startup runs at cold start and enforces its own auth and config.
"""

from __future__ import annotations

from collections.abc import Mapping

import aws_cdk as cdk
from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct


class LambdaRestStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        service_dir: str,
        handler: str,
        environment: Mapping[str, str],
        api_key_env: str,
        api_key_value: str | None,
        env: cdk.Environment | None = None,
        description: str | None = None,
    ) -> None:
        super().__init__(scope, construct_id, env=env, description=description)

        fn_env = dict(environment)
        # The API key is required at DEPLOY, not at synth: a synth with no key
        # still succeeds (keeps `cdk synth` credential-free), but deploying
        # without one is refused rather than shipping an unauthenticated service.
        if api_key_value:
            fn_env[api_key_env] = api_key_value
        else:
            cdk.Annotations.of(self).add_error(
                f"{construct_id}: no API key supplied. Deploy with "
                f"-c {construct_id.split('-')[-1]}_api_key=<value> "
                "(synth succeeds without it; deploy does not)."
            )

        # Bundle the service's source + pinned deps with uv, into the Lambda.
        # `uv export` pins to the committed lockfile so the function ships the
        # exact versions the tests gated.
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
                            # export production deps to a requirements file from the lock
                            "uv export --frozen --no-dev --no-emit-project "
                            "-o /tmp/req.txt 2>/dev/null || "
                            "uv pip compile pyproject.toml -o /tmp/req.txt",
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
            handler=handler,
            code=code,
            timeout=Duration.seconds(29),  # HTTP API integration ceiling
            memory_size=1024,
            environment=fn_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        http_api = apigw.HttpApi(
            self,
            "HttpApi",
            default_integration=integrations.HttpLambdaIntegration("Integration", fn),
        )

        CfnOutput(self, "ApiUrl", value=http_api.api_endpoint)
        CfnOutput(self, "FunctionName", value=fn.function_name)
