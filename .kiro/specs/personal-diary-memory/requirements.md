# Requirements Document

Feature: Personal Diary Memory

## Introduction

A signed-in user of the web chatbot can tell the AI Advisor about their day — how
they felt, symptoms they noticed — have it recorded as a private diary entry, and
have that history taken into account in later advice. Each person authenticates
with their own account, so the diary is private to them and consistent across
conversations.

**This feature is deployment, wiring, and authentication over code that already
exists.** The domain logic is built and tested: the ingestion service
(data-processing) has `DynamoDbProfileStore` and `DynamoDbSymptomLogStore`
adapters selectable through its composition registry, a `CognitoAuthenticator`,
the `PUT /v1/symptoms/me` write route and `SymptomLogService`, and the
learned-threshold association that reads a user's diary history and weights future
advice. The advisor has an `HttpServingClient` that calls those routes and
forwards the caller's bearer credential unmodified. The web chatbot serves the UI
and proxies to the advisor on Bedrock AgentCore.

What is missing, and what this spec covers:

1. No DynamoDB tables are provisioned; the ingestion service runs with in-memory
   or empty stores.
2. The advisor is deployed with `serving_client=scripted`, not pointed at a real
   ingestion service.
3. There is no per-user login: the chatbot has only a shared access-key gate and
   forwards no user credential, so nothing keys a diary to a person.
4. The scheduled association derivation that turns diary history into
   Learned_Thresholds is not deployed.

Because this is health-adjacent data with real persistence and per-user
authentication across three deployed services, the acceptance criteria emphasise
identity, data protection, retention, erasure, and least privilege as much as the
happy path.

Target: AWS account <AWS_ACCOUNT_ID>, region us-east-1. The advisor runtime is
`aqmadvisor_aqm_advisor-ws73wzAfQJ`. The offline test guarantee (suites pass with
no cloud credentials and no network beyond localhost; CDK synthesis resolves
nothing from an account) must be preserved throughout.

### Non-goals

- Re-implementing the diary, profile, or association domain logic — it exists and
  is tested; this spec deploys and wires it.
- Migrating the simulator or the ingest/push path; only the serving interface of
  the ingestion service is in scope.
- A public sign-up flow for arbitrary members of the public; the POC provisions a
  small set of demo users in the Cognito user pool.

## Glossary

- **Advisor:** the AI Advisor deployed on Bedrock AgentCore (runtime
  `aqmadvisor_aqm_advisor-ws73wzAfQJ`, us-east-1). It runs the advisory turn and
  reaches the ingestion service over HTTP.
- **Ingestion service (Service 2):** `data-processing`, whose serving API stores
  and serves per-user profiles and diary entries and computes advice inputs.
- **Chatbot:** the `web-chatbot` service, a browser UI that proxies to the
  advisor.
- **Diary entry / Symptom_Log:** a user-confirmed record of how their day went,
  keyed by verified user id and calendar date, stored in DynamoDB.
- **Profile:** the user's stored health-relevant profile (condition, sensitivity,
  medications), keyed by verified user id.
- **Learned_Thresholds:** per-user thresholds derived from accumulated diary
  history that weight future advice; produced by a scheduled derivation and read
  on the serving path, never recomputed there.
- **Verified identity:** the pseudonymous user id established by the
  authenticator from a verified Cognito JWT; the key for all per-user storage.
- **JWT:** the Cognito-issued token proving the user's identity, forwarded
  browser -> chatbot -> advisor -> ingestion, unmodified.

## Requirements

### Requirement 1: Per-user authentication with Cognito

**User Story:** As a user, I want to sign in with my own account, so that my diary
and advice are private to me and not shared with other users of the demo.

#### Acceptance Criteria

1. THE SYSTEM SHALL provision a Cognito user pool and app client in us-east-1 via
   CDK, with at least one demo user, such that a user can obtain a JWT by
   authenticating.
2. WHEN a user opens the chatbot and is not authenticated THE SYSTEM SHALL present
   a sign-in step and SHALL NOT allow a chat turn until a valid JWT is held.
3. WHEN a user signs in with valid credentials THE SYSTEM SHALL obtain a Cognito
   JWT and retain it only for the browser session.
4. WHEN a user signs in with invalid credentials THE SYSTEM SHALL report the
   failure without revealing whether the username or the password was wrong.
5. THE ingestion service SHALL be deployed with the `cognito` authenticator,
   configured with the same user pool id and app client id as the chatbot uses,
   so that a token minted for the chatbot is accepted by the serving API.
6. THE SYSTEM SHALL treat the Cognito subject (or the pool's configured
   pseudonymous claim) as the verified user id that keys the profile and diary,
   and SHALL NOT derive identity from any request body field.

### Requirement 2: Credential pass-through across the three services

