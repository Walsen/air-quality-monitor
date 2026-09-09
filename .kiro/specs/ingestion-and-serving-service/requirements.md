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

## Requirements

### Requirement 1: Measurement Record Contract (`/SensorData`)

**User Story:** As a Service 2 developer, I want an independent, exact model of the `/SensorData`
contract, so that ingestion works unchanged whether the payload came from the simulator or from a live
public air-quality feed, and so that any drift between the two services fails a test rather than
corrupting data.

#### Acceptance Criteria

1. THE Service SHALL model each Sensor_Data_Record with exactly these nine field names and no
   additional fields, every one of them required to be present in an accepted record: `Species`,
   `Source`, `Units`, `SiteCode`, `DateTime`, `Duration`, `ScaledValue`, `RatificationStatus`,
   `SensorContract`.
2. THE Service SHALL accept `Species` as exactly one of the case-sensitive values `NO2`, `PM25`,
   `NO2Index`, or `PM25Index`, and SHALL accept `RatificationStatus` as exactly one of the
   case-sensitive values `P` or `R`.
3. THE Service SHALL accept `DateTime` only as an ISO-8601 UTC timestamp at whole-second precision with
   no fractional-second component and a trailing `Z`, interpreting its value as the start instant of the
   half-open interval the record covers, from `DateTime` inclusive to `DateTime` plus the interval
   denoted by `Duration` exclusive.
4. THE Service SHALL accept `Duration` as an ISO-8601 duration string, SHALL interpret `PT1H` as one
   hour, and SHALL treat the set of accepted `Duration` values as configurable with default `PT1H` only.
5. THE Service SHALL accept `ScaledValue` as a JSON number and SHALL preserve the value as received in
   the Raw_Archive and as the reported value of the resulting Raw_Reading, applying no rounding of its
   own before calibration.
6. THE Service SHALL treat `Species` values `NO2` and `PM25` as Mass_Concentration measurements in the
   unit named by `Units`, whose expected value is `ug.m-3`.
7. THE Service SHALL treat `Species` values `NO2Index` and `PM25Index` as the emitting network's own
   index values, SHALL store them as received when they pass validation, and SHALL NOT use them as, or
   in the derivation of, any Sub_Index, Overall_AQI, Band, or Threshold_Crossing this Service computes,
   because they originate from an undocumented band table that is not the Breakpoint_Table of
   Requirement 10.
8. WHERE `Units` for a `Species` value of `NO2` or `PM25` is a value other than `ug.m-3`, THE Service
   SHALL quarantine the record with a rejection reason naming the field, the received value, and the
   expected value, rather than assuming the expected unit.
9. THE Service SHALL treat `SiteCode` as the identity linking a Sensor_Data_Record to a
   Sensor_Metadata_Record and SHALL apply no format constraint of its own to `SiteCode` beyond being a
   non-empty string, so that a change of site-code convention in the emitting network is not a
   breaking change here.
10. FOR ALL accepted Sensor_Data_Record values, THE Service SHALL preserve every field of the record
    unmodified through the Raw_Archive write of Requirement 16, so that the archived payload is
    byte-comparable with what was received.

### Requirement 2: Sensor Metadata Record Contract (`/ListSensors`)

**User Story:** As a Service 2 developer, I want an independent, exact model of the `/ListSensors`
contract, so that the Sensor_Registry can be populated from either source and geographic personalization
has trustworthy coordinates.

#### Acceptance Criteria

1. THE Service SHALL model each Sensor_Metadata_Record with exactly these twenty field names, in this
   order, with no additional field and no omitted field, and with every field key accepted as present
   even when its value is `null`: `SiteCode`, `SiteName`, `DeviceCode`, `InstallationCode`, `Facility`,
   `Location`, `Latitude`, `Longitude`, `Borough`, `SiteClassification`, `SensorHeightAboveGround`,
   `DistanceToKerb`, `SponsorName`, `SiteLocationType`, `StartDate`, `EndDate`, `PowerTag`,
   `SiteDescription`, `SitePhotoURL`, `SensorContract`.
2. THE Service SHALL accept `Latitude` and `Longitude` only as JSON strings holding a signed decimal
   number with exactly 7 digits after the decimal point, with `Latitude` within -90.0000000 to
   90.0000000 and `Longitude` within -180.0000000 to 180.0000000.
3. THE Service SHALL accept `Location` only as a GeoJSON object with `type` `"Feature"` and
   `geometry.type` `"Point"` whose `coordinates` array holds exactly two elements, and IF those two
   elements are not character-identical JSON strings to the sibling `Latitude` value then the sibling
   `Longitude` value, THEN THE Service SHALL quarantine the record with a rejection reason naming the
   disagreeing values.
4. THE Service SHALL accept `SiteClassification` as exactly one of the case-sensitive values
   `Roadside`, `Urban Background`, or `Suburban`, and `PowerTag` as exactly one of `Mains` or `Solar`.
5. THE Service SHALL accept `SensorHeightAboveGround` and `DistanceToKerb` as JSON numbers and
   `StartDate` as an ISO-8601 UTC timestamp at whole-second precision ending in `Z`.
