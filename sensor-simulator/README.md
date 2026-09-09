# Sensor Simulator Service

A deterministic virtual air-quality sensor swarm. It emits Breathe-London-shaped
PM2.5, NO2, and meteorology records over MQTT (push) and an authenticated REST
API (pull), so Service 2's ingestion path can be developed and tested without the
live feed.

The same seed replays byte-identically, and Backfill_Mode output equals real-time
output for the same simulated interval — those two guarantees are enforced by
property tests, not just intended.

## Prerequisites

`devbox` and `direnv` are host prerequisites; everything else is pinned in
`devbox.json`. On `cd` into the repository, direnv activates the environment
(`direnv allow` once to trust it). Without direnv, prefix commands with
`devbox run --`.

A container engine is a host prerequisite too, needed only for `just up` and the
integration checks.

## Commands

Every command is a `just` recipe, so CI and a contributor run the same code path.

| Command | What it does |
|---------|--------------|
| `just test` | The offline suite (Hypothesis ci profile, ≥100 examples). No credentials, no network beyond localhost. |
| `just test-integration` | The checks needing a container engine or a local broker. |
| `just lint` | ruff. |
| `just fmt` | Auto-fix lint findings and format. |
| `just typecheck` | mypy, strict. |
| `just run` | Run in real-time mode. |
| `just backfill START END` | Generate history without waiting on wall-clock time. |
| `just gen-certs` | Generate development X.509 material for the configured swarm. |
| `just up` / `just down` | Start / tear down the local Compose stack. |

### Real-time mode

The REST interface is the default, and it needs an API key at run time:

```bash
export AQM_API_KEY="a-development-api-key-of-16-plus-chars"
just run
# then, in another shell:
curl -s localhost:8000/health
curl -s -H "X-API-KEY: $AQM_API_KEY" localhost:8000/ListSensors
```

### Backfill mode

Backfill generates every Publish_Interval in `[START, END)` as fast as it can,
without waiting for wall-clock time:

```bash
export AQM_API_KEY="a-development-api-key-of-16-plus-chars"
just backfill 2026-07-01T00:00:00Z 2026-07-02T00:00:00Z
```

For the same seed and configuration, the records for a given interval are
identical in both modes.

## Configuration

Every value resolves in the order **environment variable → configuration file →
documented default**. With no configuration file supplied, environment variables
and defaults alone are sufficient — no error.

| Environment variable | Default | Meaning |
|----------------------|---------|---------|
| `AQM_SWARM_SIZE` | `50` | Number of Virtual_Sensors. |
| `AQM_PROFILE` | `cochabamba` | Geography_Profile name. |
| `AQM_TICK_MINUTES` | `1` | Tick interval in minutes. |
| `AQM_PUBLISH_MINUTES` | `60` | Publish_Interval; must be an integer multiple of the tick, from 1 minute to 24 hours. |
| `AQM_SEED` | *(drawn and disclosed)* | Run seed. When unset one is drawn and logged so the run can be replayed. |
| `AQM_TIME_MODE` | `realtime` | `realtime` or `backfill`. |
| `AQM_INTERFACE` | `rest` | `rest`, `mqtt`, or `both`. Only the selected interface is activated. |
| `AQM_RETENTION_DAYS` | `30` | Retention window in simulated days, 1–365. |
| `AQM_LOG_LEVEL` | `info` | `debug`, `info`, `warn`, or `error`. |
| `AQM_API_KEY` | *(required for `rest`)* | REST API key, 16–256 characters. Supplied at run time only. |
| `AQM_LISTEN_HOST` | `127.0.0.1` | REST listen address. The image sets `0.0.0.0` so the port is reachable. |
| `AQM_LISTEN_PORT` | `8000` | REST listen port. |
| `AQM_MQTT_HOST` | *(none)* | Broker host, for the `mqtt` interface. |
| `AQM_MQTT_PORT` | `8883` | Broker port, 1–65535. |
| `AQM_MQTT_CA_PATH` | `certs/ca.crt` | Broker CA used to validate the chain. |
| `AQM_MQTT_CERT_TEMPLATE` | `certs/{SiteCode}/client.crt` | Per-sensor certificate path template. |
| `AQM_MQTT_KEY_TEMPLATE` | `certs/{SiteCode}/client.key` | Per-sensor private key path template. |

Secrets are never read from source or from a committed file. Supply
`AQM_API_KEY` and the X.509 paths at run time; a git-ignored `.env.local` is
convenient for local work.

## Scenarios

Scenarios are selected by name from a registry, so adding one is a new strategy
entry rather than another branch in the tick loop.