**User Story:** As a user, I want my identity to reach the store that saves my
diary, so that the right entry is written under my account and no one else's.

#### Acceptance Criteria

1. WHEN the chatbot sends a chat turn THE SYSTEM SHALL forward the user's JWT to
   the advisor runtime as a bearer credential.
2. THE advisor SHALL forward the caller's credential to the ingestion service
   unmodified — never parsed, cached, logged, or reissued (assumption A4a).
3. WHEN the ingestion service receives a request THE SYSTEM SHALL verify the JWT
   against the configured Cognito pool before any store access.
4. WHEN the JWT is missing, malformed, expired, or minted for a different pool or
   client THE ingestion service SHALL reject the request with its documented
   unauthenticated status and no store access.
5. THE SYSTEM SHALL carry the credential only over TLS on every hop
   (browser→chatbot, chatbot→advisor, advisor→ingestion).
6. THE chatbot's existing shared access-key gate MAY be retained as a coarse
   outer gate, but SHALL NOT be the mechanism that identifies the user; the JWT
   is the identity.

### Requirement 3: Recording a diary entry

**User Story:** As a user, I want to tell the advisor how my day went and have it
saved, so that it becomes part of my record.

#### Acceptance Criteria

1. WHEN a user describes their day and confirms the entry THE advisor SHALL call
   the `PUT /v1/symptoms/me` route with the confirmed entry.
2. THE ingestion service SHALL be deployed with the `dynamodb` symptom-log
   adapter, so that a written entry is durably persisted to DynamoDB keyed by the
   verified user id and calendar date.
3. WHEN a user records a second entry for the same calendar date THE SYSTEM SHALL
   replace that date's entry rather than duplicate it (the store's documented
   composite-key behaviour).
4. WHEN the write succeeds THE SYSTEM SHALL confirm to the user that the entry was
   saved, without echoing any stored health detail back through a log.
5. IF the store write fails THE SYSTEM SHALL return a degraded, non-crashing
   response that names the failure kind and SHALL NOT claim the entry was saved.
6. THE advisor SHALL record a diary entry only after the user has explicitly
   confirmed it (the existing tool contract), never silently.

### Requirement 4: Recall and influence on future advice

**User Story:** As a user, I want my past entries to shape later advice, so that
the advisor learns what affects me rather than giving generic guidance.

#### Acceptance Criteria

1. WHEN a user starts a new conversation THE advisor SHALL retrieve that user's
   stored profile from the ingestion service, keyed to their verified identity.
2. THE ingestion service SHALL be deployed with the `dynamodb` profile adapter,
   so a profile written in one session is present in the next.
3. THE SYSTEM SHALL deploy or schedule the association-derivation path so that a
   user's accumulated diary history is turned into Learned_Thresholds.
4. WHEN Learned_Thresholds exist for a user THE serving response SHALL apply them
   so that prior diary history measurably influences the advice for that user.
4a. THE association derivation SHALL have a persisted exposure history to correlate
   against — a readings store and the sensor registry it resolves sites from — so
   the derivation can produce a threshold rather than finding nothing (see
   Requirement 10). WHERE no persisted exposure history exists for a user's sites
   THE derivation SHALL write no threshold and the response SHALL fall back to the
   remaining tiers (Requirement 4.5).
5. WHEN no diary history or association exists for a user THE SYSTEM SHALL serve
   advice exactly as it does today, with the remaining escalation tiers
   unchanged — the association is additive, never a regression.
6. THE association SHALL be read from where the derivation stored it, never
   recomputed on the serving request path.

### Requirement 5: Protection of health-adjacent data

**User Story:** As a user, I want what I tell the advisor about my health treated
carefully, so that it is not exposed, over-retained, or readable by the wrong
party.

#### Acceptance Criteria

1. THE SYSTEM SHALL encrypt the profile, symptom-log, readings, and sensor-registry tables at rest.
2. THE SYSTEM SHALL carry all diary and profile data in transit over TLS only.
3. THE SYSTEM SHALL key stored data by the pseudonymous verified identity and
   SHALL NOT store a user's plaintext credential or JWT.
4. THE SYSTEM SHALL NOT write health detail, profile field values, or credentials
   to any log at any level; a log entry about a diary write SHALL reference only
   the pseudonymous identity and the fact of the write.
5. THE SYSTEM SHALL keep the non-diagnostic disclaimer on every advisory response,
   unchanged by this feature.
6. THE SYSTEM SHALL enforce the configured retention window on diary entries, so
   entries past the window are not returned.

### Requirement 6: User erasure

**User Story:** As a user, I want to be able to have my data deleted, so that I
retain control over my health information.

#### Acceptance Criteria

1. THE SYSTEM SHALL expose the existing profile-deletion / forget-user path so a
   user's profile can be erased.
