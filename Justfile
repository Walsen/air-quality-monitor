# Air Quality Monitor — command surface.
# Every command a contributor or CI stage runs is a recipe here, so there is
# one documented way to run each thing. Run inside the devbox environment
# (direnv activates it on `cd`, or use `devbox run -- just <recipe>`).

# The container client is invoked through a variable so a per-machine
# substitution (podman for docker) needs no edit here.
docker := env_var_or_default("DOCKER", "docker")

sim_dir := "sensor-simulator"

# List available recipes.
default:
    @just --list

# Run the sensor-simulator test suite (Hypothesis ci profile, >=100 examples).
# Excludes the integration marker so the suite passes offline with no
# credentials and no network beyond localhost.
test:
    cd {{sim_dir}} && uv run pytest -m "not integration"

# Run the integration checks that need a container engine or local broker.
test-integration:
    cd {{sim_dir}} && uv run pytest -m integration

# Lint with ruff.
lint:
    cd {{sim_dir}} && uv run ruff check .

# Auto-fix lint findings and format.
fmt:
    cd {{sim_dir}} && uv run ruff check --fix . && uv run ruff format .

# Static type check with mypy.
typecheck:
    cd {{sim_dir}} && uv run mypy

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
