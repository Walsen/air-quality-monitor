---
inclusion: always
---

# Engineering practices

These practices apply to all code written or modified in this monorepo. The
examples use the project's actual stack; the principles hold regardless of
language.

## 0. Stack

Air Quality Monitor is a monorepo of three services:

| Directory | Service | Role |
|-----------|---------|------|
| `sensor-simulator/` | Sensor Simulator | Emits Breathe-London-shaped sensor data over MQTT and REST |
| `data-procesing/` | Ingestion & Serving | Ingests, calibrates, computes AQI, stores, and serves per-user views |
| `agent-advisor/` | AI Advisor Agent | Consumes the serving API and generates guidance |

- **Language:** Python 3.12 across all three services.
- **HTTP:** FastAPI. Request and response shapes are Pydantic models —
  contract validation belongs in the model, not in the handler body.
- **Concurrency / MQTT:** `asyncio` for the sensor swarm; MQTT through an
  injected client abstraction so a local broker and AWS IoT Core are
  interchangeable.
- **Testing:** `pytest` for example-based tests, `hypothesis` for
  property-based tests.
- **Logging:** standard-library `logging` emitting single-line JSON to stdout.
- **Dependencies:** pinned to exact versions in each service's own manifest.
- **Local dev:** Docker Compose (simulator plus a local MQTT broker).
- **Cloud (Service 2):** AWS IoT Core, Lambda, Timestream, DynamoDB, S3,
  API Gateway HTTP API, Cognito, Secrets Manager, CloudWatch and X-Ray.
- **IaC:** AWS CDK in Python.
- **Agent (Service 3):** Amazon Bedrock.

Each service owns its own manifest, tests, and container definition. A shared
contract package is not specced yet — until it is, do not import across
service directories. Keep each service's copy of the record contract
independent and covered by its own round-trip tests.

## 1. Clean Code & SOLID

- **Single Responsibility** — the Signal_Engine computes values, the
  Serializer renders JSON, the MQTT_Publisher moves bytes, the REST_API
  answers queries. A function that computes a PM2.5 value and also publishes
  it is doing two jobs; split it.
- **Open/Closed** — new scenarios and new geography profiles slot into their
  registry without editing unrelated branches. Adding a `dust_storm` scenario
  should mean one new strategy entry, not another arm in an if/elif chain
  inside the tick loop.
- **Liskov Substitution** — every Geography_Profile must be usable wherever a
  profile is expected. `london` and `cochabamba` differ in values only, never
  in the field names they cause to be emitted.
- **Interface Segregation** — the Signal_Engine has no use for MQTT settings;
  don't hand it the whole config object. Pass narrow, purpose-built parameter
  objects.
- **Dependency Inversion** — depend on abstractions at every boundary: the
  clock, the random stream, the MQTT transport, the HTTP client for external
  feeds, the time-series store. These are exactly the boundaries that get
  swapped (local broker to IoT Core, simulator to the live Breathe London
  feed), and the ones that make the system testable.
- Favor small, well-named functions over long ones. Extract helpers instead of
  nesting deeply. If logic is copy-pasted, factor it out.
- Naming reveals intent (`derive_index_band`, not `calc`). Comments explain
  *why*; the code should already explain *what*.

## 2. Determinism and time

The simulator must replay byte-identically from a seed, and backfill output
must equal real-time output. That makes the following non-negotiable rather
than stylistic.

- Never call module-level `random` or `numpy.random` functions in domain code.
  Take an explicit generator, seeded from the run seed and the `SiteCode`, so
  each virtual sensor has an independent stream.
- Never call `datetime.now()` inside domain logic. Take the simulated
  timestamp as a parameter or inject a clock. Real-time mode and backfill mode
  should differ only in what drives that clock.
- Iteration over sensors, species, and intervals must have a defined order. No
  set iteration and no reliance on incidental dict ordering anywhere the order
  reaches output.
- Any new source of randomness or time must arrive with a property test
  showing the determinism and mode-equivalence properties still hold.

## 3. Test-Driven Development (TDD)

- Write a failing test **before** the implementation: red → green → refactor.
- For bug fixes, first write a test that reproduces the bug and confirm it
  fails, then fix and confirm it passes.
- Use `pytest` for concrete behaviors and error cases, and `hypothesis` for
  the correctness properties the specs name: contract round-trip, seeded
  determinism, monotonic timestamps, index monotonicity, humidity
  monotonicity, spatial decay, mode equivalence, and observable effect.
