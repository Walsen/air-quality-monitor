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
   defaults are `a = 0.524`, `b = -0.0862`, and `c = 5.75` — the form and published coefficients of the
   United States-wide humidity-compensated correction derived for a widely deployed class of low-cost
   optical PM2.5 sensor, adopted here as a documented starting point rather than as a validated fit for
   this fleet; `docs/research/FINDINGS.md` SQ5 grounds the *need* for an RH-aware correction and the
   attainable error reduction, not these specific coefficients, so they are configuration rather than a
   pinned constant — and SHALL declare its Calibration_Domain as a reported concentration of 0 to 250
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
6. THE Service SHALL yield a conversion factor of 1.8804 µg/m³ per ppb, within a relative tolerance of
   1e-4, at 25 °C and 101,325 Pa, and 1.4210 µg/m³ per ppb, within the same tolerance, at 15 °C and
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

### Requirement 17: User Profile Store and Data Minimization

**User Story:** As a user with a respiratory condition, I want the service to hold the least data needed
to personalize my advice, so that sharing a health condition does not mean surrendering a clinical
record.

#### Acceptance Criteria

1. THE Service SHALL access User_Profile values exclusively through the ProfileStore port, keyed by the
   verified user identity that Requirement 18 establishes.
2. THE Service SHALL model a User_Profile with exactly these fields: the user identity, the Condition,
   the Sensitivity_Level, the Personal_Threshold set, the User_Location entries, the optional activity
   inputs of Requirement 23, the Medication_Entry set of Requirement 30, the Routine_Entry set of
   Requirement 30, the Consent_Record, and the instants the profile was created and last updated.
3. THE Service SHALL accept Condition as exactly one of `asthma`, `copd`, `allergic_rhinitis`,
   `asthma_copd_overlap`, or `none_declared`, and Sensitivity_Level as exactly one of `standard`,
   `elevated`, or `high`.
4. THE Service SHALL store no free-text clinical field beyond the bounded Symptom_Note of Requirement
   31, no diagnosis code, no date of birth, and no name or contact detail, and SHALL reject a profile
   write carrying any field outside the set criterion 2 defines, naming the offending field. THE Service
   SHALL store a medication only as a Medication_Entry under Requirement 30 — a name and a role, never a
   dose, a frequency, a route, or an administration schedule — so that there is nowhere in the stored
   shape to put the information that would make dosing advice expressible.
5. THE Service SHALL store each User_Location as a name from the set `home`, `work`, or `commute`
   together with a latitude and longitude rounded to the configured precision, with default 3 decimal
   places — approximately 110 m — so that a stored location is precise enough to select nearby sensors
   and too coarse to identify a dwelling.
6. THE Service SHALL accept at most the configured number of User_Location entries per profile, with
   default 5, and SHALL reject a write exceeding it naming the limit.
7. WHEN a profile is written, THE Service SHALL require a Consent_Record naming the consent version and
   the instant it was given, and IF it is absent or names a consent version the configuration does not
   recognize, THEN THE Service SHALL reject the write with HTTP status 400 naming the field and SHALL
   store nothing.
8. WHEN a user requests deletion of their profile, THE Service SHALL delete the User_Profile and every
   Audit_Record field that identifies that user, SHALL retain the de-identified Audit_Record counts, and
   SHALL respond confirming the deletion.
9. THE Service SHALL never write any User_Profile field into any log entry, the Raw_Archive, or any
   error message, and SHALL refer to a profile in diagnostics only by the pseudonymous user identity.
10. THE Service SHALL never include another user's profile data or Readings selected by another user's
    locations in any Serving_Response.
11. THE Service SHALL ship an in-memory ProfileStore adapter for the offline suite and a DynamoDB
    adapter keyed by the user identity, and SHALL verify both against one shared behavioral test suite.
12. WHERE no User_Profile exists for a verified user identity, THE Service SHALL serve a response using
    the configured default profile — Condition `none_declared`, Sensitivity_Level `standard`, no
    User_Location — and SHALL declare in the response that personalization used defaults, rather than
    failing the request.

### Requirement 18: Authentication and Per-User Authorization

**User Story:** As the operator of a service holding health-adjacent data, I want every data endpoint
authenticated and scoped to the calling user, so that there is no anonymous or cross-user access to
readings or profiles.

#### Acceptance Criteria

1. THE Service SHALL require a bearer credential on every endpoint except the health endpoint of
   Requirement 19 criterion 10, and SHALL resolve it through the Authenticator port to a verified user
   identity and claim set before any handler logic runs.
2. IF the `Authorization` header is absent, is not a bearer credential, or carries an empty credential,
   THEN THE Service SHALL respond with HTTP status 401 and a JSON body naming the missing or malformed
   header, and SHALL perform no store access.
3. IF the Authenticator rejects the credential as unverifiable, expired, or issued for a different
   audience, THEN THE Service SHALL respond with HTTP status 401 and a JSON body naming the rejection
   category, and SHALL NOT disclose which of those conditions applied beyond that category.
4. THE Service SHALL authenticate before validating query parameters, so that an unauthenticated request
   carrying a malformed parameter receives 401 rather than 400.
5. IF a verified user identity requests a resource belonging to a different user identity, THEN THE
   Service SHALL respond with HTTP status 403 and a JSON body naming the scope violation, and SHALL
   perform no read of that resource.
6. THE Service SHALL derive the user identity used for every profile read, Readings selection, and
   Audit_Record entry solely from the verified claim set, and SHALL never take it from a query
   parameter, a path segment, or a request body field.
