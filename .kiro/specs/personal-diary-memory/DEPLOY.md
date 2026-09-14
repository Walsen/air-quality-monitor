# Personal Diary Memory — Deployment & Teardown

This document records the live deployment of the personal-diary-memory feature
and its clean removal (Requirements 7.4 and 8.3). It is the operational
companion to `requirements.md`, `design.md`, and `tasks.md`.

Every command below is a documented `just` recipe or a `cdk`/`aws` invocation
run from inside the devbox environment (`direnv` activates it on `cd`, or prefix
with `devbox run --`). There is one documented way to run each thing.

> **All identifiers and URLs in this document are placeholders.** Real pool ids,
> client ids, ARNs, table names, and URLs are captured at deploy time and passed
> as recipe arguments or environment values. **No secret — password, access key,
> token, or private key — is ever committed.** Pass secrets as arguments, via a
> git-ignored `.env.local`, or from the platform secret store.

---

## What is deployed

Target: **AWS account <AWS_ACCOUNT_ID>, region us-east-1.**

CloudFormation stacks:

| Stack | Task | Notes |
|-------|------|-------|
| `aqm-poc-cognito` | 15 | Cognito user pool + app client |
| `aqm-poc-serving` | 16 | DynamoDB tables (profiles / symptom-log / readings / sensor-registry) + serving Lambda + HTTP API |
| `aqm-poc-association` | 18 | Association Lambda + EventBridge schedule; **imports `aqm-poc-serving`'s table-name exports cross-stack** |
| `AqmAdvisorRuntime` | 19 | CDK-managed AgentCore advisor runtime; a **separate CDK app** under `agent-advisor/infra/` (not the `infra/` app) |
| `aqm-poc-chatbot` | 20 | Pre-existing chatbot stack, redeployed to point at the new runtime + Cognito |

Also live:

- Demo users `demo-user-a`, `demo-user-b` in the pool.
- The **old starter-toolkit advisor** stack `AgentCore-aqmadvisor-default` and
  its runtime `aqmadvisor_aqm_advisor-ws73wzAfQJ`, superseded by the CDK runtime
  above and to be retired separately (**not** CDK-managed by this repo's `infra/`
  app).

---

## Prerequisites

Do these once before deploying; none can be done by the deploying agent inside
the CDK.

1. **An SSO / admin identity for account <AWS_ACCOUNT_ID> in us-east-1.** The
   workspace resolves AWS through `AWS_PROFILE`; export the profile that assumes
   the deploy role before running any deploy recipe.
2. **CDK bootstrap present** in `aws://<AWS_ACCOUNT_ID>/us-east-1` (the `CDKToolkit`
   stack). Without it a deploy fails with
   `SSM parameter /cdk-bootstrap/.../version not found`. Bootstrap from an admin
   identity: `cdk bootstrap aws://<AWS_ACCOUNT_ID>/us-east-1`.
3. **Anthropic model access enabled**, once per account, in the Bedrock console
   (Model access → Claude Sonnet 4.6 → submit the use-case form). There is no API
   the CDK can call for this.
4. **`node` on PATH for jsii** and **`TMPDIR` on a roomy disk.** Both are host
   quirks the `just` cdk recipes already handle through the `cdk_env` preamble:
   node is derived from the pinned `aws-cdk-cli` shebang, and `TMPDIR` is pointed
   at a repo-root `.cdk-tmp` because the host `/tmp` is a small tmpfs that a full
   synth (~76M of Lambda assets) overflows. If you run a raw `cdk` command
   yourself (the advisor and chatbot examples below), set `TMPDIR` to a roomy
   path first.

---

## Deploy sequence

Strictly ordered: **Cognito → serving → association**, then advisor, then
chatbot. Each downstream deploy consumes the previous stack's outputs, so capture
them as you go.

### 1. Cognito

```bash
just deploy-cognito
```

Capture from the stack outputs: **UserPoolId**, **AppClientId**, **IssuerUrl**.

### 2. Demo users (×2)

