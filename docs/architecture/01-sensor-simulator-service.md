# Service 1 — Sensor Simulator Service

**Goal:** emulate a single sensor, or a *swarm*, modelled on the Breathe London / Airly network, so
Service 2 and the AI agent can be developed and demoed without physical hardware. The simulator must
produce **realistic, schema-accurate** data indistinguishable (in shape) from the real feed.

---

## 1. Anatomy of the real sensor (what we are emulating)

Breathe London's current unit is an **Airly** sensor (earlier generations used **Clarity Node‑S** —
same "sensing‑as‑a‑service" concept). Key facts (from the Breathe London site + Airly + Clarity):

| Attribute | Value |
|-----------|-------|
| **Measurands** | PM2.5, NO2, temperature, humidity, pressure |
| **PM sensing method** | Optical **laser light-scattering** particle counter (counts & sizes particles; mass is *estimated* via calibration, not measured directly) |
| **NO2 sensing method** | **Electrochemical** gas cell |
| **Native sample rate** | **1 minute** |
| **Published resolution** | **1 hour** (1‑min readings averaged) |
| **Connectivity** | **Cellular** — Vodafone Global SIM built in (→ MQTT/HTTPS to cloud) |
| **Power** | Mains **or** solar panel |
| **Certification** | PM: MCERTS "Indicative Ambient Particulate"; NO2: Class 1 Indicative under CEN TS 17660‑1 |
| **Calibration/QA** | Co-location near reference-grade monitors + remote calibration; QA by CERC (Cambridge) |
| **Enclosure** | Weatherproof, pole/lamppost-mounted; ~2–3 m above ground, metres from kerb |
| **Cost (real)** | ~£2,095 install year, £995/yr thereafter (context only) |

### Physical → logical model for the simulator
Each virtual sensor is a small state machine that every tick produces:
- a **PM2.5** value (µg/m³) — optical, humidity-sensitive (hygroscopic growth inflates raw readings at high RH — a real artifact we should model, per research SQ5),
- an **NO2** value (µg/m³) — electrochemical, traffic-correlated, with temperature cross-sensitivity and slow drift,
- **temperature / humidity / pressure**,
- housekeeping: battery/solar state (if solar), signal quality, fault flags.

## 2. The data contract to reproduce (REAL Breathe London API)

The real API (`https://…gateway.dev`, `X-API-KEY` header, OGL v3 licensed) has **two calls**. The
simulator's REST mode must reproduce these exactly; the MQTT mode publishes the `/SensorData` object
shape as the message payload.

### `/ListSensors` → sensor metadata (array of):
```json
{
  "SiteCode": "BL0057",
  "SiteName": "St Annes RC Primary School",
  "DeviceCode": "13546",
  "InstallationCode": "120004",
  "Facility": "St Annes RC Primary School",
  "Location": { "type": "Feature",
    "geometry": { "type": "Point", "coordinates": ["51.4856300", "-0.1191570"] } },
  "Latitude": "51.4856300", "Longitude": "-0.1191570",
  "Borough": "Lambeth",
  "SiteClassification": "Urban Background",
  "SensorHeightAboveGround": 2.9,
  "DistanceToKerb": 14.6,
  "SponsorName": "GLA",
  "SiteLocationType": "School",
  "StartDate": "2025-04-09T00:00:00Z", "EndDate": null,
  "PowerTag": "Mains",
  "SiteDescription": null,
  "SitePhotoURL": "https://www.airquality.london/image/120004.jpg",
  "SensorContract": "Vodafone"
}
```

### `/SensorData` → measurements (array of):
```json
{
  "Species": "PM25",          // one of: NO2 | PM25 | NO2Index | PM25Index
  "Source": "Measurement",
  "Units": "ug.m-3",
  "SiteCode": "BL0086",
  "DateTime": "2025-04-01T00:00:00Z",
  "Duration": "PT1H",
  "ScaledValue": 5.55,
  "RatificationStatus": "P",  // P = provisional (pre-ratification)
  "SensorContract": "Vodafone"
}
```
Query params to support (both calls): `SiteCode`, `Borough`, `Sponsor`, `Facility`,
`Latitude`/`Longitude`/`RadiusKM`, plus `/SensorData` adds `Species`, `startTime`, `endTime`
(default with no time params = **latest hour**).

> **Contract fidelity is the point:** if we reproduce this exactly, Service 2 (and the agent) can be
> pointed at the *real* Breathe London feed later with zero code change — the simulator becomes a
> drop-in stand-in for offline dev, load tests, and fault-injection demos.

## 3. Simulation requirements

