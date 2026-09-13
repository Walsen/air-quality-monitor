#!/usr/bin/env python3
"""POC AWS deployment for the Air Quality Monitor services.

One stack per service so any subset can be deployed or torn down independently:

  aqm-poc-simulator   REST/pull simulator on Lambda + HTTP API
  aqm-poc-ingestion   serving API on Lambda + HTTP API

The advisor is NOT a CDK stack: it deploys to Bedrock AgentCore via its own
starter toolkit, not CloudFormation (see its POC-ADDENDUM).

Scope and deviations: .kiro/specs/*/POC-ADDENDUM.md. REST/serving only; no MQTT,
no IoT Core, no stores. Secrets arrive as stack context at deploy time and are
never committed.
"""
import os

import aws_cdk as cdk

from aqm_infra.lambda_rest_stack import LambdaRestStack
from aqm_infra.web_chatbot_stack import WebChatbotStack

REGION = os.environ.get("CDK_DEFAULT_REGION", "us-east-1")
ACCOUNT = os.environ.get("CDK_DEFAULT_ACCOUNT")
env = cdk.Environment(account=ACCOUNT, region=REGION)

app = cdk.App()

# API keys are read from CDK context (-c simulator_api_key=...), never committed.
# A synth with no key still succeeds so `cdk synth` stays credential-free for CI;
# the key is only required at deploy, enforced by the stack.
sim_key = app.node.try_get_context("simulator_api_key")

LambdaRestStack(
    app,
    "aqm-poc-simulator",
    env=env,
    service_dir="../sensor-simulator",
    handler="aqm_simulator.lambda_handler.handler",
    environment={
        "AQM_INTERFACE": "rest",
        "AQM_SEED": app.node.try_get_context("simulator_seed") or "12345",
        "AQM_SWARM_SIZE": app.node.try_get_context("simulator_swarm_size") or "50",
        "AQM_PROFILE": app.node.try_get_context("simulator_profile") or "cochabamba",
        "AQM_RETENTION_DAYS": "30",
        "AQM_LOG_LEVEL": "info",
    },
    api_key_env="AQM_API_KEY",
    api_key_value=sim_key,
    description="POC: sensor simulator REST/pull on Lambda (simulator-deployment POC addendum)",
)

# Ingestion serving API. Enabled via context flag since it is the second, lower
# priority stack and its data routes answer against empty stores in the POC.
if app.node.try_get_context("deploy_ingestion"):
    ing_key = app.node.try_get_context("ingestion_api_key")
    LambdaRestStack(
        app,
        "aqm-poc-ingestion",
        env=env,
        service_dir="../data-processing",
        handler="aqm_ingestion.lambda_handler.handler",
        environment={
            "AQM_ENABLE_SERVING": "true",
            "AQM_ENABLE_PULL": "false",
            "AQM_ENABLE_PUSH": "false",
            "AQM_AWS_REGION": REGION,
            "AQM_LOG_LEVEL": "info",
        },
        api_key_env="AQM_FEED_API_KEY",
        api_key_value=ing_key,
        description="POC: ingestion serving API on Lambda (ingestion POC addendum)",
    )

# Web Chatbot: a browser chat UI in front of the advisor runtime on AgentCore.
# The runtime ARN defaults to the deployed advisor but can be overridden by
# context; the shared access key is required at deploy (never committed).
_DEFAULT_ADVISOR_RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:862307432587:runtime/"
    "aqmadvisor_aqm_advisor-ws73wzAfQJ"
)
WebChatbotStack(
    app,
    "aqm-poc-chatbot",
    env=env,
    service_dir="../web-chatbot",
    runtime_arn=app.node.try_get_context("chatbot_runtime_arn") or _DEFAULT_ADVISOR_RUNTIME_ARN,
    region=REGION,
    access_key=app.node.try_get_context("chatbot_access_key"),
    description="POC: web chatbot proxying to the AI Advisor on Bedrock AgentCore",
)


app.synth()