7. THE Service SHALL never include the bearer credential, any claim value beyond the user identity, or
   any Authenticator internal detail in a response body, a log entry, or an Audit_Record.
8. THE Service SHALL ship a deterministic local Authenticator adapter for the offline suite that
   resolves configured test credentials to configured identities, and SHALL treat the production
   adapter — an Amazon Cognito JWT verifier — as a deployment concern behind the same port.
9. FOR ALL responses to unauthenticated or out-of-scope requests, THE Service SHALL include no Reading
   value, no profile value, and no site metadata.
10. WHEN authentication fails, THE Service SHALL log one structured `warning` naming the route, the
    rejection category, and no credential material.
11. THE Service SHALL apply the configured per-identity request rate limit, with default 60 requests per
    minute, and WHEN it is exceeded, SHALL respond with HTTP status 429 naming the limit and the retry
    interval.

### Requirement 19: Serving API Contract

**User Story:** As an agent developer, I want a stable, documented, per-user JSON contract with
predictable failure modes, so that my tool call can rely on the shape of what comes back.

#### Acceptance Criteria

1. THE Service SHALL expose `GET /v1/air-quality/me`, returning the Serving_Response for the verified
   user built from their User_Profile, the current Readings for their nearest sites, the personalization
   of Requirements 20 through 23, the enrichment of Requirement 24, and the Guardrail_Envelope of
   Requirement 25.
2. THE Service SHALL structure the `GET /v1/air-quality/me` response body with exactly these top-level
   members: `user`, `generatedAt`, `locations`, `nearestSensors`, `personalized`, `forecast`, `basis`,
   `advisoryScope`, `emergencyGuidance`, and `disclaimer`, shaped as follows.

```json
{
  "user": "u_123",
  "generatedAt": "2026-09-08T01:00:00Z",
  "locations": [
    { "name": "home", "lat": -17.394, "lon": -66.157 }
  ],
  "nearestSensors": [
    {
      "siteCode": "CB0086",
      "siteName": "Central Primary School",
      "siteClassification": "Urban Background",
      "locationName": "home",
      "distanceKm": 0.4,
      "asOf": "2026-09-08T00:00:00Z",
      "measurements": [
        {
          "species": "PM25",
          "reportedValue": 24.1,
          "correctedValue": 18.2,
          "units": "ug.m-3",
          "qualityFlag": "calibrated",
          "confidence": "high",
          "subIndex": 68,
          "band": "Moderate",
          "method": "nowcast"
        },
        {
          "species": "NO2",
          "reportedValue": 41.0,
          "correctedValue": 41.0,
          "units": "ug.m-3",
          "mixingRatioPpb": 28,
          "qualityFlag": "calibrated",
          "confidence": "medium",
          "subIndex": 26,
          "band": "Good",
          "method": "hourly"
        }
      ],
      "overallAqi": 68,
      "band": "Moderate",
      "drivingPollutant": "PM25",
      "confidence": "medium"
    }
  ],
  "personalized": {
    "condition": "asthma",
    "sensitivity": "high",
    "usedDefaultProfile": false,
    "weightedFocus": ["PM25", "NO2"],
    "unavailableWeightedSpecies": ["O3"],
    "escalationSubIndex": 51,
    "thresholdCrossed": true,
    "thresholdSource": "sensitivity_level",
    "crossings": [
      { "siteCode": "CB0086", "species": "PM25", "subIndex": 68, "threshold": 51 }
    ],
    "pollen": { "grass": "high", "tree": "low", "weed": "moderate" },
    "inhaledDose": null
  },
  "forecast": {
    "tomorrowAqi": 88,
    "trend": "rising",
    "source": "fake-local",
    "retrievedAt": "2026-09-08T00:45:00Z",
    "degraded": false
  },
  "basis": {
    "breakpointTable": "epa-2024-05-06",
    "calibrationStrategies": { "PM25": "rh_linear", "NO2": "identity" },
    "humiditySource": "provider",
    "conversionSource": "default",
    "nowcast": { "windowHours": 12, "hoursAvailable": 12, "weightFactor": 0.72 },
    "records": [
      { "siteCode": "CB0086", "species": "PM25", "dateTime": "2026-09-08T00:00:00Z", "duration": "PT1H" }
    ]
  },
  "advisoryScope": "exposure-reduction",
  "emergencyGuidance": "Severe breathlessness, a reliever that is not working, or blue lips are an emergency — contact emergency services immediately.",
  "disclaimer": "Exposure guidance only — not medical advice. Follow the action plan your clinician gave you."
}
```

3. THE Service SHALL expose `GET /v1/air-quality/history` accepting `siteCode`, an optional `species`,
   `startTime`, and `endTime`, returning the Readings in the requested half-open window ordered by
   ascending interval start, each carrying its corrected value, Quality_Flag, Confidence, Sub_Index, and
   Band, together with the Guardrail_Envelope.
4. THE Service SHALL expose `GET /v1/profile/me` returning the verified user's User_Profile, and
   `PUT /v1/profile/me` replacing it, validating the body against Requirement 17 and returning the
   stored profile.
5. THE Service SHALL expose `DELETE /v1/profile/me` performing the deletion of Requirement 17
   criterion 8.
6. IF a request supplies `startTime` later than `endTime`, either of them unparseable as an ISO-8601
   instant, one of them without the other, or a `species` outside the permitted set, THEN THE Service
   SHALL respond with HTTP status 400 and a JSON body naming each offending parameter and its permitted
   form or values, and SHALL include no Reading in the response.