6. THE Service SHALL accept `EndDate` as JSON `null`, denoting a currently active site, or as an
   ISO-8601 UTC timestamp at whole-second precision ending in `Z`, and IF `EndDate` is non-null and
   earlier than `StartDate`, THEN THE Service SHALL quarantine the record naming both values.
7. THE Service SHALL derive the geographic position used by every geographic query in this document
   solely from the `Latitude` and `Longitude` fields, parsed as decimal degrees.
8. WHERE a Sensor_Metadata_Record has a non-null `EndDate` no later than the current instant from the
   Clock, THE Service SHALL mark the site inactive in the Sensor_Registry and SHALL exclude it from the
   nearest-N results of Requirement 20 while continuing to serve its historical Readings.

### Requirement 3: Record Parsing and Rejection

**User Story:** As a Service 2 developer, I want parsing to reject anything non-conforming with a
precise reason, so that malformed upstream data is diagnosable and never reaches storage.

#### Acceptance Criteria

1. WHEN the Parser is given JSON text holding a single object or an array of objects, THE Parser SHALL
   produce the corresponding record values in the order the array declares them, accepting an array
   length of 0 up to the configured maximum batch size with default 100,000.
2. IF the Parser is given text that is not well-formed JSON, or a payload larger than the configured
   maximum payload size with default 8 MiB, THEN THE Parser SHALL raise a rejection naming the failure
   kind and SHALL produce no record value.
3. IF a record omits a required field, carries a field name outside the contract, carries a field whose
   JSON value type does not match the contract, or carries a `Species` or `RatificationStatus` value
   outside its permitted set, THEN THE Parser SHALL raise a rejection naming the offending field and
   which of those four conditions it violated, and SHALL produce no record value for that record.
4. FOR ALL Sensor_Data_Record and Sensor_Metadata_Record values the Parser accepts, rendering the record
   through the Serializer and parsing that output again SHALL produce a record whose every field equals
   the original, including null-valued fields.
5. WHEN one record inside an array fails validation, THE Parser SHALL continue to evaluate the remaining
   records, SHALL return the accepted records together with one rejection entry per failed record
   identifying it by array index, and SHALL NOT discard the accepted records because a sibling failed.
6. WHERE a record carries a `Species` value outside the permitted set and the configured meteorology
   channel mapping of Requirement 8 maps that value to a meteorology channel, THE Service SHALL route
   the record to that channel instead of rejecting it, and SHALL NOT store it as a Reading.
7. THE Parser SHALL never emit a partially populated record value: a record is either accepted with
   every field validated or rejected with no value produced.

### Requirement 4: Push Ingestion over MQTT

**User Story:** As a Service 2 operator, I want pushed sensor records ingested as they arrive, so that
the serving API reflects current conditions without polling.

#### Acceptance Criteria

1. WHEN the Service starts with the push interface enabled, THE Service SHALL subscribe through the
   MqttTransport to the configured topic filter, with default `aqm/sensors/+/data`, and SHALL log one
   structured event naming the resolved topic filter.
2. WHEN a message arrives on a subscribed topic, THE Service SHALL treat its payload as a single
   Sensor_Data_Record or an array of them and SHALL pass it through the Ingest_Pipeline of
   Requirement 6 onward.
3. THE Service SHALL extract the site identifier from the topic according to the configured topic
   pattern, with default `aqm/sensors/{SiteCode}/data`, and IF the extracted site identifier differs
   from the `SiteCode` field of a record in the payload, THEN THE Service SHALL quarantine that record
   with a rejection reason naming both values.
4. FOR ALL arriving messages, THE Service SHALL isolate processing failures to the message being
   processed: an exception raised while handling one message SHALL be caught, logged at `error` with the
   topic, the extracted site identifier, and the error type, and SHALL NOT terminate the subscription or
   prevent the next message from being processed.
5. WHEN the MqttTransport connection is lost, THE Service SHALL retry the connection with exponential
   backoff starting at 1 second and doubling to the configured maximum interval, with default 60
   seconds, retrying indefinitely, and SHALL log each attempt at `warning` with the attempt count.
6. THE Service SHALL acknowledge a message only after the Raw_Archive write of Requirement 16 has
   succeeded, so that an ingestion failure after acknowledgement cannot lose the payload silently.
7. FOR ALL ingested messages, THE Service SHALL record the transport as `mqtt` on the resulting
   Raw_Reading values, so that the ingestion path of any stored Reading is recoverable.
8. WHERE the push interface is disabled by configuration, THE Service SHALL open no subscription and
   SHALL still start the serving API and the pull interface if those are enabled.

### Requirement 5: Pull Ingestion from a Reference-Contract Feed

**User Story:** As a Service 2 operator, I want a scheduled poll of a reference-contract feed, so that
the same processing works against a live public air-quality API that offers no push transport.

#### Acceptance Criteria

