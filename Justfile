# Air Quality Monitor — command surface.
# Every command a contributor or CI stage runs is a recipe here, so there is
# one documented way to run each thing. Run inside the devbox environment
# (direnv activates it on `cd`, or use `devbox run -- just <recipe>`).

# The container client is invoked through a variable so a per-machine
# substitution (podman for docker) needs no edit here.
docker := env_var_or_default("DOCKER", "docker")

sim_dir := "sensor-simulator"
ing_dir := "data-processing"
adv_dir := "agent-advisor"

# List available recipes.
default:
    @just --list

# --- Whole-monorepo gates -------------------------------------------------
# These aggregate every service, so one command covers the repository. Each
# service also has its own recipe below, which is the single documented command
# for that service.

# Run every service's offline suite (Hypothesis ci profile, >=100 examples).
# Excludes the integration marker so the suites pass offline with no
# credentials and no network beyond localhost.
test: test-simulator test-ingestion test-advisor

# Run every service's integration checks (container engine or local broker).
# FIXED at task 20.2. This note used to say the aggregate could not pass, because
# `test-integration-advisor` selected ZERO tests and pytest exits 5 on an empty selection. The
# advisor's first `integration`-marked test now exists: the scrubbed-environment run in
# `tests/unit/test_offline_guarantee.py`, which re-invokes the offline suite with every AWS
# variable removed and the SDK config files pointed at nonexistent paths.
#
# It carries the marker for a LOAD-BEARING reason rather than because it is slow: it runs the
# suite as a subprocess with `-m "not integration"`, so an unmarked version would collect itself
# and recurse. Requirement 26.6's fence is what makes it terminate.
#
# The earlier note expected this from task 19.3 (live model, live guardrail). Those checks are
# still to come and task 19 remains open; what changed is that the recipe now selects something,
# so the leg exits zero instead of 5.
test-integration: test-integration-simulator test-integration-ingestion test-integration-advisor

# Lint every service with ruff.
lint: lint-simulator lint-ingestion lint-advisor

# Static type check every service with mypy.
typecheck: typecheck-simulator typecheck-ingestion typecheck-advisor

# Auto-fix lint findings and format every service.
fmt: fmt-simulator fmt-ingestion fmt-advisor

# Run ONE Exposure_Association derivation cycle and exit (Requirement 32.12).
# A one-shot process, not a loop: the schedule belongs outside this code (cron, an
# EventBridge rule, a CronJob), and keeping the derivation in its own invocation is
# what keeps a whole-history read off the per-request serving path. Idempotent, so a
# scheduler delivering twice is harmless. Pass user ids to re-derive a subset.
run-association *users:
    cd {{ing_dir}} && uv run python -m aqm_ingestion.jobs.entrypoint {{users}}

# --- AI Advisor Agent (Service 3) ----------------------------------------

# The single documented test command for Service 3 (Requirement 26.4).
# Excludes the integration marker, which fences anything needing a live model,
# a live guardrail, or a container engine (Requirement 35.8).
test-advisor:
    cd {{adv_dir}} && uv run pytest -m "not integration"

# The shared port-contract suites (Requirement 26.8): one suite per port, run against every
# adapter of that port. Part of the offline suite too — this recipe just runs them alone, which is
# what you want while adding an adapter, since a new adapter's first duty is to pass these.
test-contracts-advisor:
    cd {{adv_dir}} && uv run pytest -m contract

# The fenced checks: a live model or guardrail, or a container engine.
test-integration-advisor:
    cd {{adv_dir}} && uv run pytest -m integration

# The nightly property profile: every property at 1000 examples.
test-advisor-nightly:
    cd {{adv_dir}} && AQM_HYPOTHESIS_PROFILE=nightly uv run pytest -m "not integration"

lint-advisor:
    cd {{adv_dir}} && uv run ruff check .

fmt-advisor:
    cd {{adv_dir}} && uv run ruff check --fix . && uv run ruff format .

typecheck-advisor:
    cd {{adv_dir}} && uv run mypy

# Serve the agent locally. app.run() answers POST /invocations and GET /ping on
# :8080 with NO AWS involvement, which is what lets the offline suite assert the
# AgentCore deployment contract (Requirement 26.5a).
run-advisor:
    cd {{adv_dir}} && uv run python -m aqm_advisor.agentcore.app

# --- Sensor Simulator (Service 1) ----------------------------------------

test-simulator:
    cd {{sim_dir}} && uv run pytest -m "not integration"

test-integration-simulator:
    cd {{sim_dir}} && uv run pytest -m integration

lint-simulator:
    cd {{sim_dir}} && uv run ruff check .

fmt-simulator:
    cd {{sim_dir}} && uv run ruff check --fix . && uv run ruff format .

typecheck-simulator:
    cd {{sim_dir}} && uv run mypy

# --- Ingestion & Serving (Service 2) -------------------------------------

# The single documented test command for this service (Requirement 28.4):
# exits zero only if every test passes.
test-ingestion:
    cd {{ing_dir}} && uv run pytest -m "not integration"

# The container-fenced checks, kept free of any cloud dependency (Req 28.6).
test-integration-ingestion:
    cd {{ing_dir}} && uv run pytest -m integration

lint-ingestion:
    cd {{ing_dir}} && uv run ruff check .

fmt-ingestion:
    cd {{ing_dir}} && uv run ruff check --fix . && uv run ruff format .

typecheck-ingestion:
    cd {{ing_dir}} && uv run mypy

# Run the serving API locally (Requirement 28.7 local-run).
run-ingestion:
    cd {{ing_dir}} && uv run python -m aqm_ingestion.cli

# Start the ingestion local stack: the service, a local MQTT broker, and a
# local store standing in for DynamoDB and S3 (Requirement 28.8).
up-ingestion:
    {{docker}} compose -f {{ing_dir}}/docker-compose.yml up --build

# Tear down the ingestion local stack.
down-ingestion:
    {{docker}} compose -f {{ing_dir}}/docker-compose.yml down -v

# --- Simulator run commands ---------------------------------------------

# Run the simulator in real-time mode (REST interface by default).
# AQM_API_KEY must be supplied at runtime; never commit it.
run:
    cd {{sim_dir}} && uv run python -m aqm_simulator.cli

# Generate historical records without waiting on wall-clock time.
# Example: just backfill 2026-07-01T00:00:00Z 2026-07-02T00:00:00Z
backfill start end:
    cd {{sim_dir}} && AQM_TIME_MODE=backfill AQM_BACKFILL_START={{start}} \
        AQM_BACKFILL_END={{end}} uv run python -m aqm_simulator.cli

# Generate development X.509 material for every sensor in the configured swarm.
# All generated material is git-ignored and must never be committed.
gen-certs:
    cd {{sim_dir}} && uv run python scripts/gen_dev_certs.py

# Start the local Compose stack (simulator + local MQTT broker).
up:
    {{docker}} compose up --build

# Tear down the local Compose stack.
down:
    {{docker}} compose down -v

# Synthesize CDK templates (deployment spec; placeholder until infra/ exists).
synth:
    @echo "infra/ not yet scaffolded — see .kiro/specs/simulator-deployment"

# Synthesize the advisor's AgentCore CDK stack — OFFLINE: no account, no Docker daemon.
# DockerImageAsset builds at deploy, not synth, so this contacts nothing.
synth-advisor-infra:
    cd agent-advisor/infra && uv run pytest tests/ -c pyproject.toml -q
    cd agent-advisor/infra && npx cdk synth --no-lookups > /dev/null && echo "synth OK"
