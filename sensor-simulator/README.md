# Sensor Simulator Service

Service 1 of the Air Quality Monitor monorepo. Emits Breathe-London-shaped
air-quality sensor data (PM2.5 / NO2 / meteorology) over MQTT and a
reference-contract-compatible REST API, so Services 2 and 3 can be built and
demonstrated without physical hardware.

Default geography is the Cochabamba metropolitan area, Bolivia (the Kanata
region); geography is a swappable `Geography_Profile`, not a contract concern.

See the spec under `.kiro/specs/sensor-simulator-service/` for the full
requirements, design, and task plan.

## Development environment

The toolchain is declared in the repo-root `devbox.json` and pinned in
`devbox.lock`. Enter it with direnv (automatic on `cd`) or `devbox shell`, then
use the documented `just` recipes. Python dependencies are owned by `uv`
(`pyproject.toml` + `uv.lock`); the interpreter is provided by devbox.

## Commands

All recipes run from the repo root, inside the devbox environment:

| Command | What it does |
|---|---|
| `just test` | Run the suite (Hypothesis `ci` profile, ≥100 examples), excluding integration checks — passes offline. |
| `just test-integration` | Run checks needing a container engine or local broker. |
| `just lint` | Lint with ruff. |
| `just fmt` | Auto-fix and format. |
| `just typecheck` | Static type check with mypy (strict). |
| `just up` / `just down` | Start / tear down the local Compose stack (simulator + MQTT broker). |

Run commands for real-time and Backfill modes, every configuration value and
default, the scenario and profile catalogues, and the reproduced contract
fields are documented as the service is implemented (task 20.5).

## Layout

```
sensor-simulator/
  src/aqm_simulator/   # source (contract, signal, scenarios, geography,
                       #  swarm, pipeline, time, rng, interfaces, config,
                       #  observability)
  tests/{unit,properties,integration}/
  config/
```

No source module imports a test module. No API key, X.509 certificate, or
private key is ever committed — credentials load from runtime-supplied paths
or environment values.