1. WHEN the pull interface is invoked, THE Service SHALL request `/SensorData` through the FeedClient
   for the configured species set and the time window derived from criterion 3, and SHALL pass every
   returned record through the same Ingest_Pipeline the push path uses.
2. THE Service SHALL send the feed credential in the `X-API-KEY` request header, SHALL resolve that
   credential only from the environment or a runtime-supplied path, and SHALL NOT write it to any log
   entry, any response body, or the Raw_Archive.
3. THE Service SHALL derive the poll window as the interval from the most recent successfully ingested
   `DateTime` for the requested species, minus the configured overlap with default 2 hours, up to the
   current instant from the Clock, and WHERE no Reading has yet been ingested, SHALL use the configured
   initial backfill span with default 24 hours.
4. FOR ALL records the pull path ingests, THE Service SHALL produce the same Calibrated_Reading, with
   the same corrected value, Quality_Flag, Sub_Index, and Band, that the push path produces for a
   byte-identical Sensor_Data_Record under the same configuration, Clock instant, and RH input.
5. WHEN a poll invocation completes, THE Service SHALL log one structured summary naming the requested
   window, the count of records received, accepted, deduplicated, and quarantined, and the elapsed
   duration.
6. IF the FeedClient returns a transport error, a non-success status, or a body the Parser rejects
   wholesale, THEN THE Service SHALL log the failure at `error` naming the status or failure kind, SHALL
   leave the most recent successfully ingested `DateTime` unchanged so the next invocation retries the
   same window, and SHALL exit with a non-zero status without partially advancing ingestion state.
7. THE Service SHALL request `/ListSensors` through the FeedClient on the configured registry-refresh
   cadence, with default once per 24 hours, and SHALL upsert the results into the Sensor_Registry per
   Requirement 15.
8. THE Service SHALL bound a single poll invocation to the configured maximum record count, with
   default 500,000, and WHERE the derived window would exceed it, SHALL ingest the oldest portion of the
   window first and record the remaining window so the next invocation resumes without a gap.
9. WHERE the pull interface is disabled by configuration, THE Service SHALL make no outbound feed
   request.

### Requirement 6: Reading Validation and Quarantine

**User Story:** As a Service 2 developer, I want implausible and mistimed records held back from
storage with their reason retained, so that bad upstream data neither corrupts an index nor disappears
without trace.

#### Acceptance Criteria

1. FOR ALL parsed Sensor_Data_Record values, THE Service SHALL evaluate every validation rule in this
   requirement and SHALL record one rejection reason per violated rule rather than stopping at the
   first.
2. IF `ScaledValue` is not a finite number, or is negative for a `Species` of `NO2` or `PM25`, THEN THE
   Service SHALL quarantine the record naming the field and the received value.
3. IF `ScaledValue` for `Species` `PM25` exceeds the configured plausibility ceiling, with default
   1,000 µg/m³, or for `Species` `NO2` exceeds its configured ceiling, with default 5,000 µg/m³, THEN
   THE Service SHALL quarantine the record naming the species, the value, and the ceiling.
4. IF `DateTime` is later than the current instant from the Clock plus the configured clock-skew
   tolerance, with default 5 minutes, THEN THE Service SHALL quarantine the record as future-dated,
   naming the record instant and the current instant.
5. IF the age of `DateTime` relative to the current instant from the Clock exceeds the
   Retention_Window, THEN THE Service SHALL quarantine the record as stale, naming the record instant
   and the Retention_Window, and SHALL NOT write it to the ReadingsStore.
6. IF `DateTime` is not aligned to the boundary of the interval denoted by `Duration` — for `PT1H`, a
   zero minute and zero second component — THEN THE Service SHALL quarantine the record naming the
   misalignment.
7. WHERE a record's `SiteCode` is absent from the Sensor_Registry, THE Service SHALL ingest the record
   and SHALL log one `warning` naming the unknown `SiteCode`, so that a reading is never lost because
   metadata has not yet been refreshed; and THE Service SHALL exclude that site from geographic results
   until its Sensor_Metadata_Record is known, because no coordinates exist for it.
8. FOR ALL quarantined records, THE Service SHALL retain the record as received, its rejection reasons,
   the ingestion instant, the transport, and the Raw_Archive identifier, for the configured quarantine
   retention period with default 30 days.
9. FOR ALL quarantined records, THE Service SHALL emit one structured `warning` log event per record
   naming the `SiteCode`, `Species`, `DateTime`, and the rejection reason categories, and SHALL
   increment a per-reason counter that Requirement 29 exposes.
10. THE Service SHALL never write a quarantined record to the ReadingsStore and SHALL never include a
    quarantined record in any Serving_Response.
11. FOR ALL payloads, THE Service SHALL write the Raw_Archive entry of Requirement 16 before evaluating
    any validation rule, so that a payload that fails every rule is still recoverable.

### Requirement 7: Idempotent Deduplication

**User Story:** As a Service 2 operator, I want re-delivered and overlapping records to converge on one
stored value regardless of arrival order, so that an at-least-once transport and an overlapping poll
window cannot double-count or produce order-dependent state.

#### Acceptance Criteria

