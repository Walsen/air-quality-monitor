# Design Document

Feature: Personal Diary Memory

## Overview

This feature turns the advisor's existing-but-dormant memory capability on by
deploying the pieces that were left as in-memory stand-ins, and by adding per-user
Cognito authentication that flows through all three services. No domain logic is
written: the ingestion service already has DynamoDB adapters, a Cognito
authenticator, the `PUT /v1/symptoms/me` route, the profile-delete/forget path,
and the scheduled `AssociationJob` that turns diary history into
Learned_Thresholds. The work is provisioning (DynamoDB, Cognito, a scheduled
Lambda), configuration (selecting the `dynamodb` adapters and the `cognito`
authenticator), and wiring (chatbot sign-in → JWT → advisor → ingestion).

The design is deliberately additive and reversible: each new AWS resource is a
stack in the existing `infra/` CDK app that can be deployed and torn down on its
own, and every service keeps behaving exactly as it does today when the new
configuration is absent.

### Data flow

```
Browser ──JWT (Cognito)──▶ Chatbot Lambda ──JWT──▶ Advisor (AgentCore)
                                                        │ forwards JWT unmodified
                                                        ▼
                                              Ingestion serving API (Lambda)
                                                 │ verify JWT (Cognito)
                                                 ├──▶ DynamoDB: profiles
                                                 └──▶ DynamoDB: symptom log
                                                          ▲
                            EventBridge schedule ──▶ Association Lambda
                            (reads diary + readings, writes Learned_Thresholds)
```

The advisor reads the profile and current conditions at the start of a turn,
offers to record a diary entry when the user describes their day, and — on a later
turn — the serving response applies any Learned_Thresholds the association job
stored for that user, so history influences advice.

## Architecture

### Components and what changes

| Component | Change |
|-----------|--------|
| **Cognito** (new) | User pool + app client with `USER_PASSWORD_AUTH`; one or more demo users. Issuer + JWKS drive JWT verification. |
| **DynamoDB** (new) | Four tables — `profiles`, `symptom-log`, `readings`, `sensor-registry` — encrypted at rest, PAY_PER_REQUEST, removal policy DESTROY (POC). The readings + registry back the association's exposure history (Requirement 10). |
| **Ingestion serving Lambda** (reconfig) | Deploy with `profile_store=dynamodb`, `symptom_log_store=dynamodb`, `authenticator=cognito`; env carries table names + Cognito ids; IAM scoped to the two tables. |
| **Association Lambda** (new) | Runs `AssociationJob` on an EventBridge schedule; reads the profile + diary + readings (resolving sites via the registry) and writes Learned_Thresholds; IAM scoped to those four tables, no wildcard. |
| **Advisor** (redeploy) | `serving_client=http`, `AQM_ADVISOR_SERVING_BASE_URL` = ingestion serving URL. Its AgentCore role is unchanged; it reaches serving over the public HTTPS API. |
| **Chatbot** (extend) | Adds a Cognito sign-in step; obtains a JWT (USER_PASSWORD_AUTH); sends it to the advisor as the bearer credential on `/chat`. Keeps the shared access-key as an optional coarse outer gate. |
| **Readings seeding** (new) | An idempotent loader that populates the `readings` + `sensor-registry` tables with a bounded, representative exposure history for the demo sites, so the association has data to correlate. Writes through the store adapters; no ingest pipeline. |

### Identity is the spine

The Cognito subject claim is the verified user id. It is minted by Cognito, put in
the JWT, verified by the ingestion service's `CognitoAuthenticator`, and used as
the DynamoDB partition key for both profile and diary. Nothing downstream derives
identity from a request body. This is what makes the diary private per user and
consistent across conversations: the same login always resolves to the same key.

**The credential is forwarded, never parsed** (assumption A4a). The chatbot puts
the JWT in the `Authorization: Bearer` header to the advisor; the advisor's
AgentCore entrypoint already reads that header and forwards it to serving; the
ingestion service is the only place the token is verified. No hop in between reads
its claims.

## Components and Interfaces

### 1. Cognito stack (`infra/aqm_infra/cognito_stack.py`)

