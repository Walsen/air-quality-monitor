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
chat_dir := "web-chatbot"
infra_dir := "infra"

# --- CDK synth/deploy plumbing (facts baked in for THIS environment) ------
# Two host quirks make every recipe that runs `cdk` (synth, deploy, destroy, or
# the infra template-assertion tests, which bundle Lambda code at synth) need a
# small preamble. Both are folded into `cdk_env` below so no recipe repeats them.
#
# 1. node is NOT on the devbox PATH. jsii (the CDK Python runtime) shells out to
#    `node`, and the devbox env does not export one to `uv run` subprocesses. The
#    pinned `aws-cdk-cli` package DOES bring a node in its closure: the `cdk`
#    binary on PATH is a node script whose shebang names that exact interpreter.
#    So we derive node's dir from the shebang of the resolved `cdk` binary rather
#    than hardcoding a /nix/store hash — it tracks whatever node the pinned CLI
#    uses, and needs no edit on an upgrade. If node is already on PATH (a future
#    devbox may export it) the prepend is harmless.
# 2. CDK stages ~76M of Lambda assets per synth under $TMPDIR (default /tmp) and
#    never cleans them. On this host /tmp is a 7.8G tmpfs that a full infra run
#    overflows (ENOSPC); the root disk under /home has ~75G. So TMPDIR is pointed
#    at a repo-root `.cdk-tmp` (git-ignored) and each recipe clears its staging
#    afterwards. Never commit anything under .cdk-tmp.
#
# `cdk_env` resolves node's dir and exports TMPDIR; recipes prefix their cdk/uv
# invocation with it. `cdk_clean` removes the staging this run left behind.
cdk_tmp := justfile_directory() / ".cdk-tmp"
cdk_env := 'mkdir -p "' + cdk_tmp + '"; export TMPDIR="' + cdk_tmp + '"; ' + \
    'NODE_BIN="$(dirname "$(sed -n "1s/^#!//p" "$(readlink -f "$(command -v cdk)")" | cut -d" " -f1)")"; ' + \
    'export PATH="$NODE_BIN:$PATH";'
cdk_clean := 'rm -rf "' + cdk_tmp + '"/cdk.out* "' + cdk_tmp + '"/tmp* 2>/dev/null || true'

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
#
# personal-diary-memory Task 14 note: the new offline checks are ALREADY here,
# no new leg needed. The chatbot `/login` + JWT pass-through tests (Tasks 6-8)
# live in `test-chatbot`; the association Lambda handler tests (Task 9) and the
# readings/registry seeding-loader tests (Task 10) live in `test-ingestion`.
# The CDK template assertions are deliberately NOT here: they bundle Lambda code
# via Docker at synth, so they are fenced into `test-integration` as `test-infra`.
test: test-simulator test-ingestion test-advisor test-chatbot

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
#
# `test-infra` joins this fenced aggregate (personal-diary-memory Task 14): the
# CDK template assertions resolve nothing from an account, but bundle Lambda code
# via Docker at synth, so they need a container engine and belong here, NOT in the
# always-on offline `test`.
test-integration: test-integration-simulator test-integration-ingestion test-integration-advisor test-infra

# Lint every service with ruff. infra is pure (no Docker), so it is always-on.
lint: lint-simulator lint-ingestion lint-advisor lint-chatbot lint-infra

# Static type check every service with mypy. infra is pure, so it is always-on.
typecheck: typecheck-simulator typecheck-ingestion typecheck-advisor typecheck-chatbot typecheck-infra

# Auto-fix lint findings and format every service.
fmt: fmt-simulator fmt-ingestion fmt-advisor fmt-chatbot fmt-infra

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

# --- Web Chatbot ---------------------------------------------------------
# A browser chat UI in front of the advisor. Its tests use a fake advisor client
# and a tiny in-process ASGI caller, so the suite needs no AWS and no network.