1. THE Service SHALL identify one measurement interval by the Dedup_Key formed from `SiteCode`,
   `Species`, `DateTime`, and `Duration`, and SHALL treat two records sharing a Dedup_Key as candidates
   for the same stored Reading.
2. WHEN a record arrives whose Dedup_Key is absent from the ReadingsStore, THE Service SHALL process
   and store it.
3. WHEN a record arrives whose Dedup_Key is present and whose contract fields are all equal to the
   stored Reading's originating record, THE Service SHALL make no write, SHALL count the record as a
   duplicate, and SHALL report success to the caller.
4. WHEN a record arrives whose Dedup_Key is present and whose `RatificationStatus` is `R` while the
   stored Reading originated from a record with `RatificationStatus` `P`, THE Service SHALL replace the
   stored Reading with the newly processed one, because a ratified value supersedes a provisional one.
5. WHEN a record arrives whose Dedup_Key is present and whose `RatificationStatus` is `P` while the
   stored Reading originated from a record with `RatificationStatus` `R`, THE Service SHALL retain the
   stored Reading unchanged and SHALL log one `warning` naming the Dedup_Key.
6. WHEN a record arrives whose Dedup_Key is present, whose `RatificationStatus` equals that of the
   stored Reading's originating record, and whose `ScaledValue` differs from it, THE Service SHALL
   retain whichever of the two values is greater, SHALL set the Quality_Flag of the resulting Reading to
   `suspect_conflict`, and SHALL log one `warning` naming the Dedup_Key and both values — retaining the
   greater value because, for a health advisory, the more conservative of two irreconcilable readings is
   the safer one to serve.
7. FOR ALL sets of records sharing one Dedup_Key, THE Service SHALL produce a final stored Reading that
   is independent of the order in which those records were ingested.
8. FOR ALL records, ingesting the same record two or more times SHALL leave the ReadingsStore in the
   state produced by ingesting it once, and SHALL leave the count of stored Readings unchanged after the
   first.
9. THE Service SHALL apply deduplication before calibration, so that a duplicate record does not
   consume calibration or meteorology work.
10. THE Service SHALL write every received payload to the Raw_Archive regardless of deduplication
    outcome, so the archive records what arrived rather than what was stored.

### Requirement 8: Humidity-Aware Calibration

**User Story:** As a Service 2 developer, I want a humidity-aware correction applied before any index is
computed, so that the hygroscopic inflation of optical PM2.5 readings does not propagate into advice.

#### Acceptance Criteria

1. THE Service SHALL apply a Calibration_Strategy to every `PM25` Mass_Concentration Reading before
   computing any Sub_Index, NowCast, Overall_AQI, Threshold_Crossing, or Inhaled_Dose from it, and SHALL
   never compute any of those from the uncorrected reported value.
2. THE Service SHALL select the Calibration_Strategy by name from a registry, SHALL apply the strategy
   named by configuration with default `rh_linear`, and IF the configured name is absent from the
   registry, THEN THE Config_Loader SHALL reject the configuration naming the supplied value and the
   registered names.
3. THE Service SHALL implement the default `rh_linear` Calibration_Strategy as
   `corrected = a * reported + b * RH + c`, clamped below at 0, with configurable coefficients whose
   defaults are `a = 0.524`, `b = -0.0862`, and `c = 5.75` — the shape and default coefficients of the
   published United States-wide humidity-compensated correction for low-cost optical PM2.5 sensors
   (FINDINGS SQ5) — and SHALL declare its Calibration_Domain as a reported concentration of 0 to 250
   µg/m³ and an RH of 20 to 90 percent.
4. THE Service SHALL resolve the RH value for a Reading from the first available of these three sources,
   in this order: a meteorology channel record for the same `SiteCode` and interval received through the
   mapping of criterion 5; the MeteorologyProvider port for that `SiteCode` and instant; and no RH at
   all.
5. THE Service SHALL accept a configured meteorology channel mapping from a `Species` value outside the
   contract's four permitted values to one of the channels `rh`, `temperature`, or `pressure`, with an
   empty default, so that a feed publishing meteorology channels can be consumed without altering the
   record contract, and SHALL treat records so mapped as meteorology inputs rather than Readings.
6. IF no RH value is available for a `PM25` Reading, THEN THE Service SHALL apply the configured
   no-humidity fallback strategy, with default `identity`, SHALL set the Quality_Flag to `uncalibrated`,
   and SHALL set the Confidence to `low`.
7. WHERE the reported concentration or the resolved RH falls outside the Calibration_Strategy's
   Calibration_Domain, THE Service SHALL still apply the strategy, SHALL set the Quality_Flag to
   `calibrated_extrapolated`, and SHALL log one `warning` naming the out-of-domain input and its bound.
8. FOR ALL pairs of RH values within 0 to 100 percent where the first is less than or equal to the
   second, THE Service SHALL produce, for one identical reported concentration under the `rh_linear`
   strategy, a corrected value for the first that is greater than or equal to the corrected value for
   the second, so that a more humid reading is corrected downward at least as much (humidity
   monotonicity).
