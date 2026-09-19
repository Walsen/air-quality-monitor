"""The scheduled demo-data refresh Lambda, on an EventBridge schedule.

Feature: demo-data-refresh.

This stack keeps a CURRENT demo reading available throughout the hackathon window
by re-running the existing `aqm_ingestion.jobs.seed_readings` job on a schedule. It
is the DEPLOYMENT of an existing job, not new logic — a near-copy of
`AssociationStack`: a bundled Lambda that runs the seed handler, an EventBridge rule
that invokes it on a cadence, and an execution role scoped to exactly the two
tables the seed touches.

Three things are load-bearing:

* **The seed touches two stores, so two tables are wired, BOTH read+write.** The
  seed upserts each demo site into the `sensor-registry` store AND writes the
  per-site readings into the `readings` store — so, unlike the association job
  (which only READS the registry), this job needs read+write on both. The
  environment selects the `dynamodb` adapter for both ports and carries both table
  names.
* **IAM is scoped to exactly those two tables.** The tables are OWNED by the serving
  stack; this stack imports them by name with `Table.from_table_name` purely to
  attach a scoped grant, never recreating them. Importing by name keeps synth
  offline: the ARN is built from the stack's own explicit account and region.
* **The cadence is inside the freshness window, deliberately.** The serving side
  counts a reading as "current" only if its interval start is within
  `AQM_FRESHNESS_HOURS` (default 3h). `Schedule.rate(Duration.hours(2))` keeps the
  newest seeded reading at most ~2h old, comfortably inside 3h even if one run is
  late. If an operator lowers the freshness window below the cadence, the "always
  current" guarantee breaks — the coupling is noted in the spec and DEPLOY.md.

The Lambda handler string `aqm_ingestion.jobs.seed_lambda_handler.handler` adapts
the Lambda calling convention to `seed_readings.main`; it adds no seeding logic.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    Stack,
)
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct


class DemoDataRefreshStack(Stack):
    """The demo-data seed Lambda plus the EventBridge schedule that runs it."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        service_dir: str,
        region: str,
        readings_table_name: str,
        registry_table_name: str,
        env: cdk.Environment | None = None,
        description: str | None = None,
    ) -> None:
        """Provision the Lambda, the schedule, and least-privilege IAM.

        Args:
            scope: the CDK app or parent construct.
            construct_id: the stack's logical id.
            service_dir: the ingestion service source directory to bundle
                (``../data-processing``).
            region: the deploy region, carried to the Lambda as ``AQM_AWS_REGION``.
            readings_table_name: the ``readings`` table name (owned by the serving
                stack); imported here to scope a read+write grant, since the seed
                writes the readings history.
            registry_table_name: the ``sensor-registry`` table name; imported here to
                scope a read+write grant, since the seed upserts the demo site
                metadata.
            env: the target account and region, set EXPLICITLY so synthesis (and the
                imported-table ARNs) perform no account lookup.
            description: an optional CloudFormation stack description.
        """
        super().__init__(scope, construct_id, env=env, description=description)

        # --- The seed Lambda's environment ------------------------------------------
        #
        # seed_readings reads/writes two stores: it upserts the registry and writes the
        # readings. Both are the dynamodb adapter and both table names are carried. The
        # composition reads _table("readings") -> AQM_TABLE_READINGS and
        # _table("registry") -> AQM_TABLE_REGISTRY.
        #
        # Interface flags mirror the association stack: the config loader's
        # _validate_interfaces rejects all-interfaces-off, and PUSH is the honest batch
        # choice (needs no Cognito, no feed credential).
        fn_env = {
            "AQM_ENABLE_SERVING": "false",
            "AQM_ENABLE_PULL": "false",
            "AQM_ENABLE_PUSH": "true",
            "AQM_AWS_REGION": region,
            "AQM_LOG_LEVEL": "info",
            "AQM_ADAPTER_READINGS_STORE": "dynamodb",
            "AQM_ADAPTER_SENSOR_REGISTRY_STORE": "dynamodb",
            "AQM_TABLE_READINGS": readings_table_name,
            "AQM_TABLE_REGISTRY": registry_table_name,
        }

        # Bundle the service source + pinned deps with uv, ARM64. Copied verbatim from
        # the association/serving/chatbot stacks: CDK runs bundling as uid 1000 with no
        # writable HOME, so point every cache at /tmp or uv fails on /.cache.
        code = lambda_.Code.from_asset(
            service_dir,
            bundling=BundlingOptions(
                platform="linux/arm64",
                image=cdk.DockerImage("public.ecr.aws/sam/build-python3.12:latest-arm64"),
                command=[
                    "bash",
                    "-c",
                    " && ".join(
                        [
                            "export HOME=/tmp XDG_CACHE_HOME=/tmp UV_CACHE_DIR=/tmp/uv-cache "
                            "PIP_CACHE_DIR=/tmp/pip-cache",
                            "pip install uv -q",
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
            architecture=lambda_.Architecture.ARM_64,
            handler="aqm_ingestion.jobs.seed_lambda_handler.handler",
            code=code,
            timeout=Duration.minutes(5),  # a batch seed, not an HTTP request
            memory_size=1024,
            environment=fn_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # --- Least-privilege IAM ----------------------------------------------------
        #
        # The tables are owned by the serving stack. Import them by NAME only to attach
        # a scoped grant — from_table_name builds the ARN from this stack's explicit
        # account/region, so no account is contacted at synth. Both get read+write:
        # the seed upserts the registry AND writes the readings.
        readings_table = dynamodb.Table.from_table_name(
            self, "ImportedReadingsTable", readings_table_name
        )
        sensor_registry_table = dynamodb.Table.from_table_name(
            self, "ImportedSensorRegistryTable", registry_table_name
        )
        readings_table.grant_read_write_data(fn)
        sensor_registry_table.grant_read_write_data(fn)

        # --- The schedule -----------------------------------------------------------
        #
        # Every 2h, inside the 3h freshness window with a 1h margin, so a single late
        # run cannot open a gap that reads as "unavailable". Adding the Lambda as a
        # target makes CDK emit the Lambda::Permission that lets EventBridge invoke it.
        schedule = events.Rule(
            self,
            "Schedule",
            schedule=events.Schedule.rate(Duration.hours(2)),
            description="Refreshes the demo readings so a current reading always exists.",
        )
        schedule.add_target(targets.LambdaFunction(fn))

        CfnOutput(self, "FunctionName", value=fn.function_name)
        CfnOutput(self, "ScheduleRuleName", value=schedule.rule_name)
