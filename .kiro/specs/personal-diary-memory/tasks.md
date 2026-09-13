# Implementation Plan: Personal Diary Memory

## Overview

This feature deploys and wires capability that already exists in code, and adds
per-user Cognito login across the three services. There is almost no new domain
logic; the work is CDK stacks, configuration, glue (chatbot sign-in + token
pass-through, the association Lambda handler), and the tests that pin the new
seams.

Sequencing is outside-in: first the pieces that need only this repository (CDK
stacks with template assertions, chatbot `/login` and pass-through against a fake
Cognito/advisor, the association Lambda handler against fakes), then the
container-fenced round-trips against LocalStack, then the real AWS provisioning
and cross-service wiring, and only then the end-to-end demonstration that needs
every deployed piece.

Every step is TDD (practices §3): the failing test first, then the smallest clean
change. Determinism and injected boundaries apply to glue code too. The offline
guarantee is a standing constraint on every task — no unit/property test may need
cloud credentials or network beyond localhost, and CDK synthesis must resolve
nothing from an account.

Language: Python 3.12; AWS CDK in Python; `pytest` + `hypothesis`; the DynamoDB
adapters are exercised against LocalStack in the container-fenced suite only.

Each design Correctness Property gets a test tagged `Feature: personal-diary-memory, Property {n}`.

## Tasks

### Phase A — CDK stacks (repo-only, offline synth)

- [x] 1. Cognito stack with a template assertion
  - Write a failing `aws_cdk.assertions` test that synthesizing `CognitoStack`
    produces a `UserPool` and a `UserPoolClient` with `USER_PASSWORD_AUTH` enabled
    and no client secret, and outputs the pool id, client id, and issuer URL.
  - Implement `infra/aqm_infra/cognito_stack.py` to pass. Synth must need no
    account lookups.
  - _Requirements: 1.1, 1.5, 8.1, 8.4_

- [x] 2. DynamoDB tables + serving stack configuration, with assertions
  - Failing template assertions: a `profiles` table (PK `user_id`) and a
    `symptom-log` table (PK `user_id`, SK date), both encrypted at rest,
    PAY_PER_REQUEST, removalPolicy DESTROY; the serving Lambda's env selects
    `dynamodb` profile + symptom stores and the `cognito` authenticator and
    carries the table names + Cognito ids; the execution role has DynamoDB
    actions scoped to ONLY the two table ARNs (no wildcard).
  - Implement the stack (extend `LambdaRestStack` or a dedicated
    `IngestionServingStack`) to pass.
  - _Requirements: 3.2, 4.2, 5.1, 7.1, 7.2, 8.1, 8.4_

- [x] 3. Readings + sensor-registry tables (Option 1: persisted exposure history)
  - Failing template assertions: a `readings` table (PK `pk`
    `SITE#{SiteCode}#SP#{Species}`, SK `sk` interval-start ISO) and a
    `sensor-registry` table (PK `site_code`), both encrypted at rest,
    PAY_PER_REQUEST, removalPolicy DESTROY. Confirm the exact key attribute names
    against `DynamoDbReadingsStore` / `DynamoDbSensorRegistryStore` in the
    ingestion service's dynamodb adapters — do not guess.
  - Add both tables to the serving stack (or a shared tables construct) and export
    their names so the serving and association stacks reference them; the serving
    Lambda gains read access to readings + registry scoped to those ARNs, no
    wildcard.
  - _Requirements: 10.1, 5.1, 7.1, 8.4_

- [x] 4. Association Lambda + schedule stack, with assertions
  - Failing assertions: a Lambda plus an EventBridge schedule rule targeting it;
    its role has DynamoDB actions scoped to only the symptom-log (and readings)
    table ARNs.
  - Implement `infra/aqm_infra/association_stack.py` to pass; synth needs no
    account.
  - _Requirements: 4.3, 7.1, 8.1, 8.4_

- [x] 5. Register the new stacks in `infra/app.py` behind context flags
  - Wire the three stacks so each deploys independently (`--exclusively`) and
    reads names/ids/keys from deploy-time context, never committed. A full-app
    synth with placeholder context succeeds offline.
  - _Requirements: 7.2, 7.3, 8.1, 8.4_

### Phase B — chatbot sign-in and token pass-through (repo-only)

- [x] 6. Chatbot `POST /login` against a fake Cognito client (TDD)
  - Failing tests: `/login` with valid creds returns a JWT and never stores it
    server-side; with invalid creds returns a generic failure that does not say
    which field was wrong; the shared access-key still gates the endpoint.
  - Implement `/login` calling Cognito `InitiateAuth` (USER_PASSWORD_AUTH) through
    an injected client so the test uses a fake — no AWS.
  - _Requirements: 1.2, 1.3, 1.4, 2.6_