9. FOR ALL Readings, THE Service SHALL produce a corrected value that is finite and greater than or
   equal to 0.
10. FOR ALL Readings, THE Service SHALL retain the reported value alongside the corrected value on the
    Calibrated_Reading, so that the correction applied is always recoverable and auditable.
11. THE Service SHALL record on every Calibrated_Reading the identifier of the Calibration_Strategy
    that produced it and the source the RH value came from, one of `channel`, `provider`, or `none`.
12. WHERE a `Species` is `NO2`, THE Service SHALL apply the Calibration_Strategy named by the
    configured per-species strategy mapping, with default `identity`, because the humidity confounding
    that motivates the PM2.5 correction is specific to optical particle counting.
13. IF the configured coefficients or Calibration_Domain bounds are non-finite, or a domain minimum is
    greater than or equal to its maximum, THEN THE Config_Loader SHALL reject the configuration naming
    the offending value and its constraint, and THE Service SHALL ingest nothing.

### Requirement 9: Unit Conversion Between Mass Concentration and Mixing Ratio

**User Story:** As a Service 2 developer, I want an explicit, temperature- and pressure-aware conversion
between µg/m³ and ppb, so that NO2 sub-indices are correct at the altitude the fleet actually sits at.

#### Acceptance Criteria

1. THE Service SHALL convert a NO2 Mass_Concentration in µg/m³ to a Mixing_Ratio in ppb before applying
   the NO2 Breakpoint_Table, because that table's breakpoints are expressed in ppb.
2. THE Service SHALL compute the conversion as
   `mass_concentration_ug_m3 = mixing_ratio_ppb * molar_mass_g_mol * pressure_pa / (gas_constant * temperature_k) * 1e-3`,
   using the molar gas constant 8.314462618 J/(mol·K) and the molar mass 46.0055 g/mol for NO2, and
   SHALL derive the ppb value as the inverse of that relation.
3. THE Service SHALL take the temperature and absolute pressure used in the conversion as explicit
   parameters, resolved from the first available of a meteorology channel record for that `SiteCode` and
   interval, the MeteorologyProvider port, and the configured default site conditions whose defaults are
   15 °C and 740 hPa, reflecting the approximately 2,560 m elevation of the default deployment
   geography.
4. FOR ALL Mixing_Ratio values, converting to Mass_Concentration and back under one identical
   temperature and pressure SHALL return the original value within a relative tolerance of 1e-9.
5. FOR ALL temperature and pressure pairs, THE Service SHALL produce a conversion factor that is
   strictly increasing in pressure and strictly decreasing in absolute temperature.
6. THE Service SHALL yield a conversion factor of 1.8806 µg/m³ per ppb, within a relative tolerance of
   1e-4, at 25 °C and 101,325 Pa, and 1.4219 µg/m³ per ppb, within the same tolerance, at 15 °C and
   74,000 Pa, so that the altitude sensitivity the conversion exists to capture is pinned by test.
7. THE Service SHALL record on every Calibrated_Reading whose Sub_Index required a conversion the
   temperature and pressure used and the source they came from, one of `channel`, `provider`, or
   `default`.
8. WHERE the temperature and pressure came from the configured defaults rather than a measurement, THE
   Service SHALL cap the Confidence of the resulting Sub_Index at `medium`.
9. IF a resolved temperature is not greater than 0 K, or a resolved pressure is not greater than 0 Pa,
   THEN THE Service SHALL treat the conversion as unavailable, SHALL compute no NO2 Sub_Index for that
   Reading, and SHALL log one `error` naming the offending value.

### Requirement 10: AQI Sub-Index Computation

**User Story:** As an agent developer, I want a per-pollutant AQI sub-index with its band, computed from
a declared breakpoint table, so that the advice I generate rests on a standard, reviewable scale.

#### Acceptance Criteria

1. THE Service SHALL compute a Sub_Index for a pollutant from its corrected concentration by
   piecewise-linear interpolation within the containing band of the Breakpoint_Table, as
   `sub_index = (index_high - index_low) / (bp_high - bp_low) * (concentration - bp_low) + index_low`,
   and SHALL round the result to the nearest integer, resolving a tie away from zero.
2. THE Service SHALL load Breakpoint_Table definitions as data identified by a table identifier, SHALL
   use the identifier named by configuration with default `epa-2024-05-06`, and SHALL record that
   identifier on every Calibrated_Reading and in the Basis of every Serving_Response that carries a
   Sub_Index.
3. THE Service SHALL define the default `epa-2024-05-06` PM2.5 table over a 24-hour average
   concentration in µg/m³ with these bands, each lower bound inclusive and each upper bound inclusive:
   0 to 50 over 0.0 to 9.0; 51 to 100 over 9.1 to 35.4; 101 to 150 over 35.5 to 55.4; 151 to 200 over
   55.5 to 125.4; 201 to 300 over 125.5 to 225.4; and 301 to 500 over 225.5 to 325.4.
