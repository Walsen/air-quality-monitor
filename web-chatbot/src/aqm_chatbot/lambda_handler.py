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
from aqm_chatbot.client import AgentCoreAdvisorClient

_runtime_arn = os.environ["AQM_CHATBOT_RUNTIME_ARN"]
_region = os.environ.get("AQM_CHATBOT_REGION", "us-east-1")
_access_key = os.environ.get("AQM_CHATBOT_ACCESS_KEY", "")

_advisor = AgentCoreAdvisorClient(runtime_arn=_runtime_arn, region=_region)
_app = build_app(advisor=_advisor, access_key=_access_key)

handler = Mangum(cast("Any", _app), lifespan="off", api_gateway_base_path="/")
