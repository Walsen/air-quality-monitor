# Observability: X-Ray tracing & CloudWatch Synthetics

Feature: `observability-xray-synthetics`. This document covers the monitoring
surface added on top of the deployed POC stacks — active synthetic checks and the
distributed-tracing picture — and what an operator must supply to turn them on.

## What's here

### CloudWatch Synthetics canaries (`SyntheticsStack`, `aqm-poc-synthetics`)

Two scheduled black-box probes of the deployed serving API, each every 5 minutes,
with a CloudWatch alarm per canary wired to an SNS topic:

| Canary | Handler | What it proves |
|--------|---------|----------------|
| `aqm-serving-health` | `serving_health.handler` | Front door + Lambda are up (`GET /health`, no credential). First to fire on an outage. |
| `aqm-serving-auth` | `serving_authenticated.handler` | The authenticated path works: obtains a Cognito token and calls `/v1/air-quality/me`, asserting 200 (not 401) and a well-formed body carrying the `user` identity field. In strict mode also asserts a non-empty snapshot. |

The alarms fire on `SuccessPercent < 100` over a 5-minute period (a single failed
run breaches), and missing data is treated as breaching, so a stalled canary pages
rather than going quiet.

### X-Ray tracing (existing + gaps)

- **Serving and chatbot Lambdas already run `Tracing.ACTIVE`** (`IngestionServingStack`,
  `WebChatbotStack`) — CDK grants the X-Ray write permissions, so each request's
  Lambda hop appears on the service map.
- The advisor's `observability/correlation.py` propagates a session id through
  OpenTelemetry baggage; on AgentCore the platform installs the provider and the
  spans bind to it.
- **Known gaps** (not yet closed here — see "Next"): DynamoDB/httpx sub-segments
  (Active mode traces the Lambda envelope, not its downstream calls), API Gateway
  stage tracing, and trace propagation across the advisor→serving `httpx` hop.

Canary-side `active_tracing` is deliberately OFF: it is a Node/Puppeteer-only
feature the Python-Selenium runtime rejects, and the request path is already traced
server-side by the serving Lambda's own segment.

## Operator prerequisites

The authenticated canary needs a **Secrets Manager secret** holding OAuth2
client-credentials — never committed, referenced by name only:

```bash
aws secretsmanager create-secret \
  --name aqm/canary/cognito-client \
  --secret-string '{"client_id":"...","client_secret":"...","scope":"aqm/read"}'
```

This requires a Cognito **app client with the client-credentials grant** enabled
and a resource server/scope in the pool. The canary role is granted
`secretsmanager:GetSecretValue` on exactly that secret ARN.

## Deploy / teardown

```bash
# Deploy the canaries (serving stack must already be deployed — its ApiUrl is
# resolved from CloudFormation). token_url defaults empty; pass the pool's OAuth2
# token endpoint for the authenticated canary.
just deploy-synthetics aqm/canary/cognito-client \
  https://<pool-domain>.auth.us-east-1.amazoncognito.com/oauth2/token \
  ops@example.com

# Strict data-freshness mode (fails on an authorised-but-empty snapshot):
just deploy-synthetics aqm/canary/cognito-client "<token_url>" "" true

# Tear down after the demo (the artifacts bucket auto-deletes):
just teardown-synthetics
```

The offline synth gate (`just synth`) includes this stack with placeholder context,
so `cdk synth` still resolves nothing from an account.

## Next (not in this change)

1. ADOT Lambda layer + `AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument` on the serving
   Lambda for DynamoDB/httpx sub-segments (zero app-code change).
2. Trace-context propagation in the advisor's `HttpServingClient` so one trace spans
   advisor → serving.
3. Flip the association / demo-refresh / simulator Lambdas to `Tracing.ACTIVE`.
4. An `advisor-invocation` canary against the AgentCore runtime endpoint.
