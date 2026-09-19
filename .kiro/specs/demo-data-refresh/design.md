# Design Document

## Overview

`demo-data-refresh` deploys the existing `aqm_ingestion.jobs.seed_readings` job on
an EventBridge schedule so a **current** demo reading always exists during the
hackathon window. It adds no domain logic. It is a near-copy of `AssociationStack`:
a bundled Lambda, an EventBridge `rate()` rule, and IAM scoped to the two tables
the seed job touches.

The seed job is already the right shape for this: it is idempotent, it is a pure
function of an injected `Clock` and per-site RNG, and its "recent tail" is stamped
relative to `clock.now()` at run time. Re-running it on a cadence shorter than the
freshness window therefore keeps a reading inside that window continuously, while
the 60-day daily history re-puts to itself.

## Architecture

```
EventBridge Rule (rate = 2h)
        │  invokes
        ▼
  Refresh Lambda  ──reads/writes──▶  readings table
  (seed_readings)  ──reads/writes──▶  sensor-registry table
```

Both tables are OWNED by the serving stack. This stack imports them by name only
(`Table.from_table_name`) to attach scoped grants — the ARN is built from the
stack's explicit account/region, so synth contacts no account (offline guarantee).

## Components and Interfaces

### 1. Lambda handler — `aqm_ingestion.jobs.seed_lambda_handler.handler`

The seed job has a `python -m` entrypoint (`main`) but NO Lambda handler (unlike
the association job, which has `jobs.lambda_handler.handler`). We add a thin one:

```python
def handler(event, context):
    return {"status": "ok" if seed_readings.main(argv=(), env=os.environ) == 0 else "error"}
```

- Delegates entirely to `seed_readings.main`, which resolves config, builds the
  runtime with a `SystemClock`, and runs one idempotent pass. No new logic.
- The seed is the module default (deterministic history); the event payload is
  ignored (the schedule sends `{}`). A malformed/absent event never fails the run.
- Returns a small status dict; `main` already logs one structured line and handles
  its own startup failures (returns 1, logs), so the handler never raises for an
  operational fault — it reports it.

### 2. CDK stack — `DemoDataRefreshStack` (`infra/aqm_infra/demo_data_refresh_stack.py`)

Mirrors `AssociationStack`:

- **Env:** select the `dynamodb` adapter for the two ports the seed uses and carry
  their table names — `AQM_ADAPTER_READINGS_STORE=dynamodb`,
  `AQM_ADAPTER_SENSOR_REGISTRY_STORE=dynamodb`, `AQM_TABLE_READINGS`,
  `AQM_TABLE_REGISTRY`, `AQM_AWS_REGION`. Interface flags: `PUSH=true`,
  `SERVING=false`, `PULL=false` (same reasoning as the association stack — the
  config loader rejects all-interfaces-off, and PUSH is the honest batch choice
  needing no Cognito and no feed credential).
- **Bundling:** the identical uv/ARM64 `BundlingOptions` block used by the
  association/serving/chatbot stacks (HOME/cache pointed at /tmp).
- **Function:** PYTHON_3_12, ARM_64, `handler="aqm_ingestion.jobs.seed_lambda_handler.handler"`,
  timeout 5 min, memory 1024, one-week log retention.
- **IAM:** import `readings` and `sensor-registry` by name; both get
  `grant_read_write_data` (the seed upserts registry metadata AND writes readings —
  read+write on BOTH, unlike the association job which reads the registry). Never
  `Resource: "*"`.
- **Schedule:** `events.Schedule.rate(Duration.hours(2))` (freshness 3h − 1h margin,
  satisfying Req 1.3). `add_target(LambdaFunction(fn))` emits the invoke permission.
- **Outputs:** `FunctionName`, `ScheduleRuleName`.

### 3. app.py wiring

Instantiate `DemoDataRefreshStack` alongside the association stack, passing the
serving stack's `readings` and `sensor-registry` table names and the explicit
`env` (account/region), exactly as the association stack is wired.

### 4. just recipes

- `deploy-demo-refresh <readings_table> <registry_table>` — `cdk deploy
  --exclusively` the stack (mirrors `deploy-association`). It also runs the seed
  ONCE immediately after deploy so the 60-day history and a current tail exist
  without waiting for the first scheduled fire (Req 1 + history availability).
- `teardown-demo-refresh` — `cdk destroy --exclusively` the stack (Req 3.3). The
  seeded rows are left in place (harmless demo data; the readings retention window
  ages them out).

## Data Models

None new. The seed writes `CalibratedReading` and `SensorMetadataRecord` exactly
as today.

## Cadence and the freshness window (the load-bearing choice)

`AQM_FRESHNESS_HOURS` defaults to 3. The schedule fires every 2h, so the newest
reading is at most ~2h old when serving reads it — comfortably inside 3h even if
one run is slightly late. If an operator lowers the freshness window below the
cadence, the guarantee breaks; the design pins cadence ≤ freshness − margin and
the recipe/README note the coupling. We do NOT widen the freshness window: a real
product property (stale data must not read as current) stays intact, and a judge
inspecting `asOf` sees a genuinely recent timestamp.

## Error Handling

- A per-run store failure surfaces as an adapter exception; `seed_readings.main`
  catches startup faults and returns non-zero with one logged line. The handler
  maps that to an error status. A failed run does not corrupt state (idempotent
  writes) and the next scheduled run self-heals — a single failure cannot open a
  freshness gap larger than one cadence interval, which is inside the window.
- EventBridge retries on Lambda error per its default async policy; harmless
  because the job is idempotent.

## Testing Strategy

- **Handler unit test** (offline, `data-processing`): `handler({}, None)` drives
  `seed_readings` against in-memory stores (via env selecting the memory adapter)
  and returns ok; a forced startup failure returns an error status without raising.
  Reuses the existing in-memory seed test harness.
- **Synth test** (offline, `infra`): synthesising `DemoDataRefreshStack` produces
  exactly one `AWS::Lambda::Function`, one `AWS::Events::Rule` with a 2-hour rate,
  an `AWS::Lambda::Permission` for EventBridge, and IAM statements scoped to the two
  table ARNs (asserted NOT `*`). Resolves nothing from an account — stays in the
  offline suite (dev-environment offline guarantee).
- The existing `seed_readings` tests already cover the seeding behaviour itself;
  this feature adds no domain logic to test.

## Correctness Properties

### Property 1: Freshness continuity

For any instant `t` between the first successful run and Oct 8, the newest seeded
reading's age at `t` is `< cadence + max_run_skew`, which is `< freshness_window`,
so `current` resolves to a reading at every `t`. Guarded operationally by
cadence ≤ freshness − margin; the seed's tail-at-current-hour property is already
unit-tested in `seed_readings`.

**Validates: Requirements 1.1, 1.2, 1.3**

### Property 2: Idempotence

Running the refresh N times writes the same site metadata (UNCHANGED after the
first) and the same historical readings (Dedup_Key resolves to self); only the
advancing hourly tail differs, so store size is bounded by the fixed history
length, not by N. Inherited from `seed_readings`; re-asserted by the handler test
running twice.

**Validates: Requirements 2.1, 2.2**

### Property 3: Offline synth

Synthesising the stack reads no live account: tables are imported by name and env
is explicit, so the synth test is credential-free.

**Validates: Requirements 3.2**

### Property 4: Least privilege

The Lambda role's DynamoDB actions resolve only to the two table ARNs (and their
indexes), never `*`. Asserted in the synth test.

**Validates: Requirements 3.1**
