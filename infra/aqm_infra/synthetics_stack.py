"""CloudWatch Synthetics canaries for the deployed serving API, with alarms.

Feature: observability-xray-synthetics.

This stack stands up scheduled black-box probes of the live serving API and alarms
when they fail, so an outage or a regression (a 5xx, an auth break, or — in strict
mode — an empty snapshot) pages instead of waiting for a user to notice. It is the
active-monitoring complement to X-Ray: X-Ray explains a request AFTER it happens,
a canary continuously asserts the request still works.

Two canaries, chosen because liveness and function fail for different reasons and
must alarm independently:

* ``serving-health`` — GET ``/health``, no credential. Proves the front door and
  Lambda are up. First to fire when the stack is simply down.
* ``serving-authenticated`` — obtains a Cognito token from a Secrets Manager secret
  and calls ``/v1/air-quality/me``, asserting authorisation and a well-formed body.
  This is what a real user does; it catches an auth break a health check cannot.

Load-bearing decisions:

* **The target URL and Cognito details arrive as constructor params, never
  hard-coded** — same rule the other stacks follow. At synth they may be ``None``
  (placeholder), so ``cdk synth`` stays credential-free for CI; the deploy is refused
  without them.
* **No credential is committed or placed in an env var.** The authenticated canary
  fetches its client-credentials from a Secrets Manager secret at run time; this
  stack grants the canary role ``secretsmanager:GetSecretValue`` on exactly that
  secret ARN, never ``Resource: "*"``.
* **The traced request path is the SERVER side, already covered.** The serving
  Lambda runs X-Ray Active, so a canary's request appears on the service map via the
  Lambda's own segment. Canary-side ``active_tracing`` is a Node/Puppeteer-only
  feature (the Python-Selenium runtime rejects it), and it is not needed here: these
  are HTTP probes, not browser flows, so the client-side segment adds little over the
  server trace already captured.
* **Every canary has a CloudWatch alarm on ``SuccessPercent < 100``** wired to an
  SNS topic, so a failure notifies rather than sitting silent in a dashboard.
"""

from __future__ import annotations

import pathlib

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as sns_subscriptions
from aws_cdk import aws_synthetics as synthetics
from constructs import Construct

# The canary scripts live beside this stack under ``canaries/python/`` — the Python
# Synthetics runtime REQUIRES the handler at ``python/<module>.py`` inside the asset,
# so the asset root is ``canaries/`` and each handler is ``<module>.handler``.
# Resolved from THIS file rather than the CWD (the resolve-from-__file__ discipline
# the advisor stack documents), so synth finds them wherever cdk is invoked from.
_CANARY_DIR = pathlib.Path(__file__).resolve().parent.parent / "canaries"

_RUNTIME = synthetics.Runtime.SYNTHETICS_PYTHON_SELENIUM_4_0


