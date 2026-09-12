---
inclusion: manual
---

# POC deployment addendum — AI advisor

**Status:** POC, deadline-driven, and the highest-risk of the three because it
needs application code written before it can deploy.

## Decision

Deploy the advisor to **Amazon Bedrock AgentCore Runtime** from its committed
ARM64 container image, as the service's own design (Requirement 32) already
specifies. AgentCore is not substituted for Lambda — the advisor's deployment
boundary is `POST /invocations` + `GET /ping`, which is AgentCore's contract, not
a generic HTTP one.

## The blocker this addendum acknowledges

`aqm_advisor/main.py` — the composition root the Dockerfile's `CMD` targets — does
**not exist yet**. It is the service's own task 21: build a concrete `TurnPipeline`
from the model, guardrail, and audit adapters and hand a `run_turn` callable to
`agentcore/app.build_app`, which is already written and waiting for it. Until
`main.py` exists the image cannot start, so the advisor cannot deploy.

The POC therefore includes writing `main.py` as the minimum composition that lets
`build_app` run against real Bedrock. This is application code, not deployment
code, and it is where the POC is most likely to run long.

## Scope for the POC

- Write `main.py` composing the turn pipeline with the Bedrock model adapter and
  the guardrail adapter; audit to DynamoDB may be stubbed or table-backed
  depending on time, recorded at the point it is decided.
- Install `bedrock-agentcore-starter-toolkit` (host tool, not an app dependency).
- Build the ARM64 image (the local Docker engine is available) and deploy to
  AgentCore Runtime in `us-east-1`, where Claude models are already enabled.

## Obligations kept from the service design

- No secret baked into the image (Requirement 26.7); credentials at runtime.
- The top-level error boundary and health-status contract in `agentcore/app.py`
  are used as written — the POC does not reimplement them.
- `main.py` is the real composition root, not a stand-in: a placeholder that
  loaded config and failed to build a turn runner would be a broken image that
  looked packaged. If time runs out, the honest outcome is "advisor not deployed"
  rather than a fake entry.

## What "done" means for the POC

An AgentCore Runtime endpoint that answers a scripted advisory turn end to end
against real Bedrock. If `main.py` cannot be completed in time, this service is
reported as not deployed rather than partially faked.
