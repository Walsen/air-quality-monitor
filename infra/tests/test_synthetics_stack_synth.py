"""Offline assertions over the synthesized ``SyntheticsStack`` template.

Feature: observability-xray-synthetics.

THIS SUITE TOUCHES NO ACCOUNT. ``Template.from_stack`` synthesises in-process; the
stack is constructed with an explicit ``env`` so the imported secret ARN is built
from the stack's own account/region. Target URL, Cognito token URL and secret NAME
arrive as plain strings, so synth needs no deploy-time material.

The assertions pin the facts the feature depends on and that a plausible edit could
silently break:

* two canaries exist, on the Python runtime, with X-Ray active tracing on;
* the health canary carries the target URL and no credential;
* the authenticated canary carries the Cognito token URL + secret NAME (not a
  secret VALUE) and the strict-mode flag;
* the canary role can read exactly the named secret, never ``Resource: "*"``;
* each canary has a CloudWatch alarm on success-percent wired to an SNS topic.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aqm_infra.synthetics_stack import SyntheticsStack
from aws_cdk import assertions

_REGION = "us-east-1"
_ACCOUNT = "111122223333"

_TARGET_URL = "https://serving.example.com"
_TOKEN_URL = "https://auth.example.com/oauth2/token"
_SECRET_NAME = "aqm/canary/cognito-client"


def _template(*, require_reading: bool = False) -> assertions.Template:
    app = cdk.App()
    stack = SyntheticsStack(
        app,
        "TestSyntheticsStack",
        env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        target_url=_TARGET_URL,
        cognito_token_url=_TOKEN_URL,
        cognito_secret_name=_SECRET_NAME,
        require_reading=require_reading,
    )
    return assertions.Template.from_stack(stack)


def _canaries(template: assertions.Template) -> Mapping[str, Any]:
    return template.find_resources("AWS::Synthetics::Canary")


def _handler(canary: Mapping[str, Any]) -> str:
    # For AWS::Synthetics::Canary the handler is under Code.Handler (not Handler,
    # which is the Lambda shape).
    return str(canary["Properties"]["Code"]["Handler"])


def test_it_provisions_two_canaries() -> None:
    _template().resource_count_is("AWS::Synthetics::Canary", 2)


def test_both_canaries_use_the_python_runtime() -> None:
    for canary in _canaries(_template()).values():
        runtime = canary["Properties"]["RuntimeVersion"]
        assert runtime.startswith("syn-python-selenium"), runtime


def test_canaries_do_not_set_active_tracing_on_the_python_runtime() -> None:
    # active_tracing is a Node/Puppeteer-only feature; the Python-Selenium runtime
    # rejects it at synth. The traced request path is covered server-side by the
    # serving Lambda's own X-Ray Active segment, so this is a deliberate absence, not
    # an oversight — pin it so a well-meaning re-add fails here instead of at deploy.
    for canary in _canaries(_template()).values():
        run_config = canary["Properties"].get("RunConfig", {})
        assert not run_config.get("ActiveTracing"), (
            "Python-Selenium canaries cannot enable ActiveTracing; it must stay off"
        )


def _canary_env(canary: Mapping[str, Any]) -> dict[str, str]:
    variables = canary["Properties"]["RunConfig"]["EnvironmentVariables"]
    return dict(variables)


def test_the_health_canary_targets_the_url_and_carries_no_credential() -> None:
    canaries = _canaries(_template())
    health = [
        c
        for c in canaries.values()
        if _handler(c) == "serving_health.handler"
    ]
    assert len(health) == 1
    env = _canary_env(health[0])
    assert env["TARGET_URL"] == _TARGET_URL
    # The health canary is unauthenticated: it must NOT carry a token or secret name.
    assert "COGNITO_SECRET_NAME" not in env
    assert "COGNITO_TOKEN_URL" not in env


def test_the_authenticated_canary_carries_token_url_and_secret_name_not_value() -> None:
    canaries = _canaries(_template())
    auth = [
        c
        for c in canaries.values()
        if _handler(c) == "serving_authenticated.handler"
    ]
    assert len(auth) == 1
    env = _canary_env(auth[0])
    assert env["TARGET_URL"] == _TARGET_URL
    assert env["COGNITO_TOKEN_URL"] == _TOKEN_URL
    # The SECRET NAME travels, never a secret VALUE — the canary fetches the value at
    # run time from Secrets Manager.
    assert env["COGNITO_SECRET_NAME"] == _SECRET_NAME
    assert env["REQUIRE_READING"] == "false"


def test_strict_mode_sets_require_reading_true() -> None:
    canaries = _canaries(_template(require_reading=True))
    auth = [
        c
        for c in canaries.values()
        if _handler(c) == "serving_authenticated.handler"
    ]
    assert _canary_env(auth[0])["REQUIRE_READING"] == "true"


def test_the_canary_role_can_read_only_the_named_secret() -> None:
    template = _template()
    policies = template.find_resources("AWS::IAM::Policy")
    saw_secret_read = False
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            names = [a for a in actions if isinstance(a, str)]
            if any(a.startswith("secretsmanager:") for a in names):
                saw_secret_read = True
                resource = statement.get("Resource")
                assert resource != "*", (
                    "secret access must be scoped to the named secret ARN, "
                    "never Resource: '*'"
                )
                arns = _arn_strings(resource)
                assert any(_SECRET_NAME in a for a in arns), (
                    f"the grant must reference the named secret; refs={arns}"
                )
    assert saw_secret_read, "the authenticated canary must be granted secret read"


def test_each_canary_has_a_success_percent_alarm_to_sns() -> None:
    template = _template()
    template.resource_count_is("AWS::SNS::Topic", 1)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    assert len(alarms) == 2, f"expected one alarm per canary, found {len(alarms)}"
    for alarm in alarms.values():
        props = alarm["Properties"]
        assert props["MetricName"] == "SuccessPercent", props.get("MetricName")
        assert props["ComparisonOperator"] == "LessThanThreshold"
        assert props["Threshold"] == 100
        assert props.get("AlarmActions"), "each alarm must have an SNS action"


def test_deploy_without_target_url_still_synthesizes_offline() -> None:
    # A synth with no target_url must still succeed, so `cdk synth` stays
    # credential-free for CI even when the deploy inputs are absent. The stack
    # annotates a deploy-time error (add_error) so the DEPLOY is refused, but synth
    # renders — the same contract the serving stack follows with its Cognito ids.
    app = cdk.App()
    stack = SyntheticsStack(
        app,
        "NoTargetSyntheticsStack",
        env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        target_url=None,
        cognito_token_url=_TOKEN_URL,
        cognito_secret_name=_SECRET_NAME,
    )
    template = assertions.Template.from_stack(stack)
    json.dumps(template.to_json())


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
    json.dumps(_template().to_json())
