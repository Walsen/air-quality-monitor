# Requirements Document

## Introduction

The Ingestion & Serving Service (Service 2 of three in this monorepo) is the middle service. It
collects air-quality sensor readings — from the Sensor Simulator Service (Service 1) now, from a live
public air-quality feed later — validates and deduplicates them, **calibrates** them, computes **US
EPA AQI sub-indices** and the driving pollutant, stores them, and serves **per-user customized**,
authenticated, non-diagnostic JSON to the AI Advisor Agent (Service 3).

The service has three load-bearing constraints, and almost every requirement in this document exists
to serve one of them:

1. **Calibration happens before AQI and before anything is served.** Uncalibrated low-cost PM2.5 has a
   mean absolute error of about 17.40 µg/m³ — larger than the WHO 24-hour guideline of 15 µg/m³ —
   falling to about 5.85 µg/m³ with model-based, humidity-aware calibration
   (`docs/research/FINDINGS.md`, SQ5). Relative humidity is the dominant confounder: an optical
   particle counter reads hygroscopically swelled particles as extra mass. Serving an uncorrected value
   would systematically over-warn in humid air and mis-time advice, so correction precedes index
   computation and a confidence indicator travels with every value (design decision D3 in
   `docs/architecture/00-overview.md`).

2. **The serving API is network-exposed, returns health-adjacent personal data, and is therefore
   authenticated and non-diagnostic.** No anonymous access to readings or profiles. Advice framing is
   exposure-reduction only, never diagnosis and never medication dosing — the framing decision is what
   keeps the system in the general-wellness lane rather than the regulated-medical-device lane, and it
   is a product requirement rather than a disclaimer (`docs/research/FINDINGS.md`, cycle 6; decision
   D5).

3. **Contract fidelity is inherited, not re-litigated.** Service 2 consumes the reference network
   contract (`/ListSensors`, `/SensorData`) exactly as Service 1 emits it, so the simulator can be
   swapped for a live public air-quality feed with no code change on this side (decision D1). Because
   engineering practice §0 forbids importing across service directories until a shared contract package
   is specced, Service 2 keeps its **own independent copy** of the record contract, covered by its own
   round-trip tests.

Source material: `docs/architecture/02-ingestion-and-serving-service.md` (primary),
`docs/architecture/00-overview.md` (system context and decisions D1–D6),
`docs/architecture/01-sensor-simulator-service.md` (the contract this service consumes), and
`docs/research/FINDINGS.md` (SQ1 indices and breakpoints, SQ2 output taxonomy, SQ3 pollutant-to-illness
mechanisms and the exposure lag structure, SQ4 agent capabilities, SQ5 calibration and telemetry, SQ7
pollen, cycle 6 guardrails, cycle 7 condition personalization).

### Scope

In scope: the two ingestion paths (push over MQTT and scheduled pull from a reference-contract feed),
record validation and quarantine, deduplication, humidity-aware calibration, unit conversion, AQI
sub-index and NowCast computation, driving-pollutant selection, quality and confidence flagging,
sensor-fault detection, the storage abstractions (readings, sensor registry, raw archive, user
profiles), authentication and per-user authorization, the serving API and its per-user customization
(geographic, condition-weighted, threshold-based, optional inhaled dose), forecast and pollen
enrichment, the non-diagnostic guardrails and audit trail, configuration, observability, and the
service's own project scaffolding inside `data-processing/`.

Out of scope, and deferred to a separate deployment spec: cloud deployment topology and runtime
selection, infrastructure-as-code, AWS IoT Core device provisioning and authorization policies, Cognito
user-pool configuration, API Gateway and WAF configuration, KMS key management, S3 lifecycle
transitions, CloudWatch alarm and X-Ray wiring, and continuous integration. Also out of scope: the
Sensor Simulator Service itself (Service 1, already specced and implemented) and the Bedrock advisor
agent (Service 3), which consumes this service's API as a tool.

This document specifies **application logic**. Every boundary that would otherwise reach a cloud
service is expressed as an injected port with a local fake, so the whole suite runs with no AWS
credentials and no network access beyond localhost. The deployment spec chooses adapters behind those
ports; it does not change the domain.

### Assumptions pending confirmation

These assumptions are recorded so the requirements are complete and testable; each is a decision the
reader should confirm or override.

- **A1 — Language/runtime:** Python 3.12, matching the monorepo stack (engineering practice §0), with
  FastAPI for the serving API and Pydantic models carrying contract validation.

