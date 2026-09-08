# Service 2 — Ingestion, Processing & Serving Service

**Goal:** collect sensor data (from the simulator now, the real Breathe London feed later), process
and calibrate it, store it, and serve **per-user customized** views to the AI Air Quality Monitor
agent over an authenticated API.

---

## 1. Responsibilities
1. **Ingest** sensor readings (push via MQTT, and/or pull via scheduled poll of a BL-compatible API).
2. **Process**: validate, deduplicate, **calibrate/correct** (RH compensation), compute **AQI sub-indices** and the driving pollutant.
3. **Store**: time-series readings, sensor metadata, user profiles, raw archive.
4. **Serve**: authenticated, **per-user customized** JSON to the agent (nearest sensors, condition-weighted pollutants, personal thresholds, optional inhaled-dose).
5. **Enrich**: pull external forecasts (AQI, pollen) for the agent's anticipatory advice.

## 2. AWS services & why

| Concern | Service | Why |
|---------|---------|-----|
| Device connectivity / ingestion | **AWS IoT Core** | Managed MQTT, per-device X.509 auth, Rules Engine to route messages; scales to zero |
| Message routing | **IoT Rules** | SQL-like routing → Lambda / Kinesis / Timestream; no glue servers |
| Stream buffer (high volume) | **Kinesis Data Streams** *(optional, at scale)* | smooths spikes, enables replay; skip for demo |
| Processing / business logic | **AWS Lambda** | validate, calibrate, AQI compute, per-user shaping; pay-per-invoke |
| Scheduled pull (real BL feed) | **EventBridge Scheduler → Lambda** | hourly poll of `/SensorData`; matches BL's hourly cadence |
| Time-series store | **Amazon Timestream (LiveAnalytics)** | purpose-built for sensor time-series; memory tier for recent + magnetic for history; cheap writes, time-window queries |
| Metadata + user profiles | **DynamoDB** | sensor registry, user condition/prefs/thresholds; single-digit-ms lookups for per-request personalization |
| Raw archive / data lake | **Amazon S3** | immutable raw JSON (replay, audit, ML later); lifecycle to Glacier |
| Serving API | **API Gateway (HTTP API)** | cheap, low-latency REST for the agent tool call |
| AuthN/Z | **Amazon Cognito** (+ IAM, optional WAF) | user identity → JWT; scopes per-user data; health data must be protected |
| AI agent runtime | **Amazon Bedrock** *(the agent — separate service)* | consumes this API as a tool |
| Secrets | **Secrets Manager / SSM Parameter Store** | BL API key, forecast API keys |
| Observability | **CloudWatch + X-Ray** | metrics, logs, tracing, data-quality alarms |
| IaC | **AWS CDK** (or Terraform) | reproducible stacks; CDK matches team's prior work |

## 3. Reference architecture

```
   Simulator / real BL sensors
        │ MQTT                         ┌───────────── EventBridge (hourly) ── Lambda (BL poller)
        ▼                              │                                          │ pull /SensorData
  ┌──────────────┐   IoT Rule   ┌──────┴───────┐                                  │
  │  IoT Core    ├─────────────▶│  Ingest      │◀─────────────────────────────────┘
  │ (MQTT broker)│              │  Lambda      │
  └──────────────┘              │  validate/   │
        │ (raw copy)            │  calibrate/  │──▶ Timestream (readings: conc + AQI + quality_flag)
        ▼                       │  AQI compute │──▶ DynamoDB   (sensor registry)
     S3 (raw archive)           └──────────────┘──▶ S3         (processed archive)

  Agent (Bedrock)  ──JWT──▶  API Gateway (HTTP API) ──▶  Serving Lambda ──▶ Timestream + DynamoDB
                                          │                     │  join readings × user profile
                                       Cognito              (+ forecast/pollen enrich)
                                                                 ▼
                                                    per-user customized JSON
```

### Data flow
1. Sensor publishes `/SensorData` object to `aqm/sensors/{SiteCode}/data` (MQTT) **or** poller pulls it hourly.
2. IoT Rule copies raw → S3 and invokes the **Ingest Lambda**.
3. Ingest Lambda: schema-validate → dedup → **RH-aware calibration/correction** → compute **AQI sub-index + driving pollutant** → attach `quality_flag`/confidence → write Timestream + upsert DynamoDB registry.
4. **Serving Lambda** (behind API Gateway + Cognito) answers agent queries with **per-user customized** payloads.

## 4. Per-user customization (the differentiator)

The agent passes the authenticated user (Cognito JWT). The Serving Lambda joins **sensor readings ×
the user's profile** (DynamoDB) to tailor the response:

- **Geo-personalization:** return the **nearest / most relevant sensors** to the user's home, work, and commute (lat/lon + `RadiusKM`, reusing the BL query semantics).
- **Condition weighting:** emphasize the pollutants that matter for the user's condition — **COPD → NO2/O3**, **asthma/allergic → PM2.5 (+ pollen)** (research SQ3/SQ7).
- **Personal thresholds:** compare against the user's learned/set sensitivity level, not just national breakpoints; flag when the user's **"Orange" (AQI 100+)** trigger is crossed (research SQ1).
- **Inhaled dose (optional):** if the user shares activity data, return activity-adjusted exposure, not just ambient concentration (research SQ2).
- **Forecast context:** attach next-day AQI + pollen so the agent can warn *ahead* (research SQ4).

> **Example response shape (agent-facing):**
> ```json
> {
>   "user": "u_123", "generatedAt": "2026-09-08T01:00:00Z",
>   "location": {"lat": 51.51, "lon": -0.13},
>   "nearestSensors": [{"siteCode":"BL0086","distanceKm":0.4,
>       "pm25":18.2,"no2":41.0,"aqi":63,"drivingPollutant":"NO2",
>       "confidence":"calibrated","asOf":"2026-09-08T00:00:00Z"}],
>   "personalized": {"condition":"asthma","sensitivity":"high",
>       "thresholdCrossed":false,"weightedFocus":["PM25"],
>       "pollenToday":"grass:high"},
>   "forecast": {"tomorrowAqi":88,"trend":"rising"},
>   "disclaimer": "Exposure guidance only — not medical advice."
> }
> ```

## 5. Security & compliance (non-negotiable)
- **The serving API is network-exposed and returns personal health-adjacent data → it MUST be authenticated.** Cognito user pools (JWT authorizer on API Gateway); no anonymous access.
- Least-privilege IAM per Lambda; per-device X.509 certs on IoT Core (revocable).
- Encrypt at rest (Timestream/DynamoDB/S3 KMS) and in transit (TLS/mTLS).
- **Health-data handling:** consent, data minimization, region residency; HIPAA/GDPR posture if profiles hold condition data (research cycle 6 guardrails).
- **Non-diagnostic framing enforced at the API** (disclaimer field + no treatment/dosing output) to stay in the FDA *general-wellness* lane.
- Optional **WAF** + API throttling/usage plans; secrets in Secrets Manager.

## 6. Approximate costs (us-east-1, USD/month — ORDER OF MAGNITUDE)

> ⚠️ Estimates for sizing only; validate with the [AWS Pricing Calculator](https://calculator.aws).
> Excludes the Bedrock agent. New accounts get free-tier credits that likely cover the demo entirely.

**Demo/workshop — 50 sensors:**
| Item | Assumption | ~Cost |
|------|-----------|-------|
| IoT Core messaging | hourly publish → ~36K msgs/mo (or ~2.1M at 1‑min) | <$1 (hourly) / ~$2 (1‑min) |
| Lambda | ~ingest + serve invocations, low | ~$0 (free tier) |
| Timestream | ~small writes + memory store + light queries | ~$5–20 |
| DynamoDB (on-demand) | tiny | ~$1–3 |
| API Gateway (HTTP API) | agent queries, low volume | ~$1 |
| S3 | raw archive, GBs | ~$1 |
| Cognito | < 50 MAU | free tier |
| **Total** | | **~$10–30** |

**City scale — 5,000 sensors, 1‑min:** IoT messaging ~216M msgs ≈ ~$216; Timestream writes/storage
dominate next; realistic **~$300–700/mo**. **Lever:** batch/pre-average to hourly before publish to
cut messages+writes ~60× (biggest single cost reduction).

## 7. Requirements checklist (acceptance)
- [ ] Ingest via MQTT (IoT Core → Rule → Lambda) **and** via scheduled BL-API poll.
- [ ] Calibration/correction (RH-aware) applied before AQI + storage; quality/confidence flag persisted.
- [ ] AQI sub-index + driving-pollutant computed per reading.
- [ ] Timestream (readings) + DynamoDB (registry + user profiles) + S3 (raw) wired.
- [ ] Serving API behind Cognito; per-user geo + condition + threshold customization.
- [ ] Forecast/pollen enrichment integrated.
- [ ] Non-diagnostic disclaimer + health-data protections enforced.
- [ ] IaC (CDK/Terraform); CloudWatch/X-Ray observability + data-quality alarms.

## Provenance
AWS patterns: [Serverless IoT backend reference architecture](https://github.com/freethinkingit/lambda-refarch-iotbackend) ·
[Building event-driven architectures with IoT sensor data](https://aws.amazon.com/fr/blogs/architecture/building-event-driven-architectures-with-iot-sensor-data/) ·
[Remote asset health monitoring (IoT Core + SiteWise + Grafana)](https://aws.amazon.com/blogs/iot/empowering-operations-a-scalable-remote-asset-health-monitoring-solution-the-internet-of-things-aws-official-blog).
Pricing: [AWS IoT Core pricing](https://aws.amazon.com/iot-core/pricing/) · [Amazon Timestream pricing](https://aws.amazon.com/timestream/pricing/).
Customization/guardrail logic cross-referenced with [`../research/FINDINGS.md`](../research/FINDINGS.md) (SQ1–SQ7 + cycle 6).