4. THE Service SHALL define the default `epa-2024-05-06` NO2 table over a 1-hour Mixing_Ratio in ppb
   with these bands: 0 to 50 over 0 to 53; 51 to 100 over 54 to 100; 101 to 150 over 101 to 360; 151 to
   200 over 361 to 649; 201 to 300 over 650 to 1249; and 301 to 500 over 1250 to 2049.
5. THE Service SHALL map Sub_Index ranges to Band names as 0 to 50 `Good`, 51 to 100 `Moderate`, 101 to
   150 `Unhealthy for Sensitive Groups`, 151 to 200 `Unhealthy`, 201 to 300 `Very Unhealthy`, and 301 and
   above `Hazardous`.
6. THE Service SHALL truncate a PM2.5 concentration to one decimal place, and a NO2 Mixing_Ratio to a
   whole number of ppb, before locating its band, matching the reporting precision the tables are
   defined at.
7. FOR ALL pairs of concentrations for one species where the first is less than or equal to the second,
   THE Service SHALL produce a Sub_Index for the first that is less than or equal to the Sub_Index for
   the second (index monotonicity).
8. FOR ALL band boundaries in a Breakpoint_Table, THE Service SHALL produce exactly the band's lower
   index value at its lower breakpoint and exactly the band's upper index value at its upper breakpoint.
9. WHERE a concentration exceeds the highest breakpoint of its table, THE Service SHALL compute the
   Sub_Index by extrapolating the highest band's slope, SHALL cap the reported Sub_Index at the
   configured ceiling with default 500, and SHALL report the Band as `Hazardous`.
10. FOR ALL Sub_Index values, THE Service SHALL produce the same value for the same corrected
    concentration, table identifier, and species on every evaluation.
11. IF a configured Breakpoint_Table has overlapping bands, a gap between consecutive bands, a
    non-increasing breakpoint or index sequence, or a band whose unit differs from the species unit the
    table declares, THEN THE Config_Loader SHALL reject the configuration naming the offending band and
    the violated constraint, and THE Service SHALL compute no Sub_Index.
12. THE Service SHALL compute a Sub_Index only for species the selected Breakpoint_Table defines, and
    SHALL omit rather than approximate a Sub_Index for any other species.

### Requirement 11: NowCast Computation

**User Story:** As an agent developer, I want the current-hour PM2.5 index computed by NowCast, so that
an hourly reading can be placed on a 24-hour breakpoint scale honestly and recent change is not
flattened by a plain average.

#### Acceptance Criteria

1. THE Service SHALL compute the PM2.5 concentration used for Sub_Index computation as the NowCast over
   the NowCast_Window of corrected hourly values ending at the Reading's interval, because the PM2.5
   Breakpoint_Table is defined over a 24-hour average and a single hourly value cannot be placed on it
   directly.
2. THE Service SHALL use a NowCast_Window of the configured length, with default 12 hours, taking at
   most one corrected value per hour for the site.
3. THE Service SHALL compute the NowCast as `sum(w**i * c_i) / sum(w**i)` over the available hours of
   the window, where `i` is 0 for the most recent hour and increases into the past, `c_i` is that hour's
   corrected concentration, and `w` is the weight factor of criterion 4, skipping hours with no value in
   both sums.
4. THE Service SHALL compute the weight factor as `w = 1 - (c_max - c_min) / c_max` over the available
   values of the window, bounded below at 0.5, and WHERE `c_max` is 0, SHALL use a weight factor of 1.
5. IF fewer than two of the three most recent hours of the window have a value, THEN THE Service SHALL
   report no NowCast for that interval, SHALL fall back to the Reading's own corrected hourly value for
   Sub_Index computation, SHALL cap the Confidence at `low`, and SHALL record the fallback in the Basis.
6. FOR ALL windows, THE Service SHALL produce a NowCast no less than the minimum and no greater than the
   maximum of the available corrected values in that window.
7. WHERE every available value in the window is one identical value, THE Service SHALL produce a
   NowCast equal to that value.
8. FOR ALL windows, THE Service SHALL weight the most recent available hour no less than any older
   available hour.
9. THE Service SHALL record on every Calibrated_Reading carrying a NowCast-derived Sub_Index the count
   of hours available in the window, the window length, and the weight factor used.
10. THE Service SHALL apply NowCast only to PM2.5, and SHALL compute the NO2 Sub_Index from the
    Reading's own 1-hour Mixing_Ratio, because the NO2 Breakpoint_Table is already defined over a 1-hour
    interval.
11. FOR ALL windows, THE Service SHALL produce the same NowCast for the same ordered series of
    corrected values on every evaluation.

### Requirement 12: Driving Pollutant and Overall AQI

**User Story:** As an agent developer, I want the overall AQI and the pollutant driving it, so that
advice can attribute the risk to a cause rather than reporting a bare number.

#### Acceptance Criteria

1. THE Service SHALL compute the Overall_AQI for a site and instant as the maximum Sub_Index across the
   species for which a Sub_Index is available at that site and instant.