- A `UserPool` (email or username sign-in), and a `UserPoolClient` with
  `USER_PASSWORD_AUTH` enabled and no client secret (a browser-side public client
  cannot keep a secret; the shared access-key remains the coarse gate).
- Outputs: user pool id, app client id, issuer URL
  (`https://cognito-idp.<region>.amazonaws.com/<pool-id>`). These feed both the
  ingestion service config and the chatbot.
- Demo users are created out-of-band (documented `just` recipe using `awscli2`
  admin-create-user + set-password), NOT committed. The stack provisions the pool
  only.

### 2. DynamoDB tables

The serving stack provisions `profiles` and `symptom-log`; the readings + registry
tables (Requirement 10) are provisioned alongside them so both the serving stack
and the association stack can reference them by name.

- `profiles`: partition key `user_id` (string). Matches `DynamoDbProfileStore`.
- `symptom-log`: partition key `user_id`, sort key the ISO date. Matches
  `DynamoDbSymptomLogStore`'s composite key and its learned-threshold items.
- `readings`: partition key `pk` = `SITE#{SiteCode}#SP#{Species}`, sort key `sk`
  = interval-start ISO. Matches `DynamoDbReadingsStore` (which also uses a
  conditional write on `pk`/`sk`). Backs the association's exposure history.
- `sensor-registry`: partition key `site_code`. Matches
  `DynamoDbSensorRegistryStore`; the association's `sites_for` resolver reads it
  to map a user's location to nearby site codes.
- Both: `encryption=AWS_MANAGED` (or a CMK if required later), `billingMode`
  PAY_PER_REQUEST, `removalPolicy=DESTROY` for the POC.
- Table names are passed to the Lambda as env (`AQM_TABLE_PROFILES`,
  `AQM_SYMPTOM_LOG_TABLE`), matching the composition's `_table(...)` /
  `_required_setting(...)` reads.

### 3. Ingestion serving Lambda (reconfigured `LambdaRestStack` or a dedicated stack)

- Environment selects the real adapters and the authenticator:
  - `AQM_ENABLE_SERVING=true`, `AQM_ENABLE_PULL=false`, `AQM_ENABLE_PUSH=false`
  - `AQM_PROFILE_STORE=dynamodb`, `AQM_SYMPTOM_LOG_STORE=dynamodb`
    (adapter-selection keys per the loader), other stores may remain `memory`
    where they are not part of this feature (readings/registry back the
    air-quality view; see Open Questions).
  - `AQM_AUTHENTICATOR=cognito`, `AQM_COGNITO_USER_POOL_ID`,
    `AQM_COGNITO_CLIENT_ID`, `AQM_COGNITO_ISSUER`, `AQM_AWS_REGION`.
  - `AQM_TABLE_PROFILES`, `AQM_SYMPTOM_LOG_TABLE`.
- IAM: `dynamodb:GetItem/PutItem/Query/DeleteItem/UpdateItem` on ONLY the two
  table ARNs. No wildcard. The JWKS fetch is public HTTPS, no IAM.
- Fail-fast: `resolve_and_validate` already refuses a bad configuration at cold
  start; a missing table name or Cognito id raises there (Req 8.5).

### 4. Association Lambda + schedule (`infra/aqm_infra/association_stack.py`)

- A Lambda whose handler constructs the `AssociationJob` from the same config
  path and runs it for the enrolled users (POC: iterate the demo users, or the
  profiles table). `AssociationJob` reads the PROFILE (to resolve the user's
  sites, via a `sites_for` resolver backed by the sensor registry), the
  SYMPTOM-LOG (the diary), and the READINGS store (the exposure history), then
  writes Learned_Thresholds back to the symptom-log via `put_learned_thresholds`.
- An EventBridge (scheduler) rule invokes it on a schedule (hourly for the demo,
  so a freshly recorded diary influences advice within the demo window).
- IAM scoped to exactly four tables: read the profiles, readings, and
  sensor-registry tables; read+write the symptom-log table. No wildcard.
- The readings + registry must be SEEDED (Requirement 10) before the derivation
  can produce anything; without an exposure history the job writes no threshold
  and the serving path falls back to the remaining tiers (additive, Req 4.5).
