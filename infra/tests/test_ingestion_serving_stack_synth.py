"""Offline assertions over the synthesized ``IngestionServingStack`` template.

Feature: personal-diary-memory, Property 7 — offline guarantee.

THIS SUITE TOUCHES NO ACCOUNT. ``Template.from_stack`` runs synthesis in-process
and emits CloudFormation without contacting an account; the stack is constructed
with an explicit ``env`` and placeholder Cognito ids so synth performs no
account/region lookup and succeeds without deploy-time material.

The assertions pin the facts this feature depends on and that a plausible edit
could silently break:

* four DynamoDB tables — ``profiles`` (PK ``user_id``), ``symptom-log``
  (PK ``user_id``, SK ``entry_date``), ``readings`` (PK ``pk``, SK ``sk``) and
  ``sensor-registry`` (PK ``site_code``) — whose key attribute names match what
  ``DynamoDbProfileStore``, ``DynamoDbSymptomLogStore``, ``DynamoDbReadingsStore``
  and ``DynamoDbSensorRegistryStore`` read;
* every table encrypted at rest, PAY_PER_REQUEST, and a POC ``DESTROY`` removal
  policy (asserted as ``DeletionPolicy: Delete``);
* the serving Lambda's environment selecting the ``dynamodb`` profile, symptom,
  readings and sensor-registry stores and the ``cognito`` authenticator, carrying
  the four table names and the three Cognito ids;
* the execution role holding DynamoDB actions scoped to ONLY the four table ARNs
  — never a ``Resource: "*"`` DynamoDB grant.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aqm_infra.ingestion_serving_stack import IngestionServingStack
from aws_cdk import assertions

_REGION = "us-east-1"
_SERVING_HANDLER = "aqm_ingestion.lambda_handler.handler"


def _serving_function(template: assertions.Template) -> Mapping[str, Any]:
    """The serving Lambda, selected by its handler.

    ``log_retention`` provisions a second ``AWS::Lambda::Function`` (the
    LogRetention custom-resource provider), so the serving function must be picked
    out by handler rather than assumed to be the only one.
    """
    functions = template.find_resources("AWS::Lambda::Function")
    serving = [
        fn
        for fn in functions.values()
        if fn["Properties"].get("Handler") == _SERVING_HANDLER
    ]
    assert len(serving) == 1, f"expected exactly one serving Lambda, found {len(serving)}"
    return serving[0]


def _template() -> assertions.Template:
    app = cdk.App()
    stack = IngestionServingStack(
        app,
        "TestIngestionServingStack",
        env=cdk.Environment(account="111122223333", region=_REGION),
        service_dir="../data-processing",
        region=_REGION,
        # Cognito ids are required at DEPLOY, not synth: placeholders here so the
        # offline suite synthesizes with nothing resolved from an account.
        cognito_user_pool_id=None,
        cognito_client_id=None,
        cognito_issuer=None,
    )
    return assertions.Template.from_stack(stack)


def test_it_provisions_exactly_four_dynamodb_tables() -> None:
    # profiles, symptom-log, readings, sensor-registry (Requirement 10.1 adds the
    # last two so the association has a persisted exposure history).
    _template().resource_count_is("AWS::DynamoDB::Table", 4)


def test_the_profiles_table_is_keyed_by_user_id() -> None:
    # DynamoDbProfileStore reads Key={"user_id": ...}: the partition key must be
    # user_id (S) and there is no sort key.
    _template().has_resource_properties(
        "AWS::DynamoDB::Table",
        assertions.Match.object_like(
            {
                "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
                "AttributeDefinitions": assertions.Match.array_with(
                    [{"AttributeName": "user_id", "AttributeType": "S"}]
                ),
            }
        ),
    )


def test_the_symptom_log_table_is_keyed_by_user_id_and_entry_date() -> None:
    # DynamoDbSymptomLogStore reads Key={"user_id", "entry_date"} and queries the
    # entry_date sort key: PK user_id (S), SK entry_date (S, the ISO date string).
    _template().has_resource_properties(
        "AWS::DynamoDB::Table",
        assertions.Match.object_like(
            {
                "KeySchema": [
                    {"AttributeName": "user_id", "KeyType": "HASH"},
                    {"AttributeName": "entry_date", "KeyType": "RANGE"},
                ],
                "AttributeDefinitions": assertions.Match.array_with(
                    [
                        {"AttributeName": "user_id", "AttributeType": "S"},
                        {"AttributeName": "entry_date", "AttributeType": "S"},
                    ]
                ),
            }
        ),
    )


def test_the_readings_table_is_keyed_by_pk_and_sk() -> None:
    # DynamoDbReadingsStore reads Key={"pk", "sk"}: pk = SITE#{SiteCode}#SP#{Species}
    # partition, sk = interval-start ISO sort. Both string attributes.
    _template().has_resource_properties(
        "AWS::DynamoDB::Table",
        assertions.Match.object_like(
            {
                "KeySchema": [
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                "AttributeDefinitions": assertions.Match.array_with(
                    [
                        {"AttributeName": "pk", "AttributeType": "S"},
                        {"AttributeName": "sk", "AttributeType": "S"},
                    ]
                ),
            }
        ),
    )


def test_the_sensor_registry_table_is_keyed_by_site_code() -> None:
    # DynamoDbSensorRegistryStore reads Key={"site_code": ...} and scans for active
    # sites: partition key site_code (S), no sort key.
    _template().has_resource_properties(
        "AWS::DynamoDB::Table",
        assertions.Match.object_like(
            {
                "KeySchema": [{"AttributeName": "site_code", "KeyType": "HASH"}],
                "AttributeDefinitions": assertions.Match.array_with(
                    [{"AttributeName": "site_code", "AttributeType": "S"}]
                ),
            }
        ),
    )


def test_all_tables_are_pay_per_request() -> None:
    template = _template()
    tables = template.find_resources("AWS::DynamoDB::Table")
    assert len(tables) == 4
    for table in tables.values():
        assert table["Properties"]["BillingMode"] == "PAY_PER_REQUEST"


def test_all_tables_are_encrypted_at_rest() -> None:
    template = _template()
    tables = template.find_resources("AWS::DynamoDB::Table")
    assert len(tables) == 4
    for table in tables.values():
        sse = table["Properties"].get("SSESpecification")
        assert sse is not None, "every table must declare an SSESpecification"
        assert sse.get("SSEEnabled") is True, "encryption at rest must be enabled"


def test_all_tables_carry_the_poc_destroy_removal_policy() -> None:
    # RemovalPolicy.DESTROY renders as DeletionPolicy: Delete on the resource.
    template = _template()
    tables = template.find_resources("AWS::DynamoDB::Table")
    assert len(tables) == 4
    for table in tables.values():
        assert table["DeletionPolicy"] == "Delete"
        assert table["UpdateReplacePolicy"] == "Delete"


def test_the_serving_lambda_selects_the_dynamodb_stores_and_cognito_auth() -> None:
    # The ingestion config loader selects adapters via AQM_ADAPTER_<NAME_UPPER>.
    # Ports readings_store and sensor_registry_store back the air-quality view, so
    # the serving path selects their dynamodb adapters too.
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
                            "AQM_ADAPTER_SENSOR_REGISTRY_STORE": "dynamodb",
                            "AQM_ADAPTER_AUTHENTICATOR": "cognito",
                            "AQM_ENABLE_SERVING": "true",
                            "AQM_ENABLE_PULL": "false",
                            "AQM_ENABLE_PUSH": "false",
                            "AQM_AWS_REGION": _REGION,
                        }
                    )
                }
            }
        ),
    )


def test_the_serving_lambda_carries_the_table_names_in_env() -> None:
    # The composition reads _table("profiles") -> AQM_TABLE_PROFILES,
    # _required_setting("AQM_SYMPTOM_LOG_TABLE"), _table("readings") ->
    # AQM_TABLE_READINGS, and _table("registry") -> AQM_TABLE_REGISTRY; all must be
    # present, and as CloudFormation references (the CDK-generated table names).
    template = _template()
    variables = _serving_function(template)["Properties"]["Environment"]["Variables"]
    for key in (
        "AQM_TABLE_PROFILES",
        "AQM_SYMPTOM_LOG_TABLE",
        "AQM_TABLE_READINGS",
        "AQM_TABLE_REGISTRY",
    ):
        assert key in variables, f"{key} must be present in the serving Lambda env"
        # Table names are the generated names, passed via table.table_name -> a Ref.
        assert isinstance(variables[key], dict), f"{key} must be a CloudFormation ref"
        assert "Ref" in variables[key], f"{key} must reference the generated table name"


def test_the_serving_lambda_carries_the_cognito_ids_in_env() -> None:
    # Cognito ids come in as (optional at synth) params; the env keys must exist
    # so a deploy that supplies them wires the authenticator.
    template = _template()
    variables = _serving_function(template)["Properties"]["Environment"]["Variables"]
    for key in (
        "AQM_COGNITO_USER_POOL_ID",
        "AQM_COGNITO_CLIENT_ID",
        "AQM_COGNITO_ISSUER",
    ):
        assert key in variables, f"{key} must be present in the serving Lambda env"


def test_the_serving_lambda_uses_the_documented_handler() -> None:
    _template().has_resource_properties(
        "AWS::Lambda::Function",
        assertions.Match.object_like(
            {"Handler": "aqm_ingestion.lambda_handler.handler"}
        ),
    )


def test_the_serving_lambda_is_arm64() -> None:
    _template().has_resource_properties(
        "AWS::Lambda::Function",
        assertions.Match.object_like({"Architectures": ["arm64"]}),
    )


def test_the_execution_role_has_no_dynamodb_wildcard_grant() -> None:
    # Least privilege: grant_read_write_data / grant_read_data scope to the table
    # ARNs. No DynamoDB policy statement may name Resource "*". Scan the rendered
    # IAM policies for a dynamodb action paired with a "*" resource.
    template = _template()
    policies = template.find_resources("AWS::IAM::Policy")
    assert policies, "the serving Lambda must have an execution-role policy"
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
                # A wildcard may not hide inside a list of resources either.
                if isinstance(resource, list):
                    assert "*" not in resource, "no wildcard DynamoDB resource permitted"


def test_the_execution_role_references_all_four_tables() -> None:
    # The serving adapters read/write profiles + symptom-log and read readings +
    # sensor-registry. grant_read_write_data / grant_read_data reference each table
    # ARN. Assert the core actions are present and all four table ARNs referenced.
    template = _template()
    policies = template.find_resources("AWS::IAM::Policy")
    granted: set[str] = set()
    referenced_logical_ids: set[str] = set()
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            for action in actions:
                if isinstance(action, str) and action.startswith("dynamodb:"):
                    granted.add(action)
            # Table ARNs render as Fn::GetAtt <TableLogicalId> Arn inside the
            # statement's Resource; collect the referenced logical ids.
            referenced_logical_ids |= set(_get_att_targets(statement.get("Resource")))
    for required in (
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:DeleteItem",
        "dynamodb:Scan",
    ):
        assert required in granted, f"{required} must be granted to the serving role"

    table_logical_ids = set(_template().find_resources("AWS::DynamoDB::Table"))
    assert len(table_logical_ids) == 4
    # All four tables must be referenced by the role policy.
    assert table_logical_ids <= referenced_logical_ids, (
        "the role must reference ALL FOUR table ARNs; "
        f"referenced={sorted(referenced_logical_ids)} tables={sorted(table_logical_ids)}"
    )


def _get_att_targets(resource: object) -> list[str]:
    """Collect logical ids named by any Fn::GetAtt <id> Arn inside a Resource.

    CDK renders a table ARN as ``{"Fn::GetAtt": ["<TableLogicalId>", "Arn"]}`` and
    an index ARN as a ``Fn::Join`` of that ARN plus ``/index/*``. Both carry the
    table's logical id, so walking the structure for GetAtt targets finds the
    tables the policy is scoped to.
    """
    found: list[str] = []
    if isinstance(resource, dict):
        for key, value in resource.items():
            if key == "Fn::GetAtt" and isinstance(value, list) and value:
                found.append(str(value[0]))
            else:
                found.extend(_get_att_targets(value))
    elif isinstance(resource, list):
        for item in resource:
            found.extend(_get_att_targets(item))
    return found


def test_it_exports_all_four_table_names_as_outputs() -> None:
    # Task 5 passes the readings + registry names into the AssociationStack; the
    # stack exports them (and the existing profiles/symptom-log names) as outputs.
    template = _template()
    outputs = template.find_outputs("*")
    output_ids = set(outputs)
    for expected in (
        "ProfilesTableName",
        "SymptomLogTableName",
        "ReadingsTableName",
        "SensorRegistryTableName",
    ):
        assert expected in output_ids, f"{expected} must be a CfnOutput"


def test_the_stack_synthesizes_offline_with_placeholder_cognito_ids() -> None:
    # A synth with no Cognito ids must succeed (offline guarantee); the template
    # must be JSON-serializable CloudFormation.
    template = _template()
    json.dumps(template.to_json())