7. IF a requested `siteCode` is absent from the Sensor_Registry, THEN THE Service SHALL respond with
   HTTP status 404 and a JSON body naming the unknown `siteCode`.
8. FOR ALL requests, THE Service SHALL never respond with HTTP status 500 because of malformed,
   out-of-range, or unexpected request input; every such failure SHALL map to its documented 400, 401,
   403, 404, or 429 response.
9. THE Service SHALL bound the history window to the configured maximum span, with default 30 days, and
   IF a request exceeds it, THEN THE Service SHALL respond with HTTP status 400 naming the requested
   span and the maximum.
10. THE Service SHALL expose an unauthenticated `GET /health` returning the service status, the resolved
    Breakpoint_Table identifier, and the resolved Calibration_Strategy names, and SHALL include in it no
    Reading, no profile data, and no credential.
11. THE Service SHALL respond to `GET /v1/air-quality/me` within the configured latency budget, with
    default 2 seconds, for a User_Profile of up to 5 User_Location entries against a registry of up to
    500 sites.
12. FOR ALL responses carrying a Reading value, THE Service SHALL include the `basis` member and the
    Guardrail_Envelope members, and SHALL omit no member that criterion 2 declares, emitting `null` for
    a member whose value is unavailable rather than dropping the key.
13. THE Service SHALL return every timestamp as an ISO-8601 UTC instant at whole-second precision ending
    in `Z`.

### Requirement 20: Geographic Personalization

**User Story:** As a user, I want the response built from the sensors nearest the places I actually
spend time, so that the readings reflect my exposure rather than a city average.

#### Acceptance Criteria

1. WHEN building a Serving_Response, THE Service SHALL resolve, for each User_Location in the profile,
   the N nearest active sites through the SensorRegistryStore, with N configurable and default 3.
2. THE Service SHALL apply the configured maximum selection radius, with default 10 km, and SHALL
   include a site whose distance equals the radius.
3. THE Service SHALL report each selected site's distance in kilometres rounded to the configured
   precision, with default 2 decimal places, and the name of the User_Location it was selected for.
4. WHERE one site is nearest to two or more User_Location entries, THE Service SHALL include it once and
   SHALL name every User_Location it serves.
5. WHERE a User_Location has no active site inside the maximum selection radius, THE Service SHALL
   include no site for it and SHALL declare that in the response rather than widening the radius
   silently.
6. WHERE the User_Profile has no User_Location entry, THE Service SHALL apply the configured fallback
   selection, with default the N nearest active sites to the configured default geographic centre, and
   SHALL declare that the fallback was used.
7. FOR ALL selections, THE Service SHALL order the sites of one User_Location by ascending distance,
   resolving ties by ascending `SiteCode`.
8. THE Service SHALL select for each site only Readings whose interval start is within the configured
   freshness window of the current instant from the Clock, with default 3 hours, and SHALL report the
   interval start as `asOf` so staleness is visible.
9. WHERE a selected site has no Reading inside the freshness window, THE Service SHALL include the site
   with an empty measurement set and SHALL report no Overall_AQI for it, rather than reporting an older
   value as current.
10. THE Service SHALL derive every distance from the stored `Latitude` and `Longitude` of the
    Sensor_Metadata_Record and the reduced-precision User_Location coordinates, using the great-circle
    computation of Requirement 15 criterion 4.

### Requirement 21: Condition Weighting

**User Story:** As a user with a specific respiratory condition, I want the pollutants that matter for my
condition foregrounded, so that the advice addresses my actual triggers.

#### Acceptance Criteria

1. THE Service SHALL select a Condition_Weighting by the User_Profile's Condition from a registry, so
   that adding a condition is a new registry entry rather than a change to existing logic.
2. THE Service SHALL define the default weighting map as: `asthma` to the ordered species `PM25`, `O3`,
   `NO2` with pollen relevant; `copd` to `NO2`, `O3`, `PM25` with pollen not relevant;
   `allergic_rhinitis` to `PM25`, `NO2` with pollen relevant and primary; `asthma_copd_overlap` to
   `NO2`, `PM25`, `O3` with pollen relevant; and `none_declared` to `PM25`, `NO2` with pollen not
   relevant — grounded in the finding that NO2 and O3 are the strongest gaseous drivers of COPD
   exacerbation while PM2.5 and aeroallergens dominate allergic asthma and rhinitis
   (`docs/research/FINDINGS.md`, SQ3 and cycle 7).
3. THE Service SHALL compute the Weighted_Focus as the weighting's ordered species restricted to those
   for which the response carries a Sub_Index, preserving the weighting's order.
4. THE Service SHALL report in `unavailableWeightedSpecies` every species the weighting names for which
   the response carries no Sub_Index, so that the absence of a weighted pollutant is explicit rather
   than invisible.
5. THE Service SHALL never substitute a proxy species for an unavailable weighted species and SHALL
   never infer a value for one.
6. WHERE a Condition_Weighting marks pollen relevant, THE Service SHALL include the pollen enrichment of
   Requirement 24 in the response when it is available, and SHALL declare it unavailable when it is not.
7. THE Service SHALL order the measurements of each site in the response by the Weighted_Focus order
   first and the configured species precedence second, so the pollutant that matters most to the user
   appears first.
8. THE Service SHALL compute the same Weighted_Focus for the same Condition and available species on
   every evaluation.
