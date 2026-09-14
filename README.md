# Air Quality Monitor

An AI air-quality assistant for people with respiratory conditions. It ingests
low-cost air-quality sensor data, calibrates it, computes an air-quality index,
and serves **per-user** views to an AI advisor that turns a question like *"is it
safe to run outside today?"* into grounded, non-diagnostic guidance — and
remembers the user's symptom diary so its advice improves over time.

> **Not medical advice.** This system produces general air-quality guidance, not
> a diagnosis, prescription, or dosing instruction. Every advisory response
> carries a non-diagnostic disclaimer.

## What it does

- **Simulates a sensor swarm** shaped like a real municipal low-cost sensor
  network (PM2.5, NO2, meteorology), so the whole pipeline can be built and demoed
  without physical hardware. The output is deterministic — the same seed replays
  byte-for-byte, and backfilled history equals real-time output.
- **Ingests, calibrates, and indexes** those readings — humidity-aware
  calibration, unit conversion, per-species sub-indices, a NowCast, and the
  overall AQI with its driving pollutant.
- **Serves per-user views** over a Cognito-authenticated API: geo-filtered to the
  user's locations, weighted by their respiratory condition, and honoring their
  personal thresholds.
- **Advises** through a language model (Amazon Bedrock) that retrieves the user's
  conditions before it answers, escalates anything that reads as an emergency
  *before* the model is consulted, and verifies every answer — quantitative claims
  must be values it actually retrieved, and diagnoses/dosing are rejected. It
  **stays in scope**: questions outside air quality and its bearing on breathing
  (write me code, tell me a joke, politics) are declined in one line that redirects
  to what it is for.
- **Remembers a personal diary.** A user records daily symptoms; a scheduled job
  correlates that diary against their exposure history to derive personal
  thresholds that shape future advice. Each user's data is strictly isolated.
- **Talks to users through a web chatbot** — a browser sign-in + chat UI in front
  of the advisor.

## Architecture

Three core services (plus a web front-end and the infrastructure code), each
owning its own manifest, tests, and container. They share one contract — the
sensor-data schema — so the simulator could be swapped for a live public feed
without changing the ingestion service.

```
  ┌──────────────────────┐   MQTT (push) / REST (pull)   ┌───────────────────────────┐
  │  Sensor Simulator    │ ────────────────────────────▶ │  Ingestion & Serving       │
  │  (sensor-simulator/) │   Breathe-London-shaped        │  (data-processing/)        │
  │  N virtual sensors   │   PM2.5 / NO2 / meteorology    │  validate → calibrate →    │
  └──────────────────────┘                                │  AQI → store → serve       │
                                                          │  DynamoDB + S3, HTTP API   │
                                                          │  gated by Cognito          │
                                                          └────────────┬──────────────┘
                                        per-user JSON (tool call,       │
                                        carrying the user's JWT)        ▼
  ┌──────────────────────┐   Cognito JWT   ┌───────────────────────────────────────────┐
  │  Web Chatbot         │ ──────────────▶ │  AI Advisor Agent (agent-advisor/)         │
  │  (web-chatbot/)      │   sign in +     │  on Amazon Bedrock AgentCore Runtime       │
  │  browser UI          │   chat turn     │  retrieve → generate → verify → escalate   │
  └──────────────────────┘                 └───────────────────────────────────────────┘
```

| Directory | Service | Role |
|-----------|---------|------|
| [`sensor-simulator/`](sensor-simulator/README.md) | Sensor Simulator | Emits Breathe-London-shaped sensor data over MQTT (push) and an authenticated REST API (pull). Deterministic, replayable, with a backfill mode. |
| [`data-processing/`](data-processing/README.md) | Ingestion & Serving | Ingests, validates, deduplicates, calibrates, computes AQI, stores, and serves per-user personalized views over a Cognito-gated HTTP API. Also runs the scheduled diary→threshold association job. |
| [`agent-advisor/`](agent-advisor/README.md) | AI Advisor Agent | Consumes the serving API and turns an utterance into a verified, non-diagnostic `Advisory_Response`. Deploys to Amazon Bedrock AgentCore Runtime. |
| `web-chatbot/` | Web Chatbot | A browser sign-in + chat UI that proxies to the advisor, gated by a shared key and per-user Cognito login. |
| [`infra/`](infra/) | Infrastructure (CDK) | AWS CDK (Python) stacks for the deployable pieces: Cognito, the serving API + DynamoDB tables, and the scheduled association Lambda. |

Design principles carried through every service:

- **Hexagonal / ports-and-adapters.** Every I/O boundary — the clock, the random
  stream, the MQTT transport, the model, the stores — is a port with a local
  adapter, so the whole test suite runs with **no cloud account and no network
  beyond localhost**.
- **Determinism.** No `datetime.now()` or module-level `random` in domain code;
  time and randomness are injected, which is what makes the simulator replayable
  and the tests reproducible.