- [x] 7. Chatbot `/chat` forwards the JWT to the advisor (TDD)
  - Failing tests: `/chat` takes the browser's `Authorization` bearer and passes
    it to the advisor client unchanged; a turn without a JWT is refused with a
    "sign in" response; the JWT is never logged.
  - Implement the pass-through in the advisor client call. (Property 2.)
  - _Requirements: 2.1, 2.2, 2.5, 5.4_

- [x] 8. Chatbot sign-in UI
  - Add the username/password sign-in step to the page; hold the JWT in the
    browser session only; block chat until signed in; "sign in again" on 401.
    Assert the page embeds no token and no client secret.
  - _Requirements: 1.2, 1.3, 5.3_

### Phase C — association Lambda handler (repo-only)

- [x] 9. Association Lambda handler against fakes (TDD)
  - Failing tests: the handler builds the `AssociationJob` from config and runs it
    for the enrolled users; a per-user failure is logged with the pseudonymous id
    and skipped, leaving others processed (Property 4's additive behaviour and the
    error-isolation rule); no health detail is logged.
  - Implement the handler using injected stores/clock so the test needs no AWS.
  - _Requirements: 4.3, 4.5, 5.4_

- [x] 10. Readings + registry seeding loader (TDD)
  - Failing tests: an idempotent loader writes a bounded, representative exposure
    history (demo sites in the registry + a readings history per site) through the
    readings + registry store adapters (or their in-memory equivalents in the unit
    test); re-running writes no duplicates. No ingest/push pipeline is used.
  - Implement the loader as a small module invocable from a `just` recipe and the
    fenced tests; it selects the same dynamodb adapters via config.
  - _Requirements: 10.3, 10.4_

### Phase D — container-fenced round-trips (LocalStack)

- [x] 11. Profile + diary round-trip through the DynamoDB adapters (LocalStack)
  - Integration-marked test: write a profile and a diary entry for a user, read
    them back; a second entry for the same date replaces; a second user's data is
    isolated (Property 1). Runs in the fenced suite, not offline.
  - _Requirements: 3.2, 3.3, 4.2, 9.2_

- [x] 12. Association end-to-end over LocalStack
  - Integration-marked test: seed a diary + readings, run the association job,
    read Learned_Thresholds back through the serving path, and assert the served
    advice reflects them for that user while a history-less user is unchanged
    (Property 4).
  - _Requirements: 4.3, 4.4, 4.5_

- [x] 13. Erasure over LocalStack
  - Integration-marked test: forget a user; the profile and diary are gone and the
    Learned_Thresholds with them; a later read serves the default; one user cannot
    erase another's data (Property 5).
  - _Requirements: 6.1, 6.2, 6.3, 6.4, 5.6_

### Phase E — command surface and offline guarantee

- [x] 14. Wire the new/changed checks into the Justfile and confirm offline
  - Add chatbot login/pass-through tests, the association handler tests, and the
    CDK template assertions to `just test`/`lint`/`typecheck`; add fenced recipes
    for the LocalStack round-trips; add deploy/create-user/invoke-association/
    teardown recipes.
  - Confirm the whole offline suite passes with AWS variables scrubbed and CDK
    synth resolves nothing from an account (Property 7).
  - _Requirements: 8.3, 8.6, 8.4_

### Phase F — real provisioning and cross-service wiring (needs the account)

- [ ] 15. Deploy Cognito + create demo users
  - Deploy `CognitoStack`; create demo users A and B via a documented `just`
    recipe (`awscli2` admin-create-user + set-password); do NOT commit
    credentials. Capture pool id / client id / issuer for downstream config.
  - _Requirements: 1.1, 1.5, 7.3_

- [ ] 16. Deploy the ingestion serving stack with tables + dynamodb + cognito
  - Deploy the tables and the serving Lambda configured for dynamodb stores and
    the cognito authenticator; verify cold-start fails fast on a missing table or
    Cognito id (Req 8.5); verify a valid demo JWT is accepted and an invalid one
    rejected before store access.
  - _Requirements: 1.5, 1.6, 2.3, 2.4, 3.2, 4.2, 8.5_

- [ ] 17. Seed the readings + registry tables
  - Run the seeding loader against the deployed readings + sensor-registry tables
    so the association has a persisted exposure history for the demo sites. Verify
    idempotence (a second run is a no-op).
  - _Requirements: 10.2, 10.3, 10.4_

- [ ] 18. Deploy the association Lambda + schedule; on-demand invoke
  - Deploy `AssociationStack`; provide a `just` recipe to invoke it on demand for
    the demo; confirm it writes thresholds for a seeded user.
  - _Requirements: 4.3_

- [ ] 19. Redeploy the advisor at real serving
  - Redeploy the AgentCore runtime with `serving_client=http` and
    `AQM_ADVISOR_SERVING_BASE_URL` = the ingestion serving URL. Confirm a turn now
    reaches the real store (a diary write persists; a profile read returns it).
  - _Requirements: 3.1, 4.1, 8.2_