2. THE Service SHALL report the Driving_Pollutant as the species whose Sub_Index equals the
   Overall_AQI, and WHERE two or more species tie, SHALL report the one that sorts first in the
   configured species precedence, with default `PM25` before `NO2`, so that the result is deterministic.
3. FOR ALL sites and instants, THE Service SHALL produce an Overall_AQI greater than or equal to every
   available Sub_Index at that site and instant, and equal to at least one of them.
4. THE Service SHALL report the Band of the Overall_AQI using the mapping of Requirement 10
   criterion 5.
5. WHERE a Sub_Index is available for only one species, THE Service SHALL report that species as the
   Driving_Pollutant and its Sub_Index as the Overall_AQI, and SHALL declare in the Basis which species
   had no Sub_Index and why.
6. IF no Sub_Index is available for a site and instant, THEN THE Service SHALL report no Overall_AQI and
   no Driving_Pollutant for it, rather than reporting a zero or a default band.
7. THE Service SHALL report alongside the Overall_AQI the method that produced each contributing
   Sub_Index, one of `nowcast` or `hourly`, so that the reader can tell a NowCast-derived value from a
   direct one.
8. THE Service SHALL set the Confidence of an Overall_AQI to the lowest Confidence among the
   Sub_Index values that contributed to it.

### Requirement 13: Quality Flags, Confidence, and Sensor Fault Detection

**User Story:** As an agent developer, I want a quality flag and a confidence indicator on every value,
plus detection of a misbehaving sensor, so that I never present low-cost sensor output as
reference-grade and a stuck sensor does not drive advice.

#### Acceptance Criteria

1. THE Service SHALL attach exactly one Quality_Flag to every Calibrated_Reading, from the set
   `calibrated`, `calibrated_extrapolated`, `uncalibrated`, `suspect_fault`, and `suspect_conflict`.
2. THE Service SHALL derive the Confidence deterministically as `high` for a Quality_Flag of
   `calibrated` with a complete NowCast_Window where a NowCast applies, `medium` for `calibrated` with
   an incomplete window or for `calibrated_extrapolated`, and `low` for `uncalibrated`,
   `suspect_fault`, or `suspect_conflict`, and SHALL apply the caps that Requirement 9 criterion 8 and
   Requirement 11 criterion 5 impose.
3. THE Service SHALL assign a Quality_Flag of `suspect_fault` to a Reading whose site and species the
   Fault_Detector currently flags, and SHALL retain the Reading rather than quarantining it, because a
   suspected fault is an assessment rather than a validation failure.
4. THE Fault_Detector SHALL flag a stuck value when the configured number of consecutive intervals for
   one site and species, with default 6, carry a reported value equal within the configured tolerance,
   default 0.01 µg/m³.
5. THE Fault_Detector SHALL flag a dropout when the count of consecutive expected intervals with no
   accepted Reading for one site and species reaches the configured threshold, with default 3.
6. THE Fault_Detector SHALL flag drift when the mean corrected value for one site and species over the
   configured comparison window, with default 24 hours, differs from the median of the same statistic
   across the configured minimum number of peer sites within the configured peer radius, defaults 3
   sites and 10 km, by more than the configured multiple of that median, default 3.0.
7. WHERE fewer than the configured minimum number of peer sites is available, THE Fault_Detector SHALL
   evaluate no drift flag for that site and SHALL log one `debug` event naming the site and the peer
   count.
8. WHEN the Fault_Detector raises or clears a flag, THE Service SHALL log one structured event naming
   the `SiteCode`, the `Species`, the flag category, and whether it was raised or cleared.
9. THE Service SHALL clear a fault flag for a site and species when the configured number of subsequent
   consecutive intervals, with default 3, satisfy none of the flag conditions.
10. FOR ALL Serving_Response documents, THE Service SHALL include the Quality_Flag and the Confidence
    alongside every concentration and index value it reports, and SHALL never report a value without
    them.
11. THE Service SHALL expose the count of Readings currently carrying each Quality_Flag, and the count
    of sites currently carrying each fault category, as the metrics Requirement 29 describes.
12. THE Service SHALL compute the same Quality_Flag and Confidence for the same inputs on every
    evaluation.

### Requirement 14: Readings Store and Retention

**User Story:** As a Service 2 developer, I want a narrow readings port with window-query semantics, so
that the domain is testable offline and the store choice stays a deployment decision.

#### Acceptance Criteria

1. THE Service SHALL access every stored Calibrated_Reading exclusively through the ReadingsStore port,
   and SHALL place no store-specific type, query language, or client object in any domain component.
2. THE ReadingsStore SHALL support writing one Calibrated_Reading and writing a batch of them, keyed by
   Dedup_Key, such that a write for an existing Dedup_Key resolves per Requirement 7.
3. THE ReadingsStore SHALL support querying by `SiteCode`, an optional species set, and a half-open time
   window from a start instant inclusive to an end instant exclusive, returning Readings ordered by
   ascending interval start then by the configured species precedence.
4. THE ReadingsStore SHALL support querying the most recent Reading per species for a set of
   `SiteCode` values, which is the query the serving path of Requirement 19 issues.