### 3.1 Signal realism (the hard part)
Generate plausible time-series rather than random noise:
- **Diurnal patterns:** NO2 shows morning/evening **rush-hour peaks** (traffic); flat overnight. Scale by `SiteClassification` (Roadside ≫ Urban Background ≫ Suburban).
- **PM2.5:** slow-moving regional baseline + episodic spikes (e.g. still/cold days, bonfire/wildfire scenario), weak diurnal component.
- **Meteorology coupling:** temperature diurnal cycle; **RH inversely tracks temp**; model **RH-driven PM overestimation** so Service 2's calibration step has something to correct.
- **Realistic ranges** (ground in research thresholds): PM2.5 typically ~3–35 µg/m³ urban, spikes to 100+; NO2 ~10–90 µg/m³ roadside. WHO 24‑h refs: PM2.5 15, NO2 25 µg/m³ → useful for triggering "unhealthy" demo scenarios.
- **Sensor artifacts:** Gaussian noise, slow **drift**, occasional **dropouts / null hours**, and `RatificationStatus` mostly `P`.
- **Index species:** also emit `NO2Index` / `PM25Index` (the BL "index" values) derived from the raw concentration.

### 3.2 Swarm behavior
- Configurable **N** virtual sensors, each with a stable identity (`SiteCode`, `DeviceCode`, lat/lon, `Borough`, `SiteClassification`, `PowerTag`).
- Geographic spread across a bounding box (default: London boroughs; parameterizable to any city for our own deployment).
- **Spatial correlation:** nearby sensors should see correlated pollution (a shared regional field + local road offset) — not independent noise. A simple approach: one city-wide latent field + per-site local modifiers.

### 3.3 Scenario injection (for demos & agent testing)
Toggleable scenarios so we can drive the agent's advice logic:
- **Pollution episode** (city-wide PM2.5 ramp to "Unhealthy for Sensitive Groups", AQI 100+).
- **Rush-hour NO2 spike** at roadside sites.
- **Wildfire/smoke** (sharp PM2.5, RH-independent).
- **Sensor fault** (stuck value, drift, dropout) — to test Service 2 data-quality flags.
- **Clean day** (baseline).

### 3.4 Control & config
- Config file / env: sensor count, geo box, cadence (1‑min native, hourly publish), scenario schedule, random seed (**deterministic replay** for reproducible demos).
- Time modes: **real-time** (1 msg/sensor/min) and **fast-forward / backfill** (generate a historical range quickly to seed Timestream).

## 4. Interfaces (two modes)
| Mode | Transport | Use |
|------|-----------|-----|
| **Push (primary)** | MQTT → AWS IoT Core (topic e.g. `aqm/sensors/{SiteCode}/data`), payload = `/SensorData` object | realistic device behavior; drives Service 2 ingestion |
| **Pull (secondary)** | REST endpoints `/ListSensors` + `/SensorData` with `X-API-KEY`, BL-compatible | mirrors the real BL API; lets Service 2 use its "poller" path |

Each virtual sensor authenticates to IoT Core with **its own X.509 certificate** (per-device
identity + revocation), matching real fleet-provisioning practice.

## 5. Tech & deployment
- **Language:** Python (rich scientific/time-series libs) or Node/TypeScript — either is fine; pick to match the team.
- **Packaging:** single container image; one process can spawn the whole swarm (async tasks) for demo, or one task per sensor for realism.
- **Local dev:** `docker compose` (simulator + a local MQTT broker / IoT Core endpoint).
- **Cloud:** **ECS Fargate** task (no servers to manage) or a scheduled Lambda for hourly pull-mode; IoT Core Device SDK for MQTT.
- **Provisioning:** IoT Core fleet provisioning / just-in-time registration for the certs.

## 6. Requirements checklist (acceptance)
- [ ] Reproduces `/ListSensors` + `/SensorData` JSON exactly (field names, `Species` enum, `PT1H`, `RatificationStatus`).
- [ ] Push (MQTT) and pull (REST) modes both emit the same canonical record.
- [ ] Diurnal + spatial-correlated + RH-coupled signals, not white noise.
- [ ] Scenario injection (episode / rush-hour / wildfire / fault / clean).
- [ ] Deterministic seed for replay; backfill mode to seed historical data.
- [ ] N-sensor swarm with stable per-site identity + per-device certs.
- [ ] Configurable geography (London default, any city for our deployment).

## Provenance
[Breathe London — Join the network](https://www.breathelondon.org/sensors/join-the-breathe-london-network) ·
[Breathe London Developers/API](https://www.breathelondon.org/developers) ·
[Airly Breathe London package](https://airly.org/en/solutions/breathe-london-package/) ·
[Clarity Node‑S product](https://www.clarity.io/products/clarity-node-s) ·
[Clarity QA / calibration](https://help.clarity.io/knowledge/clarity-data-quality-assurance-process).
Signal-realism thresholds cross-referenced with [`../research/FINDINGS.md`](../research/FINDINGS.md) (SQ0/SQ1/SQ5).