2. WHEN a user's data is erased THE SYSTEM SHALL remove the stored profile and
   SHALL return the documented deletion receipt.
3. WHEN a user's data is erased THE SYSTEM SHALL ensure no subsequent advice
   reflects the erased profile, falling back to the served default.
4. THE erasure path SHALL be authenticated as the same user and SHALL NOT allow
   one user to erase another's data.

### Requirement 7: Least-privilege provisioning

**User Story:** As the operator, I want each component to hold only the access it
needs, so that a compromise of one part cannot read or write everything.

#### Acceptance Criteria

1. THE SYSTEM SHALL grant each Lambda an execution role scoped to only the
   resources it uses: the ingestion serving Lambda may read/write only the
   profile and symptom-log tables; the chatbot Lambda may invoke only the advisor
   runtime; the advisor's role is unchanged except as needed to reach serving.
2. THE SYSTEM SHALL supply table names, the serving base URL, and Cognito
   identifiers as deploy-time configuration, never committed to the repository.
3. THE SYSTEM SHALL keep any secret material (Cognito app client secret if used,
   API keys) in Secrets Manager or SSM, or supplied at runtime, never in a
   committed file.
4. THE DynamoDB tables SHALL carry a removal policy appropriate to a POC so the
   stack can be torn down cleanly, and the teardown command SHALL be documented.

### Requirement 8: Provisioning and wiring, without breaking the offline guarantee

**User Story:** As a contributor, I want the whole feature deployable from the
existing tooling and still testable offline, so that CI stays green and the
environment stays reproducible.

#### Acceptance Criteria

1. THE SYSTEM SHALL provision the DynamoDB tables, the Cognito pool, and the
   updated Lambdas through the existing `infra/` CDK app, as stacks that can be
   deployed and torn down independently.
2. THE SYSTEM SHALL redeploy the advisor with `serving_client=http` and
   `AQM_ADVISOR_SERVING_BASE_URL` pointed at the deployed ingestion serving API.
3. THE SYSTEM SHALL provide the single documented commands (via `just` recipes)
   to deploy, invoke, and tear down the feature.
4. THE offline test suite SHALL continue to pass with no AWS credentials and no
   network beyond localhost, and CDK synthesis SHALL resolve nothing from an
   account, with the full toolchain present.
5. WHERE a store or authenticator is unavailable at cold start due to
   misconfiguration THE affected service SHALL fail fast with a clear message
   rather than serve a half-configured request path.
6. THE SYSTEM SHALL add each new or changed service's checks to the existing
   test/lint/typecheck command surface so CI exercises them.

### Requirement 9: End-to-end demonstration

**User Story:** As the operator, I want to prove the loop works, so that the demo
shows memory rather than claims it.

#### Acceptance Criteria

1. WHEN a signed-in user records a diary entry in one conversation and returns in
   a later conversation THE SYSTEM SHALL demonstrate that the entry persisted and
   that the user's profile is recalled.
2. WHEN two different users use the demo THE SYSTEM SHALL keep their diaries and
   profiles separate, each seeing only their own.
3. THE SYSTEM SHALL demonstrate that accumulated diary history changes the advice
   a user receives, relative to a user with no history.
4. THE demonstration SHALL be reproducible from documented commands and SHALL NOT
   require reading data that belongs to another user.


### Requirement 10: Persisted exposure history for the association

**User Story:** As a user, I want the advisor to learn what air quality affects
me, so that its advice reflects my real exposures and not a blank history.

#### Acceptance Criteria

1. THE SYSTEM SHALL provision a readings table and a sensor-registry table in
   DynamoDB, keyed as the ingestion service's `DynamoDbReadingsStore`
   (`SITE#{SiteCode}#SP#{Species}` partition, interval-start sort) and
   `DynamoDbSensorRegistryStore` (`site_code` partition) expect, both encrypted at
   rest and with the POC removal policy.
2. THE SYSTEM SHALL deploy the association Lambda with the `dynamodb` readings and
   sensor-registry adapters selected and their table names in its environment, so
   the derivation reads a persisted exposure history rather than an empty store.
3. THE SYSTEM SHALL provide a documented seeding path that populates the readings
   and sensor-registry tables with a bounded, representative exposure history for
   the demo sites, so the association has data to correlate against.
4. THE seeding path SHALL be idempotent and SHALL NOT require the ingest/push
   pipeline; it MAY write directly through the readings and registry store
   adapters (or a small loader), consistent with the serving-only POC scope.
5. THE association Lambda's execution role SHALL be scoped to read the readings
   and sensor-registry tables and read/write the symptom-log table, and read the
   profiles table, and SHALL hold no wildcard DynamoDB grant.
6. THE serving air-quality view MAY continue to use a seeded or in-memory source
   for the current-conditions numbers; only the association's exposure HISTORY
   requires the persisted readings store.
