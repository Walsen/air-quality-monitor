---
inclusion: manual
---

# POC deployment addendum — ingestion & serving

**Status:** POC, deadline-driven. Records a narrowed deployment of the serving
API only. Does not supersede the service's own design.

## Decision

Deploy the **serving API on AWS Lambda behind an API Gateway HTTP API**, packaged
from source. The **ingestion path (IoT Core rule → processing) and the backing
stores (Timestream, DynamoDB, S3) are out of scope for the POC.**

## Why, and the honest limitation

The serving API is a FastAPI app built the same way as the simulator's, so it
wraps under Mangum unchanged and will answer authentication and `/health`
immediately. Its data responses depend on backing stores that the POC does not
stand up, so **read endpoints will answer against empty or absent stores** unless
a store is wired later. This is a genuine limitation, stated rather than hidden:
the POC demonstrates the serving API is live and authenticates in AWS, not that
it returns populated per-user views.

## Deviations from the full design, each temporary

| Full design | POC | Deferred |
|-------------|-----|----------|
| IoT Core → rule → ingest Lambda → stores | Not deployed | Full ingest path |
| Timestream + DynamoDB + S3 | None | Stores + adapter wiring |
| Cognito JWT on the serving API | `X-API-KEY` or the app's own auth as built | Cognito as specified |
| Full CI/CD | Manual `cdk deploy` from a live SSO session | Pipeline |

## Obligations kept

- No secret committed or echoed; secrets supplied at deploy as runtime env.
- Non-diagnostic disclaimer and auth behavior exactly as the app already builds
  them — the POC changes deployment, not application logic.
- One stack, `aqm-poc-ingestion`, one-command teardown.

## What "done" means for the POC

A live API Gateway URL answering `/health` and enforcing auth on the serving
routes. Populated data is explicitly a later milestone once stores are stood up.
