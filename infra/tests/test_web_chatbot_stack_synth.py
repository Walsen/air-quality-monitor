"""Offline assertions over the synthesized ``WebChatbotStack`` template.

Feature: personal-diary-memory, Property 7 — offline guarantee.

THIS SUITE TOUCHES NO ACCOUNT. ``Template.from_stack`` runs synthesis in-process
and emits CloudFormation without contacting an account; the stack is constructed
with an explicit dummy ``env`` so nothing resolves from a live account. The
runtime ARN, access key, and Cognito client id all arrive as plain strings, so
synth needs no deploy-time material at all.

The assertions pin the facts Task 20 depends on and that a plausible edit could
silently break:

* the chatbot Lambda carries the Cognito app-client id in env, so the deployed
  chatbot can sign a user in against the live pool (the Task 20 regression guard
  — without the wiring the variable is ``""`` or absent and ``/login`` is 503);
* the existing runtime-ARN and access-key wiring stays intact;
* the execution role may invoke ONLY the advisor runtime, never ``Resource: "*"``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aws_cdk as cdk
from aqm_infra.web_chatbot_stack import WebChatbotStack
from aws_cdk import assertions

_REGION = "us-east-1"
_ACCOUNT = "111122223333"
_CHATBOT_HANDLER = "aqm_chatbot.lambda_handler.handler"

_RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/test-abc"
_ACCESS_KEY = "test-key"
_COGNITO_CLIENT_ID = "test-client-id"


def _chatbot_function(template: assertions.Template) -> Mapping[str, Any]:
    """The chatbot Lambda, selected by its handler.

    ``log_retention`` provisions a second ``AWS::Lambda::Function`` (the
    LogRetention custom-resource provider), so the chatbot function must be
    picked out by handler rather than assumed to be the only one.
    """
    functions = template.find_resources("AWS::Lambda::Function")
    chatbot = [
        fn
        for fn in functions.values()
        if fn["Properties"].get("Handler") == _CHATBOT_HANDLER
    ]
    assert len(chatbot) == 1, (
        f"expected exactly one chatbot Lambda, found {len(chatbot)}"
    )
    return chatbot[0]


def _template() -> assertions.Template:
    app = cdk.App()
    stack = WebChatbotStack(
        app,
        "TestWebChatbotStack",
        env=cdk.Environment(account=_ACCOUNT, region=_REGION),
        service_dir="../web-chatbot",
        runtime_arn=_RUNTIME_ARN,
        region=_REGION,
        access_key=_ACCESS_KEY,
        cognito_client_id=_COGNITO_CLIENT_ID,
    )
    return assertions.Template.from_stack(stack)


def test_the_chatbot_lambda_carries_the_cognito_client_id_in_env() -> None:
    # Task 20 regression guard: the deployed chatbot can only sign a user in when
    # the Cognito app-client id is in env; the handler degrades /login to 503 when
    # it is absent/empty. Without the stack wiring this variable would be "" or
    # missing entirely.
    variables = _chatbot_function(_template())["Properties"]["Environment"][
        "Variables"
    ]
    assert variables["AQM_CHATBOT_COGNITO_CLIENT_ID"] == _COGNITO_CLIENT_ID


def test_the_chatbot_lambda_carries_the_runtime_arn_and_access_key() -> None:
    # Guards the pre-existing wiring stays intact: the Lambda proxies to exactly
    # this advisor runtime and is gated by the shared access key.
    variables = _chatbot_function(_template())["Properties"]["Environment"][
        "Variables"
    ]
    assert variables["AQM_CHATBOT_RUNTIME_ARN"] == _RUNTIME_ARN
    assert variables["AQM_CHATBOT_ACCESS_KEY"] == _ACCESS_KEY


def test_the_role_can_invoke_only_the_advisor_runtime() -> None:
    # Least privilege: the execution role grants bedrock-agentcore:InvokeAgentRuntime
    # scoped to the runtime ARN (and its /* children), never Resource "*".
    template = _template()
    policies = template.find_resources("AWS::IAM::Policy")
    assert policies, "the chatbot Lambda must have an execution-role policy"

    invoke_statements: list[Mapping[str, Any]] = []
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            names = [a for a in actions if isinstance(a, str)]
            if any(a == "bedrock-agentcore:InvokeAgentRuntime" for a in names):
                invoke_statements.append(statement)

    assert invoke_statements, (
        "the role must grant bedrock-agentcore:InvokeAgentRuntime"
    )
    for statement in invoke_statements:
        resource = statement.get("Resource")
        assert resource != "*", (
            "the runtime grant must be scoped to the runtime ARN, never Resource: '*'"
        )
        arns = _arn_strings(resource)
        if isinstance(resource, list):
            assert "*" not in resource, "no wildcard runtime resource permitted"
        joined = " ".join(arns)
        assert _RUNTIME_ARN in joined, (
            f"the role must reference the advisor runtime ARN; refs={arns}"
        )


def _arn_strings(resource: object) -> list[str]:
    """Collect literal ARN fragments from a Resource, following nested structures."""
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


def test_the_chatbot_lambda_has_xray_active_tracing() -> None:
    # The chatbot is the entry hop; tracing it lets a question's trace start at the browser proxy
    # and continue through the advisor and serving, giving the full end-to-end workflow view.
    template = _template()
    functions = template.find_resources("AWS::Lambda::Function")
    chatbot = [
        fn for fn in functions.values()
        if fn["Properties"].get("Handler") == "aqm_chatbot.lambda_handler.handler"
    ]
    assert len(chatbot) == 1, f"expected one chatbot Lambda, found {len(chatbot)}"
    assert chatbot[0]["Properties"].get("TracingConfig", {}).get("Mode") == "Active"