test-chatbot:
    cd {{chat_dir}} && uv run pytest

lint-chatbot:
    cd {{chat_dir}} && uv run ruff check .

fmt-chatbot:
    cd {{chat_dir}} && uv run ruff check --fix . && uv run ruff format .

typecheck-chatbot:
    cd {{chat_dir}} && uv run mypy

# Run the chatbot locally against the live advisor runtime (needs AWS creds).
# The access key and runtime ARN come from the environment.
run-chatbot:
    cd {{chat_dir}} && uv run python -m aqm_chatbot.app

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

# --- infra/ CDK stacks (personal-diary-memory) ---------------------------
# The deployment stacks and their template-assertion tests. Lint and typecheck
# are pure Python — always-on, folded into the top-level aggregates. The test
# recipe and everything that runs `cdk` are fenced or deploy-time (see below).

# Template-assertion tests (FENCED — needs a container engine, not an account).
# The assertions synthesize each stack and check the emitted CloudFormation. They
# pass an explicit dummy env (account 111122223333) so synthesis RESOLVES NOTHING
# FROM AN ACCOUNT — they run with no credentials. But the serving/association
# stacks bundle their Lambda code with BundlingOptions(image=...), which runs
# DOCKER during synth, so this needs an engine. That is why it lives in
# `test-integration`, fenced out of the offline `test` (Requirement 8.6/8.4).
test-infra:
    {{cdk_env}} cd {{infra_dir}} && uv run pytest
    @{{cdk_clean}}

# Pure checks (no Docker, no account): always-on in the lint/typecheck aggregates.
lint-infra:
    cd {{infra_dir}} && uv run ruff check .

typecheck-infra:
    cd {{infra_dir}} && uv run mypy

fmt-infra:
    cd {{infra_dir}} && uv run ruff check --fix . && uv run ruff format .

# Synthesize the full app OFFLINE, resolving NOTHING from an account (Property 7,
# Requirement 8.4). Supersedes the old placeholder. Runs `cdk synth --no-lookups`
# under a SCRUBBED environment — every AWS variable unset and the SDK config files
# pointed at nonexistent paths — with the diary stacks enabled via placeholder
# context. If it still succeeds, synthesis proves it needs no account.
#
# It DOES need a container engine (the serving/association Lambdas bundle at
# synth) and TMPDIR on the root disk — same fencing as `test-infra`. It stays
# credential-free: the placeholder context values are not secrets and nothing here
# reads a real key.
synth:
    {{cdk_env}} cd {{infra_dir}} && \
        env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
            -u AWS_PROFILE -u AWS_DEFAULT_REGION -u AWS_REGION \
            -u CDK_DEFAULT_ACCOUNT -u CDK_DEFAULT_REGION \
            AWS_CONFIG_FILE=/nonexistent/config \
            AWS_SHARED_CREDENTIALS_FILE=/nonexistent/credentials \
        uv run cdk synth --no-lookups \
            -c deploy_ingestion=true -c deploy_diary_memory=true \
            -c simulator_api_key=placeholder -c ingestion_api_key=placeholder \
            -c chatbot_access_key=placeholder \
            -c cognito_user_pool_id=us-east-1_PLACEHOLD \
            -c cognito_client_id=placeholderclientid \
            -c cognito_issuer=https://cognito-idp.us-east-1.amazonaws.com/us-east-1_PLACEHOLD \
            > /dev/null && echo "synth OK — resolved nothing from an account"
    @{{cdk_clean}}

# --- Personal Diary Memory: LocalStack round-trips (FENCED) ---------------

# The diary/association/erasure round-trips over the DynamoDB + S3 adapters
# (personal-diary-memory Tasks 11-13). Integration-marked, so they skip cleanly
# unless the store emulator is up — run `just up-ingestion` first to start it.
# Fenced out of the offline `test`; part of the container/emulator suite.
test-integration-diary:
    cd {{ing_dir}} && uv run pytest tests/integration/test_local_stack.py -m integration

