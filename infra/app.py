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
from aqm_infra.association_stack import AssociationStack
from aqm_infra.cognito_stack import CognitoStack
from aqm_infra.ingestion_serving_stack import IngestionServingStack
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
# The runtime ARN defaults to the deployed advisor in the *current* account
# (derived from CDK_DEFAULT_ACCOUNT/REGION, so no account id is hardcoded here)
# but can be overridden by context; the shared access key is required at deploy
# (never committed). With no resolved account (offline synth without
# credentials) the default is None, and the stack requires
# `-c chatbot_runtime_arn=<arn>` at deploy — synth still succeeds.
_ADVISOR_RUNTIME_NAME = "aqmadvisor_aqm_advisor-ws73wzAfQJ"
_DEFAULT_ADVISOR_RUNTIME_ARN = (
    f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/{_ADVISOR_RUNTIME_NAME}"
    if ACCOUNT
    else None
)
WebChatbotStack(
    app,
    "aqm-poc-chatbot",
    env=env,
    service_dir="../web-chatbot",
    runtime_arn=app.node.try_get_context("chatbot_runtime_arn") or _DEFAULT_ADVISOR_RUNTIME_ARN,
    region=REGION,
    access_key=app.node.try_get_context("chatbot_access_key"),
    # Cognito app-client id from context only, never committed; None at synth. With
    # it the deployed chatbot can sign a user in against the live pool (Task 20),
    # without it /login is unavailable (503) and chat still works.
    cognito_client_id=app.node.try_get_context("chatbot_cognito_client_id"),
    description="POC: web chatbot proxying to the AI Advisor on Bedrock AgentCore",
)

# Personal Diary Memory (personal-diary-memory feature): per-user Cognito login,
# the serving API backed by DynamoDB, and the scheduled association job. Gated
# behind a single opt-in flag — mirroring `deploy_ingestion` above — so these
# three stacks don't synth into every run unless requested. Each is a separate
# stack so it deploys/tears down independently (`cdk deploy --exclusively <id>`).
#
# Everything identity- or storage-related comes from deploy-time context, never
# committed. At synth with no context the Cognito ids are None; the serving stack
# already handles None -> add_error at deploy while staying synth-clean, so a
# full-app synth with placeholder context resolves nothing from an account.
#
# Deploy ordering (Req 7.2, 7.3): CognitoStack first (its outputs supply the
# Cognito ids), then the serving stack, then the association stack. The
# association stack consumes the serving stack's CDK-generated table names, which
# creates a cross-stack CloudFormation export/import — so the serving stack MUST
# deploy before the association stack.
if app.node.try_get_context("deploy_diary_memory"):
    CognitoStack(
        app,
        "aqm-poc-cognito",
        env=env,
        description="POC: Cognito user pool for per-user diary sign-in (personal-diary-memory)",
    )

    serving_stack = IngestionServingStack(
        app,
        "aqm-poc-serving",
        env=env,
        service_dir="../data-processing",
        region=REGION,
        # Cognito ids from context only (from the CognitoStack outputs). None at
        # synth -> the stack adds a deploy-time error but synthesizes cleanly.
        cognito_user_pool_id=app.node.try_get_context("cognito_user_pool_id"),
        cognito_client_id=app.node.try_get_context("cognito_client_id"),
        cognito_issuer=app.node.try_get_context("cognito_issuer"),
        description="POC: ingestion serving API on Lambda + DynamoDB (personal-diary-memory)",
    )

    # The association job needs the SAME tables the serving stack generates.
    # Pass the serving stack's exposed instance attributes (Task 3 exposed them
    # for exactly this): CDK turns each into a cross-stack export/import, so the
    # serving stack must deploy first and the names never need to be committed.
    AssociationStack(
        app,
        "aqm-poc-association",
        env=env,
        service_dir="../data-processing",
        region=REGION,
        profiles_table_name=serving_stack.profiles_table_name,
        symptom_log_table_name=serving_stack.symptom_log_table_name,
        readings_table_name=serving_stack.readings_table_name,
        registry_table_name=serving_stack.sensor_registry_table_name,
        description="POC: scheduled association-derivation Lambda (personal-diary-memory)",
    )


app.synth()
