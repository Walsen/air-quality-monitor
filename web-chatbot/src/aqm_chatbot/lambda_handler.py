"""AWS Lambda entrypoint for the chatbot (API Gateway HTTP API).

Adds no logic. It resolves configuration from the environment, builds the app
with the real AgentCore client, and wraps it with Mangum. A missing access key or
runtime ARN raises here, at cold start, so the function fails to initialize rather
than serving an open or misconfigured proxy.
"""

from __future__ import annotations

import os
from typing import Any, cast

from mangum import Mangum

from aqm_chatbot.app import build_app
from aqm_chatbot.auth import AgentCoreCognitoClient
from aqm_chatbot.client import AgentCoreAdvisorClient

_runtime_arn = os.environ["AQM_CHATBOT_RUNTIME_ARN"]
_region = os.environ.get("AQM_CHATBOT_REGION", "us-east-1")
_access_key = os.environ.get("AQM_CHATBOT_ACCESS_KEY", "")
# Deploy-time wiring of the Cognito app client id is Task 20; here it is read from
# env with a sensible name. When absent, /login returns 503 rather than failing.
_cognito_client_id = os.environ.get("AQM_CHATBOT_COGNITO_CLIENT_ID", "")

_advisor = AgentCoreAdvisorClient(runtime_arn=_runtime_arn, region=_region)
_cognito = (
    AgentCoreCognitoClient(client_id=_cognito_client_id, region=_region)
    if _cognito_client_id
    else None
)
_app = build_app(
    advisor=_advisor,
    access_key=_access_key,
    cognito=_cognito,
    cognito_client_id=_cognito_client_id or None,
)

handler = Mangum(cast("Any", _app), lifespan="off", api_gateway_base_path="/")