```bash
just create-demo-user <UserPoolId> demo-user-a '<password-a>'
just create-demo-user <UserPoolId> demo-user-b '<password-b>'
```

**Never commit the passwords.** They are set PERMANENT so sign-in needs no
challenge.

### 3. Serving stack (tables + serving Lambda)

```bash
just deploy-serving <UserPoolId> <AppClientId> <IssuerUrl>
```

Capture: the **ApiUrl** and the four generated table names —
**readings**, **sensor-registry**, **profiles**, **symptom-log**.

### 4. Seed the readings + sensor-registry tables

```bash
just seed-readings <readings_table> <registry_table>
```

Idempotent — a second run is a no-op. Gives the association a persisted exposure
history for the demo sites (Requirement 10).

### 5. Association Lambda + schedule

```bash
just deploy-association
```

This imports the serving stack's table-name exports cross-stack, so serving
**must** already be deployed (step 3).

### 6. Advisor runtime (separate CDK app)

The advisor lives in `agent-advisor/infra/` and its `app.py` requires the deploy
env even to synth. Deploy it with the same Cognito client id Service 2 validates
(one identity, not two that drift).

**Preferred — one command that resolves identifiers from CloudFormation:**

```bash
just deploy-advisor
```

`deploy-advisor` reads the account from STS, the Cognito app-client id and user
pool id from the `aqm-poc-cognito` stack outputs, and the serving base URL from
`aqm-poc-serving` — so nothing is hardcoded and the values cannot drift from the
stacks that own them. It derives the discovery URL from the pool id and defaults
the model to the `us.` inference profile. To enable prompt caching for a
cost/latency experiment, pass `caching=true`. To target a different account or
stack set, override any of the resolved values:

```bash
just deploy-advisor client_id=<AppClientId> pool_id=<UserPoolId> \
    serving_base_url=<serving ApiUrl>
```

**Manual equivalent** (for a context where `just` or the stacks are not
available), exporting the env the advisor `app.py` reads:

```bash
cd agent-advisor/infra
export TMPDIR=<roomy-path>            # host /tmp is a small tmpfs
export CDK_DEPLOY_ACCOUNT=<AWS_ACCOUNT_ID>
export CDK_DEPLOY_REGION=us-east-1
export AQM_ADVISOR_MODEL_ID=us.anthropic.claude-sonnet-4-6
export AQM_COGNITO_CLIENT_ID=<AppClientId>
export AQM_COGNITO_DISCOVERY_URL=https://cognito-idp.us-east-1.amazonaws.com/<UserPoolId>/.well-known/openid-configuration
export AQM_ADVISOR_SERVING_BASE_URL=<serving ApiUrl>
cdk deploy AqmAdvisorRuntime
```

Keep the `us.` inference-profile id — the bare `anthropic.claude-sonnet-4-6` has
no in-region on-demand support and errors at invoke. Capture the **new runtime
ARN** from the output.

### 7. Chatbot redeploy

Point the pre-existing chatbot at the new advisor runtime and the Cognito client:

```bash
cd infra
export TMPDIR=<roomy-path>
cdk deploy --exclusively aqm-poc-chatbot \
    -c chatbot_runtime_arn=<new advisor ARN> \
    -c chatbot_access_key=<shared access key> \
    -c chatbot_cognito_client_id=<AppClientId>
```

---

## Gotchas we hit (so a re-deploy does not rediscover them)

These are the real fixes made during Phase F. Keep them factual.

- **Serving Lambda needs `cryptography` shipped.** RS256 JWT verification
  requires `pyjwt[crypto]`; without the native `cryptography` wheel bundled, the
  Cognito authenticator cannot validate tokens.
- **Association stack needs the batch interface flag + the sensor-registry
  store.** Deploy with `AQM_ENABLE_PUSH=true` and the sensor-registry store
  selected, or the derivation has no exposure history to correlate against.
- **The advisor authorizer must validate `aud` only, not `client_id`.** The
  forwarded token is the Cognito **ID token**, whose audience is the client id;
  validating a `client_id` claim (present on access tokens) rejects it.