| Name | Effect |
|------|--------|
| `clean` | Baseline; no modification. |
| `pollution_episode` | City-wide PM2.5 elevation ramping in over the window. |
| `rush_hour_no2` | NO2 elevation at roadside sites during rush-hour windows. |
| `wildfire_smoke` | Large multiplicative PM2.5 excursion after an onset delay. |
| `sensor_fault` | A targeted fault (noise, drift, stuck value, or dropout) on named sensors. |

## Geography profiles

Two profiles ship built in. A profile changes values only — never the field names
a record carries — so any profile is usable wherever a profile is expected.

| Field | `cochabamba` | `reference` |
|-------|--------------|-------------|
| Bounding box (lat) | −17.50 … −17.29 | 51.28 … 51.69 |
| Bounding box (lon) | −66.40 … −66.02 | −0.51 … 0.33 |
| Timezone | `America/La_Paz` | `UTC` |
| Elevation (m) | 2560 | 35 |
| `SiteCode` prefix | `CB` | `RF` |
| Sponsor | Kanata Air Quality Network | Reference Network |
| Sensor contract | `Cellular-BO` | `Cellular-REF` |
| Temperature (°C) | 5 … 30 | −5 … 32 |
| Relative humidity (%) | 15 … 90 | 30 … 95 |
| Pressure (hPa) | 730 … 755 | 980 … 1040 |
| Seasonal PM multiplier | 2.0 | 1.2 |
| Sub-areas | Cochabamba, Sacaba, Quillacollo, Tiquipaya, Colcapirhua, Vinto, Sipe Sipe | Central, North, South, East, West |

### Declaring another profile

Supply a profile in the configuration file under `profile_overrides`, giving every
field of the built-ins above. It is validated on registration — the bounding box
must be non-empty, every sub-area must fall inside it, and ranges must be ordered:

```yaml
profile_overrides:
  name: my_city
  lat_min: -1.40
  lat_max: -1.20
  lon_min: 36.75
  lon_max: 36.95
  timezone: Africa/Nairobi
  sub_areas: ["Central", "Westlands"]
  elevation_m: 1795.0
  site_code_prefix: NB
  sponsor_name: My Network
  sensor_contract: Cellular-KE
  temp_min_c: 10.0
  temp_max_c: 28.0
  rh_min_pct: 25.0
  rh_max_pct: 85.0
  pressure_min_hpa: 800.0
  pressure_max_hpa: 830.0
  seasonal_pm_multiplier: 1.5
```

Then run with `AQM_PROFILE=my_city`.

## The record contract

`/SensorData` and every MQTT message carry exactly these nine fields, in this
order. One message holds exactly one record.

| Field | Type | Notes |
|-------|------|-------|
| `Species` | string | `PM25`, `NO2`, `PM25Index`, or `NO2Index`. |
| `Source` | string | `Measurement`. |
| `Units` | string | `ug.m-3` for concentrations, `index` for index species. |
| `SiteCode` | string | Profile prefix plus a zero-padded four-digit number. |
| `DateTime` | string | ISO-8601 UTC, whole seconds, ending in `Z`. Start of the interval. |
| `Duration` | string | ISO-8601 duration matching the Publish_Interval, e.g. `PT1H`. |
| `ScaledValue` | number | Interval mean, two decimal places, half away from zero. |
| `RatificationStatus` | string | `P` provisional or `R` ratified. |
| `SensorContract` | string | The profile's contract label. |

Each concentration record is paired with exactly one index record sharing its
`SiteCode`, `DateTime`, `Duration`, and `RatificationStatus`. A dropout omits
both.

`/ListSensors` returns the twenty-field Sensor_Metadata_Record, whose `Location`
coordinates are character-identical to its own `Latitude` and `Longitude`.

## REST API

`/health` is unauthenticated and reports swarm size and the current simulated
timestamp. `/ListSensors` and `/SensorData` require an `X-API-KEY` header whose
full value matches exactly, compared case-sensitively; a missing, empty, or wrong
key is `401` **before** any query parameter is validated. A 200 response is
always a top-level JSON array.

Note the two endpoints differ deliberately: `/ListSensors` matches its filters
case-**insensitively**, while `/SensorData` matches case-**sensitively**.

Bad input always returns `400` with a JSON body naming the offending parameter —
never a `500`, and never a stack trace.

## Local stack

```bash
just gen-certs                 # development X.509 into the git-ignored certs/
export AQM_API_KEY="a-development-api-key-of-16-plus-chars"
just up
```

The stack is the simulator plus a local Mosquitto broker. It starts with no AWS
credentials and publishes ports on loopback only. The broker's TLS listener
requires a client certificate signed by the generated development CA, so a sensor
without valid material is refused rather than silently accepted.

The generated CA, certificates, and keys are development-only and git-ignored.
Never commit key material or an API key.
