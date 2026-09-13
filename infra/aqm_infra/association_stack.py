"""The scheduled association-derivation Lambda, on an EventBridge schedule.

Feature: personal-diary-memory (Requirements 4.3, 7.1, 8.1, 8.4).

This stack provisions the job that turns diary history into Learned_Thresholds —
the "take history into account" part of the feature. It is the deployment of an
existing domain job, ``aqm_ingestion.jobs.association.AssociationJob``, not new
logic: the stack is a Lambda that runs that job, an EventBridge rule that invokes
it on a schedule, and an execution role scoped to exactly the tables the job
reads and writes.

Three things are load-bearing and worth stating, because each is what a
well-meaning edit could silently reverse:

* **The job touches four stores, so four tables are wired.** ``AssociationJob``
  is assembled by ``build_association_job``, which constructs a ``GeoSelector``.
  It reads the ``profiles`` store (``get`` — to find a user), the ``symptom-log``
  store (``query_window`` for the diary AND ``put_learned_thresholds`` to write
  the result — so read AND write), the ``readings`` store (``query_window`` for
  the exposure history — read only), and the ``sensor-registry`` store (read
  only — the ``GeoSelector`` resolves the user's sites from the registry). The
  environment therefore selects the ``dynamodb`` adapter for all four ports and
  carries all four table names. Wiring fewer would leave the job unable to
  resolve a user's sites (and therefore their exposure), and it would derive
  nothing — a cold start would fail ``resolve_and_validate`` on the missing
  fourth port.
* **IAM is scoped to exactly those four tables.** The tables are OWNED by the
  serving stack (Task 2); this stack imports them by name with
  ``Table.from_table_name`` purely to attach a scoped grant, never recreating
  them. ``grant_read_write_data`` on the symptom-log ARN and ``grant_read_data``
  on the profiles, readings, and sensor-registry ARNs scope the DynamoDB actions
  to those ARNs (and their indexes), never ``Resource: "*"``. Importing by name
  keeps synth offline: the ARN is built from the stack's own (explicit) account
  and region, resolving nothing from a live account.
* **The schedule is a demo cadence.** ``Schedule.rate(Duration.hours(1))`` runs
  the derivation hourly so a freshly recorded diary entry influences advice within
  a demo window (design Open Question 3). The requirement is only that the
  derivation "runs on its own schedule"; the exact rate is a POC choice, and an
  on-demand ``just`` invoke (Task 15) covers the "don't wait an hour" case.

The Lambda handler string ``aqm_ingestion.jobs.lambda_handler.handler`` is
referenced here but WRITTEN in Task 8; this stack only provisions the Lambda that
will run it. Table names are the CDK-generated names from the serving stack,
passed in as strings, so nothing about identity or storage is committed.
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


class AssociationStack(Stack):
    """The association-derivation Lambda plus the EventBridge schedule that runs it."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        service_dir: str,
        region: str,
        profiles_table_name: str,
        symptom_log_table_name: str,
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
            profiles_table_name: the ``profiles`` table name (owned by the serving
                stack); imported here to scope a read grant, since the job reads a
                profile to resolve the user's sites.
            symptom_log_table_name: the ``symptom-log`` table name; imported here to
                scope a read+write grant, since the job reads the diary AND writes the
                Learned_Thresholds back to it.
            readings_table_name: the ``readings`` table name; imported here to scope a
                read grant, since the job reads the exposure history.
            registry_table_name: the ``sensor-registry`` table name; imported here to
                scope a read grant, since the ``GeoSelector`` reads the registry to
                resolve the user's sites.
            env: the target account and region, set EXPLICITLY so synthesis (and the
                imported-table ARNs) perform no account lookup.
            description: an optional CloudFormation stack description.
        """
        super().__init__(scope, construct_id, env=env, description=description)

        # --- The association Lambda's environment -----------------------------------
        #
        # AssociationJob reads four stores, so all four are the dynamodb adapter and
        # all four table names are carried. The composition reads _table("profiles")
        # -> AQM_TABLE_PROFILES, _table("readings") -> AQM_TABLE_READINGS,
        # _table("registry") -> AQM_TABLE_REGISTRY, and
        # _required_setting("AQM_SYMPTOM_LOG_TABLE"). Cold start's resolve_and_validate
        # fails fast on a missing table name (Req 8.5).
        #
        # Interface flags: the config loader's _validate_interfaces (Req 26.10) rejects
        # all-interfaces-off — at least one of push/pull/serving must be enabled — so a
        # batch derivation (which has no natural interface) still has to pick one.
        # PUSH is the honest choice: it is the ingestion-side/batch interface and needs
        # no Cognito (serving would falsely imply an HTTP API + Cognito authenticator)
        # and no feed credential (pull would demand one the batch job does not have).
        fn_env = {
            "AQM_ENABLE_SERVING": "false",
            "AQM_ENABLE_PULL": "false",
            "AQM_ENABLE_PUSH": "true",
            "AQM_AWS_REGION": region,
            "AQM_LOG_LEVEL": "info",
            # Adapter selection: the loader reads AQM_ADAPTER_<PORT_UPPER>. The job
            # reads profiles + readings + sensor registry and reads/writes the symptom
            # log.
            "AQM_ADAPTER_PROFILE_STORE": "dynamodb",
            "AQM_ADAPTER_SYMPTOM_LOG_STORE": "dynamodb",
            "AQM_ADAPTER_READINGS_STORE": "dynamodb",
            "AQM_ADAPTER_SENSOR_REGISTRY_STORE": "dynamodb",
            # Table names: the exact keys the composition reads. Generated names
            # (from the serving stack), passed in as strings, so nothing is committed.
            "AQM_TABLE_PROFILES": profiles_table_name,
            "AQM_SYMPTOM_LOG_TABLE": symptom_log_table_name,
            "AQM_TABLE_READINGS": readings_table_name,
            "AQM_TABLE_REGISTRY": registry_table_name,
        }

        # Bundle the service source + pinned deps with uv, ARM64 to match the ARM_64
        # function below. Copied verbatim from the serving/chatbot stacks: CDK runs
        # bundling as uid 1000 with no writable HOME, so point every cache at /tmp or
        # uv fails on /.cache.
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
            # Written in Task 8; only referenced here.
            handler="aqm_ingestion.jobs.lambda_handler.handler",
            code=code,
            timeout=Duration.minutes(5),  # a batch derivation, not an HTTP request
            memory_size=1024,
            environment=fn_env,
            log_retention=logs.RetentionDays.ONE_WEEK,
        )

        # --- Least-privilege IAM ----------------------------------------------------
        #
        # The tables are owned by the serving stack (Task 2). Import them by NAME only
        # to attach a scoped grant — from_table_name builds the ARN from this stack's
        # explicit account/region, so no account is contacted at synth. Grants scope
        # the DynamoDB actions to each table ARN, never Resource: "*".
        profiles_table = dynamodb.Table.from_table_name(
            self, "ImportedProfilesTable", profiles_table_name
        )
        symptom_log_table = dynamodb.Table.from_table_name(
            self, "ImportedSymptomLogTable", symptom_log_table_name
        )
        readings_table = dynamodb.Table.from_table_name(
            self, "ImportedReadingsTable", readings_table_name
        )
        sensor_registry_table = dynamodb.Table.from_table_name(
            self, "ImportedSensorRegistryTable", registry_table_name
        )

        # symptom-log: read the diary (query_window) AND write the Learned_Thresholds
        # (put_learned_thresholds) -> read+write.
        symptom_log_table.grant_read_write_data(fn)
        # profiles + readings + sensor-registry: read only (get / query_window; the
        # GeoSelector reads the registry to resolve the user's sites).
        profiles_table.grant_read_data(fn)
        readings_table.grant_read_data(fn)
        sensor_registry_table.grant_read_data(fn)

        # --- The schedule -----------------------------------------------------------
        #
        # Hourly for the demo (design Open Question 3), so a freshly recorded diary
        # entry influences advice within the demo window. Adding the Lambda as a target
        # makes CDK emit the Lambda::Permission that lets EventBridge invoke it.
        schedule = events.Rule(
            self,
            "Schedule",
            schedule=events.Schedule.rate(Duration.hours(1)),
            description="Runs the association derivation (diary history -> Learned_Thresholds).",
        )
        schedule.add_target(targets.LambdaFunction(fn))

        CfnOutput(self, "FunctionName", value=fn.function_name)
        CfnOutput(self, "ScheduleRuleName", value=schedule.rule_name)


__all__ = ["AssociationStack"]