- A `just` recipe can invoke it on demand for the demo so we don't wait for the
  schedule.

### 5. Advisor redeploy (agent-advisor/deploy)

- Change env on the AgentCore runtime: `AQM_ADVISOR_SERVING_CLIENT=http`,
  `AQM_ADVISOR_SERVING_BASE_URL=<ingestion serving URL>`. Redeploy via the
  existing `agentcore deploy` flow. No code change — `HttpServingClient` exists.
- The advisor's existing behaviour (tool loop, grounding, medication-closure,
  fail-closed) is unchanged; it now talks to a real store instead of a stand-in.

### 6. Chatbot sign-in (`web-chatbot`)

- Add a sign-in step to the page: username + password, POSTed to a new
  `POST /login` endpoint on the chatbot Lambda.
- `/login` calls Cognito `InitiateAuth` (USER_PASSWORD_AUTH) via boto3, returns
  the JWT (id or access token per what the ingestion authenticator validates) to
  the browser to hold for the session. The chatbot never stores it server-side.
- `/chat` accepts the JWT (Authorization header from the browser) and forwards it
  to the advisor as the bearer credential. The shared access-key gate remains as
  a coarse outer check but is no longer the identity.
- boto3 `cognito-idp:InitiateAuth` needs no IAM for an unauthenticated public
  client flow using `USER_PASSWORD_AUTH` with the app client id.

## Data Models

No new domain models. The wire and storage shapes already exist:

- **UserProfile** — stored by `DynamoDbProfileStore`, keyed by `user_id`.
- **SymptomEntry** — stored by `DynamoDbSymptomLogStore`, keyed by
  `(user_id, entry_date)`; a re-put for a date replaces it.
- **LearnedThreshold** — stored alongside the diary (same store, same lifecycle,
  so erasing the diary erases the thresholds), read on the serving path.

## Error Handling

- **Auth failures** (missing/expired/wrong-pool JWT): the ingestion service
  rejects before any store access with its documented unauthenticated status; the
  advisor surfaces a degraded response; the chatbot shows "please sign in again".
- **Store write failure** on a diary entry: the advisor returns a degraded,
  non-crashing response naming the kind and does NOT claim the entry saved
  (Req 3.5). No health detail in the log.
- **Cold-start misconfiguration** (missing table/Cognito id): fail fast at
  `resolve_and_validate`, so the Lambda never serves a half-configured request.
- **Association job failure** for one user: logged with the pseudonymous id and
  skipped; the serving path simply finds no thresholds and serves the remaining
  tiers unchanged (Req 4.5).
- **Chatbot `/login` failure**: report a generic failure without revealing which
  field was wrong (Req 1.4).

## Security and Data Protection

- Encryption at rest on both tables; TLS on every hop (API Gateway, AgentCore,
  Cognito are HTTPS).
- Least-privilege IAM per Lambda: serving Lambda → only its two tables;
  association Lambda → only the tables it reads/writes; chatbot Lambda → only
  invoke the advisor runtime (+ the unauthenticated Cognito InitiateAuth, which
  needs no IAM). No wildcards.
- No health data, profile field values, or tokens in logs — the services already
  log only the pseudonymous id and the fact of a write; this feature must not add
  a log line that violates that, asserted by test where practical.
- The non-diagnostic disclaimer stays on every response.
- Retention: the store applies the configured window at query time; entries past
  it are not returned.
- Erasure: the existing profile-delete/forget path is exposed and authenticated
  as the same user; forgetting deletes the diary and, with it, the learned
  thresholds.

## Testing Strategy

- **Offline first.** Every service's unit/property suite must keep passing with no
  AWS and no network beyond localhost. The Cognito key resolver is already
  deferred behind a callable so verification logic is testable without JWKS
  fetches; the DynamoDB adapters are exercised against LocalStack in the
  container-fenced suite, not the offline one.
- **CDK synthesis stays credential-free.** The new stacks (Cognito, DynamoDB,
  association) must synth with no account lookups; assertions over the synthesized
  template run in the offline suite.