9. THE Service SHALL apply Condition_Weighting only to ordering, emphasis, and pollen relevance, and
   SHALL NOT let it alter any Sub_Index, Overall_AQI, Band, or corrected value.

### Requirement 22: Personal Thresholds and Escalation

**User Story:** As a sensitive user, I want to be told when conditions cross my own trigger point rather
than a national average one, so that a warning arrives while it is still actionable for me.

#### Acceptance Criteria

1. THE Service SHALL determine an effective escalation Sub_Index for the user as the first available of:
   a Personal_Threshold for the species, a Learned_Threshold for the species under Requirement 32, the
   Sensitivity_Level mapping of criterion 2, and the Orange_Band lower bound of 101. A Learned_Threshold
   SHALL rank BELOW a Personal_Threshold and SHALL NOT displace one: a threshold the user stated is
   their explicit instruction, and inference does not overrule an instruction.
2. THE Service SHALL define the default Sensitivity_Level mapping to escalation Sub_Index as `standard`
   to 101, `elevated` to 76, and `high` to 51, so that a respiratory user escalates at the Orange_Band
   or earlier rather than at the public `Unhealthy` threshold of 151
   (`docs/research/FINDINGS.md`, SQ1).
3. THE Service SHALL report a Threshold_Crossing for a site and species WHEN that species' Sub_Index at
   that site is greater than or equal to the effective escalation Sub_Index, treating equality as a
   crossing.
4. THE Service SHALL accept a Personal_Threshold as either a Sub_Index value from 1 to 500 or a
   concentration with its species and unit, and IF a supplied value falls outside those ranges or names
   a species the Breakpoint_Table does not define, THEN THE Service SHALL reject the profile write with
   HTTP status 400 naming the offending value.
5. WHERE a Personal_Threshold is expressed as a concentration, THE Service SHALL convert it to a
   Sub_Index using the same Breakpoint_Table the response uses, so that thresholds and reported values
   are comparable.
6. THE Service SHALL report `thresholdCrossed` as true WHEN one or more Threshold_Crossing entries
   exist, SHALL list every crossing with its site, species, Sub_Index, and the threshold it crossed, and
   SHALL report `thresholdSource` as one of `personal_threshold`, `sensitivity_level`, or
   `default_orange_band`.
7. FOR ALL Sub_Index and threshold pairs, THE Service SHALL report a crossing if and only if the
   Sub_Index is greater than or equal to the threshold.
8. THE Service SHALL never escalate on a value whose Confidence is `low` without reporting that
   Confidence alongside the crossing, so that a crossing driven by an uncalibrated or suspect reading is
   distinguishable from a confident one.
9. THE Service SHALL report the effective escalation Sub_Index in the response, so the basis of a
   crossing is reviewable.
10. THE Service SHALL compute the same crossings for the same Sub_Index values, profile, and
    configuration on every evaluation.

### Requirement 23: Inhaled Dose Estimation

**User Story:** As an active user, I want exposure expressed as what I actually breathed in, so that a
run in moderate air is not treated as equivalent to resting in the same air.

#### Acceptance Criteria

1. WHERE the User_Profile supplies the activity inputs — an activity level and a duration — THE Service
   SHALL compute an Inhaled_Dose per species as
   `dose_ug = corrected_concentration_ug_m3 * breathing_rate_m3_per_h * duration_h` and SHALL report it
   with its unit.
1a. WHERE the User_Profile supplies Routine_Entry records under Requirement 30 that fall within the
   window being reported, THE Service SHALL compute the Inhaled_Dose per routine window using that
   window's own activity level and duration against the concentration measured in that window, and SHALL
   report the per-window doses and their sum. A single whole-day activity figure attributes a morning
   run's breathing rate to the whole day, which is the error this criterion exists to remove.
1b. WHERE both a Routine_Entry set and the whole-day activity inputs are present, THE Service SHALL use
   the Routine_Entry records and SHALL report which basis it used, because reporting a dose without
   saying which basis produced it would make two different numbers indistinguishable.
2. THE Service SHALL accept an activity level from the set `rest`, `light`, `moderate`, or `vigorous`,
   and SHALL map each to a configurable breathing rate in m³/h whose defaults are 0.5, 1.0, 2.0, and
   3.2 respectively.
3. WHERE the User_Profile supplies no activity inputs, THE Service SHALL report `inhaledDose` as `null`
   and SHALL NOT assume an activity level, because an assumed dose is not a measured one.
4. THE Service SHALL compute the Inhaled_Dose from the corrected concentration, never from the reported
   value and never from a Sub_Index, and SHALL name in the Basis the concentration and breathing rate
   used.
5. FOR ALL inputs, THE Service SHALL produce an Inhaled_Dose that is non-negative, is zero when the
   duration is zero, and is strictly increasing in each of concentration, breathing rate, and duration
   while the others are held positive and constant.
6. THE Service SHALL accept a duration from greater than 0 up to the configured maximum, with default 24
   hours, and IF a supplied duration falls outside that range, THEN THE Service SHALL reject the profile
   write with HTTP status 400 naming the value and the range.
7. THE Service SHALL report the Inhaled_Dose alongside the Confidence of the concentration it was
   derived from, and SHALL cap the dose's Confidence at that value.
8. THE Service SHALL frame the Inhaled_Dose as an exposure quantity only and SHALL attach to it no
   clinical interpretation, consistent with Requirement 25.

### Requirement 24: Forecast and Pollen Enrichment

