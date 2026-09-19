"""Offline assertions over the synthesized ``DemoDataRefreshStack`` template.

Feature: demo-data-refresh, Property 3 (offline synth) and Property 4 (least
privilege).

THIS SUITE TOUCHES NO ACCOUNT. ``Template.from_stack`` synthesises in-process and
emits CloudFormation without contacting an account; the stack is constructed with
an explicit ``env`` so ``Table.from_table_name`` builds each imported ARN from the
stack's own account/region. Table NAMES arrive as plain strings, so synth needs no
deploy-time material.

The assertions pin the facts the feature depends on and that a plausible edit could
silently break:

* a seed Lambda (ARM64, the documented seed handler string);
* it selects the dynamodb adapter for readings + sensor-registry and carries both
  table names, PUSH-only interface flags;
* an EventBridge rule at a 2-hour rate targeting that Lambda, with the
  ``Lambda::Permission`` EventBridge needs;
* the execution role holds DynamoDB actions scoped to ONLY the two tables the seed
  touches, read+write on both, never ``Resource: "*"``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aqm_infra.demo_data_refresh_stack import DemoDataRefreshStack
from aws_cdk import assertions

_REGION = "us-east-1"
_ACCOUNT = "111122223333"
_SEED_HANDLER = "aqm_ingestion.jobs.seed_lambda_handler.handler"

_READINGS_TABLE = "aqm-poc-readings"
_REGISTRY_TABLE = "aqm-poc-sensor-registry"


def _template() -> assertions.Template:
    app = cdk.App()
    stack = DemoDataRefreshStack(
        app,
        "TestDemoDataRefreshStack",
        env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        service_dir="../data-processing",
        region=_REGION,
        readings_table_name=_READINGS_TABLE,
        registry_table_name=_REGISTRY_TABLE,
    )
    return assertions.Template.from_stack(stack)


def _seed_function(template: assertions.Template) -> Mapping[str, Any]:
    """The seed Lambda, selected by its handler.

    ``log_retention`` provisions a second ``AWS::Lambda::Function`` (the LogRetention
    provider), so the seed function must be picked out by handler rather than assumed
    to be the only one.
    """
    functions = template.find_resources("AWS::Lambda::Function")
    seed = [
        fn
        for fn in functions.values()
        if fn["Properties"].get("Handler") == _SEED_HANDLER
    ]
    assert len(seed) == 1, f"expected exactly one seed Lambda, found {len(seed)}"
    return seed[0]


def test_it_provisions_the_seed_lambda() -> None:
    _template().has_resource_properties(
        "AWS::Lambda::Function",
        assertions.Match.object_like({"Handler": _SEED_HANDLER}),
    )


def test_the_seed_lambda_is_arm64() -> None:
    fn = _seed_function(_template())
    assert fn["Properties"]["Architectures"] == ["arm64"]


def test_the_seed_lambda_selects_the_two_dynamodb_stores() -> None:
    _template().has_resource_properties(
        "AWS::Lambda::Function",
        assertions.Match.object_like(
            {
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {
                            "AQM_ADAPTER_READINGS_STORE": "dynamodb",
                            "AQM_ADAPTER_SENSOR_REGISTRY_STORE": "dynamodb",
                            "AQM_AWS_REGION": _REGION,
                        }
                    )
                }
            }
        ),
    )


def test_the_seed_lambda_carries_the_two_table_names_in_env() -> None:
    variables = _seed_function(_template())["Properties"]["Environment"]["Variables"]
    assert variables["AQM_TABLE_READINGS"] == _READINGS_TABLE
    assert variables["AQM_TABLE_REGISTRY"] == _REGISTRY_TABLE


def test_the_seed_lambda_enables_exactly_one_batch_interface() -> None:
    # Same regression guard as the association stack: the config loader rejects
    # all-interfaces-off, so a batch job must pick one. PUSH is the honest batch
    # choice (no Cognito, no feed credential).
    variables = _seed_function(_template())["Properties"]["Environment"]["Variables"]
    assert variables["AQM_ENABLE_PUSH"] == "true"
    assert variables["AQM_ENABLE_SERVING"] == "false"
    assert variables["AQM_ENABLE_PULL"] == "false"


def test_it_provisions_an_eventbridge_schedule_rule_at_a_two_hour_rate() -> None:
    template = _template()
    template.resource_count_is("AWS::Events::Rule", 1)
    # The cadence is load-bearing: it must stay inside the freshness window. Pin the
    # 2-hour rate so a change to something laxer (which could open an "unavailable"
    # gap) fails here.
    template.has_resource_properties(
        "AWS::Events::Rule",
        assertions.Match.object_like({"ScheduleExpression": "rate(2 hours)"}),
    )


def test_the_schedule_rule_targets_the_seed_lambda() -> None:
    template = _template()
    functions = template.find_resources("AWS::Lambda::Function")
    seed_logical_ids = {
        logical_id
        for logical_id, fn in functions.items()
        if fn["Properties"].get("Handler") == _SEED_HANDLER
    }
    assert len(seed_logical_ids) == 1

    rules = template.find_resources("AWS::Events::Rule")
    assert len(rules) == 1
    (rule,) = rules.values()
    targets = rule["Properties"]["Targets"]
    assert targets, "the schedule rule must carry at least one target"
    target_ids: set[str] = set()
    for target in targets:
        arn = target.get("Arn")
        if isinstance(arn, dict) and "Fn::GetAtt" in arn:
            target_ids.add(str(arn["Fn::GetAtt"][0]))
    assert seed_logical_ids <= target_ids, (
        f"the schedule rule must target the seed Lambda; "
        f"targets={sorted(target_ids)} fn={sorted(seed_logical_ids)}"
    )


def test_eventbridge_is_permitted_to_invoke_the_function() -> None:
    _template().has_resource_properties(
        "AWS::Lambda::Permission",
        assertions.Match.object_like(
            {
                "Action": "lambda:InvokeFunction",
                "Principal": "events.amazonaws.com",
            }
        ),
    )


def test_the_execution_role_has_no_dynamodb_wildcard_grant() -> None:
    template = _template()
    policies = template.find_resources("AWS::IAM::Policy")
    assert policies, "the seed Lambda must have an execution-role policy"
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            names = [a for a in actions if isinstance(a, str)]
            if any(a.startswith("dynamodb:") for a in names):
                resource = statement.get("Resource")
                assert resource != "*", (
                    "DynamoDB grant must be scoped to the table ARNs, never Resource: '*'"
                )
                if isinstance(resource, list):
                    assert "*" not in resource, "no wildcard DynamoDB resource permitted"


def test_the_role_can_read_write_both_tables() -> None:
    # The seed upserts the registry AND writes the readings — read+write on BOTH.
    # Assert write actions are granted and both table ARNs are referenced.
    template = _template()
    policies = template.find_resources("AWS::IAM::Policy")
    granted: set[str] = set()
    referenced_arns: list[str] = []
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            for action in actions:
                if isinstance(action, str) and action.startswith("dynamodb:"):
                    granted.add(action)
            referenced_arns.extend(_arn_strings(statement.get("Resource")))

    for required in ("dynamodb:PutItem", "dynamodb:GetItem"):
        assert required in granted, f"{required} must be granted"

    joined = " ".join(referenced_arns)
    for table_name in (_READINGS_TABLE, _REGISTRY_TABLE):
        assert f"table/{table_name}" in joined, (
            f"the role must reference the {table_name} ARN; refs={referenced_arns}"
        )


def _arn_strings(resource: object) -> list[str]:
    found: list[str] = []
    if isinstance(resource, str):
        found.append(resource)
    elif isinstance(resource, dict):
        for value in resource.values():
            found.extend(_arn_strings(value))
    elif isinstance(resource, list):
        for item in resource:
            found.extend(_arn_strings(item))
    return found


def test_the_stack_synthesizes_offline() -> None:
    template = _template()
    json.dumps(template.to_json())