- [ ] 20. Redeploy the chatbot with Cognito ids
  - Redeploy the chatbot stack with the pool/client ids in env; confirm sign-in
    works against the live pool and a chat turn carries the JWT through to serving.
  - _Requirements: 1.2, 2.1_

### Phase G — end-to-end demonstration

- [ ] 21. Demonstrate persistence, isolation, and influence
  - Sign in as user A, record a day, start a new conversation, confirm the entry
    persisted and the profile is recalled (Property 6). Sign in as user B, confirm
    isolation (Property 1). Run the association and show A's advice changes with
    history versus a history-less user (Property 4). All from documented commands.
  - _Requirements: 9.1, 9.2, 9.3, 9.4_

- [ ] 22. Document teardown
  - Document and verify `cdk destroy --exclusively` per new stack, the advisor env
    revert, and demo-user cleanup, so the whole feature can be removed cleanly.
  - _Requirements: 7.4, 8.3_

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1", "2", "6", "9", "10"], "note": "repo-only, no interdependencies" },
    { "wave": 2, "tasks": ["3", "4", "7"], "note": "3 needs 2's stack; 4 refs the tables; 7 needs 6" },
    { "wave": 3, "tasks": ["5", "8"], "note": "5 registers 1-4 in app.py; 8 UI needs 7" },
    { "wave": 4, "tasks": ["11"], "note": "profile/diary round-trip; needs 2,3 (LocalStack)" },
    { "wave": 5, "tasks": ["12", "13"], "note": "12 needs 9,10,11; 13 needs 11" },
    { "wave": 6, "tasks": ["14"], "note": "offline gate; needs 1-13 green" },
    { "wave": 7, "tasks": ["15"], "note": "real AWS begins; needs 14 green" },
    { "wave": 8, "tasks": ["16"], "note": "serving+tables; needs 15" },
    { "wave": 9, "tasks": ["17", "19"], "note": "17 seeds readings; 19 advisor->http; both need 16" },
    { "wave": 10, "tasks": ["18", "20"], "note": "18 association needs 16,17; 20 chatbot needs 15,19" },
    { "wave": 11, "tasks": ["21"], "note": "demo; needs 15-20" },
    { "wave": 12, "tasks": ["22"], "note": "teardown; needs 21" }
  ]
}
```


```
Phase A (CDK stacks, offline)   1 ─┐   2 ──▶ 3 ──▶ 5
                                4 ─┴──────────────┘   (4 done; refs tables)
Phase B (chatbot, offline)      6 ──▶ 7 ──▶ 8
Phase C (association, offline)  9 ; 10 (seeding loader)
Phase D (LocalStack, fenced)    (2,3) ──▶ 11 ; (9,10,11) ──▶ 12 ; 11 ──▶ 13
Phase E (offline gate)          (1..13) ──▶ 14
Phase F (real AWS)              15 ──▶ 16 ──▶ {17 seed, 19 advisor} ──▶ {18 assoc, 20 chatbot}
Phase G (demo)                  (15..20) ──▶ 21 ──▶ 22
```

- Phases A, B, C are independent of each other and need only the repository.
- Phase D depends on the table stacks (2, 3), the association handler (9), and
  the seeding loader (10).
- Phase E gates the move to real AWS: the offline suite must be green first.
- Phase F is strictly ordered — Cognito before serving, serving before advisor,
  advisor before chatbot — because each consumes the previous one's outputs
  (pool/client ids, serving URL). It is the only block that needs an account.
- Phase G needs everything in F deployed.

## Notes

- **No new domain logic.** Tasks that look like "implement X" are almost always
  configuration, CDK, or thin glue (chatbot `/login`, the association Lambda
  handler). The diary, profile, association, and Cognito verifier already exist
  and are tested; do not reimplement them.
- **Offline guarantee is a standing gate.** Every Phase A–C and E task must keep
  the unit/property suites passing with no AWS credentials and no network beyond
  localhost, and CDK synthesis must resolve nothing from an account. LocalStack
  work (Phase D) is integration-marked and fenced out of the offline suite.
- **Credential is forwarded, never parsed** on the chatbot→advisor→ingestion path;
  only the ingestion service verifies the JWT (Property 2).
- **Health-data care** is not optional: encryption at rest, TLS in transit,
  least-privilege IAM per Lambda, no health detail or tokens in logs, retention
  enforced, erasure total. These are acceptance criteria, not nice-to-haves.
- **One open decision to pin during execution** (design Open Question 2): which
  token the ingestion `CognitoAuthenticator` validates (id vs access) and the
  exact subject claim used as `user_id`. The readings decision is DECIDED —
  Option 1, full persistence: real `readings` + `sensor-registry` DynamoDB tables,
  seeded with a bounded demo history (Requirement 10, Tasks 3, 10, 17).
- **Deploy target:** account 862307432587, us-east-1; advisor runtime
  `aqmadvisor_aqm_advisor-ws73wzAfQJ`.