**User Story:** As an agent developer, I want next-day AQI and pollen attached to the response, so that I
can warn the user before conditions worsen rather than after.

#### Acceptance Criteria

1. THE Service SHALL retrieve the next-day AQI forecast and the current pollen outlook for a user's
   primary User_Location through the ForecastClient port, and SHALL attach them to the Serving_Response.
2. THE Service SHALL treat the ForecastClient as the sole source of forecast and pollen values and SHALL
   NOT derive, model, or extrapolate either from stored Readings, consistent with the finding that this
   service's forecasting value is fusion rather than prediction (`docs/research/FINDINGS.md`, cycle 8).
3. THE Service SHALL report with every forecast the provider identifier and the instant the value was
   retrieved.
4. IF the ForecastClient fails, times out after the configured timeout with default 2 seconds, or
   returns an unusable body, THEN THE Service SHALL serve the response without forecast values, SHALL
   set `degraded` to true, SHALL log one `warning` naming the failure kind, and SHALL NOT fail the
   request, because current-conditions advice remains useful without a forecast.
5. THE Service SHALL cache forecast and pollen values for the configured time-to-live, with default 60
   minutes, keyed by the rounded User_Location coordinates, and SHALL serve a cached value within its
   time-to-live rather than issuing a new request.
6. THE Service SHALL report the forecast `trend` as one of `rising`, `steady`, or `falling`, derived by
   comparing the forecast AQI to the current Overall_AQI against the configured band width, with default
   5 index points.
7. THE Service SHALL report the pollen outlook as a per-taxon category from the set `none`, `low`,
   `moderate`, `high`, or `very_high`, and SHALL include it only when the Condition_Weighting marks
   pollen relevant.
8. THE Service SHALL never pass a User_Profile field other than the rounded coordinates of the primary
   User_Location to the ForecastClient, and SHALL never pass the Condition or the user identity.
9. THE Service SHALL ship a deterministic local ForecastClient adapter for the offline suite, and SHALL
   treat provider selection and credentials as a deployment concern behind the same port.
10. THE Service SHALL resolve any ForecastClient credential only from the environment or a
    runtime-supplied path and SHALL never log it or include it in a response.

### Requirement 25: Non-Diagnostic Guardrails and Audit Trail

**User Story:** As the operator of a health-adjacent advisory service, I want the non-diagnostic
framing, the transparency of basis, and the audit trail enforced by the API rather than left to the
agent, so that the boundary that keeps this a general-wellness tool cannot be bypassed downstream.

#### Acceptance Criteria

1. FOR ALL responses carrying any Reading, Sub_Index, threshold, or dose value, THE Service SHALL include
   the Guardrail_Envelope: a non-empty `disclaimer`, an `advisoryScope` of exactly
   `exposure-reduction`, and a non-empty `emergencyGuidance`.
2. THE Service SHALL emit an `emergencyGuidance` value that directs the reader to emergency services for
   red-flag respiratory symptoms — severe breathlessness, a reliever that is not working, or blue lips —
   consistent with `docs/research/FINDINGS.md` cycle 6.
3. THE Service SHALL emit a `disclaimer` value that states the output is exposure guidance rather than
   medical advice and that defers to the clinician's action plan.
4. THE Service SHALL never emit a diagnosis, a statement that the user is experiencing an exacerbation
   or attack, a medication name, a dose, a dosing schedule, or an instruction to start, stop, or change
   any treatment.
5. FOR ALL responses, THE Service SHALL populate the `basis` member with the Breakpoint_Table
   identifier, the Calibration_Strategy per species, the RH source, the conversion source, the NowCast
   window and coverage, and the identifying fields of every contributing Reading, so that every reported
   value is independently reviewable (`docs/research/FINDINGS.md`, cycle 6 transparency requirement).
6. THE Service SHALL never present a Reading as reference-grade and SHALL always accompany a value with
   its Quality_Flag and Confidence per Requirement 13 criterion 10.
7. WHEN a Serving_Response is returned, THE Service SHALL append one Audit_Record naming the verified
   user identity, the instant, the route, the Breakpoint_Table identifier, the Calibration_Strategy set,
   whether a Threshold_Crossing was reported, and the identifying fields of the contributing Readings.
8. THE Service SHALL store in an Audit_Record no Condition, no Sensitivity_Level, no Personal_Threshold
   value, no User_Location coordinate, and no forecast or pollen value, so that the audit trail does not
   become a second copy of the health-adjacent data.
9. THE Audit_Record store SHALL be append-only within the Service, and THE Service SHALL retain
   Audit_Record entries for the configured period, with default 365 days.
10. THE Service SHALL take no autonomous action on a Threshold_Crossing beyond reporting it in the
    response and the audit trail — no notification, no message, and no external call.
11. FOR ALL Serving_Response documents, THE Service SHALL produce a body containing none of the
    configured forbidden-phrase patterns, whose defaults cover diagnosis and dosing language, and SHALL
    fail the request with HTTP status 500 and log one `error` if a response would violate this, because
    emitting a guardrail-violating body is worse than emitting none.
12. THE Service SHALL apply criteria 1 through 6 to every data-bearing endpoint of Requirement 19,
    including the history endpoint, not only the primary per-user endpoint.

### Requirement 26: Configuration

**User Story:** As a Service 2 operator, I want configuration validated completely before anything
starts, so that a misconfigured deployment fails immediately and visibly rather than serving wrong
numbers.

#### Acceptance Criteria