- **The advisor runtime must allowlist the `Authorization` header** so the
  forwarded bearer credential reaches the container.
- **The chatbot must invoke over HTTPS Bearer, not boto3 SigV4.** AgentCore
  behind a JWT authorizer expects the bearer token, not a SigV4-signed call.
- **The advisor adapters are per-port env vars, not a combined string:**
  `AQM_ADVISOR_SERVING_CLIENT=http`, `AQM_ADVISOR_MODEL=bedrock`, and
  `AQM_ADVISOR_MODEL_REGION` — each selects one port's adapter.
- **Serving `_build_app` wires the `SymptomLogService`** — the diary write route
  is only present when the app is built through that composition path.
- **A serving→association redeploy must redeploy serving first** to publish the
  registry export the association stack imports; otherwise the association deploy
  cannot resolve the cross-stack import.

---

## Teardown sequence

Reverse of deploy, for a clean removal (Requirements 7.4, 8.3). `teardown-diary`
alone does **not** remove everything — the advisor runtime, the chatbot, the demo
users, and the old starter-toolkit advisor are separate steps.

### 1. Diary stacks (association → serving → cognito)

```bash
just teardown-diary
```

Reverse cross-stack order: `aqm-poc-association` imports `aqm-poc-serving`'s
table-name exports, and CloudFormation refuses to delete a stack whose exports
are still imported, so **association must be destroyed before serving**. The
recipe already sequences association → serving → cognito with `--exclusively`.

> **Data loss is intentional here.** The DynamoDB tables carry
> `removalPolicy=DESTROY` (POC), so destroying `aqm-poc-serving` **removes the
> diary and profile data** along with the readings and registry. This is the
> expected POC behaviour and a deliberate data-loss step.

### 2. Advisor runtime

```bash
just teardown-advisor <AppClientId> \
    https://cognito-idp.us-east-1.amazonaws.com/<UserPoolId>/.well-known/openid-configuration \
    <serving ApiUrl>
```

The recipe exports the same deploy env `app.py` requires (it raises `SystemExit`
without an account even for a destroy) and runs
`cdk destroy AqmAdvisorRuntime --force` in `agent-advisor/infra`.

### 3. Chatbot

```bash
cd infra
cdk destroy --exclusively aqm-poc-chatbot \
    -c chatbot_runtime_arn=<new advisor ARN> \
    -c chatbot_access_key=<shared access key> \
    -c chatbot_cognito_client_id=<AppClientId>
```

(The context values match what the redeploy used; they are not secrets beyond the
access key, which is supplied at runtime.)

### 4. Demo users

```bash
just delete-demo-user <UserPoolId> demo-user-a
just delete-demo-user <UserPoolId> demo-user-b
```

(Destroying `aqm-poc-cognito` in step 1 removes the pool and its users; these
commands are the documented way to remove an individual user when the pool is
being retained.)

### 5. Advisor env revert / retiring the old advisor

The chatbot and advisor previously pointed at the **starter-toolkit runtime**
`aqmadvisor_aqm_advisor-ws73wzAfQJ`.

- **If keeping the old advisor:** repoint the chatbot's `chatbot_runtime_arn`
  back at that runtime and redeploy the chatbot (step 3 style, with the old ARN).
- **Otherwise retire it:** delete the old starter-toolkit stack
  `AgentCore-aqmadvisor-default` **separately** — it is **not** CDK-managed by
  this repo's `infra/` app, so none of the recipes above touches it.

---

## Verification (offline)

- `just --list` shows the deploy and teardown recipes (`deploy-cognito`,
  `create-demo-user`, `deploy-serving`, `seed-readings`, `deploy-association`,
  `teardown-diary`, `teardown-advisor`, `delete-demo-user`).
- `just synth` synthesizes the `infra/` app resolving nothing from an account.
- `just synth-advisor-infra` synthesizes the advisor app offline.

These stay credential-free and need no network beyond localhost, preserving the
offline guarantee (Requirement 8.4).