- **A2 — Service directory:** `data-processing/`, with the package at
  `data-processing/src/aqm_ingestion/`. The steering document
  `.kiro/steering/engineering-practices.md` §0 spells this directory `data-procesing/`, missing an `s`.
  No such directory exists yet, so nothing needs renaming; this spec uses the corrected spelling and
  the steering table needs a one-line correction to match. That correction is deliberately **not** made
  by this spec.

- **A3 — Readings store is DynamoDB, not Timestream.** This is a deliberate, documented deviation from
  the service table in `docs/architecture/02-ingestion-and-serving-service.md` §2, which names Amazon
  Timestream (LiveAnalytics) as the time-series store. **AWS closed new-customer access to Timestream
  for LiveAnalytics effective 2025-06-20**; existing customers continue, and AWS directs new workloads
  to Timestream for InfluxDB
  ([availability change notice](https://docs.aws.amazon.com/timestream/latest/developerguide/AmazonTimestreamForLiveAnalytics-availability-change.html)).
  Timestream for InfluxDB is a provisioned instance that does not scale to zero, which contradicts the
  "scales to zero for demo; pay-per-use" rationale behind decision D6. DynamoDB is already required by
  this service for the sensor registry and user profiles, so choosing it for readings removes a service
  from the stack rather than adding one, and the access pattern this service actually needs — a bounded
  recent time window for a small set of nearby sites — is a partition-plus-range query that DynamoDB
  serves directly. All readings access is behind the ReadingsStore port (Requirement 14), so the
  deployment spec may still select Timestream for InfluxDB, an existing Timestream for LiveAnalytics
  table, or another store as an adapter swap with no domain change.

- **A4 — Independent record-contract copy.** Service 2 defines its own `/SensorData` and `/ListSensors`
  models rather than importing Service 1's, because engineering practice §0 forbids cross-service
  imports until a shared contract package is specced. The copy is field-for-field identical to the
  contract Service 1 emits, and Requirement 1 and Requirement 2 state it independently so a drift
  between the two services is a test failure rather than a silent incompatibility.

- **A5 — Breakpoint tables are versioned data, not constants.** The default table identifier is
  `epa-2024-05-06`, reflecting the US EPA AQI revision effective 2024-05-06 that changed the PM2.5
  breakpoints. Tables are loaded as data and selected by identifier so a future revision, or a
  non-US index such as the EU EAQI or the UK DAQI, is a new table rather than a code change. Every
  served AQI value declares the table identifier that produced it.

- **A6 — Authentication adapter deferred.** The domain depends on an Authenticator port that resolves a
  bearer credential to a verified user identity and claims. A local fake with deterministic tokens
  serves the offline suite; the Amazon Cognito JWT adapter, its user-pool configuration, and JWKS
  handling belong to the deployment spec. The security *behavior* — no anonymous access, per-user
  scoping, documented 401 and 403 responses — is specified here and tested here.

- **A7 — Unit conversion is parameterized by temperature and pressure.** The EPA NO2 breakpoints are
  expressed as a volume mixing ratio in ppb, while the record contract carries mass concentration in
  µg/m³. The conversion depends on air temperature and absolute pressure, so it takes both as
  parameters rather than hard-coding the sea-level factor of about 1.88 µg/m³ per ppb. Defaults come
  from the configured site conditions; under Cochabamba's approximately 740 hPa at 2,560 m, using the
  sea-level factor would understate the ppb mixing ratio by roughly 24 percent, which is enough to move
  a reading a whole AQI band.

- **A8 — First milestone target is local-only:** a local MQTT broker, a locally hosted serving API, and
  in-memory or local-container store adapters. The intended cloud split — invocation-scoped functions
  for ingestion and serving, a scheduled invocation for the feed poller — is deferred to the deployment
  spec, which the determinism of Requirement 27 makes safe to defer.

- **A9 — Forecast and pollen provider selection is deferred** behind the ForecastClient port. FINDINGS
  cycle 8 establishes that this service *consumes* external forecasts rather than modelling air quality
  itself, and names candidate providers (AirNow, OpenAQ, IQAir for AQI; a pollen API for aeroallergens)
  without this spec committing to one. Requirement 24 specifies the port's contract, its caching, and
  its graceful degradation; the concrete provider, its credentials, and its rate limits belong to the
  deployment spec.

- **A10 — Relative humidity does not arrive on the wire, so it comes from a port.** This is a genuine
  gap between the two services rather than an oversight, and it is called out here because it changes
  the calibration design. The reference contract's `Species` enumeration carries only `NO2`, `PM25`,
  `NO2Index`, and `PM25Index`; Service 1 models temperature, RH, and pressure internally and records
  them **only in its own diagnostic output**, explicitly excluding them from emitted records (Service 1
  requirements 5.10 and 5.11). Service 2 therefore cannot read RH from an ingested `/SensorData`
  payload as the contract stands. Requirement 8 resolves this with a three-source precedence — a
  configured meteorology channel mapping if a feed does publish met species, then the
  MeteorologyProvider port, then no RH at all with an explicitly degraded confidence — so the service
  is correct today and forward-compatible if either the simulator or a live feed starts publishing met
  channels. The alternative, extending the `Species` enumeration, is deliberately *not* chosen because
  it would break the contract fidelity that decision D1 exists to protect.

- **A11 — Ozone and the other WHO classical pollutants are not measured by this network.** FINDINGS SQ0
  identifies six classical pollutants and SQ3 finds NO2 and O3 the strongest gaseous drivers of COPD
  exacerbation, but the reference contract carries only PM2.5 and NO2. Condition weighting
  (Requirement 21) therefore weights only the species actually available and **declares** in the
  response which weighted species were unavailable, rather than silently dropping them or substituting
  a proxy. Adding O3 later is a new species in the breakpoint table and the weighting map, not a
  redesign.

- **A12 — Symptom logs and learned personal thresholds are configured, not inferred.** FINDINGS SQ4 and
  cycle 7 describe learning a per-user trigger threshold from a symptom and reliever-use log. This spec
  stores and applies an explicitly set Personal_Threshold and leaves the *learning* of one to a later
  spec, because inferring a health threshold from logged symptoms is a materially different problem with
  its own evidentiary and guardrail requirements. Requirement 22 is written so a learned threshold can
  replace a set one without a contract change.

## Glossary

- **Service**: the Ingestion & Serving Service as a whole, comprising the components below.
- **Sensor_Data_Record**: one JSON object matching the `/SensorData` contract — one Species, one site,
  one time interval. Service 2's independent copy of Service 1's record of the same name.
- **Sensor_Metadata_Record**: one JSON object matching the `/ListSensors` contract.
- **Parser**: the component that reads `/SensorData` and `/ListSensors` JSON text into
  Sensor_Data_Record and Sensor_Metadata_Record values, rejecting anything that does not conform.
- **Serializer**: the component that renders record values back to JSON text, used for the raw archive
  and for round-trip verification.
- **Raw_Reading**: an accepted Sensor_Data_Record together with its ingestion metadata — the ingestion
  instant, the transport it arrived on, and the identifier of its archived raw payload — before any
  correction is applied.
- **Reading**: a Raw_Reading or Calibrated_Reading, where the distinction does not matter.
- **Calibrated_Reading**: the internal value produced from a Raw_Reading after calibration, unit
  conversion, index computation, and flagging. It carries the original reported value, the corrected
  value, the Quality_Flag, the Confidence, the Sub_Index, the band, and the identifiers of the
  Breakpoint_Table and Calibration_Strategy that produced them. This is the value that is stored and
  served; it is never rendered as a `/SensorData` record, because it is not part of the contract.
- **Ingest_Pipeline**: the fixed sequence every ingested payload passes through — parse, validate,
  archive, deduplicate, calibrate, convert units, compute index, flag, store — with pluggable steps.
- **Quarantine**: the terminal state of a payload or record that failed validation. A quarantined record
  is retained for diagnosis with its rejection reason, is never stored as a Reading, and is never
  served.
- **Dedup_Key**: the tuple identifying one measurement interval for one site and species —
  `SiteCode`, `Species`, `DateTime`, `Duration` — used to make ingestion idempotent.
- **Calibration_Strategy**: a named, independently testable correction from a reported concentration to
  a corrected concentration, selected by name from a registry. The default is humidity-aware.
- **Calibration_Domain**: the input range over which a Calibration_Strategy is validated — its
  concentration and RH bounds. Applying a strategy outside its Calibration_Domain is permitted but is
  flagged.
- **Quality_Flag**: the categorical assessment attached to every Calibrated_Reading, one of
  `calibrated`, `calibrated_extrapolated`, `uncalibrated`, `suspect_fault`, or `suspect_conflict`.
- **Confidence**: the coarse indicator served alongside every value, one of `high`, `medium`, or `low`,
  derived deterministically from the Quality_Flag and the completeness of the NowCast input window.
- **Breakpoint_Table**: the versioned, data-defined mapping from a pollutant concentration in a stated
  unit to an AQI Sub_Index and band, identified by a table identifier whose default is
  `epa-2024-05-06`.
- **Sub_Index**: the AQI value computed for one pollutant from its concentration by piecewise-linear
  interpolation within a Breakpoint_Table band.
- **Band**: the named AQI category a Sub_Index falls in — `Good`, `Moderate`,
  `Unhealthy for Sensitive Groups`, `Unhealthy`, `Very Unhealthy`, or `Hazardous`.
- **Orange_Band**: the `Unhealthy for Sensitive Groups` band, AQI 101 to 150. The escalation trigger for
  respiratory users, one band earlier than the public `Unhealthy` threshold (FINDINGS SQ1).
- **NowCast**: the US EPA adaptive-weighted average that converts a series of hourly PM2.5 values into
  the current-hour value the 24-hour breakpoints expect, weighting recent hours more heavily as the
  series becomes more variable.
- **NowCast_Window**: the trailing series of hourly values the NowCast is computed over, default 12
  hours.
- **Overall_AQI**: the maximum Sub_Index across the pollutants available for a site and instant.
- **Driving_Pollutant**: the species whose Sub_Index equals the Overall_AQI.
- **Mixing_Ratio**: a gas concentration expressed by volume, in ppb, as the EPA NO2 breakpoints require.
- **Mass_Concentration**: a concentration expressed by mass per volume, in µg/m³, as the record
  contract carries.
- **Fault_Detector**: the component that flags a site and species as `suspect_fault` on a stuck value,
  a dropout, or drift.
- **Retention_Window**: the maximum age of a Reading the ReadingsStore retains and serves, default 90
  days.
- **Sensor_Registry**: the store of the current Sensor_Metadata_Record for every known site, supporting
  lookup by `SiteCode` and nearest-N geographic query.
- **Raw_Archive**: the immutable, append-only store of every ingested payload exactly as received,
  written before processing so that any Reading can be replayed or audited.
- **User_Profile**: the stored per-user record holding the Condition, the Sensitivity_Level, the
  Personal_Threshold values, the named User_Location entries, the optional activity inputs, and the
  Consent_Record. It deliberately holds no free-text clinical narrative.
- **Condition**: the user's respiratory condition category, one of `asthma`, `copd`,
  `allergic_rhinitis`, `asthma_copd_overlap`, or `none_declared`.
- **Sensitivity_Level**: the user's coarse sensitivity, one of `standard`, `elevated`, or `high`,
  shifting the escalation point relative to the Orange_Band.
- **Condition_Weighting**: the named strategy mapping a Condition to an ordered set of weighted species,
  used to order and emphasize what the response foregrounds.
- **Weighted_Focus**: the ordered species list a Condition_Weighting produces, restricted to species
  actually available for the site.
- **Personal_Threshold**: a user-specific concentration or Sub_Index value whose crossing is reported,
  overriding the default band trigger.
- **Threshold_Crossing**: the determination that a served value has reached or passed a
  Personal_Threshold or the user's effective escalation band.
- **User_Location**: a named point of interest in a User_Profile — `home`, `work`, or `commute` — stored
  at reduced precision.
- **Inhaled_Dose**: the optional activity-adjusted exposure estimate, concentration times an
  activity-adjusted breathing rate times duration, served only when the User_Profile supplies the
  activity inputs it requires.
- **Serving_Response**: the per-user customized JSON document the serving API returns to the agent.
- **Basis**: the block within a Serving_Response that names everything a recommendation rests on — the
  Breakpoint_Table identifier, the Calibration_Strategy, the NowCast coverage, the threshold source, and
  the contributing record identifiers — so the recommendation is independently reviewable.
- **Guardrail_Envelope**: the set of fields every Serving_Response carries unconditionally — the
  advisory scope, the emergency guidance, and the non-diagnostic disclaimer.
- **Audit_Record**: the append-only record of what was served to which user identity, over which
  Readings, on which Basis, holding no clinical or precise-location values.
- **Consent_Record**: the stored evidence that the user consented to storing health-adjacent data, with
  its version and instant.
- **Clock**: the injected time boundary. Domain code never reads the wall clock.
- **ReadingsStore**: the port for writing and querying Calibrated_Reading values by site, species, and
  time window.
- **SensorRegistryStore**: the port for upserting and querying Sensor_Metadata_Record values.
- **RawArchive**: the port for the immutable raw payload store.
- **ProfileStore**: the port for reading and writing User_Profile values.
- **ForecastClient**: the port for external AQI-forecast and pollen retrieval.
- **MeteorologyProvider**: the port for retrieving the RH, temperature, and pressure a calibration or a
  unit conversion needs when the ingested payload does not carry them.
- **MqttTransport**: the port for subscribing to the broker that carries pushed records.
- **FeedClient**: the port for pulling `/ListSensors` and `/SensorData` from a reference-contract feed.
- **Authenticator**: the port that resolves a bearer credential to a verified user identity and claims,
  or rejects it.
- **Config_Loader**: the component that reads and validates configuration from file and environment,
  failing fast with one message per invalid value.