1. THE Config_Loader SHALL resolve configuration from a file and from environment variables, with the
   environment taking precedence over the file and the file over the built-in defaults, and SHALL log
   the resolved non-secret configuration once at startup.
2. THE Config_Loader SHALL validate every resolved value before the Service opens a listener, subscribes
   to a topic, or issues a store call.
3. THE Config_Loader SHALL NOT stop at the first invalid value: it SHALL complete validation of all
   values, write one single-line JSON message per invalid value naming the value and the constraint it
   violated, exit with a non-zero status, and never half-start.
4. IF a configuration key outside the recognized set is supplied, THEN THE Config_Loader SHALL reject it
   naming the key and SHALL list the recognized keys in the same category.
5. THE Config_Loader SHALL validate that the selected Breakpoint_Table identifier, every
   Calibration_Strategy name, the Condition_Weighting entries, the ReadingsStore, SensorRegistryStore,
   RawArchive, ProfileStore, ForecastClient, MeteorologyProvider, and Authenticator adapter names all
   resolve in their registries, naming the supplied value and the registered names when one does not.
6. THE Config_Loader SHALL validate that the Retention_Window, the NowCast_Window, the freshness window,
   the maximum history span, the quarantine retention, and the Audit_Record retention are each positive
   durations, and that the Retention_Window is greater than or equal to the maximum history span.
7. THE Config_Loader SHALL validate that every numeric bound pair has a minimum strictly less than its
   maximum, and that every count, radius, and limit is positive.
8. IF a secret-bearing value required by an enabled interface — the feed credential, a store endpoint
   credential, or a ForecastClient credential — cannot be resolved, THEN THE Config_Loader SHALL write
   one message per unresolved value naming the configuration value and never the secret itself, and
   SHALL exit non-zero.
9. IF the configuration file is unreadable or unparseable, THEN THE Config_Loader SHALL name the path and
   the failure kind, exit non-zero, and SHALL NOT fall back to defaults.
10. THE Config_Loader SHALL validate that at least one interface — push ingestion, pull ingestion, or the
    serving API — is enabled, and SHALL reject a configuration enabling none.
11. THE Config_Loader SHALL never write a resolved secret value to any log entry or error message.

### Requirement 27: Determinism and Time Injection

**User Story:** As a Service 2 developer, I want every time and ordering dependency injected, so that
processing and serving are reproducible and testable without waiting on a real clock.

#### Acceptance Criteria

1. THE Service SHALL obtain the current instant exclusively from the injected Clock, and no domain
   component SHALL read the wall clock directly.
2. FOR ALL identical inputs — the same stored Readings, the same User_Profile, the same forecast values,
   the same configuration, and the same Clock instant — THE Service SHALL produce a byte-identical
   Serving_Response.
3. FOR ALL identical inputs, THE Service SHALL produce identical Calibrated_Reading values from
   ingestion, including the corrected value, Quality_Flag, Confidence, Sub_Index, Band, and
   Driving_Pollutant.
4. THE Service SHALL apply a defined order to every iteration whose result reaches a response or a
   stored value — sites by the ordering of Requirement 20 criterion 7, species by the ordering of
   Requirement 21 criterion 7, Readings by ascending interval start — and SHALL rely on no set iteration
   order and no incidental mapping order.
5. THE Service SHALL use no source of randomness in any computation whose result reaches a stored
   Reading or a Serving_Response; WHERE randomness is needed for retry jitter, it SHALL be confined to
   transport scheduling and SHALL be injected.
6. THE Service SHALL derive every generated identifier that reaches stored data — the archive identifier
   in particular — from injected inputs rather than from an ambient source, so that a replay produces
   the same identifiers.
7. FOR ALL ingestion of one identical ordered payload sequence, THE Service SHALL produce an identical
   final ReadingsStore state regardless of whether it was ingested through the push path or the pull
   path.
8. THE Service SHALL express every internal instant as a timezone-aware UTC value and SHALL perform no
   local-time conversion outside presentation of a configured display timezone.

### Requirement 28: Project Scaffolding and Local Development

**User Story:** As a contributor, I want one documented command surface and a suite that passes offline,
so that I can develop and verify this service without cloud access.

#### Acceptance Criteria

1. THE Service SHALL live in `data-processing/` with its package at `data-processing/src/aqm_ingestion/`
   and its tests in a sibling `tests/` tree divided into `unit/`, `properties/`, and `integration/`.
2. THE Service SHALL declare its own `pyproject.toml` naming Python 3.12 and pinning every direct
   dependency to one exact version, with its resolved `uv.lock` committed.
3. THE Service SHALL import no module from another service directory in the monorepo, keeping its record
   contract copy independent per assumption A4.
4. THE Service SHALL expose its full test suite behind one documented command, which SHALL exit zero only
   if every test passes.
5. THE full test suite SHALL pass with no AWS credentials present and no network access beyond
   localhost, exercising the in-memory or local adapter for every port.
6. THE Service SHALL mark every check that requires a container engine so that the offline suite excludes
   it and a separate command runs it, and SHALL keep those checks free of any dependency on cloud
   credentials.
7. THE Service SHALL provide `just` recipes for the test, lint, typecheck, local-run, and local-stack
   commands, so that a contributor and a pipeline invoke the same code path.
8. THE Service SHALL provide a Docker Compose definition standing up the service together with a local
   MQTT broker, and a local store adapter sufficient to exercise the DynamoDB and S3 adapters without a
   cloud account.