- **Container-fenced integration** (LocalStack): profile + diary round-trip
  through the DynamoDB adapters; the association job reads a seeded diary and
  writes thresholds; the serving path reads them back and the advice changes.
- **End-to-end (manual, documented):** sign in as demo user A, record a day,
  start a new conversation, confirm recall; sign in as user B, confirm isolation;
  show advice differs with vs without history. Reproducible from `just` recipes.
- **TDD** for any glue code written (chatbot `/login`, the association Lambda
  handler, the CDK stacks' template assertions).

## Correctness Properties

These are the invariants this feature must hold. Most are enforced by the
existing services' suites; the ones this feature introduces get their own tests.

### Property 1: Per-user isolation

For any two distinct authenticated users, neither can read or write the other's
profile or diary. The DynamoDB key is the verified subject, and no route accepts
a user id from the body. Verified by a property test over generated identities
against the store adapters under LocalStack.

**Validates: Requirements 1.6, 2.3, 9.2**

### Property 2: Credential forwarded, never parsed

On the chatbot -> advisor -> ingestion path, the JWT is byte-identical at each hop
until the ingestion service verifies it; no intermediate hop decodes its claims.
Asserted against the advisor's existing forward contract and the chatbot's
pass-through.

**Validates: Requirements 2.2, 2.1**

### Property 3: No secret or health detail in logs

For any diary write or profile read, no log record at any level contains a token,
a profile field value, or diary content — only the pseudonymous id and the event.
A scan-based test, mirroring the advisor's existing log-redaction tests.

**Validates: Requirements 5.3, 5.4**

### Property 4: Association is additive

For a user with no diary history, the served advice is identical to the
pre-feature behaviour; Learned_Thresholds only ever add the fourth precedence
tier, never remove or weaken the other three.

**Validates: Requirements 4.4, 4.5**

### Property 5: Erasure is total

After a forget for a user, no subsequent response reflects that user's profile or
diary, and their Learned_Thresholds are gone (they share the diary's lifecycle).

**Validates: Requirements 6.2, 6.3, 5.6**

### Property 6: Same login, same key

Two separate sign-ins by the same Cognito user resolve to the same storage key,
so a diary entry from one conversation is visible in the next.

**Validates: Requirements 4.1, 4.2, 9.1**

### Property 7: Offline guarantee preserved

The full offline suite passes with no AWS credentials and no network beyond
localhost, and CDK synthesis of the new stacks resolves nothing from an account.

**Validates: Requirements 8.4**

## Deployment and Teardown

- New/changed stacks in `infra/`: `aqm-poc-cognito`, the ingestion serving stack
  (now with tables + dynamodb + cognito), `aqm-poc-association`. Deployed with
  `--exclusively` and context for names/ids, so each is independent.
- Advisor redeploy via `agentcore deploy` with the two changed env vars.
- Chatbot redeploy via its stack with the Cognito ids in env.
- `just` recipes: deploy each, create a demo user, invoke the association job on
  demand, and tear everything down (`cdk destroy --exclusively` per stack;
  `agentcore` env revert or redeploy for the advisor).

## Open Questions / Decisions

1. **Air-quality readings source — DECIDED (Option 1, full persistence).** The
   association job needs a PERSISTED exposure history to correlate symptoms
   against, so this feature provisions real `readings` + `sensor-registry`
   DynamoDB tables and seeds them with a bounded demo history (Requirement 10).
   The serving path's CURRENT-conditions numbers may still come from a
   seeded/in-memory source; only the association's exposure HISTORY requires the
   persisted readings store. This widens the blast radius from two tables to four
   plus a seeding loader, accepted deliberately so "it learns what affects me"
   works end to end.
2. **Which token** the ingestion `CognitoAuthenticator` validates (id vs access
   token) and the exact claim used as `user_id` — pin against the authenticator's
   implementation during design-to-tasks so the chatbot forwards the right one.
3. **Association cadence** for the demo (hourly vs on-demand). On-demand via a
   `just` recipe is the most demoable; a schedule proves the "runs on its own"
   requirement. Do both: a schedule plus an on-demand invoke.
