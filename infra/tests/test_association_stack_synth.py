"""Offline assertions over the synthesized ``AssociationStack`` template.

Feature: personal-diary-memory, Property 7 — offline guarantee.

THIS SUITE TOUCHES NO ACCOUNT. ``Template.from_stack`` runs synthesis in-process
and emits CloudFormation without contacting an account; the stack is constructed
with an explicit ``env`` so ``Table.from_table_name`` builds each imported ARN
from the stack's own account/region rather than resolving anything from a live
account. Table NAMES arrive as plain strings (the serving stack in Task 2 owns
the tables; this stack only imports them by name to scope IAM), so synth needs no
deploy-time material at all.

The assertions pin the facts Task 3 depends on and that a plausible edit could
silently break:

* an association Lambda (ARM64, the documented association handler string);
* an EventBridge schedule rule carrying a ``ScheduleExpression`` and targeting
  that Lambda;
* the ``Lambda::Permission`` CDK adds so EventBridge may invoke the function;
* the execution role holding DynamoDB actions scoped to ONLY the three tables the
  ``AssociationJob`` touches (symptom-log read+write, readings read, profiles
  read) — never a ``Resource: "*"`` DynamoDB grant.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aqm_infra.association_stack import AssociationStack
from aws_cdk import assertions

_REGION = "us-east-1"
_ACCOUNT = "111122223333"
_ASSOCIATION_HANDLER = "aqm_ingestion.jobs.lambda_handler.handler"

_PROFILES_TABLE = "aqm-poc-profiles"
_SYMPTOM_LOG_TABLE = "aqm-poc-symptom-log"
_READINGS_TABLE = "aqm-poc-readings"


def _association_function(template: assertions.Template) -> Mapping[str, Any]:
    """The association Lambda, selected by its handler.

    ``log_retention`` provisions a second ``AWS::Lambda::Function`` (the
    LogRetention custom-resource provider), so the association function must be
    picked out by handler rather than assumed to be the only one.
    """
    functions = template.find_resources("AWS::Lambda::Function")
    association = [
        fn
        for fn in functions.values()
        if fn["Properties"].get("Handler") == _ASSOCIATION_HANDLER
    ]
    assert len(association) == 1, (
        f"expected exactly one association Lambda, found {len(association)}"
    )
    return association[0]


def _template() -> assertions.Template:
    app = cdk.App()
    stack = AssociationStack(
        app,
        "TestAssociationStack",
        env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        service_dir="../data-processing",
        region=_REGION,
        profiles_table_name=_PROFILES_TABLE,
        symptom_log_table_name=_SYMPTOM_LOG_TABLE,
        readings_table_name=_READINGS_TABLE,
    )
    return assertions.Template.from_stack(stack)


def test_it_provisions_the_association_lambda() -> None:
    _template().has_resource_properties(
        "AWS::Lambda::Function",
        assertions.Match.object_like({"Handler": _ASSOCIATION_HANDLER}),
    )


def test_the_association_lambda_is_arm64() -> None:
    template = _template()
    fn = _association_function(template)
    assert fn["Properties"]["Architectures"] == ["arm64"]


def test_the_association_lambda_selects_the_three_dynamodb_stores() -> None:
    # The AssociationJob reads profiles (get), symptom-log (query + write learned
    # thresholds), and readings (query_window). All three stores must be the
    # dynamodb adapter, selected via AQM_ADAPTER_<NAME_UPPER>.
    _template().has_resource_properties(
        "AWS::Lambda::Function",
        assertions.Match.object_like(
            {
                "Environment": {
                    "Variables": assertions.Match.object_like(
                        {
                            "AQM_ADAPTER_PROFILE_STORE": "dynamodb",
                            "AQM_ADAPTER_SYMPTOM_LOG_STORE": "dynamodb",
                            "AQM_ADAPTER_READINGS_STORE": "dynamodb",
                            "AQM_AWS_REGION": _REGION,
                        }
                    )
                }
            }
        ),
    )


def test_the_association_lambda_carries_the_three_table_names_in_env() -> None:
    # The composition reads _table("profiles") -> AQM_TABLE_PROFILES,
    # _table("readings") -> AQM_TABLE_READINGS, and
    # _required_setting("AQM_SYMPTOM_LOG_TABLE"). All three must be present and
    # carry the names the serving stack generated (passed in as strings here).
    variables = _association_function(_template())["Properties"]["Environment"][
        "Variables"
    ]
    assert variables["AQM_TABLE_PROFILES"] == _PROFILES_TABLE
    assert variables["AQM_SYMPTOM_LOG_TABLE"] == _SYMPTOM_LOG_TABLE
    assert variables["AQM_TABLE_READINGS"] == _READINGS_TABLE


def test_it_provisions_an_eventbridge_schedule_rule() -> None:
    template = _template()
    template.resource_count_is("AWS::Events::Rule", 1)
    template.has_resource_properties(
        "AWS::Events::Rule",
        assertions.Match.object_like(
            {"ScheduleExpression": assertions.Match.any_value()}
        ),
    )


def test_the_schedule_rule_targets_the_association_lambda() -> None:
    # The rule must have a Target whose Arn is the association function's ARN.
    template = _template()
    functions = template.find_resources("AWS::Lambda::Function")
    association_logical_ids = {
        logical_id
        for logical_id, fn in functions.items()
        if fn["Properties"].get("Handler") == _ASSOCIATION_HANDLER
    }
    assert len(association_logical_ids) == 1

    rules = template.find_resources("AWS::Events::Rule")
    assert len(rules) == 1
    (rule,) = rules.values()
    targets = rule["Properties"]["Targets"]
    assert targets, "the schedule rule must carry at least one target"
    target_ids = set()
    for target in targets:
        arn = target.get("Arn")
        if isinstance(arn, dict) and "Fn::GetAtt" in arn:
            target_ids.add(str(arn["Fn::GetAtt"][0]))
    assert association_logical_ids <= target_ids, (
        "the schedule rule must target the association Lambda; "
        f"targets={sorted(target_ids)} fn={sorted(association_logical_ids)}"
    )


def test_eventbridge_is_permitted_to_invoke_the_function() -> None:
    # Adding the Lambda as an EventBridge target makes CDK emit a
    # Lambda::Permission with principal events.amazonaws.com. Assert it exists.
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
    # Least privilege: grants scope to the imported table ARNs. No DynamoDB policy
    # statement may name Resource "*".
    template = _template()
    policies = template.find_resources("AWS::IAM::Policy")
    assert policies, "the association Lambda must have an execution-role policy"
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


def test_the_role_can_read_write_symptom_log_and_read_profiles_and_readings() -> None:
    # The job reads the symptom log and writes learned thresholds to it
    # (read+write), reads profiles, and reads readings. Assert write actions are
    # granted (they only come from the symptom-log read/write grant) and that all
    # three table ARNs are referenced by the role.
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

    # Read actions cover profiles/readings/symptom-log reads.
    for required in ("dynamodb:GetItem", "dynamodb:Query"):
        assert required in granted, f"{required} must be granted"
    # Write actions can only originate from the symptom-log read/write grant.
    for required in ("dynamodb:PutItem",):
        assert required in granted, f"{required} must be granted for learned thresholds"

    joined = " ".join(referenced_arns)
    for table_name in (_PROFILES_TABLE, _SYMPTOM_LOG_TABLE, _READINGS_TABLE):
        assert f"table/{table_name}" in joined, (
            f"the role must reference the {table_name} ARN; refs={referenced_arns}"
        )


def _arn_strings(resource: object) -> list[str]:
    """Collect literal ARN fragments from a Resource, following Fn::Join parts.

    An imported table (``Table.from_table_name``) renders its ARN as an
    ``Fn::Join`` of literal strings and the account id; the ``table/<name>``
    fragment is a literal in that join, so walking the structure for strings
    recovers it.
    """
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
    # A synth must succeed with no account lookup; the template must be
    # JSON-serializable CloudFormation.
    template = _template()
    json.dumps(template.to_json())