9. THE Service SHALL commit no secret: every credential SHALL be supplied at runtime through the
   environment or a runtime-supplied path, and the repository SHALL ignore any generated credential
   tree.
10. THE Service SHALL provide one shared behavioral test suite per port, executed against every adapter
    of that port, per Requirements 14, 15, 16, and 17.
11. THE Service SHALL implement each correctness property named in the design document as exactly one
    property-based test running at least 100 examples.

### Requirement 29: Observability

**User Story:** As a Service 2 operator, I want structured logs and data-quality counters, so that I can
tell a healthy pipeline from a degrading one without reading raw payloads.

#### Acceptance Criteria

1. THE Service SHALL emit every log event as one single-line JSON object to stdout, configured once at
   startup, and SHALL never use `print` or emit non-JSON text to stdout.
2. THE Service SHALL include on every log event the ISO-8601 UTC instant, the level, the event name,
   and, where the event concerns one, the `SiteCode`, the `Species`, the record interval start, and the
   route.
3. THE Service SHALL use `DEBUG` for developer detail, `INFO` for operational events including ingestion
   summaries, `WARNING` for recoverable conditions including quarantine, extrapolated calibration, fault
   flags, dedup conflicts, and forecast degradation, `ERROR` for handled failures including archive
   write failure and poll failure, and `CRITICAL` for unrecoverable ones.
4. THE Service SHALL never log a resolved secret, a bearer credential, an Authenticator claim beyond the
   user identity, or any User_Profile field, per Requirement 17 criterion 9 and Requirement 18
   criterion 7.
5. WHEN an ingestion batch completes, THE Service SHALL emit one summary event naming the counts of
   records received, accepted, deduplicated, and quarantined, the count per Quality_Flag, and the
   elapsed duration.
6. THE Service SHALL expose counters for records ingested per transport, records quarantined per
   rejection reason, Readings per Quality_Flag, sites per fault category, dedup conflicts, forecast
   degradations, authentication rejections per category, and responses served per route.
7. THE Service SHALL expose the age of the most recent accepted Reading per site as a gauge, so that a
   silent upstream is detectable.
8. THE Service SHALL log every handled error, so that no failure is silent, and SHALL include the
   exception type and stack information in the log entry while never returning it to a client.
9. IF the configured log level is outside the recognized set, THEN THE Config_Loader SHALL reject it
   naming the supplied value and the permitted values.
10. THE Service SHALL treat metric transport and alarm definition as a deployment concern, exposing the
    counters and gauges through one internal interface that a deployment adapter publishes.

### Requirement 30: Medication List and Routine Schedule

**User Story:** As a user, I want the service to know what my clinician has already prescribed and what
my week actually looks like, so that guidance can name the inhaler I own and can be about the run I
actually do at seven in the morning.

#### Acceptance Criteria

1. THE Service SHALL model a Medication_Entry with exactly two fields — a display name and a
   Medication_Role — and SHALL reject a Medication_Entry carrying any other field, naming the offending
   field without echoing its value.
2. THE Service SHALL accept Medication_Role as exactly one of `reliever`, `preventer`, or `other`,
   because the role is what makes preparedness guidance expressible and is the only clinical property
   the guidance needs.
3. THE Service SHALL store NO dose, NO frequency, NO route of administration, NO prescriber, and NO
   administration schedule for any Medication_Entry. This is a structural prohibition rather than a
   validation rule: with nowhere in the stored shape to put a dose, medication-dosing advice has no
   input to draw on, which is the same technique Requirement 25 criterion 8 applies to the Audit_Record.
4. THE Service SHALL accept at most the configured number of Medication_Entry records per profile, with
   default 10, rejecting a write beyond it naming the limit.
5. THE Service SHALL model a Routine_Entry with exactly these fields: a set of days of the week, a start
   time of day, a duration in hours, an activity level from Requirement 23's set, and an optional
   User_Location name from Requirement 17 criterion 5's set.
6. THE Service SHALL accept at most the configured number of Routine_Entry records per profile, with
   default 14, rejecting a write beyond it naming the limit.
7. THE Service SHALL reject a Routine_Entry whose duration is not greater than zero, or whose duration
   would extend the window past the end of its start day, naming the field and the permitted range
   without echoing the value.
8. THE Service SHALL order Routine_Entry records deterministically by day of week then start time, so
   that a per-window dose report and any iteration reaching a response is reproducible.
9. THE Service SHALL treat a Medication_Entry name and a Routine_Entry as profile fields under
   Requirement 17 criterion 9, so neither appears in any log entry, any error message, or any
   Audit_Record, and the User_Profile's own rendering continues to reveal only the pseudonymous
   identity.
10. THE Service SHALL include Medication_Entry and Routine_Entry records in the erasure of Requirement 17
    criterion 8, and SHALL report them in the deletion receipt's counts.
11. THE Service SHALL NOT return a Medication_Entry to any caller other than the authenticated owner,
    and SHALL NOT include a Medication_Entry in the air-quality response body of Requirement 19; the
    advisor reads the profile explicitly when it needs one.

### Requirement 31: Symptom Log

**User Story:** As a user, I want to record how I felt each day, so that over time the advice is about
what actually affects me rather than about a population average.

#### Acceptance Criteria

1. THE Service SHALL access Symptom_Entry values exclusively through a SymptomLogStore port, keyed by the
   verified user identity.
2. THE Service SHALL model a Symptom_Entry with exactly these fields: the user identity, the calendar
   date the entry describes, a Symptom_Severity, a set of Symptom_Marker values, whether a reliever was
   used, an optional bounded Symptom_Note, and the instant the entry was recorded.