- **Safety by construction.** The advisor's red-flag escalation is deterministic
  and runs before the model; its output is verified (grounding, forbidden claims,
  medication closure, guardrail) before it can leave the process.

For the deployed AWS footprint — the services, their relationships, the JWT
identity path, and least-privilege IAM — see the cloud deployment diagram in
[`docs/architecture/03-cloud-deployment.md`](docs/architecture/03-cloud-deployment.md).
The full intended system — the live IoT Core ingestion path, an InfluxDB readings store (Amazon Timestream for InfluxDB), per-device identity, and everything the POC deferred — is designed in [`docs/architecture/04-target-architecture.md`](docs/architecture/04-target-architecture.md).
Deeper background lives in [`docs/architecture/`](docs/architecture/) (the
original investigation and per-service design) and [`docs/research/`](docs/research/).
Each service README documents its own commands, configuration, and contracts.

## Features

- **End-to-end air-quality pipeline** — sensor swarm → ingestion → calibration →
  AQI → per-user serving.
- **Per-user personalization** — geo-filtering to the user's locations,
  condition-weighted interpretation, and personal thresholds.
- **AI advisor with enforced safety** — retrieval-grounded answers, deterministic
  emergency escalation, topic scoping (off-topic requests are declined), and a
  negative/adversarial test suite that pins what the agent must *not* say.
- **Personal diary memory** — a per-user symptom diary that a scheduled
  association job turns into learned thresholds influencing future advice, with
  strict per-user isolation and total erasure. See the feature's deployment guide:
  [`.kiro/specs/personal-diary-memory/DEPLOY.md`](.kiro/specs/personal-diary-memory/DEPLOY.md).
- **Web chatbot** — Cognito sign-in and a chat UI in front of the advisor; the
  user's JWT is forwarded through the advisor to the serving API so one identity
  is validated end to end.
- **Cost/latency optimizations, config-gated** — Bedrock prompt caching over the
  stable prompt-and-tools prefix (`AQM_ADVISOR_MODEL_PROMPT_CACHING`), and an
  explicit sequential tool executor (`AQM_ADVISOR_TOOLS_CONCURRENT`) chosen because
  the retrieval tools have an ordering dependency and share per-turn state. Both are
  off by default so the offline test suite never depends on them. See
  [`agent-advisor/README.md`](agent-advisor/README.md) for the full settings.

## Tech stack

- **Language:** Python 3.12 across all services.
- **HTTP / contracts:** FastAPI with Pydantic models (validation lives in the
  model, not the handler).
- **Testing:** `pytest` for example-based tests, `hypothesis` for property-based
  tests (correctness properties: contract round-trip, seeded determinism,
  monotonic timestamps, spatial decay, mode equivalence, and more).
- **AI:** Amazon Bedrock (Claude via a cross-region inference profile) on Bedrock
  AgentCore Runtime.
- **AWS (deployed pieces):** Lambda, DynamoDB, Cognito, API Gateway HTTP API, S3,
  EventBridge (the association schedule), and Bedrock AgentCore.
- **Infrastructure as code:** AWS CDK in Python.
- **Local runtime:** Docker Compose (a service plus a local MQTT broker and store
  emulation).

## Development environment

The whole toolchain is declarative and pinned, so two machines and CI resolve the
same versions from files in the repo.

- **`devbox`** manages the non-Python toolchain, pinned in `devbox.json`
  (`python`, `uv`, `just`, `jq`, `yq`, the docker client, `docker-compose`,
  `aws-cdk-cli`, `awscli2`, `git`, `gh`).
- **`direnv`** activates that environment on `cd` (run `direnv allow` once).
  Without it, prefix commands with `devbox run --`.
- **`uv`** owns Python dependencies and the per-service virtual environments;
  each service commits its own `uv.lock`.
- **`just`** is the command surface — every command a contributor or CI runs is a
  recipe, so both take the same code path.

`devbox` and `direnv` are host prerequisites (they cannot install themselves); a
container **engine** is a host prerequisite too (only the client is pinned).
Everything else is pinned in the repo.

### Quick start

```bash
# Enter the environment (direnv does this automatically on cd).
devbox shell

# Run the offline test suites — no AWS credentials, no network beyond localhost.
just test            # every service's offline suite
just lint            # ruff, every service
just typecheck       # mypy --strict, every service

# Run a service locally.
just run             # the sensor simulator (real-time)
just up-ingestion    # the ingestion stack: service + MQTT broker + store emulation

# Synthesize the CDK infrastructure offline (resolves nothing from an account).
just synth
```

Run `just --list` for the full command surface (per-service test/lint/typecheck
recipes, the LocalStack round-trips, and the deploy/seed/teardown recipes).

## Testing (for hackathon judges)

The project is **live and free to use** for judging. It is a browser chat app in
front of the AI advisor; you sign in, then ask about air quality and your
exposure in natural language.