# --- Personal Diary Memory: deploy + operational recipes (need the account) ---
# Deploy-time recipes for Phases F/G. They DO need AWS credentials and the target
# account (862307432587, us-east-1). Secrets and identifiers are recipe ARGUMENTS
# or environment values, NEVER literals here (§7). Each runs `cdk`, so it carries
# the node/TMPDIR preamble and cleans its staging. Deploy ordering mirrors
# infra/app.py: Cognito -> serving -> association.

# Deploy the Cognito user pool (Task 15). Its outputs (pool id, client id, issuer)
# feed the serving + chatbot deploys. `--exclusively` so only this stack moves.
deploy-cognito:
    {{cdk_env}} cd {{infra_dir}} && \
        uv run cdk deploy --exclusively aqm-poc-cognito \
            -c deploy_diary_memory=true --require-approval never
    @{{cdk_clean}}

# Create a demo user in the pool (Task 15). Password is set PERMANENT so sign-in
# needs no challenge. NEVER commit credentials — pass the pool id, username, and
# password as arguments (or via a git-ignored .env.local / the secret store).
create-demo-user user_pool_id username password:
    aws cognito-idp admin-create-user \
        --user-pool-id {{user_pool_id}} --username {{username}} \
        --message-action SUPPRESS
    aws cognito-idp admin-set-user-password \
        --user-pool-id {{user_pool_id}} --username {{username}} \
        --password {{password}} --permanent

# Deploy the serving stack backed by DynamoDB + the Cognito authenticator
# (Task 16). Pass the pool id / client id / issuer captured from `deploy-cognito`.
deploy-serving pool_id client_id issuer:
    {{cdk_env}} cd {{infra_dir}} && \
        uv run cdk deploy --exclusively aqm-poc-serving \
            -c deploy_diary_memory=true \
            -c cognito_user_pool_id={{pool_id}} \
            -c cognito_client_id={{client_id}} \
            -c cognito_issuer={{issuer}} \
            --require-approval never
    @{{cdk_clean}}

# Seed the deployed readings + sensor-registry tables (Task 17). Runs the Task 10
# loader against real DynamoDB by selecting the dynamodb adapters and the deployed
# table names through the config the loader reads. Idempotent (a second run is a
# no-op). Env var names CONFIRMED against data-processing's composition root:
#   - AQM_ADAPTER_READINGS_STORE / AQM_ADAPTER_SENSOR_REGISTRY_STORE = dynamodb
#     (loader `_REGISTERED_ADAPTERS`, selected via AQM_ADAPTER_<NAME>).
#   - AQM_TABLE_READINGS / AQM_TABLE_REGISTRY name the tables (composition
#     `_table(suffix)` reads AQM_TABLE_<SUFFIX>; suffixes are `readings`/`registry`).
#   - AQM_AWS_REGION is required by the loader path; AWS_REGION is what boto3 itself
#     resolves for `boto3.resource("dynamodb")` (the adapters pass no region), so
#     both are set. No AQM_AWS_ENDPOINT_URL -> the real service, not an emulator.
# Table names are the deployed CDK-generated names; pass them as arguments.
seed-readings readings_table registry_table:
    cd {{ing_dir}} && \
        AQM_ADAPTER_READINGS_STORE=dynamodb \
        AQM_ADAPTER_SENSOR_REGISTRY_STORE=dynamodb \
        AQM_TABLE_READINGS={{readings_table}} \
        AQM_TABLE_REGISTRY={{registry_table}} \
        AQM_AWS_REGION=us-east-1 AWS_REGION=us-east-1 \
        uv run python -m aqm_ingestion.jobs.seed_readings

# Deploy the association Lambda + EventBridge schedule (Task 18). It imports the
# serving stack's table names cross-stack, so serving must be deployed first.
deploy-association:
    {{cdk_env}} cd {{infra_dir}} && \
        uv run cdk deploy --exclusively aqm-poc-association \
            -c deploy_diary_memory=true --require-approval never
    @{{cdk_clean}}