5. FOR ALL written Readings, a subsequent query whose window contains the Reading's interval start SHALL
   return a Reading equal in every field to the one written.
6. FOR ALL queries, THE ReadingsStore SHALL return every stored Reading whose interval start falls
   inside the requested window and no Reading whose interval start falls outside it.
7. THE Service SHALL retain Readings for the Retention_Window, with default 90 days, and SHALL exclude
   from every query result any Reading whose interval start is older than the Retention_Window measured
   from the current instant from the Clock.
8. THE Service SHALL bound any single query result to the configured maximum, with default 10,000
   Readings, and SHALL report to the caller when a result was truncated rather than silently returning a
   partial set.
9. THE Service SHALL ship an in-memory ReadingsStore adapter used by the offline suite, and SHALL ship a
   DynamoDB adapter whose partition key is the `SiteCode` and `Species` pair and whose sort key is the
   interval start, so a site-species time window is one range query.
10. FOR ALL ReadingsStore adapters, THE Service SHALL satisfy the same behavioral contract, verified by
    one shared test suite executed against every adapter, so that an adapter swap cannot change domain
    behavior.
11. THE ReadingsStore SHALL never be the source of a Serving_Response value that has no Quality_Flag
    and Confidence, since those travel with the stored Reading.

### Requirement 15: Sensor Registry and Geographic Query

**User Story:** As a Service 2 developer, I want a registry of site metadata with nearest-N lookup, so
that a user's response can be built from the sites actually near them.

#### Acceptance Criteria

1. THE Service SHALL access sensor metadata exclusively through the SensorRegistryStore port.
2. WHEN a Sensor_Metadata_Record is ingested, THE Service SHALL upsert it by `SiteCode`, replacing the
   stored record when any field differs and making no write when every field is equal.
3. THE SensorRegistryStore SHALL support lookup by `SiteCode`, listing all active sites, and querying
   the N nearest active sites to a supplied latitude and longitude.
4. THE Service SHALL compute the distance between two positions as the great-circle distance in
   kilometres, using an Earth radius of 6,371.0088 km.
5. WHEN the N nearest active sites are requested, THE Service SHALL return them ordered by ascending
   distance, resolving ties by ascending `SiteCode` so the order is deterministic, and SHALL return
   fewer than N when fewer active sites exist.
6. WHEN a maximum radius is supplied with a nearest-N query, THE Service SHALL return only sites whose
   distance is less than or equal to that radius, treating the boundary distance as a match.
7. FOR ALL nearest-N results, THE Service SHALL produce a sequence whose distances are non-decreasing,
   and SHALL exclude every site whose distance exceeds the supplied radius.
8. THE Service SHALL exclude sites marked inactive per Requirement 2 criterion 8 from every nearest-N
   result while continuing to resolve them by `SiteCode`.
9. WHEN an upsert changes a site's `Latitude` or `Longitude`, THE Service SHALL log one structured
   `warning` naming the `SiteCode` and both positions, since a moved site invalidates historical
   spatial assumptions.
10. THE Service SHALL ship an in-memory SensorRegistryStore adapter for the offline suite and a
    DynamoDB adapter keyed by `SiteCode`, and SHALL verify both against one shared behavioral test
    suite.
11. THE Service SHALL record on every registry entry the instant it was last upserted, so a stale
    registry is detectable.

### Requirement 16: Raw Archive

**User Story:** As a Service 2 operator, I want every ingested payload stored immutably before
processing, so that any stored value can be replayed, audited, or reprocessed after a calibration
change.

#### Acceptance Criteria

1. THE Service SHALL write every ingested payload to the RawArchive port exactly as received, before
   parsing, validation, deduplication, or calibration.
2. THE Service SHALL store with each archive entry the ingestion instant, the transport, the source
   topic or request window, and a generated archive identifier, and SHALL record that identifier on
   every Raw_Reading and Calibrated_Reading derived from the payload.
3. THE RawArchive SHALL be append-only: THE Service SHALL never update or delete an existing entry as
   part of ingestion or serving.
4. FOR ALL archived payloads, reading the entry back SHALL yield bytes identical to those written.
5. THE Service SHALL derive the archive key from the ingestion instant and the archive identifier, in a
   layout ordered by time so that a time range of archived payloads can be enumerated without scanning
   the whole archive, with default layout `raw/{yyyy}/{MM}/{dd}/{HH}/{archive_id}.json`.
6. THE Service SHALL never write a resolved feed credential, bearer credential, or any User_Profile
   field into the RawArchive.
7. IF the RawArchive write fails, THEN THE Service SHALL not acknowledge the message on the push path
   and SHALL fail the poll invocation on the pull path, in both cases logging one `error` naming the
   failure kind, so that a payload is never processed without being archived.
8. THE Service SHALL ship an in-memory RawArchive adapter for the offline suite and an S3 adapter, and
   SHALL verify both against one shared behavioral test suite.
9. THE Service SHALL treat archive retention and storage-class transition as a deployment concern and
   SHALL impose no expiry of its own on archived payloads.