**Live demo — Web Chatbot:** https://5g0wmcmn7k.execute-api.us-east-1.amazonaws.com

**Demo credentials** (throwaway accounts provisioned for judging; rotated after
the Judging Period):

| Username | Password |
|----------|----------|
| `demo-user-a` | `AgentsForHumans!2026` |
| `demo-user-b` | `AgentsForHumans!2026` |

> These are sandbox demo logins for a public, non-sensitive demo — not real user
> accounts. `demo-user-b` is a second identity you can use to confirm that one
> user never sees another's data.

### Walkthrough — sign in and try it

1. **Open** the live demo URL above. You'll see the *Air Quality Advisor* chat
   page with its "Not medical advice" disclaimer.
2. **Sign in** with `demo-user-a` and the password above. The browser exchanges
   the credentials for a Cognito token and holds it for the session; every chat
   turn is authenticated with it.
3. **Ask about current air quality** — e.g. *"What's the air quality where I am
   right now?"* The advisor calls its `air_quality` tool and answers with the
   current AQI and the driving pollutant for your saved location, grounded in
   values it actually retrieved.
4. **Ask for a trend** — e.g. *"How has PM2.5 been over the last 3 days?"* This
   exercises the `history` tool over a day-window.
5. **Ask for advice** — e.g. *"Is it safe for me to go for a run this
   afternoon?"* The advisor weighs the reading against your profile and answers
   with non-diagnostic guidance. Anything that reads as a medical emergency is
   escalated to seek-help *before* the model is consulted.
6. **Set up your profile** — e.g. *"I have asthma and I use a salbutamol
   inhaler."* The advisor restates what it understood and asks you to confirm
   before it writes anything (`profile_put`); confirmed conditions and
   medications then shape later advice.
7. **Record a symptom** — e.g. *"I was wheezing this morning and it felt worse
   than yesterday."* The advisor restates the inferred diary entry and, on your
   confirmation, records it (`symptom_entry_put`). A scheduled job correlates the
   diary against exposure history to derive personal thresholds over time.
8. **Confirm data isolation (optional)** — sign out, sign in as `demo-user-b`,
   and note that none of `demo-user-a`'s profile or diary is visible. Each user's
   data is strictly isolated.

What the advisor will **not** do, by design: give a diagnosis, prescribe or dose
medication, or state a quantitative figure it did not actually retrieve. Every
response carries a non-diagnostic disclaimer.

### Running the automated tests

Judges who clone the repo can run the full offline suite with no AWS credentials
and no network beyond localhost:

```bash
just test        # every service's offline suite
just lint
just typecheck
just synth        # CDK synthesis, resolves nothing from an account
```

## Testing philosophy

- **Offline by default.** The suite for every service passes with no AWS
  credentials and no network beyond localhost, exercising a local adapter for
  every port. This is enforced, not just intended.
- **Fenced integration checks.** Anything that needs a container engine, a local
  broker, or a live model/guardrail is marked so the offline suite excludes it and
  a separate command runs it.
- **Properties, not just examples.** Each service's correctness properties are
  property-based tests at ≥100 examples; the advisor additionally maintains a
  negative suite asserting what it must never say and trajectory assertions over
  which tools a turn actually called.
- **CDK synth is credential-free.** Template assertions resolve nothing from an
  account, so the infrastructure is testable offline.

## Deployment

The deployable footprint (Cognito, the serving API + DynamoDB tables, the
scheduled association Lambda) is AWS CDK in `infra/`; the advisor deploys to
Bedrock AgentCore via its own CDK app in `agent-advisor/infra/`. The full deploy
and teardown sequence — including the operator prerequisites and the fixes made
during live bring-up — is documented in
[`.kiro/specs/personal-diary-memory/DEPLOY.md`](.kiro/specs/personal-diary-memory/DEPLOY.md).

This is a proof-of-concept deployment. It runs on Lambda + DynamoDB with a seeded
exposure history; it does **not** stand up the IoT Core / InfluxDB ingestion
path sketched in the original architecture investigation — the simulator's push
pipeline runs against a local broker in Docker Compose rather than a cloud
ingress. Wiring the simulator into a live cloud ingestion path remains a separate
initiative.

## Repository layout

```
sensor-simulator/     Service 1 — the virtual sensor swarm
data-processing/      Service 2 — ingestion, calibration, AQI, serving, association job
agent-advisor/        Service 3 — the AI advisor (Bedrock AgentCore)
  agent-advisor/infra/  the advisor's own CDK app (AgentCore runtime)
web-chatbot/          the browser chat UI in front of the advisor
infra/                AWS CDK (Python) — Cognito, serving + tables, association Lambda
docs/                 architecture investigation and research findings
.kiro/specs/          the specs (requirements, design, tasks) each service was built from
Justfile              the command surface
devbox.json           the pinned toolchain
```