class SyntheticsStack(Stack):
    """Scheduled canaries over the serving API, their artifacts bucket, and alarms."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        target_url: str | None,
        cognito_token_url: str | None,
        cognito_secret_name: str | None,
        require_reading: bool = False,
        alarm_email: str | None = None,
        env: cdk.Environment | None = None,
        description: str | None = None,
    ) -> None:
        """Provision the canaries, alarms, SNS topic, and least-privilege IAM.

        Args:
            scope: the CDK app or parent construct.
            construct_id: the stack's logical id.
            target_url: the serving API base URL (the serving stack's ``ApiUrl``
                output). Required at DEPLOY; ``None`` allowed at synth.
            cognito_token_url: the Cognito token endpoint for the authenticated
                canary. Required at deploy for that canary.
            cognito_secret_name: the Secrets Manager secret NAME holding the
                client-credentials JSON (``client_id``/``client_secret``/``scope``).
                Required at deploy for the authenticated canary.
            require_reading: when True, the authenticated canary fails on an
                authorised-but-empty snapshot (data-freshness check). Default False.
            alarm_email: an optional email subscribed to the alarm SNS topic.
            env: the target account and region, set EXPLICITLY so synthesis performs
                no account lookup.
            description: an optional CloudFormation stack description.
        """
        super().__init__(scope, construct_id, env=env, description=description)

        # target_url is required at DEPLOY, not synth: a synth with None still
        # succeeds (keeps `cdk synth` credential-free for CI); deploying without it is
        # refused rather than shipping canaries that probe nothing.
        if not target_url:
            cdk.Annotations.of(self).add_error(
                f"{construct_id}: no target_url. Deploy with the serving stack's ApiUrl "
                "output (synth succeeds without it; deploy does not)."
            )

        # One artifacts bucket for all canaries: screenshots, HAR and run logs. DESTROY
        # so the POC tears down cleanly; encrypted at rest.
        artifacts = s3.Bucket(
            self,
            "CanaryArtifacts",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(14))],
        )

        # The alarm sink. An optional email subscription so a human is paged; the topic
        # exists regardless so other subscribers (a chatbot, a pager) can attach later.
        alarm_topic = sns.Topic(self, "CanaryAlarms", display_name="AQM canary alarms")
        if alarm_email:
            alarm_topic.add_subscription(
                sns_subscriptions.EmailSubscription(alarm_email)
            )

        every_5_min = synthetics.Schedule.rate(Duration.minutes(5))

        # --- Canary 1: unauthenticated liveness -------------------------------------
        health = synthetics.Canary(
            self,
            "ServingHealth",
            canary_name="aqm-serving-health",
            runtime=_RUNTIME,
            test=synthetics.Test.custom(
                code=synthetics.Code.from_asset(str(_CANARY_DIR)),
                handler="serving_health.handler",
            ),
            schedule=every_5_min,
            environment_variables={"TARGET_URL": target_url or ""},
            artifacts_bucket_location=synthetics.ArtifactsBucketLocation(
                bucket=artifacts, prefix="serving-health"
            ),
            success_retention_period=Duration.days(7),
            failure_retention_period=Duration.days(14),
        )

        # --- Canary 2: authenticated functional check -------------------------------
        auth = synthetics.Canary(
            self,
            "ServingAuthenticated",
            canary_name="aqm-serving-auth",
            runtime=_RUNTIME,
            test=synthetics.Test.custom(
                code=synthetics.Code.from_asset(str(_CANARY_DIR)),
                handler="serving_authenticated.handler",
            ),
            schedule=every_5_min,
            environment_variables={
                "TARGET_URL": target_url or "",
                "COGNITO_TOKEN_URL": cognito_token_url or "",
                "COGNITO_SECRET_NAME": cognito_secret_name or "",
                "REQUIRE_READING": "true" if require_reading else "false",
            },
            artifacts_bucket_location=synthetics.ArtifactsBucketLocation(
                bucket=artifacts, prefix="serving-authenticated"
            ),
            success_retention_period=Duration.days(7),
            failure_retention_period=Duration.days(14),
        )

        # Least privilege: the authenticated canary reads exactly ONE secret. Import by
        # name to build the ARN from this stack's explicit account/region (no account
        # lookup at synth) and grant read on it alone, never Resource: "*".
        if cognito_secret_name:
            secret = secretsmanager.Secret.from_secret_name_v2(
                self, "CognitoClientSecret", cognito_secret_name
            )
            # The Canary is not itself an IGrantable — grant on its execution role.
            secret.grant_read(auth.role)
        else:
            cdk.Annotations.of(self).add_error(
                f"{construct_id}: no cognito_secret_name. The authenticated canary needs a "
                "Secrets Manager secret holding the client-credentials (synth ok; deploy not)."
            )

        # An alarm per canary on SuccessPercent: a single failed run (0%) breaches the
        # <100 threshold, so a real outage pages on the next evaluation rather than
        # waiting for a sustained average to sag.
        for name, canary in (("Health", health), ("Authenticated", auth)):
            alarm = cloudwatch.Alarm(
                self,
                f"{name}FailureAlarm",
                metric=canary.metric_success_percent(period=Duration.minutes(5)),
                threshold=100,
                comparison_operator=cloudwatch.ComparisonOperator.LESS_THAN_THRESHOLD,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.BREACHING,
                alarm_description=f"AQM {name} canary success dropped below 100%.",
            )
            alarm.add_alarm_action(cw_actions.SnsAction(alarm_topic))

        self.alarm_topic_arn = alarm_topic.topic_arn
        CfnOutput(self, "AlarmTopicArn", value=alarm_topic.topic_arn)
        CfnOutput(self, "HealthCanaryName", value=health.canary_name)
        CfnOutput(self, "AuthenticatedCanaryName", value=auth.canary_name)
        CfnOutput(self, "ArtifactsBucketName", value=artifacts.bucket_name)


__all__ = ["SyntheticsStack"]