# Invoke the deployed association function on demand (Task 18 demo). Pass the
# deployed function name (read it from the AssociationStack's output). Writes the
# response body to /dev/stdout so the caller sees the invocation result.
invoke-association function_name:
    aws lambda invoke --function-name {{function_name}} \
        --cli-binary-format raw-in-base64-out /dev/stdout

# Tear down the diary stacks in REVERSE dependency order (Task 22): association
# (imports serving's exports) -> serving -> cognito. This recipe removes ONLY
# those three stacks. The rest of the feature's live footprint is torn down by
# SEPARATE steps, all documented in .kiro/specs/personal-diary-memory/DEPLOY.md:
# the CDK advisor runtime (`just teardown-advisor`), the chatbot stack
# (`cdk destroy --exclusively aqm-poc-chatbot ...`), the demo users
# (`just delete-demo-user`), and the OLD starter-toolkit advisor stack
# `AgentCore-aqmadvisor-default` (not CDK-managed by this repo's infra app). So
# `teardown-diary` alone does NOT remove everything — see the DEPLOY doc.
teardown-diary:
    {{cdk_env}} cd {{infra_dir}} && \
        uv run cdk destroy --exclusively \
            aqm-poc-association aqm-poc-serving aqm-poc-cognito \
            -c deploy_diary_memory=true --force
    @{{cdk_clean}}

# Tear down the CDK advisor runtime (Task 22). The advisor is a SEPARATE cdk app
# under agent-advisor/infra, whose app.py raises SystemExit without a deploy env
# even to synth for a destroy — so we export the same env the deploy used. The
# identifiers (client id, discovery url, serving base url) are recipe ARGUMENTS,
# never literals (§7); the model id defaults to the `us.` inference profile (the
# bare id has no in-region on-demand support and errors at invoke). Carries the
# node/TMPDIR preamble and cleans its staging like the other cdk recipes, but
# runs in the advisor's own infra dir, not {{infra_dir}}.
teardown-advisor client_id discovery_url serving_base_url:
    {{cdk_env}} cd {{adv_dir}}/infra && \
        CDK_DEPLOY_ACCOUNT="${CDK_DEPLOY_ACCOUNT:?set CDK_DEPLOY_ACCOUNT}" \
        CDK_DEPLOY_REGION="${CDK_DEPLOY_REGION:-us-east-1}" \
        AQM_ADVISOR_MODEL_ID="${AQM_ADVISOR_MODEL_ID:-us.anthropic.claude-sonnet-4-6}" \
        AQM_COGNITO_CLIENT_ID={{client_id}} \
        AQM_COGNITO_DISCOVERY_URL={{discovery_url}} \
        AQM_ADVISOR_SERVING_BASE_URL={{serving_base_url}} \
        uv run cdk destroy AqmAdvisorRuntime --force
    @{{cdk_clean}}

# Delete a demo user from the Cognito pool (Task 22). Demo-user cleanup is a
# SEPARATE teardown step from `teardown-diary` — destroying the Cognito stack
# removes the pool, but this is the documented way to remove an individual user
# (e.g. before retaining the pool). Pass the pool id and username as arguments.
delete-demo-user user_pool_id username:
    aws cognito-idp admin-delete-user \
        --user-pool-id {{user_pool_id}} --username {{username}}

# Synthesize the advisor's AgentCore CDK stack — OFFLINE: no account, no Docker daemon.
# DockerImageAsset builds at deploy, not synth, so this contacts nothing.
synth-advisor-infra:
    cd agent-advisor/infra && uv run pytest tests/ -c pyproject.toml -q
    cd agent-advisor/infra && AQM_CDK_SYNTH_PLACEHOLDER=1 npx cdk synth --no-lookups > /dev/null && echo "synth OK"
