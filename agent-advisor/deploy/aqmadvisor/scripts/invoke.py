#!/usr/bin/env python3
"""Invoke the live AgentCore advisor with the service's own {"utterance": ...} contract.

Usage:
    AWS_PROFILE=Walsen python scripts/invoke.py
    AWS_PROFILE=Walsen python scripts/invoke.py "your question here"
"""
from __future__ import annotations

import json
import sys
import uuid

import boto3

RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:862307432587:runtime/"
    "aqmadvisor_aqm_advisor-ws73wzAfQJ"
)
REGION = "us-east-1"

# A data-focused question grounds reliably. Health/medication questions can
# fail-closed to a safe degraded answer (documented limitation).
DEFAULT = (
    "What is my current local air quality reading and its band? "
    "Just report the retrieved figures, with no distances, durations, or other numbers."
)


def main() -> None:
    """Send one utterance to the deployed runtime and print the response."""
    utterance = sys.argv[1] if len(sys.argv) > 1 else DEFAULT
    client = boto3.client("bedrock-agentcore", region_name=REGION)
    session_id = "test-" + uuid.uuid4().hex + uuid.uuid4().hex  # >= 33 chars
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=RUNTIME_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps({"utterance": utterance}).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    body = json.loads(resp["response"].read())
    print(json.dumps(body, indent=2))
    print("\n--- degraded:", body.get("degraded"), "| basis present:", bool(body.get("basis")))


if __name__ == "__main__":
    main()
