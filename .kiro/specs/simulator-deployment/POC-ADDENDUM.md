---
inclusion: manual
---

# POC deployment addendum — sensor simulator

**Status:** POC, deadline-driven. This addendum records a deliberately narrowed
deployment that departs from the full `simulator-deployment` design. It does not
supersede that design; it scopes a first live slice and names every deviation so
the gap back to the full spec stays visible.

## Decision

Deploy the simulator's **REST/pull interface only**, on **AWS Lambda behind an
API Gateway HTTP API**, packaged from source (no container image, no ECR). The
MQTT push path, IoT Core, and per-device X.509 provisioning are **out of scope
for the POC** and remain as specified for later.

## Why this is defensible for a POC

- The REST interface is stateless request/response. Every served value is a pure
  function of Seed, `SiteCode`, and simulated timestamp (sensor-simulator-service
  Requirement 11), so a Lambda recomputes any response cold with no carried
  state. This is the invocation-scoped runtime the full design already assigns to
  the pull interface (`simulator-deployment` DP1, DP10).
- `cli.py` builds the app through `interfaces.rest.build_app`, returning a FastAPI
  object driven in-process by the tests. Mangum wraps that object as a Lambda
  handler with no change to the application.

## Deviations from the full design, each temporary

| Full design | POC | Deferred work |
|-------------|-----|---------------|
| One image, three entrypoints, digest-pinned (DP2) | Zip/source package for the REST handler only | Container image + ECR when the push path lands |
| Real-time MQTT push on a resident task (DP1) | Not deployed | Fargate + IoT Core per the full spec |
| Per-device X.509, one Thing per `SiteCode` (Req 2, 4, 5) | None; REST needs no device identity | Full provisioning custom resource |
| Fleet provisioning, rotation, drift checks (Req 3, 6) | None | As specified |
| Full CI/CD pipeline (Req 11) | Manual `cdk deploy` from a live SSO session | Pipeline as specified |

## POC-specific obligations kept

- The `X-API-KEY` secret is supplied at deploy time as a runtime environment
  value, never committed and never echoed in a response or log
  (sensor-simulator-service Requirement 14.6, 14.10).
- `/health` stays unauthenticated (Requirement 14.8) and is the readiness probe.
- The deployed retention window is the documented 30-simulated-day default
  (Requirement 14.7).
- One CloudFormation stack, named `aqm-poc-simulator`, torn down with one command.

## What "done" means for the POC

A live API Gateway URL in `us-east-1` answering `GET /health` 200, and
`GET /ListSensors` / `GET /SensorData` behind `X-API-KEY`, serving generated
records. Nothing about the MQTT contract is demonstrated or claimed.