3. THE Service SHALL accept Symptom_Severity as an integer from 1 to 5 inclusive, and SHALL reject a
   value outside that range naming the field and the range.
4. THE Service SHALL accept Symptom_Marker values from a closed configured set whose defaults are
   `cough`, `wheeze`, `breathlessness`, `chest_tightness`, `nasal_congestion`, and `sleep_disturbance`,
   and SHALL reject an unrecognized marker naming the recognized set.
5. THE Service SHALL accept an optional Symptom_Note of at most a configured length, with default 280
   characters, and SHALL NOT accept any other free-text field.
6. THE Service SHALL treat the Symptom_Note as recorded FOR THE USER'S OWN RECALL ONLY: it SHALL NOT
   contribute to any computation, SHALL NOT be returned in the air-quality response body, and SHALL NOT
   be included in any value the Requirement 32 association reads. Prose is a clinical narrative and an
   injection vector; the structured fields are what the association is computed from, and keeping the
   note out of every derivation is what lets it exist at all.
7. THE Service SHALL store at most one Symptom_Entry per user per calendar date, and a write for a date
   that already has an entry SHALL replace it rather than accumulate, because two entries for one day
   would double-count that day in the association.
8. THE Service SHALL apply a configured retention window to Symptom_Entry records, with default 365
   days, and SHALL exclude an entry older than the window from every query, applied at query time
   against the injected Clock as Requirement 14 criterion 7 does for Readings.
9. THE Service SHALL remove every Symptom_Entry for a user on the erasure of Requirement 17 criterion 8,
   and SHALL report the number removed in the deletion receipt. Unlike the Audit_Record, a Symptom_Entry
   carries real clinical content, so erasure here deletes rather than de-identifies.
10. THE Service SHALL never write a Symptom_Severity, a Symptom_Marker, a reliever-use flag, or a
    Symptom_Note to any log entry, and SHALL log at most the pseudonymous identity and the date of an
    entry that was recorded.
11. THE Service SHALL accept a Symptom_Entry whose date is not in the future relative to the injected
    Clock, rejecting a future-dated entry naming the field.

### Requirement 32: Exposure–Symptom Association and Learned Thresholds

**User Story:** As a user with a diary, I want the service to notice which pollutant at which delay
actually tracks how I feel, so that my alerts fire at my own threshold rather than a default one.

#### Acceptance Criteria

1. THE Service SHALL compute an Exposure_Association between the Symptom_Severity series and the stored
   Sub_Index series per species, evaluated at each of a configured set of lags in whole days whose
   defaults are 0 and 3.
2. THE Service SHALL evaluate lag 3 by default because the evidence base places the gaseous-pollutant
   effect at the same day and the particulate effect about three days later
   (`docs/research/FINDINGS.md`, cycle 2). A same-day-only association would systematically miss the
   particulate signal, which is the signal that matters most for the PM-sensitive user this feature
   exists to serve.
3. THE Service SHALL report with every Exposure_Association the species, the lag, the number of paired
   observations it was computed from, and the association strength.
4. THE Service SHALL compute no Exposure_Association from fewer than a configured minimum of paired
   observations, with default 14, and SHALL report the shortfall rather than a value. A threshold learned
   from four days is worse than no learned threshold, because it will be stated with the same
   confidence and acted on.
5. THE Service SHALL bound the association's reach by the Readings retention window of Requirement 14,
   and WHERE the Symptom_Entry retention window is the longer of the two THE Service SHALL report the
   effective reach as the shorter, so a diary entry with no surviving exposure data to pair with is not
   counted as an observation.
6. THE Service SHALL derive a Learned_Threshold for a species only WHERE the Exposure_Association for
   that species meets both the minimum observation count of criterion 4 and a configured minimum
   association strength, and SHALL record the species, the Sub_Index, the lag, and the observation count
   the derivation rested on.
7. THE Service SHALL expose a Learned_Threshold through the Requirement 22 criterion 1 precedence as the
   Threshold_Source value `learned`, ranked below a Personal_Threshold, and SHALL report that source in
   the response exactly as it reports the others.
8. THE Service SHALL constrain a Learned_Threshold to the Sub_Index range 1 to 500 and SHALL NOT emit
   one below a configured floor, with default 51, because learning an escalation point inside the Good
   band would alert continuously and teach the user to ignore the alerts.
9. THE Service SHALL describe an Exposure_Association as an ASSOCIATION and SHALL NOT name it, type it,
   or report it as a cause. It is computed on one person's small sample; a personalized
   concentration-to-symptom response is evidence-backed as a phenomenon
   (`docs/research/FINDINGS.md`, cycle 4) but a specific individual's correlation is not a causal
   finding, and the distinction is the difference between an observation and a clinical claim.
10. THE Service SHALL compute the Exposure_Association as a pure function of the stored entries, the
    stored Readings, and the configuration, using no source of randomness and reading no wall clock, so
    that the same stored data yields the same association and the derivation is reproducible from an
    audit.
11. THE Service SHALL apply a defined order to every iteration the association performs — dates
    ascending, species by the configured precedence of Requirement 14 criterion 3 — so no incidental
    ordering reaches a stored or reported value.
12. THE Service SHALL NOT compute an Exposure_Association on the serving path of Requirement 19; the
    derivation reads a history and SHALL be performed on its own schedule, with the Learned_Threshold it
    produces stored for the serving path to read.
