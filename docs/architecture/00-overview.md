# Air Quality Monitor — Services Investigation & Architecture Overview

> Investigation to inform standing up two separable services for the Air Quality Monitor
> solution. Background research (parameters, AQI, health mechanisms, agent design, guardrails)
> lives in [`../research/FINDINGS.md`](../research/FINDINGS.md). This document is the
> **build-facing** design derived from it.

## Problem framing

We are building an **AI Air Quality Monitor agent** that advises people with respiratory illness.
To develop and demo it without owning physical hardware, we need two independent services:

1. **Sensor Simulator Service** — emulates a single sensor or a *swarm* of sensors modelled on
   a public municipal air-quality network built on commercial low-cost sensor units, producing
   realistic PM2.5 / NO2 / meteorology data.
   → [`01-sensor-simulator-service.md`](01-sensor-simulator-service.md)
2. **Ingestion & Serving Service** — collects sensor data, processes/calibrates it, stores it,
   and serves **per-user customized** views to the AI agent over an authenticated API.
   → [`02-ingestion-and-serving-service.md`](02-ingestion-and-serving-service.md)

## System context (C4 level 1)

```
                 ┌─────────────────────────┐
                 │  Sensor Simulator Svc   │   (Service 1 — this repo)
                 │  N virtual AQ sensors   │
                 └───────────┬─────────────┘
                   MQTT (push) │  or REST (pull, reference-compatible)
                             ▼
   ┌───────────────────────────────────────────────────────────┐
   │              Ingestion & Serving Service (Service 2)        │
   │  IoT Core ─▶ Rule ─▶ Lambda(validate/calibrate/AQI) ─▶      │
   │  Timestream (readings) + DynamoDB (meta + user profiles) +  │
   │  S3 (raw archive).  Serving API (HTTP API + Cognito).       │
   └───────────────────────────┬───────────────────────────────┘
                    per-user customized JSON │  (tool call)
                                             ▼
                 ┌─────────────────────────┐
                 │  AI Air Quality Monitor  │   (the agent — separate)
                 │  agent (Amazon Bedrock)  │
                 └─────────────────────────┘
```

The two services share **one contract**: the sensor data schema (the reference network contract —
see Service 1). This lets us swap the simulator for a live public air-quality feed later with no
change to Service 2.

## Why model on a public municipal air-quality network

Public municipal air-quality networks built on commercial low-cost sensor units give us a proven
template. A typical unit measures **PM2.5, NO2, temperature, humidity, pressure** at 1‑minute
resolution, averaged to **1‑hour** published points, over **cellular**, with co-location
calibration and independent QA. Such networks commonly expose a public API (`/ListSensors`,
`/SensorData`). That is an ideal starting point: a small, well-documented pollutant set that maps
directly onto our research (PM2.5 + NO2 are two of the most respiratory-relevant pollutants), plus
a documented JSON contract we can reproduce exactly.

## Key design decisions (carried into both services)

| # | Decision | Rationale (source) |
|---|----------|--------------------|
| D1 | Reproduce the **reference network contract** JSON schema as the canonical schema | lets us swap sim ↔ a live public feed; the contract shape is captured in Service 1 |
| D2 | Support **both push (MQTT→IoT Core)** and **pull (reference-contract-compatible REST)** | push = realistic device behavior; pull = matches the public API shape |
| D3 | **Calibration/correction happens in Service 2**, before AQI/agent inference | uncalibrated low-cost PM2.5 MAE ~17 µg/m³ > WHO limit (research SQ5) |
| D4 | **Per-user customization** = geo-filter + condition-weighting + personal thresholds | research SQ4/SQ7 + condition personalization |
| D5 | Serving API is **authenticated (Cognito) and non-diagnostic** | health data + FDA general-wellness guardrails (research cycle 6) |
| D6 | **Serverless-first** (IoT Core, Lambda, Timestream, DynamoDB, HTTP API) | scales to zero for demo; pay-per-use; low ops |

## Approximate cost summary (us-east-1, USD/month — ORDER OF MAGNITUDE)

> ⚠️ Estimates only, for sizing intuition. Region-dependent and subject to change — validate with the
> [AWS Pricing Calculator](https://calculator.aws) before committing. Excludes the Bedrock agent
> (billed as part of the agent, not these services) and free-tier credits.

| Scenario | Sensors | Cadence | IoT msgs/mo | Rough infra cost |
|----------|---------|---------|-------------|------------------|
| **Demo / workshop** | 20–50 | 1 min (or hourly) | ~2M (1-min) / ~36K (hourly) | **< $10–30** (much on free tier) |
| **Pilot** | 500 | 1 min | ~22M | **~$40–120** |
| **City scale** | 5,000 | 1 min | ~216M | **~$300–700** |

Main cost drivers: IoT Core messaging (~$1/million msgs), Timestream writes+storage, API Gateway
requests, Lambda duration. **Cost lever:** publish 1‑min data but batch or pre-average to hourly to
cut message/write counts ~60×. Detailed per-service breakdowns are in the two service docs.

## Account / model note

The **Bedrock model** powering the agent must run on an account with flagship Anthropic access
(e.g. a Sonnet-capable account). Bedrock model enablement is orthogonal to these two services but is
a prerequisite for the agent that consumes Service 2.

## Recommended build order

1. Service 1 simulator emitting the reference contract schema over MQTT (local first, then Fargate).
2. Service 2 ingestion path (IoT Core → Lambda → Timestream) validated against the simulator.
3. Service 2 serving API + Cognito + per-user customization.
4. Point the Bedrock agent at the serving API as a tool.
5. (Optional) swap in a live public air-quality feed alongside the simulator.

## Provenance

Sensor facts: publicly documented municipal air-quality network APIs and commercial low-cost sensor datasheets, openly licensed for reuse (contract shape captured in Service 1). AWS patterns: [Serverless IoT backend ref-arch](https://github.com/freethinkingit/lambda-refarch-iotbackend), [AWS IoT event-driven architectures](https://aws.amazon.com/fr/blogs/architecture/building-event-driven-architectures-with-iot-sensor-data/). Pricing: [IoT Core](https://aws.amazon.com/iot-core/pricing/), [Timestream](https://aws.amazon.com/timestream/pricing/).