- A property test asserts an invariant across generated inputs; an example
  test pins one concrete case. Most behaviors deserve both.
- Test names state the expected behavior
  (`test_serializer_round_trips_every_species`,
  `test_sensordata_rejects_start_time_after_end_time`).
- Every new function, route, and class needs its normal case, edge cases, and
  error cases covered before it counts as done.
- Run the full suite before presenting a change as complete. The suite must
  pass with no AWS credentials and no network access beyond localhost.

## 4. Design patterns — apply when they fit, not by default

- Reach for a pattern only when it solves a real structural problem.
- **Strategy** — scenarios (`clean`, `pollution_episode`, `rush_hour_no2`,
  `wildfire_smoke`, `sensor_fault`) and Geography_Profiles are genuine
  strategies. Keep each independently testable and selected by name from a
  registry, not by a long conditional.
- **Factory** — Virtual_Sensor construction from a Geography_Profile or a
  supplied site list. Centralize identity assignment; don't scatter
  `SiteCode` formatting across call sites.
- **Template Method** — the publish pipeline (tick → average over the
  Publish_Interval → apply artifacts → derive index species → serialize →
  publish) is a fixed sequence with pluggable steps.
- **Adapter** — the MQTT transport, and the external forecast and pollen
  clients.
- Document *why* a pattern was chosen when it isn't obvious. A single
  conditional does not need a pattern.

## 5. Robust error handling

- Never let a raw stack trace reach a client. Catch at the boundaries: FastAPI
  handlers, Lambda handlers, CLI entry points.
- Catch the exception types you expect (`OSError`, `ValueError`,
  `ValidationError`), not bare `except:`. Broad catches belong only at a true
  top-level boundary, and must log there.
- Validate external input at the edge. Every documented failure mode returns
  its documented status with a JSON body naming the offending parameter — 400
  for a bad query parameter, 401 for a missing or non-matching `X-API-KEY`.
  Bad input must never produce a 500.
- The Config_Loader fails fast: validate every value, write one message per
  invalid value, exit non-zero. Never half-start with a partly valid config.
- Isolate per-sensor failures. One Virtual_Sensor raising must not stop the
  swarm — log with its `SiteCode` and carry on with the rest.
- Fail loudly in development, safely in production.

## 6. Robust logging

- Configure a structured logger once at startup, emitting one single-line JSON
  object per event to stdout. Never `print()`. The simulator's contract
  requires that nothing non-JSON reaches stdout.
- Levels: `DEBUG` for developer detail; `INFO` for operational events
  (scenario window opened, publish-interval summary); `WARNING` for
  recoverable issues (value clamped, growth factor capped, buffer trimmed,
  broker reconnect); `ERROR` for handled failures (per-sensor tick failure,
  broker rejection); `CRITICAL` for unrecoverable ones.
- Include context: `SiteCode`, `Species`, the simulated timestamp, the route,
  the scenario name. Use `logger.exception(...)` inside except blocks.
- Never log secrets or personal health data: API keys, X.509 private keys,
  Cognito tokens, or user condition and profile fields.
- Errors handled per section 5 must still be logged. Silent failure is not
  acceptable.
- Configure format and output centrally, not ad hoc per module.

## 7. Security and secrets

- No secret in a committed file — API keys, X.509 certificates, private keys,
  tokens. Load them from environment values or runtime-supplied paths, and
  from Secrets Manager or SSM Parameter Store in AWS.
- Network-exposed endpoints are authenticated. The simulator REST API is gated
  by `X-API-KEY`; the Service 2 serving API is gated by a Cognito JWT. No
  anonymous access to sensor readings or user profiles.
- Least-privilege IAM per Lambda. Per-device X.509 identity on IoT Core so
  individual devices can be revoked.
- Service 2 holds health-adjacent data: minimize what is stored, encrypt at
  rest and in transit, and keep the non-diagnostic disclaimer on every
  response.

## Applying these practices

When implementing a feature or fix:
1. Write the failing test(s) first.
2. Implement the smallest clean, SOLID-compliant change to pass them.
3. Introduce a design pattern only if the change reveals a genuine structural
   need for one.
4. Add explicit error handling for the new code's failure modes.
5. Add logging at the appropriate points and levels.
6. Run the test suite and confirm everything passes before considering the
   work done.
