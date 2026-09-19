# Implementation Plan

## Overview

Deploy the existing `seed_readings` job on an EventBridge schedule so a current demo
reading always exists through Oct 8. No domain logic changes: a thin Lambda handler,
a CDK stack mirroring `AssociationStack`, app wiring, and `just` recipes. TDD
throughout; both offline suites (data-processing, infra) must stay credential-free.

## Task Dependency Graph

```
1 (seed Lambda handler)
        │
        ▼
2 (DemoDataRefreshStack) ──▶ 3 (synth test)
        │
        ▼
4 (app.py wiring)
        │
        ▼
5 (just recipes)
        │
        ▼
6 (deploy, verify, document)
```

- Task 1 is independent (data-processing).
- Task 2 depends on 1 (references the handler string).
- Task 3 depends on 2 (tests the stack).
- Task 4 depends on 2. Task 5 depends on 4. Task 6 depends on 3, 4, 5.

```json
{
  "waves": [
    { "wave": 1, "tasks": ["1"] },
    { "wave": 2, "tasks": ["2"] },
    { "wave": 3, "tasks": ["3", "4"] },
    { "wave": 4, "tasks": ["5"] },
    { "wave": 5, "tasks": ["6"] }
  ],
  "dependencies": {
    "1": [],
    "2": ["1"],
    "3": ["2"],
    "4": ["2"],
    "5": ["4"],
    "6": ["3", "5"]
  }
}
```

## Tasks

- [ ] 1. Add a Lambda handler for the seed job
  - Create `data-processing/src/aqm_ingestion/jobs/seed_lambda_handler.py` with a
    `handler(event, context)` that delegates to `seed_readings.main(argv=(),
    env=os.environ)` and returns a small status dict (ok/error), never raising for
    an operational fault.
  - Write the failing test first: `handler({}, None)` against in-memory stores
    (env selecting the memory adapter) returns ok and writes readings; a forced
    startup failure returns an error status without raising; running the handler
    twice is idempotent (same site metadata, advancing tail only).
  - _Requirements: 1.1, 2.1, 2.2_

- [ ] 2. Add the `DemoDataRefreshStack`
  - Create `infra/aqm_infra/demo_data_refresh_stack.py` mirroring
    `association_stack.py`: bundled ARM64 Lambda (handler
    `aqm_ingestion.jobs.seed_lambda_handler.handler`), env selecting the dynamodb
    adapter for readings + sensor-registry and carrying both table names plus
    PUSH-only interface flags, `grant_read_write_data` on BOTH imported tables, and
    an EventBridge `Schedule.rate(Duration.hours(2))` target.
  - _Requirements: 1.1, 1.3, 3.1, 3.2, 3.4_

- [ ] 3. Write the offline synth test
  - Write the failing test first in `infra/tests/`: synthesising the stack yields
    one Lambda, one Events::Rule at a 2h rate, a Lambda::Permission for
    EventBridge, and DynamoDB IAM statements whose Resource is the two table ARNs
    (assert NOT `*`). Resolves nothing from an account.
  - _Requirements: 1.3, 3.1, 3.2_

- [ ] 4. Wire the stack into `app.py`
  - Instantiate `DemoDataRefreshStack` with the serving stack's readings +
    sensor-registry table names and the explicit env, alongside the association
    stack.
  - _Requirements: 1.1, 3.2_

- [ ] 5. Add `just` recipes
  - `deploy-demo-refresh <readings_table> <registry_table>`: `cdk deploy
    --exclusively` the stack, then run the seed once immediately so the 60-day
    history and a current tail exist without waiting for the first fire.
  - `teardown-demo-refresh`: `cdk destroy --exclusively` the stack.
  - _Requirements: 1.1, 3.3_

- [ ] 6. Deploy, verify, document
  - Run lint + typecheck + full offline suites for both `data-processing` and
    `infra`; deploy the stack; confirm a current reading resolves for demo-user-c
    (advisor returns live numbers, not "unavailable"); note the cadence↔freshness
    coupling and teardown step in DEPLOY.md.
  - _Requirements: 1.1, 1.2, 2.1, 3.3_

## Notes

- The cadence (2h) must stay ≤ freshness window (`AQM_FRESHNESS_HOURS`, default 3h)
  minus a safety margin, or a gap can read as "unavailable". This coupling is the
  one thing an operator must not break; it is called out in the design and DEPLOY.md.
- The stack is a demo-window artefact. `teardown-demo-refresh` removes it after
  the hackathon; seeded rows age out via the readings retention window.
- No new domain logic — the seed behaviour itself is already covered by the
  existing `seed_readings` tests.
