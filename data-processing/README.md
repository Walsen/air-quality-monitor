# Ingestion & Serving Service

Ingests Breathe-London-shaped sensor records from MQTT or a reference-contract feed, validates and
deduplicates them, applies a humidity-aware calibration, converts units, computes sub-indices and a
NowCast, derives the overall AQI and driving pollutant, stores the result, and serves per-user
personalized views over a Cognito-gated HTTP API.

Python 3.12, hexagonal: nine port protocols with a local adapter for each, so the whole suite runs
with no cloud account and no network beyond localhost.

## Commands

Every command is a `just` recipe, so a contributor and a pipeline take the same code path.

| Command | Does |
|---|---|
| `just test-ingestion` | The offline suite. Exits zero only if every test passes. |
| `just test-integration-ingestion` | The container-fenced checks. Needs a container engine, never a cloud account. |
| `just lint-ingestion` | ruff. |
| `just typecheck-ingestion` | mypy `--strict`. |
| `just run-ingestion` | Run the service locally. |
| `just up-ingestion` / `just down-ingestion` | The local stack: the service, a mosquitto broker, and DynamoDB + S3 emulation. |

Property tests run at 100 examples by default (`ci`); `HYPOTHESIS_PROFILE=nightly` raises that to
1000 and `dev` drops it to 20 for fast local iteration.

## Configuration

Resolved from environment variables, then a file given by `AQM_CONFIG_FILE`, then built-in defaults.
Every value is validated before any listener opens, and every invalid value produces its own message
before the process exits non-zero.

### Interfaces

| Variable | Default | Meaning |
|---|---|---|
| `AQM_ENABLE_PUSH` | `true` | Subscribe to MQTT. |
| `AQM_ENABLE_PULL` | `false` | Poll the reference feed. |
| `AQM_ENABLE_SERVING` | `true` | Serve the HTTP API. |

Only the enabled interfaces are constructed, so a serving-only deployment never opens a subscription.

### Adapter selection

Each port is chosen by name through `AQM_ADAPTER_<PORT>`; the first value listed is the default.

| Port | Names |
|---|---|
| `readings_store` | `memory`, `dynamodb` |
| `sensor_registry_store` | `memory`, `dynamodb` |
| `raw_archive` | `memory`, `s3` |
| `profile_store` | `memory`, `dynamodb` |
| `forecast_client` | `memory`, `http` |
| `meteorology_provider` | `memory`, `http` |
| `authenticator` | `local`, `cognito` |

The two transports are not in this table on purpose: enabling the push interface *is* choosing MQTT,
so a name there would only ever restate the switch. What varies is whether a broker is reachable —
the real transport is used when `AQM_MQTT_HOST` is set and a scripted stand-in otherwise, which is
what lets the local stack start with nothing to connect to.

### Retention and windows

| Variable | Default |
|---|---|
| `AQM_RETENTION_DAYS` | `90` |
| `AQM_QUARANTINE_RETENTION_DAYS` | `30` |
| `AQM_AUDIT_RETENTION_DAYS` | `365` |
| `AQM_NOWCAST_WINDOW_HOURS` | `12` |
| `AQM_MAX_HISTORY_SPAN_DAYS` | `30` |
| `AQM_RATE_LIMIT_PER_MINUTE` | `60` |

Retention is applied at query time, so data ages out with the injected clock and no timer.

### Credentials

Never committed, and never read by the configuration loader — it takes a predicate and asks only
whether a credential *resolves*, so a loader that cannot see a secret cannot log one.

| Variable | Used by |
|---|---|
| `AQM_FEED_API_KEY` or `AQM_FEED_CREDENTIAL_PATH` | The feed's `X-API-KEY` header. |
| `AQM_FORECAST_API_KEY` or `AQM_FORECAST_CREDENTIAL_PATH` | The forecast and meteorology providers. |
| `AQM_MQTT_CA_CERT`, `AQM_MQTT_CLIENT_CERT`, `AQM_MQTT_CLIENT_KEY` | Mutual TLS to the broker. Paths only; contents are read at connect time. |
| `AQM_COGNITO_USER_POOL_ID`, `AQM_COGNITO_CLIENT_ID`, `AQM_COGNITO_ISSUER` | The JWT verifier. |
| `AQM_LOCAL_CREDENTIALS` | `token=user` pairs for the `local` authenticator. Empty by default, so it accepts nothing. |

## Routes

All are gated by a Cognito JWT except `/health`, and every one is scoped to the calling identity.

| Route | Returns |
|---|---|
| `GET /health` | Liveness. Exempt from the gate and the rate limit. |
| `GET /v1/air-quality/me` | The personalized current view, with its basis and guardrail envelope. |
| `GET /v1/air-quality/history` | Readings over a bounded window; defaults to the maximum span ending now. |
| `GET`/`PUT`/`DELETE /v1/profile/me` | The user profile. `DELETE` de-identifies rather than dropping, preserving counts. |

A missing or non-matching token is `401`, a `userId` naming another user is `403`, a bad parameter is
`400` naming the parameter. Malformed input never produces a `500`.

## What the design is careful about

- **Determinism.** No domain module reads the wall clock or imports `random`; both are enforced by an
  AST check over the real source. Two service instances built from one configuration and one clock
  instant produce byte-identical responses and identical stored readings.
- **Health-adjacent data is minimized.** The profile is an allowlist that *rejects* rather than
  ignores unknown fields, locations are rounded on the way in, and the model refuses to render its
  own contents so a stray f-string cannot leak a condition. The audit trail holds no clinical field
  at all, which is what makes erasure tractable.
- **One shared behavioural suite per port**, run against every adapter of that port, so an adapter
  swap cannot change domain behaviour. The cloud half skips offline and runs in the fenced job.
- **Forty correctness properties**, each implemented in exactly one module and tagged
  `Feature: ingestion-and-serving-service, Property {n}`. A test asserts that every property the
  design names has an implementation, so adding one to the spec fails the suite until it is written.

## Layout

```
src/aqm_ingestion/
  contract/     record models, serializer, parser — an independent copy, never imported across services
  domain/       pure rules: validation, dedup, calibration, conversion, AQI, quality, faults, profile
  ports/        the nine protocols, the Clock, and the shared archive-key derivation
  adapters/     memory (local), dynamodb, s3, mqtt, http_feed, forecast, auth
  ingest/       archive-first pipeline and the two entry points
  serving/      geo selection, enrichment, auth gate, response assembly, guardrails, audit
  config/       the fail-fast loader
  composition.py  the one place adapters are chosen and ports are injected
```
