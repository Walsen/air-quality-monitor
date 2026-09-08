# Requirements Document

## Introduction

The Sensor Simulator Service (Service 1 of three in this monorepo) emulates one virtual air-quality
sensor or a swarm of them, modelled on a public municipal air-quality network built on commercial
low-cost sensor units. It produces realistic, schema-accurate PM2.5 / NO2 / meteorology data so that
the Ingestion & Serving Service (Service 2) and the AI Advisor Agent (Service 3) can be developed,
tested, and demonstrated without physical hardware.

The service has one load-bearing constraint: **contract fidelity**. It reproduces the reference
network contract (`/ListSensors`, `/SensorData`) exactly, so Service 2 can later be pointed at a live
public air-quality feed with no code change (design decision D1 in
`docs/architecture/00-overview.md`). It offers both a push interface (MQTT, matching real device
behavior) and a pull interface (reference-contract-compatible REST), per design decision D2.

The simulated fleet is deployed over the **Cochabamba metropolitan area, Bolivia** by default, while
still emitting the reference network contract field names and value formats. Geography is a
configuration concern, not a contract concern.

Source material: `docs/architecture/01-sensor-simulator-service.md` (primary),
`docs/architecture/00-overview.md` (system context and decisions D1–D6), and
`docs/research/FINDINGS.md` (SQ0/SQ1/SQ5 — pollutant thresholds, humidity confounding).

### Scope

In scope: signal generation, swarm behavior, scenario injection, the two interfaces, deterministic
replay, configuration, and the simulator's own project scaffolding inside `sensor-simulator/`.

Out of scope (owned by Service 2 or later specs): ingestion, calibration/correction, AQI computation,
Timestream/DynamoDB/S3 storage, user profiles, the serving API, and the Bedrock agent. Also out of
scope, and deferred to a separate deployment spec: cloud deployment topology and runtime selection,
infrastructure-as-code, IoT Core device provisioning and authorization policies, and continuous
integration. This document constrains the simulator only up to the contract boundary those services
consume.

### Assumptions pending confirmation

These assumptions are recorded so the requirements are complete and testable; each is a decision the
reader should confirm or override.

- **A1 — Language/runtime:** Python 3.12 (chosen from the two options in the docs for its
  time-series/statistics libraries).
- **A2 — First milestone target:** local-only — a local MQTT broker and a locally hosted REST API.
  Cloud runtime selection is deferred to a separate deployment spec. The intended split is a
  stateless invocation-scoped runtime, such as AWS Lambda, for pull/REST and Backfill_Mode — which
  the seeded determinism of Requirement 11 and the mode-equivalence of Requirement 12 criterion 4
  make possible without carrying state between invocations — and a long-running container task, such
  as ECS Fargate, for real-time MQTT push, which needs persistent per-device TLS sessions and
  in-memory buffering.
- **A3 — Default geography:** the Cochabamba metropolitan area, Bolivia (the Kanata region) is the
  default profile. Geography is expressed as a swappable Geography_Profile so that the `reference`
  profile (a temperate sea-level reference profile) remains available for contract-parity checks
  against a live public air-quality feed. Additional regions can be added as further
  Geography_Profile entries without a contract change.
  Two consequences worth flagging: Cochabamba sits at roughly 2,560 m above sea level, so absolute
  barometric pressure is around 740 hPa rather than a sea-level value; and local time is
  `America/La_Paz` (UTC-4, no daylight saving), which is the frame for every diurnal window in this
  document.
- **A4 — Monorepo tooling:** this spec covers only `sensor-simulator/` scaffolding. A shared contract
  package, workspace-wide build orchestration, and CI are deferred to a later spec.
- **A5 — Index species semantics:** the numeric basis of the `NO2Index` / `PM25Index` species in the
  captured reference network contract is not documented, so the simulator derives them from a
  configurable breakpoint table whose default is the default ten-band index table with band
  identifiers 1 to 10. An alternative band table can be substituted through configuration without
  changing the record contract.
- **A6 — Retention-window record source:** whether wide `/SensorData` queries across the retention
  window are served from in-process retention, from recomputation from the Seed, or from a backing
  store is deferred to the deployment spec. This document requires only that every served record fall
  inside the retention window (Requirement 14 criterion 7) and deliberately mandates no storage
  mechanism.

## Glossary

- **Simulator**: the Sensor Simulator Service as a whole, comprising the components below.
- **Virtual_Sensor**: one simulated sensor unit with a stable identity (`SiteCode`, `DeviceCode`,
  location, `Borough`, `SiteClassification`, `PowerTag`) and its own signal state.
- **Swarm**: the configured collection of Virtual_Sensor instances managed by one Simulator process.
- **Swarm_Manager**: the component that instantiates, identifies, and ticks every Virtual_Sensor.
- **Signal_Engine**: the component that computes pollutant and meteorology values for each tick.
- **Regional_Field**: the single city-wide latent pollution signal shared by all Virtual_Sensor
  instances, used to produce spatial correlation.
- **Scenario_Engine**: the component that applies named scenarios to the Regional_Field and to
  individual Virtual_Sensor instances over scheduled time windows.
- **Sensor_Data_Record**: one JSON object matching the `/SensorData` contract (one Species, one site,
  one time interval).
- **Sensor_Metadata_Record**: one JSON object matching the `/ListSensors` contract.
- **Serializer**: the component that renders Sensor_Data_Record and Sensor_Metadata_Record values as
  JSON text.
- **Parser**: the component that reads `/SensorData` and `/ListSensors` JSON text into
  Sensor_Data_Record and Sensor_Metadata_Record values.
- **REST_API**: the pull-mode HTTP interface exposing `/ListSensors` and `/SensorData`.
- **MQTT_Publisher**: the push-mode component publishing Sensor_Data_Record payloads to an MQTT
  broker.
- **Config_Loader**: the component that reads and validates simulator configuration from file and
  environment.
- **Long_Running_Deployment**: a deployment in which one Simulator process stays resident across two
  or more consecutive Publish_Intervals, such as a container task. Contrast an invocation-scoped
  deployment, which computes a bounded set of Publish_Intervals and then exits.
- **Geography_Profile**: a named entry in a Geography_Profile registry, holding a self-contained set
  of geographic defaults — bounding box, local timezone, area name list, elevation, `SiteCode`
  prefix, `SponsorName`, `SensorContract`, and meteorology ranges, the last including a relative
  humidity range whose default is 15 to 90 percent under the `cochabamba` profile. Two profiles ship
  built in: `cochabamba` (default) and `reference` (a temperate sea-level reference profile).
  Additional named profiles can be declared in configuration or registered in the profile registry
  without a source change.
- **Kanata_Region**: the Cochabamba metropolitan region, comprising the municipalities Cochabamba,
  Sacaba, Quillacollo, Tiquipaya, Colcapirhua, Vinto, and Sipe Sipe. These municipality names populate
  the `Borough` field under the `cochabamba` profile.
- **Species**: the measurand identifier, one of `NO2`, `PM25`, `NO2Index`, `PM25Index`.
- **Site_Classification**: the site type, one of `Roadside`, `Urban Background`, `Suburban`.
- **RH**: relative humidity, expressed as a percentage.
- **Dry_Concentration**: the PM2.5 mass concentration the Signal_Engine computes before applying the
  humidity growth artifact.
- **Reported_Concentration**: the PM2.5 value the Simulator emits, after the humidity growth artifact
  and sensor artifacts are applied.
- **Tick**: one native 1-minute sampling step of a Virtual_Sensor. The Tick interval is a
  configuration value the Config_Loader validates alongside the Publish_Interval.
- **Publish_Interval**: the interval at which Tick values are averaged and emitted, default 1 hour.
- **Seed**: the integer that initializes every pseudo-random stream in the Simulator.
- **Backfill_Mode**: the time mode that generates a historical range of records faster than real time.
- **Ratification_Lag**: the configured age at which a record's `RatificationStatus` changes from `P`
  to `R`, default 90 days.

## Requirements

### Requirement 1: Sensor Metadata Contract (`/ListSensors`)

**User Story:** As a Service 2 developer, I want the simulator to emit sensor metadata in the exact
reference network contract `/ListSensors` shape, so that I can build ingestion against a contract
that also works with a live public air-quality feed.

#### Acceptance Criteria

1. FOR ALL Sensor_Metadata_Record values, THE Simulator SHALL emit exactly these twenty field names,
   in this order, with no additional field and no omitted field, and with every field key present
   even when its value is `null`: `SiteCode`, `SiteName`, `DeviceCode`, `InstallationCode`,
   `Facility`, `Location`, `Latitude`, `Longitude`, `Borough`, `SiteClassification`,
   `SensorHeightAboveGround`, `DistanceToKerb`, `SponsorName`, `SiteLocationType`, `StartDate`,
   `EndDate`, `PowerTag`, `SiteDescription`, `SitePhotoURL`, `SensorContract`.
2. THE Simulator SHALL emit `Latitude` and `Longitude` as JSON strings holding a signed decimal
   number with exactly 7 digits after the decimal point, with `Latitude` within -90.0000000 to
   90.0000000 and `Longitude` within -180.0000000 to 180.0000000, and SHALL emit
   `SensorHeightAboveGround` and `DistanceToKerb` as JSON numbers rounded to 2 decimal places.
3. THE Simulator SHALL emit `Location` as a GeoJSON object with `type` `"Feature"` and
   `geometry.type` `"Point"`, whose `coordinates` array holds exactly two elements that are
   character-identical JSON strings to the sibling `Latitude` value then the sibling `Longitude`
   value.
4. THE Simulator SHALL emit `StartDate` as an ISO-8601 UTC timestamp at whole-second precision ending
   in `Z` and no later than the current simulated time, and SHALL emit `EndDate` as JSON `null` for a
   Virtual_Sensor that is currently active.
5. THE Simulator SHALL emit `PowerTag` as exactly one of the case-sensitive values `Mains` or `Solar`,
   and `SensorContract` as the Geography_Profile contract name, with default `Cellular-BO` under the
   `cochabamba` profile and `Cellular-REF` under the `reference` profile.
6. THE Simulator SHALL emit `Borough` as a value character-identical to one of the Geography_Profile
   area names, which under the `cochabamba` profile are the seven Kanata_Region municipality names.
7. THE Simulator SHALL emit `SiteCode` as the Geography_Profile prefix followed by a zero-padded
   four-digit decimal number in the range 0001 to 9999, with default prefix `CB` under the
   `cochabamba` profile and `RF` under the `reference` profile.
8. WHEN a client requests `/ListSensors` with no query parameters, THE REST_API SHALL return a JSON
   array containing exactly one Sensor_Metadata_Record for every Virtual_Sensor in the Swarm, ordered
   by ascending `SiteCode`, within 2 seconds for a Swarm of up to 500 Virtual_Sensor instances.
9. WHEN a client requests `/ListSensors` with any combination of `SiteCode`, `Borough`, `Sponsor`, or
   `Facility`, THE REST_API SHALL match each supplied value against the whole corresponding field
   value (`Sponsor` against `SponsorName`) case-insensitively and SHALL return only the
   Sensor_Metadata_Record values matching every supplied parameter.
10. WHEN a client requests `/ListSensors` with `Latitude`, `Longitude`, and `RadiusKM`, THE REST_API
    SHALL return only the Sensor_Metadata_Record values whose great-circle distance in kilometres
    from the supplied point is less than or equal to `RadiusKM`, treating the boundary distance as a
    match.
11. IF a client supplies `RadiusKM` without both `Latitude` and `Longitude`, THEN THE REST_API SHALL
    respond with HTTP status 400 and a JSON body naming the missing parameters, and SHALL exclude all
    Sensor_Metadata_Record values from the response body.
12. IF a Virtual_Sensor is configured as no longer active, THEN THE Simulator SHALL emit `EndDate` as
    an ISO-8601 UTC timestamp at whole-second precision ending in `Z` and no earlier than that
    Virtual_Sensor's `StartDate`.
13. IF a client supplies `Latitude`, `Longitude`, or `RadiusKM` with a non-numeric value, or with a
    value outside -90 to 90 for `Latitude`, -180 to 180 for `Longitude`, or greater than 0 and less
    than or equal to 500 for `RadiusKM`, THEN THE REST_API SHALL respond with HTTP status 400 and a
    JSON body naming each offending parameter and its permitted range, and SHALL exclude all
    Sensor_Metadata_Record values from the response body.
14. WHEN a client requests `/ListSensors` with a query parameter name outside the set `SiteCode`,
    `Borough`, `Sponsor`, `Facility`, `Latitude`, `Longitude`, and `RadiusKM`, THE REST_API SHALL
    ignore that parameter and SHALL apply only the recognized parameters as filters.

### Requirement 2: Measurement Contract (`/SensorData`)

**User Story:** As a Service 2 developer, I want measurements in the exact reference network contract
`/SensorData` shape, so that my ingestion, validation, and storage code needs no change when the real
feed replaces the simulator.

#### Acceptance Criteria

1. THE Simulator SHALL represent each Sensor_Data_Record with exactly these nine field names and no
   additional fields, every one of them present in every emitted record: `Species`, `Source`,
   `Units`, `SiteCode`, `DateTime`, `Duration`, `ScaledValue`, `RatificationStatus`,
   `SensorContract`.
2. THE Simulator SHALL emit `Species` as one of `NO2`, `PM25`, `NO2Index`, or `PM25Index`, matched
   case-sensitively, and SHALL emit `SiteCode` and `SensorContract` values identical to those of the
   Sensor_Metadata_Record of the emitting Virtual_Sensor.
3. THE Simulator SHALL emit `Source` as `Measurement` for all four `Species` values, and SHALL emit
   `Units` as `ug.m-3` for `Species` values `NO2` and `PM25`.
4. THE Simulator SHALL emit `Units` for `Species` values `NO2Index` and `PM25Index` as the configured
   index unit label, with default value `index`.
5. THE Simulator SHALL emit `DateTime` as an ISO-8601 UTC timestamp with whole-second precision, no
   fractional-second component, and a trailing `Z`, whose value is the start instant of the
   Publish_Interval the record covers, so that the record covers the half-open interval from
   `DateTime` inclusive to `DateTime` plus Publish_Interval exclusive.
6. WHERE the Publish_Interval is 1 hour, THE Simulator SHALL emit `Duration` as `PT1H` and SHALL emit
   `DateTime` with zero minute and zero second components.
7. THE Simulator SHALL emit `ScaledValue` as a JSON number, never a JSON string, rounded to exactly 2
   decimal places using half-away-from-zero rounding.
8. WHILE the age of a Sensor_Data_Record, computed as current simulated time minus its `DateTime`, is
   less than the Ratification_Lag, with default 90 days, THE Simulator SHALL emit
   `RatificationStatus` as `P`.
9. WHILE the age of a Sensor_Data_Record, computed as current simulated time minus its `DateTime`, is
   greater than or equal to the Ratification_Lag, THE Simulator SHALL emit `RatificationStatus` as
   `R`.
10. WHEN a client requests `/SensorData` with no `startTime` and no `endTime`, THE REST_API SHALL
    return the records for the most recently completed Publish_Interval only, being the interval
    whose end instant is at or before current simulated time.
11. WHEN a client requests `/SensorData` with both `startTime` and `endTime` as ISO-8601 UTC
    timestamps, THE REST_API SHALL return the records whose `DateTime` is greater than or equal to
    `startTime`, less than `endTime`, and inside the configured retention window, ordered by
    ascending `DateTime`, returning an empty JSON array when `startTime` equals `endTime`.
12. WHEN a client requests `/SensorData` with `Species`, `SiteCode`, `Borough`, `Sponsor`, or
    `Facility`, THE REST_API SHALL return only the records whose corresponding value is
    case-sensitively equal to every supplied parameter value.
13. WHEN a client requests `/SensorData` with `Latitude`, `Longitude`, and `RadiusKM`, THE REST_API
    SHALL return only records from Virtual_Sensor instances whose great-circle distance from the
    supplied point is less than or equal to `RadiusKM`.
14. IF a client supplies a `startTime` later than the supplied `endTime`, THEN THE REST_API SHALL
    respond with HTTP status 400 and a JSON body naming the invalid time range, SHALL exclude every
    record from the response body, and SHALL continue generating and retaining records unchanged.
15. IF a client supplies a `Species` value outside the four permitted values, matched
    case-sensitively, THEN THE REST_API SHALL respond with HTTP status 400 and a JSON body listing
    the permitted values, and SHALL exclude every record from the response body.
16. IF a client supplies `startTime` without `endTime` or `endTime` without `startTime`, THEN THE
    REST_API SHALL respond with HTTP status 400 and a JSON body naming the missing parameter, and
    SHALL exclude every record from the response body.
17. IF a client supplies a `startTime` or `endTime` value that is not a parseable ISO-8601 UTC
    timestamp, THEN THE REST_API SHALL respond with HTTP status 400 and a JSON body naming the
    offending parameter, and SHALL exclude every record from the response body.
18. IF a client supplies `RadiusKM` without both `Latitude` and `Longitude`, or supplies a `RadiusKM`
    value outside the range 0.01 to 500 kilometres, THEN THE REST_API SHALL respond with HTTP status
    400 and a JSON body naming the offending parameter, and SHALL exclude every record from the
    response body.

### Requirement 3: Canonical Record Serialization and Parsing

**User Story:** As a developer of any of the three services, I want a single canonical record
representation with matching serializer and parser, so that push mode and pull mode emit identical
data and contract drift is caught by tests.

#### Acceptance Criteria

1. THE Serializer SHALL render a Sensor_Data_Record as JSON text containing exactly the nine field
   names listed in Requirement 2 criterion 1, and a Sensor_Metadata_Record as JSON text containing
   exactly the twenty field names listed in Requirement 1 criterion 1, with no additional fields,
   with fields emitted in the order declared in those criteria, and with the JSON value type (string,
   number, object, or null) that Requirements 1 and 2 specify for each field.
2. THE Parser SHALL read `/SensorData` and `/ListSensors` JSON text into Sensor_Data_Record and
   Sensor_Metadata_Record values, accepting either a single JSON object or a JSON array of 0 to
   100,000 objects, and SHALL preserve the order in which records appear in the array.
3. FOR ALL Sensor_Data_Record values whose `Species` is one of the four permitted values and whose
   `DateTime` falls within the configured retention window, over at least 100 generated values
   covering all four `Species` values, parsing the Serializer output SHALL produce a record in which
   every field equals the corresponding field of the original record, including fields whose value is
   null, with `ScaledValue` equal at 2 decimal places (round-trip property).
4. FOR ALL Sensor_Metadata_Record values, over at least 100 generated values covering both `PowerTag`
   values and all three Site_Classification values, parsing the Serializer output SHALL produce a
   record in which every field equals the corresponding field of the original record, including a
   null `EndDate`, with `Latitude` and `Longitude` equal character for character at 7 decimal places
   (round-trip property).
5. IF the Parser receives JSON text in which any field required by Requirement 1 criterion 1 or
   Requirement 2 criterion 1 is absent, or in which a field name outside those two field lists is
   present, THEN THE Parser SHALL raise a validation error naming the offending field and indicating
   whether that field was missing or unknown, and SHALL produce no record value.
6. WHEN the same Tick data is emitted through the MQTT_Publisher and through the REST_API, THE
   Simulator SHALL render the corresponding Sensor_Data_Record through the same Serializer and SHALL
   produce byte-identical JSON text, identical in field order, field values, and numeric formatting,
   for the record sharing the same `SiteCode`, `Species`, and `DateTime`.
7. IF the Parser receives JSON text in which a `Species` value falls outside the four permitted
   values, or in which a field value has a JSON type other than the type Requirements 1 and 2 specify
   for that field, THEN THE Parser SHALL raise a validation error naming the offending field together
   with the permitted values or the required type, and SHALL produce no record value.
8. IF the Parser receives text that is not well-formed JSON, or text larger than the configured
   maximum payload size with default 10 megabytes, THEN THE Parser SHALL raise a validation error
   indicating that the input could not be read as JSON, and SHALL produce no record value.
9. FOR ALL JSON text the Parser accepts without error, re-rendering the parsed record through the
   Serializer SHALL produce JSON text byte-identical to the accepted text (serialize-parse-serialize
   stability property).

### Requirement 4: Pollutant Signal Realism

**User Story:** As an agent developer, I want pollutant time series that behave like real urban air
quality rather than white noise, so that the advice logic I build is exercised by plausible patterns.

#### Acceptance Criteria

1. THE Signal_Engine SHALL compute NO2 as a base level plus a non-negative diurnal traffic component
   that reaches its daily maximum inside the configured morning and evening rush-hour windows, with
   defaults 07:00-09:00 and 17:00-19:00 in the Geography_Profile local timezone, which is
   `America/La_Paz` (UTC-4, no daylight saving) under the `cochabamba` profile, and that stays at or
   below 30 percent of that daily maximum during the 00:00-04:00 local-time window.
2. FOR ALL Virtual_Sensor instances with `SiteClassification` `Roadside`, over any simulated 72-hour
   window under the `clean` scenario, THE Signal_Engine SHALL produce a mean rush-hour-window NO2
   value between 1.3 and 4.0 times the mean 00:00-04:00 NO2 value, where each mean is the arithmetic
   mean of the emitted hourly `ScaledValue` values whose `DateTime` converted to the
   Geography_Profile local timezone starts inside the named window.
3. WHERE the Swarm contains at least one Virtual_Sensor of each Site_Classification, THE
   Signal_Engine SHALL scale the NO2 traffic component by Site_Classification so that, averaged over
   any simulated 72-hour window under the `clean` scenario, mean NO2 for `Roadside` sites is at least
   1.25 times mean NO2 for `Urban Background` sites, and mean NO2 for `Urban Background` sites is at
   least 1.15 times mean NO2 for `Suburban` sites.
4. THE Signal_Engine SHALL compute PM2.5 as a Regional_Field baseline that changes by at most
   5 µg/m³ between consecutive simulated hours, plus a per-site local component, such that the
   relative diurnal amplitude of PM2.5 is at most 0.5 times the relative diurnal amplitude of NO2,
   where relative diurnal amplitude is the maximum minus the minimum of the 24-hour mean-of-hour
   profile over the evaluated window divided by the mean over that window.
5. FOR ALL simulated 72-hour windows under the `clean` scenario, THE Signal_Engine SHALL keep every
   emitted hourly Reported_Concentration PM2.5 value within the inclusive range 3 to 35 µg/m³ and
   every emitted hourly NO2 value within the inclusive range 5 to 90 µg/m³, evaluated over the
   Publish_Interval records actually emitted and excluding intervals suppressed by a scheduled
   dropout.
6. WHILE a pollution episode scenario is active, THE Signal_Engine SHALL produce at least one PM2.5
   hourly value strictly above 35 µg/m³ and SHALL keep every PM2.5 hourly value at or below the
   configured episode peak, with default peak 120 µg/m³.
7. THE Signal_Engine SHALL constrain every emitted pollutant `ScaledValue` to be greater than or
   equal to 0 after measurement noise, drift, and the humidity growth artifact are applied and after
   the 2-decimal rounding of Requirement 2.
8. THE Signal_Engine SHALL apply a seasonal PM2.5 multiplier from the Geography_Profile to the
   Regional_Field baseline, which under the `cochabamba` profile is at least 1.5 and at most 3.0
   across the dry-season burning months of 1 July through 31 October local time, relative to the
   wet-season reference months of 1 December through 31 March local time, compared as calendar-month
   mean baseline values.
9. FOR ALL Virtual_Sensor instances, over any simulated 72-hour window under the `clean` scenario,
   THE Signal_Engine SHALL produce hourly NO2 and hourly PM2.5 series whose lag-1 autocorrelation is
   at least 0.6, so that the emitted series is distinguishable from an independent random series.
10. IF the loaded signal configuration contains a rush-hour window whose start is not earlier than
    its end, a rush-hour window falling outside 00:00 to 23:59 local time, an episode peak PM2.5
    value at or below 35 µg/m³ or above 500 µg/m³, or a seasonal multiplier below 1.0 or above 5.0,
    THEN THE Config_Loader SHALL reject the configuration with an error naming the offending
    parameter and its permitted range, and THE Simulator SHALL not begin signal generation.
11. IF a computed pollutant value falls outside the range 0 to the configured maximum plausible
    concentration, with defaults 500 µg/m³ for PM2.5 and 400 µg/m³ for NO2, THEN THE Signal_Engine
    SHALL clamp the emitted `ScaledValue` to the nearest bound and SHALL record the clamping event
    with the affected `SiteCode`, `Species`, and simulated timestamp in the diagnostic output.

### Requirement 5: Meteorology Signals and Humidity-Driven PM Overestimation

**User Story:** As a Service 2 developer, I want humidity-inflated PM2.5 readings, so that the
calibration and correction step I build has a real artifact to correct.

#### Acceptance Criteria

1. FOR ALL simulated days, THE Signal_Engine SHALL place the maximum hourly temperature of each
   Virtual_Sensor at an hour inside the configured afternoon window, with default 14:00-16:00
   inclusive in the Geography_Profile local timezone, which is `America/La_Paz` under the
   `cochabamba` profile.
2. FOR ALL Ticks, THE Signal_Engine SHALL compute temperature within the inclusive Geography_Profile
   temperature range, with default 5 to 30 °C under the `cochabamba` profile, and SHALL clamp any
   computed value outside that range to the nearer bound.
3. FOR ALL simulated days, THE Signal_Engine SHALL produce a difference between the daily minimum and
   the daily maximum hourly temperature of at least 10 °C and at most the configured maximum diurnal
   range, with default 20 °C, matching the wide diurnal range of a high-altitude semi-arid valley.
4. FOR ALL Virtual_Sensor instances, over any simulated 72-hour window, THE Signal_Engine SHALL
   produce hourly RH and hourly temperature series whose Pearson correlation is less than or equal to
   -0.5.
5. FOR ALL Ticks, THE Signal_Engine SHALL compute absolute barometric pressure within the inclusive
   Geography_Profile pressure range, with default 730 to 755 hPa under the `cochabamba` profile,
   reflecting an elevation of approximately 2,560 m, and 980 to 1040 hPa under the `reference`
   profile, and SHALL clamp any computed value outside that range to the nearer bound.
6. THE Simulator SHALL express every emitted barometric pressure value as absolute station pressure
   at the Virtual_Sensor elevation rather than as a sea-level-normalized value, so that the value
   matches what a device barometer reads at the site elevation.
7. FOR ALL Ticks, THE Signal_Engine SHALL compute PM2.5 Reported_Concentration as Dry_Concentration
   multiplied by a hygroscopic growth factor determined solely by that Tick's RH value over the RH
   domain 0 to 100 percent, where the factor is monotonically non-decreasing in RH, is strictly
   increasing for RH values greater than 50 percent and less than 85 percent, and never exceeds the
   configured maximum growth factor, with default 2.0.
8. WHILE RH is less than or equal to 50 percent, THE Signal_Engine SHALL apply a growth factor of
   exactly 1.0, so that Reported_Concentration equals Dry_Concentration within the 2-decimal rounding
   of the emitted `ScaledValue`.
9. WHILE RH is greater than or equal to 85 percent, THE Signal_Engine SHALL apply a growth factor of
   at least 1.5 and at most the configured maximum growth factor, with default 2.0.
10. FOR ALL Ticks, THE Simulator SHALL record the Dry_Concentration, the Reported_Concentration, the
    RH value, and the applied growth factor in its own diagnostic output, keyed by `SiteCode` and the
    Tick timestamp, so that a test can measure the correction Service 2 is expected to recover.
11. FOR ALL Sensor_Data_Record values emitted over the MQTT_Publisher and the REST_API, THE Simulator
    SHALL exclude Dry_Concentration and the applied growth factor, so that the serialized record
    carries only the field names listed in Requirement 2, criterion 1.
12. FOR ALL pairs of RH values in 0 to 100 percent where the first is less than or equal to the
    second, THE Signal_Engine SHALL produce, for one identical Dry_Concentration, a
    Reported_Concentration for the first that is less than or equal to the Reported_Concentration for
    the second (humidity-monotonicity property).
13. FOR ALL Ticks, THE Signal_Engine SHALL compute RH within the inclusive Geography_Profile RH
    range, with default 15 to 90 percent under the `cochabamba` profile, and SHALL keep every emitted
    and recorded RH value within 0 to 100 percent.
14. IF the configuration supplies a Geography_Profile temperature, RH, or pressure range whose
    minimum is greater than or equal to its maximum, an RH range outside 0 to 100 percent, or a
    maximum growth factor less than 1.0, THEN THE Config_Loader SHALL reject the configuration with
    an error naming the offending range and its bounds, and THE Simulator SHALL generate no signals.

### Requirement 6: Index Species Derivation

**User Story:** As a Service 2 developer, I want the simulator to emit the index species the real feed
emits, so that my ingestion handles all four Species values.

#### Acceptance Criteria

1. WHEN the Simulator emits a `PM25` Sensor_Data_Record for a Virtual_Sensor and Publish_Interval,
   THE Simulator SHALL emit exactly one `PM25Index` Sensor_Data_Record for that same Virtual_Sensor
   and Publish_Interval, carrying identical `SiteCode`, `DateTime`, `Duration`,
   `RatificationStatus`, and `SensorContract` values.
2. WHEN the Simulator emits an `NO2` Sensor_Data_Record for a Virtual_Sensor and Publish_Interval,
   THE Simulator SHALL emit exactly one `NO2Index` Sensor_Data_Record for that same Virtual_Sensor
   and Publish_Interval, carrying identical `SiteCode`, `DateTime`, `Duration`,
   `RatificationStatus`, and `SensorContract` values.
3. THE Simulator SHALL derive each index `ScaledValue` from the Reported_Concentration `ScaledValue`
   of its corresponding concentration record for the same Publish_Interval, rather than from
   individual Tick values, using the configured per-Species breakpoint table whose default is the
   default ten-band index table with band identifiers 1 to 10 for PM2.5 and NO2, and SHALL treat each
   band's lower concentration bound as inclusive and its upper concentration bound as exclusive, so
   that a concentration equal to a band boundary yields the higher band.
4. FOR ALL pairs of concentration values within 0 to 1000 µg/m³ where the first is less than or equal
   to the second, THE Simulator SHALL derive, from the same configured breakpoint table, an index
   value for the first that is less than or equal to the index value for the second (monotonicity
   property), and FOR ALL repeated derivations from the same concentration value and the same table,
   THE Simulator SHALL derive the same index value.
5. THE Simulator SHALL emit every index `ScaledValue` as a JSON number whose value is an integer band
   identifier within the inclusive bounds of the configured breakpoint table, with default bounds 1
   and 10.
6. IF a Reported_Concentration is greater than or equal to the upper concentration bound of the
   highest band in the configured breakpoint table, as occurs under the `pollution_episode` and
   `wildfire_smoke` scenarios, THEN THE Simulator SHALL emit the highest index value defined by that
   table and SHALL record the clamping event in its diagnostic output.
7. IF the configured breakpoint table defines fewer than one band for either PM2.5 or NO2, or defines
   bands whose concentration ranges overlap or leave a gap between 0 µg/m³ and the highest upper
   bound, or defines index values that do not strictly increase with concentration, THEN THE
   Config_Loader SHALL reject the configuration with an error naming the offending band and SHALL NOT
   begin signal generation.
8. WHERE a dropout or a fault suppresses the concentration record for a Virtual_Sensor and
   Publish_Interval, THE Simulator SHALL omit the corresponding index record for that Virtual_Sensor
   and Publish_Interval.

### Requirement 7: Sensor Artifacts and Fault Modes

**User Story:** As a Service 2 developer, I want realistic sensor imperfection, so that my data-quality
flagging and dropout handling are exercised before real hardware arrives.

#### Acceptance Criteria

1. THE Signal_Engine SHALL add zero-mean Gaussian measurement noise, drawn from the Virtual_Sensor
   pseudo-random stream, to each pollutant Tick value before Publish_Interval averaging, using a
   per-Species standard deviation configurable from 0.0 to 10.0 µg/m³ with defaults 1.5 µg/m³ for
   `PM25` and 3.0 µg/m³ for `NO2`, and SHALL clamp the resulting Tick value to be greater than or
   equal to 0.
2. THE Signal_Engine SHALL apply a per-Virtual_Sensor drift term whose sign is fixed for a given Seed
   and `SiteCode` and whose magnitude grows linearly with simulated elapsed time at the configured
   drift rate, configurable from 0.0 to 5.0 µg/m³ per 30 simulated days with default 0.5 µg/m³ per 30
   simulated days, and SHALL cap the accumulated drift magnitude at the configured maximum with
   default 5.0 µg/m³.
3. WHILE a dropout window is active for a Virtual_Sensor, THE Simulator SHALL omit every
   Sensor_Data_Record for that Virtual_Sensor and every Publish_Interval that starts within the
   window, for all four Species, SHALL emit no placeholder or null-valued record in their place, and
   SHALL continue emitting housekeeping telemetry for that Virtual_Sensor.
4. WHILE a stuck-value fault window is active for a Virtual_Sensor, THE Simulator SHALL emit, for each
   affected Species in every Publish_Interval, the same `ScaledValue` that Species held in the last
   Publish_Interval completed before the window started, and SHALL derive the corresponding index
   Species value from that repeated value.
5. WHEN a stuck-value fault window or dropout window ends, THE Simulator SHALL resume emitting values
   computed from the Signal_Engine for the next completed Publish_Interval.
6. WHEN a Publish_Interval completes, THE Simulator SHALL emit exactly one housekeeping telemetry
   record per Virtual_Sensor containing a signal-quality value as an integer from 0 to 100 and an
   active-fault list naming zero or more of stuck value, drift, and dropout, where an empty list
   indicates no active fault.
7. WHERE `PowerTag` is `Solar`, THE Simulator SHALL include in each housekeeping telemetry record for
   that Virtual_Sensor a battery state of charge as an integer from 0 to 100 percent.
8. WHERE `PowerTag` is `Mains`, THE Simulator SHALL exclude battery state of charge from the
   housekeeping telemetry record for that Virtual_Sensor.
9. THE Simulator SHALL publish housekeeping telemetry to an MQTT topic distinct from the measurement
   topic of Requirement 13, using a payload that contains none of the Sensor_Data_Record fields
   listed in Requirement 2, and THE REST_API SHALL exclude housekeeping telemetry from every
   `/SensorData` response, so that the measurement contract stays unchanged.
10. IF the configured noise standard deviation, drift rate, or maximum drift magnitude falls outside
    its permitted range, or a fault window end precedes its start, THEN THE Config_Loader SHALL
    reject the configuration with an error naming the offending parameter and its permitted range,
    and THE Simulator SHALL emit no Sensor_Data_Record.

### Requirement 8: Swarm Identity and Configurable Geography

**User Story:** As a demo operator, I want to run N virtual sensors with stable identities across the
Cochabamba metropolitan area or any configured city, so that Service 2 sees a realistic fleet without
the geography being hard-coded.

#### Acceptance Criteria

1. THE Swarm_Manager SHALL instantiate exactly the configured number of Virtual_Sensor instances for
   any configured integer value from 1 to 500 inclusive within a single Simulator process, with
   default 50, and SHALL complete instantiation before the first Tick is computed.
2. FOR ALL pairs of distinct Virtual_Sensor instances in the Swarm, THE Swarm_Manager SHALL assign
   `SiteCode` values that differ and `DeviceCode` values that differ, with each generated `SiteCode`
   matching the Geography_Profile prefix and zero-padded four-digit form defined in Requirement 1.
3. WHEN the Simulator restarts with unchanged configuration and Seed, THE Swarm_Manager SHALL
   reproduce, for every Virtual_Sensor, identical values for `SiteCode`, `SiteName`, `DeviceCode`,
   `InstallationCode`, `Latitude`, `Longitude`, `Borough`, `SiteClassification`, `PowerTag`,
   `SensorHeightAboveGround`, and `DistanceToKerb`.
4. FOR ALL Virtual_Sensor instances, THE Swarm_Manager SHALL assign a `Latitude` within the
   Geography_Profile bounding box latitude bounds inclusive and a `Longitude` within its longitude
   bounds inclusive, where the default box is the Kanata_Region box spanning latitude -17.50 to
   -17.29 and longitude -66.40 to -66.02, expressed at the 7-decimal precision of Requirement 1.
5. THE Swarm_Manager SHALL assign each Virtual_Sensor coordinates that fall inside exactly one
   Geography_Profile sub-area and SHALL set `Borough` to that sub-area's name, which under the
   `cochabamba` profile is one of the seven Kanata_Region municipality names.
6. THE Swarm_Manager SHALL assign each Virtual_Sensor a `SiteClassification` from the configured
   classification mix, with default proportions 30 percent `Roadside`, 50 percent `Urban Background`,
   and 20 percent `Suburban`, such that for a configured Swarm size of 100 or more the observed share
   of each classification is within 5 percentage points of its configured proportion.
7. THE Swarm_Manager SHALL assign each Virtual_Sensor a `SensorHeightAboveGround` from 2.0 to 3.0
   metres inclusive and a `DistanceToKerb` from 0.5 to 30.0 metres inclusive.
8. WHERE a site list is supplied in configuration, THE Swarm_Manager SHALL instantiate one
   Virtual_Sensor per supplied entry using the supplied identity and location values in place of
   generated ones, and SHALL set the Swarm size to the number of supplied entries.
9. FOR ALL registered Geography_Profile values, THE Simulator SHALL emit the identical set of
   Sensor_Metadata_Record and Sensor_Data_Record field names defined in Requirement 1 and
   Requirement 2, differing only in field values.
10. IF the configured Swarm size is not an integer from 1 to 500 inclusive, or the configured
    classification mix proportions do not sum to 100 percent, THEN THE Config_Loader SHALL reject the
    configuration with an error naming the offending setting and its permitted range, and THE
    Swarm_Manager SHALL instantiate no Virtual_Sensor.
11. IF a supplied site list contains a duplicate `SiteCode`, a duplicate `DeviceCode`, an entry
    missing `SiteCode`, `DeviceCode`, `Latitude`, or `Longitude`, or coordinates outside the active
    Geography_Profile bounding box, THEN THE Config_Loader SHALL reject the configuration with an
    error naming the offending entry and field, and THE Swarm_Manager SHALL instantiate no
    Virtual_Sensor.
12. IF the configured Geography_Profile name is not one of the supported profile names, THEN THE
    Config_Loader SHALL reject the configuration with an error naming the supplied value and listing
    the supported profile names, and THE Swarm_Manager SHALL instantiate no Virtual_Sensor.
13. THE Config_Loader SHALL require every registered Geography_Profile, whether built in or declared
    in configuration, to supply a complete value set comprising bounding box, local timezone,
    sub-area name list, elevation, `SiteCode` prefix, `SponsorName`, `SensorContract`, temperature
    range, RH range, pressure range, and seasonal PM2.5 multiplier, and SHALL validate a
    configuration-declared profile against the same rules it applies to a built-in profile.
14. IF a configuration-declared Geography_Profile omits any value required by criterion 13, reuses
    the name of an already-registered profile, declares a `SiteCode` prefix equal to another
    registered profile's prefix, or declares one or more sub-areas that do not fall within its own
    bounding box, THEN THE Config_Loader SHALL reject the configuration with an error naming the
    offending profile and value, and THE Swarm_Manager SHALL instantiate no Virtual_Sensor.

### Requirement 9: Spatial Correlation

**User Story:** As an agent developer, I want nearby sensors to agree, so that "nearest sensor"
selection and geo-personalization in Service 2 produce coherent answers.

#### Acceptance Criteria

1. THE Signal_Engine SHALL compute each Virtual_Sensor pollutant Dry_Concentration as the sum of one
   shared Regional_Field component, identical for every Virtual_Sensor in the Swarm at a given
   simulated hour, and a per-site local modifier drawn from that Virtual_Sensor's own pseudo-random
   stream, with the Regional_Field component accounting for at least the configured share of hourly
   PM2.5 variance, default 60 percent, over any simulated 72-hour window under the `clean` scenario.
2. FOR ALL pairs of Virtual_Sensor instances whose great-circle separation is less than 2 km and
   whose `SiteClassification` values are equal, over any simulated 72-hour window under the `clean`
   scenario with a single Seed, THE Signal_Engine SHALL produce hourly Reported_Concentration PM2.5
   series with a Pearson correlation of at least 0.6, computed over the hourly intervals for which
   both instances emitted a Sensor_Data_Record and requiring at least 48 such common intervals.
3. FOR ALL ordered band pairs drawn from the great-circle separation bands 0 to 2 km, 2 to 5 km, 5 to
   10 km, and greater than 10 km, where the first band is the nearer one, over any simulated 72-hour
   window under the `clean` scenario, THE Signal_Engine SHALL produce a mean hourly PM2.5 Pearson
   correlation for the nearer band that is greater than or equal to the mean for the farther band
   minus a tolerance of 0.05 (spatial-decay property), where each band mean is taken over
   equal-`SiteClassification` pairs having at least 48 common hourly intervals.
4. WHILE a city-wide scenario is active, THE Scenario_Engine SHALL apply the scenario to the
   Regional_Field so that, for every Virtual_Sensor in the Swarm that is not under an active dropout
   or stuck-value fault, at least one emitted hourly PM2.5 `ScaledValue` inside the scenario window
   differs by at least 10 percent from the `clean`-baseline value for the same Seed, Virtual_Sensor,
   and hour.
5. FOR ALL pairs of Virtual_Sensor instances whose great-circle separation is less than 2 km and
   whose `SiteClassification` values are equal, over any simulated 72-hour window under the `clean`
   scenario, THE Signal_Engine SHALL produce a mean absolute hourly PM2.5 difference of no more than
   8 µg/m³, computed over the hourly intervals for which both instances emitted a
   Sensor_Data_Record.
6. WHEN a scenario is scheduled with target `SiteCode` values, THE Scenario_Engine SHALL apply the
   scenario effect only to the targeted Virtual_Sensor instances and SHALL leave the Regional_Field
   unchanged, so that every non-targeted Virtual_Sensor reproduces its `clean`-baseline `ScaledValue`
   values for the same Seed and time window.

### Requirement 10: Scenario Injection

**User Story:** As a demo operator, I want to trigger named air-quality scenarios on a schedule, so
that I can drive the agent's advice logic and Service 2's quality flags on demand.

#### Acceptance Criteria

1. THE Scenario_Engine SHALL support exactly the scenario names `clean`, `pollution_episode`,
   `rush_hour_no2`, `wildfire_smoke`, and `sensor_fault`, and SHALL treat any other name as
   unrecognized.
2. WHEN the `pollution_episode` scenario is active, THE Scenario_Engine SHALL ramp the Regional_Field
   PM2.5 component so that, within the configured ramp duration of default 3 simulated hours and for
   every Virtual_Sensor in the Swarm, the emitted PM2.5 `ScaledValue` is at or above 35.5 µg/m³ and
   at or below the configured episode peak of default 120 µg/m³, and SHALL hold values at or above
   35.5 µg/m³ until the scenario window ends, so that the derived index reaches at least the band of
   the configured breakpoint table whose lower bound is 35.5 µg/m³.
3. WHEN the `rush_hour_no2` scenario is active, THE Scenario_Engine SHALL multiply NO2 at every
   `Roadside` Virtual_Sensor during the rush-hour windows of Requirement 4 by at least the configured
   multiplier, with default 1.8 and permitted range 1.0 to 5.0, measured against the `clean` baseline
   NO2 `ScaledValue` for the same Seed, `SiteCode`, and Publish_Interval, and SHALL leave NO2 at
   `Urban Background` and `Suburban` Virtual_Sensor instances within 0.01 µg/m³ of that same `clean`
   baseline.
4. WHEN the `wildfire_smoke` scenario is active, THE Scenario_Engine SHALL multiply the
   Regional_Field PM2.5 component by the configured wildfire multiplier, with default 3.0 and
   permitted range 1.5 to 10.0, SHALL reach the full multiplier within the configured onset duration
   of default 1 simulated hour, SHALL cap the emitted PM2.5 `ScaledValue` at the configured wildfire
   peak of default 250 µg/m³, SHALL apply the same hygroscopic growth factor as the `clean` scenario
   for the same Seed, `SiteCode`, and Tick, and SHALL emit NO2 `ScaledValue` values within
   0.01 µg/m³ of the `clean` baseline for the same Seed, `SiteCode`, and Publish_Interval. This
   scenario represents the dry-season biomass-burning smoke that reaches the Cochabamba valley
   between July and October.
5. WHEN the `sensor_fault` scenario is active for a named Virtual_Sensor, THE Scenario_Engine SHALL
   apply the configured fault type, one of stuck value, accelerated drift, or dropout, to that
   Virtual_Sensor only for the duration of the scenario window, SHALL apply accelerated drift at the
   configured accelerated drift rate with default 5 times the Requirement 7 drift rate, and SHALL
   emit every other Virtual_Sensor's `ScaledValue` within 0.01 of the `clean` baseline for the same
   Seed, `SiteCode`, and Publish_Interval.
6. THE Config_Loader SHALL accept a scenario schedule of up to the configured maximum number of
   entries, with default 100, where each entry names one supported scenario, an ISO-8601 UTC start
   and end simulated timestamp, and an optional list of up to 500 target `SiteCode` values, and SHALL
   treat an entry with no target list as applying to every Virtual_Sensor in the Swarm.
7. WHILE two or more scenario windows overlap in simulated time for the same Virtual_Sensor, THE
   Scenario_Engine SHALL apply every overlapping effect in the configured precedence order, whose
   default is the order the scenarios are listed in criterion 1, and SHALL record the applied
   scenario names and the affected `SiteCode` values in the diagnostic output for that
   Publish_Interval.
8. FOR ALL scenarios other than `clean`, over the same Seed, configuration, and simulated time
   window, THE Simulator SHALL produce at least one observable difference from the `clean` baseline,
   being either an emitted Sensor_Data_Record whose `ScaledValue` differs by at least 0.01 from the
   `clean` baseline record with the same `Species`, `SiteCode`, and `DateTime`, or the absence of a
   record that the `clean` baseline emits for that `Species`, `SiteCode`, and `DateTime`
   (observable-effect property).
9. WHILE no scheduled scenario window covers the current simulated timestamp for a Virtual_Sensor,
   THE Scenario_Engine SHALL apply no scenario modifier to that Virtual_Sensor, so that its emitted
   values equal the `clean` baseline for the same Seed, `SiteCode`, and Publish_Interval.
10. WHEN a scenario window ends, THE Scenario_Engine SHALL reduce that scenario's contribution to
    zero over the configured recovery duration, with default 2 simulated hours, so that after that
    duration every affected `ScaledValue` is within 10 percent of the `clean` baseline value for the
    same Seed, `SiteCode`, and Publish_Interval.
11. IF the scenario schedule contains an entry with an unrecognized scenario name, an unrecognized
    fault type, a `SiteCode` absent from the Swarm, an end timestamp at or before its start
    timestamp, a multiplier outside its permitted range, or more entries or target values than the
    configured maximum, THEN THE Config_Loader SHALL reject the configuration with a non-zero exit
    status and one message per offending entry naming the entry and the constraint it violated, and
    THE Simulator SHALL NOT begin generating signals.

### Requirement 11: Deterministic Replay

**User Story:** As a developer, I want byte-identical output for a given seed, so that demos are
reproducible and property tests can compare runs.

#### Acceptance Criteria

1. THE Config_Loader SHALL accept a Seed as an integer within 0 to 4,294,967,295 that initializes
   every pseudo-random stream in the Simulator, including the streams used for Swarm identity,
   pollutant signals, meteorology, sensor artifacts, and fault scheduling.
2. FOR ALL Seed and configuration pairs, two Simulator runs in separate processes over the same
   simulated time range of at least 1 hour and at most 30 simulated days SHALL emit, for each
   `SiteCode` and `Species`, the same ordered sequence of Sensor_Data_Record values with
   byte-identical Serializer output (determinism property).
3. WHEN two Simulator runs use different Seed values with otherwise identical configuration, THE
   Simulator SHALL emit, over any simulated 24-hour range, at least one Sensor_Data_Record whose
   `ScaledValue` differs from the corresponding record of the other run by 0.01 or more at the same
   `SiteCode`, `Species`, and `DateTime`.
4. THE Simulator SHALL assign each Virtual_Sensor an independent pseudo-random stream derived from
   the Seed and the `SiteCode`, so that WHEN the configured Swarm size changes with the Seed and all
   other configuration unchanged, THE Simulator SHALL emit byte-identical Serializer output for every
   retained `SiteCode` over the same simulated time range.
5. FOR ALL Virtual_Sensor instances and FOR each Species, THE Simulator SHALL emit Sensor_Data_Record
   values in strictly increasing `DateTime` order, with each successive `DateTime` separated by a
   whole multiple of the Publish_Interval, and SHALL emit no two records sharing the same `SiteCode`,
   `Species`, and `DateTime` (monotonic-timestamp property).
6. IF the supplied Seed is not an integer or falls outside 0 to 4,294,967,295, THEN THE Config_Loader
   SHALL reject the configuration with an error naming the Seed field and the rejected value, and THE
   Simulator SHALL emit no Sensor_Data_Record.
7. WHEN no Seed is supplied in configuration, THE Config_Loader SHALL select a Seed within the
   permitted range and SHALL record the selected Seed value in the diagnostic output before the first
   Sensor_Data_Record is emitted, so that the run can be replayed.

### Requirement 12: Time Modes

**User Story:** As a developer, I want both live streaming and fast historical generation, so that I
can demo real-time behavior and also seed Service 2's store with history.

#### Acceptance Criteria

1. WHILE the Simulator runs in real-time mode, THE Simulator SHALL produce exactly one Tick per
   Virtual_Sensor per elapsed wall-clock minute, with each Tick's simulated timestamp aligned to the
   start of that minute.
2. THE Simulator SHALL compute each emitted `ScaledValue` as the arithmetic mean of every Tick value
   within the Publish_Interval the record covers, which is 60 Tick values for the default 1-hour
   Publish_Interval, rounded to 2 decimal places.
3. WHEN the Simulator runs in Backfill_Mode with a start timestamp and an end timestamp supplied as
   ISO-8601 UTC timestamps, THE Simulator SHALL generate one Sensor_Data_Record per Virtual_Sensor
   per Species for every Publish_Interval whose start is greater than or equal to the start timestamp
   and less than the end timestamp, in non-decreasing `DateTime` order, at a rate of at least 24
   simulated hours of records per wall-clock second per Virtual_Sensor and without waiting for
   wall-clock time to advance.
4. FOR ALL Seed and configuration pairs, THE Simulator SHALL emit the same ordered sequence of
   Sensor_Data_Record values for the same simulated interval in Backfill_Mode and in real-time mode,
   with every contract field equal, `RatificationStatus` being compared against the same reference
   time in both modes (mode-equivalence property).
5. WHERE a Publish_Interval other than 1 hour is configured, THE Simulator SHALL average the Tick
   values of that configured interval and SHALL emit the ISO-8601 duration matching that configured
   interval in the `Duration` field.
6. IF a Backfill_Mode end timestamp is earlier than or equal to its start timestamp, or the range
   between them exceeds the configured maximum backfill span with default 365 simulated days, THEN
   THE Config_Loader SHALL reject the configuration with an error naming both timestamps and the
   permitted span, and THE Simulator SHALL emit no Sensor_Data_Record for that range.
7. WHEN a Publish_Interval completes while the Simulator runs in real-time mode, THE Simulator SHALL
   emit one Sensor_Data_Record per Virtual_Sensor per Species for that interval within 5 seconds of
   the interval boundary, with `DateTime` aligned to the start of that interval.
8. IF a Publish_Interval holds fewer Tick values for a Virtual_Sensor than the interval's full
   expected Tick count, because the Simulator started or stopped part way through the interval, THEN
   THE Simulator SHALL omit every Sensor_Data_Record for that Virtual_Sensor and that
   Publish_Interval and SHALL retain the records of all completed intervals.
9. IF the configured Publish_Interval is not an integer multiple of the 1-minute Tick, or lies outside
   1 minute to 24 hours inclusive, THEN THE Config_Loader SHALL reject the configuration with an
   error naming the configured value and the permitted range, and THE Simulator SHALL emit no
   Sensor_Data_Record.

### Requirement 13: Push Mode over MQTT

**User Story:** As a Service 2 developer, I want the simulator to behave like real cellular devices
publishing over MQTT, so that my IoT ingestion path is exercised end to end.

#### Acceptance Criteria

1. WHERE the `mqtt` interface is selected, WHEN the Simulator emits a Sensor_Data_Record for a
   completed Publish_Interval, THE MQTT_Publisher SHALL publish that record to the topic
   `aqm/sensors/{SiteCode}/data`, substituting the publishing Virtual_Sensor's `SiteCode`.
2. THE MQTT_Publisher SHALL use the Serializer output for exactly one Sensor_Data_Record as the
   message payload, and SHALL NOT combine more than one Sensor_Data_Record in a single message.
3. THE MQTT_Publisher SHALL connect only over TLS, SHALL validate the broker certificate chain
   against the configured certificate authority, SHALL treat a failed validation as a connection
   failure, and SHALL authenticate each Virtual_Sensor with a certificate and private key used by no
   other Virtual_Sensor.
4. THE Config_Loader SHALL accept an MQTT endpoint host, a port in the range 1 to 65535 with default
   8883, a broker certificate authority path, a per-Virtual_Sensor credential path template
   containing a `{SiteCode}` placeholder from which it resolves one certificate path and one private
   key path for every Virtual_Sensor, with defaults `certs/{SiteCode}/client.crt` and
   `certs/{SiteCode}/client.key`, and an optional explicit certificate path and private key path for
   an individual `SiteCode` that overrides the template for that Virtual_Sensor, so that the same
   build targets a local broker or AWS IoT Core with no source change.
5. WHERE the Simulator runs in a Long_Running_Deployment, IF a connection attempt fails or an
   established connection drops, THEN THE MQTT_Publisher SHALL retry with an interval starting at 1
   second and doubling on each consecutive failure up to the configured maximum interval, with
   default 60 seconds, and SHALL keep retrying at that maximum interval for as long as the Simulator
   runs.
6. WHERE the Simulator runs in a Long_Running_Deployment, WHILE the MQTT connection is unavailable,
   THE Simulator SHALL continue computing Ticks and generating Sensor_Data_Record values at the
   configured cadence, and THE MQTT_Publisher SHALL hold them in an in-memory buffer whose configured
   maximum is in the range 1 to 100,000 records, with default 1000.
7. WHERE the Simulator runs in a Long_Running_Deployment, IF a Sensor_Data_Record is generated while
   the buffer already holds its configured maximum, THEN THE MQTT_Publisher SHALL discard the oldest
   buffered record, SHALL retain the newly generated record, and SHALL increment a dropped-record
   counter by 1 per discarded record.
8. WHERE the Simulator runs in a Long_Running_Deployment, WHEN the MQTT connection is restored, THE
   MQTT_Publisher SHALL publish every buffered Sensor_Data_Record exactly once, in non-decreasing
   `DateTime` order per Virtual_Sensor, before publishing any Sensor_Data_Record generated after the
   restore.
9. IF the `mqtt` interface is selected and a configured certificate authority path, Virtual_Sensor
   certificate path, or private key path is absent or unreadable at startup, THEN THE Config_Loader
   SHALL exit with a non-zero status code, SHALL report one error per affected value naming the
   configuration value and the affected `SiteCode`, and SHALL NOT start signal generation.
10. IF the broker rejects connection attempts for a Virtual_Sensor on the configured number of
    consecutive attempts, with default 5, for certificate-validation or client-authentication
    reasons, THEN THE MQTT_Publisher SHALL stop attempting to connect for that Virtual_Sensor, SHALL
    report an error identifying the `SiteCode` and the rejection category, and SHALL continue
    publishing for the remaining Virtual_Sensor instances.
11. WHERE the Simulator runs in an invocation-scoped deployment, IF the MQTT connection is unavailable
    or a Sensor_Data_Record remains unpublished when the invocation's time budget is exhausted, THEN
    THE MQTT_Publisher SHALL report each unpublished record with its `SiteCode`, `Species`, and
    `DateTime`, and SHALL NOT rely on in-memory buffered records surviving the end of the invocation,
    so that a later invocation can republish that Publish_Interval.

### Requirement 14: Pull Mode over an Authenticated REST API

**User Story:** As a Service 2 developer, I want a reference-contract-compatible REST endpoint, so
that my scheduled poller path works against the simulator and the real feed alike.

#### Acceptance Criteria

1. WHEN the Simulator starts with the `rest` interface selected, THE REST_API SHALL accept
   `GET /ListSensors` and `GET /SensorData` requests over HTTP at the configured listen address,
   being a host and port where the Simulator hosts the listener itself, or the deployment-provided
   endpoint where the REST_API is fronted by a managed HTTP endpoint, before emitting its first
   Sensor_Data_Record.
2. THE REST_API SHALL require every `/ListSensors` and `/SensorData` request to carry an `X-API-KEY`
   header whose full value matches the configured API key exactly, compared case-sensitively.
3. IF a request to `/ListSensors` or `/SensorData` omits the `X-API-KEY` header, supplies an empty
   value, or supplies a non-matching value, THEN THE REST_API SHALL respond with HTTP status 401
   before validating any query parameter, SHALL exclude every Sensor_Data_Record and
   Sensor_Metadata_Record from the response body, and SHALL include an error indication that
   authentication failed.
4. THE REST_API SHALL respond to every `/ListSensors` and `/SensorData` request that returns HTTP
   status 200 with `Content-Type` `application/json` and a top-level JSON array body.
5. WHEN a `/ListSensors` or `/SensorData` query is authenticated and matches no Virtual_Sensor and no
   Sensor_Data_Record, THE REST_API SHALL respond with HTTP status 200 and an empty JSON array.
6. THE REST_API SHALL obtain the API key at startup from an environment variable or a
   runtime-injected secret value of 16 to 256 characters, and SHALL NOT read the API key from source
   code or from any file committed to the repository.
7. THE REST_API SHALL serve only Sensor_Data_Record values whose `DateTime` falls inside the
   configured retention window, measured backward from the current simulated timestamp, with default
   30 simulated days and permitted range 1 to 365 simulated days.
8. THE REST_API SHALL respond to `GET /health` with HTTP status 200 and a JSON object body containing
   the current Swarm size as an integer and the current simulated timestamp as an ISO-8601 UTC
   timestamp ending in `Z`, and SHALL serve `/health` without requiring the `X-API-KEY` header.
9. IF the `rest` interface is selected and the injected API key value is absent or empty, THEN THE
   Simulator SHALL exit with a non-zero status code before accepting any HTTP request and SHALL
   report an error indicating that the API key secret is missing.
10. THE REST_API SHALL exclude the API key value from every response body, from the `/health`
    response, and from every diagnostic output it produces.
11. WHEN an authenticated `/SensorData` request supplies a `startTime` earlier than the start of the
    retention window, THE REST_API SHALL respond with HTTP status 200 and only those records whose
    `DateTime` falls inside the retention window.

### Requirement 15: Configuration

**User Story:** As a demo operator, I want all simulator behavior driven by configuration, so that I
can change fleet size, geography, cadence, and scenarios without editing code.

#### Acceptance Criteria

1. THE Config_Loader SHALL resolve every accepted configuration value in the precedence order
   environment variable over configuration file over documented default, and WHEN no configuration
   file path is supplied at startup, THE Config_Loader SHALL resolve every value from environment
   variables and documented defaults alone without reporting an error.
2. THE Config_Loader SHALL accept Swarm size, Geography_Profile name, geographic bounding box,
   optional site list, classification mix, Tick interval in minutes, Publish_Interval in minutes,
   Seed, time mode, Backfill_Mode start and end timestamps, scenario schedule, index breakpoint
   table, interface selection, MQTT settings, REST settings, and the Geography_Profile value keys
   local timezone, sub-area name list, elevation, `SiteCode` prefix, `SponsorName`, `SensorContract`,
   temperature range, RH range, pressure range, and seasonal PM2.5 multiplier, and SHALL reject any
   unrecognized configuration key by name.
3. THE Config_Loader SHALL default the Geography_Profile to `cochabamba`, and WHEN an individual
   profile value is supplied, THE Config_Loader SHALL apply that value over the named profile's
   default while retaining every profile value that was not supplied.
4. THE Config_Loader SHALL validate every resolved configuration value before the Simulator computes
   its first Tick, opens the REST_API listener, or connects to the MQTT broker, accepting Swarm size
   as an integer from 1 to 500, Tick interval as an integer from 1 to 60 minutes with default 1,
   Publish_Interval as an integer from 1 to 1440 minutes with default 60 and an integer multiple of
   the Tick interval, Seed as an integer from 0 to 4,294,967,295, bounding box latitudes from -90 to
   90 and longitudes from -180 to 180 with each minimum strictly less than its maximum, an optional
   site list of at most 500 entries, a classification mix whose proportions each fall from 0 to 100
   and sum to 100, a scenario schedule of at most 100 entries, and a REST retention window as an
   integer from 1 to 365 simulated days with default 30.
5. IF any resolved configuration value fails validation, THEN THE Config_Loader SHALL complete
   validation of all remaining values, SHALL write one message per invalid value naming the value and
   the constraint it violated, SHALL exit with a non-zero status code, and SHALL emit no
   Sensor_Data_Record and open no interface.
6. THE Config_Loader SHALL apply documented defaults for every accepted value except the MQTT
   endpoint, MQTT port, and per-Virtual_Sensor credential paths, with interface selection defaulting
   to `rest`, so that the Simulator reaches a state serving `GET /health` with no configuration file
   present and no MQTT setting supplied.
7. THE Config_Loader SHALL accept an interface selection of `mqtt`, `rest`, or both, THE Simulator
   SHALL activate only the selected interfaces, and IF the interface selection contains any other
   value, THEN THE Config_Loader SHALL reject the configuration with a message naming the supplied
   value and the three permitted values.
8. IF a configuration file path is supplied at startup and that path cannot be read or its contents
   cannot be parsed, THEN THE Config_Loader SHALL write a message naming the path and the failure
   kind, SHALL exit with a non-zero status code, and SHALL NOT fall back to documented defaults.
9. IF the resolved Geography_Profile name is not the name of a registered Geography_Profile, whether
   built in or declared in configuration, THEN THE Config_Loader SHALL reject the configuration with
   a message naming the supplied value and listing every registered profile name, and SHALL exit with
   a non-zero status code.
10. IF the interface selection includes `mqtt` and the MQTT endpoint, the MQTT port, or any
    per-Virtual_Sensor credential path is absent, THEN THE Config_Loader SHALL write one message per
    absent value naming that value, SHALL exit with a non-zero status code, and SHALL open no
    interface.
11. WHEN configuration declares a Geography_Profile whose name is not already registered and whose
    value set is complete and valid as defined in Requirement 8 criterion 13, THE Config_Loader SHALL
    register that profile in the Geography_Profile registry and SHALL make that profile selectable by
    name, so that retargeting the Swarm to a new region requires no source change.

### Requirement 16: Project Scaffolding and Local Development

**User Story:** As a developer joining the monorepo, I want the simulator to be a self-contained,
runnable, testable project, so that I can start it and its tests with documented commands.

#### Acceptance Criteria

1. THE Simulator SHALL live under the `sensor-simulator/` directory of the monorepo, with source,
   tests, and configuration held in three separate sibling subdirectories, and SHALL contain no
   source module that imports a test module.
2. THE Simulator SHALL declare Python 3.12 as its required runtime and SHALL pin every direct
   dependency to one exact version in a single package manifest committed to the repository, so that
   two installs from the same manifest resolve identical direct dependency versions.
3. WHEN a developer runs the single documented test command, THE test suite SHALL execute
   property-based tests generating at least 100 examples for each of the named properties in this
   document, being round-trip (Requirement 3), determinism and monotonic-timestamp (Requirement 11),
   index monotonicity (Requirement 6), spatial-decay (Requirement 9), mode-equivalence (Requirement
   12), humidity growth monotonicity (Requirement 5), and observable-effect (Requirement 10), and
   SHALL exit with status 0 if and only if every test passed.
4. THE test suite SHALL include one test per REST_API failure mode named in Requirements 1, 2, and
   14, being `RadiusKM` supplied without both `Latitude` and `Longitude`, `startTime` later than
   `endTime`, a `Species` value outside the four permitted values, an omitted `X-API-KEY` header, and
   a non-matching `X-API-KEY` value, each asserting the HTTP status code documented in those
   requirements and a JSON error body naming the offending parameter or the permitted values.
5. WHERE the Simulator runs in a Long_Running_Deployment, WHEN the committed container image
   definition is built and run with no configuration file supplied, THE Simulator SHALL run the whole
   Swarm in one process in REST-only mode and SHALL answer `GET /health` with HTTP status 200 within
   30 seconds of container start.
6. WHEN a developer runs the documented Docker Compose command with no AWS credentials present, THE
   Docker Compose definition SHALL start the Simulator container together with a local MQTT broker,
   and THE MQTT_Publisher SHALL establish a broker connection within 60 seconds of container start
   and publish one Sensor_Data_Record per Virtual_Sensor within one Publish_Interval.
7. THE Simulator SHALL provide a README documenting the run commands for real-time mode and
   Backfill_Mode, every configuration value named in Requirement 15 with its default, every scenario
   name named in Requirement 10, every built-in Geography_Profile name with its values, how to
   declare an additional Geography_Profile in configuration, and the `/ListSensors` and `/SensorData`
   contract fields it reproduces.
8. THE Simulator SHALL contain no committed file holding an API key, X.509 client certificate, or
   private key, SHALL load each of those from a path or environment value supplied at runtime, and
   THE Docker Compose definition SHALL obtain local broker credentials from runtime-supplied or
   locally generated material rather than from committed files.
9. IF a required secret for a selected interface is not resolvable from its runtime-supplied path or
   environment value at startup, THEN THE Config_Loader SHALL exit with a non-zero status code and
   SHALL write one message per unresolved secret naming the configuration value, excluding the secret
   value itself from the message.
10. WHEN the documented test command runs on a machine with no AWS credentials and no network access
    beyond the local host, THE test suite SHALL pass every test other than the Docker Compose
    integration check.
11. THE Simulator SHALL provide a documented command that generates local development certificate and
    private key material for every Virtual_Sensor in the configured Swarm at the locations resolved
    from the Requirement 13 criterion 4 credential path template, and SHALL exclude every generated
    credential file from version control.

### Requirement 17: Observability

**User Story:** As a demo operator, I want visible evidence of what the simulator is doing, so that I
can confirm a scenario fired and diagnose a silent stream.

#### Acceptance Criteria

1. THE Simulator SHALL write each log event to standard output as exactly one single-line JSON object
   containing an ISO-8601 UTC timestamp ending in `Z`, a level of `debug`, `info`, `warn`, or
   `error`, and an event name, and SHALL write no non-JSON text to standard output.
2. WHEN a scenario window starts, THE Simulator SHALL write one log line at level `info` containing
   the scenario name, the simulated timestamp of the window start, and the affected `SiteCode`
   values, listing every `SiteCode` in the Swarm where the scenario applies city-wide.
3. WHEN a Publish_Interval closes, THE Simulator SHALL write exactly one summary log line at level
   `info` containing the simulated start timestamp of that Publish_Interval and four non-negative
   integer counts: records generated, records published, records buffered, and records dropped.
4. IF a Tick or publish operation raises an error for one Virtual_Sensor, THEN THE Simulator SHALL
   write one log line at level `error` containing the `SiteCode`, the operation name, and the error
   type, SHALL continue generating and publishing for every remaining Virtual_Sensor instance within
   the same Publish_Interval, and SHALL leave the process running.
5. THE Config_Loader SHALL accept a log level of `debug`, `info`, `warn`, or `error`, with default
   `info`, and THE Simulator SHALL write log lines at the configured level and above and SHALL
   suppress log lines below it.
6. WHEN a scenario window ends, THE Simulator SHALL write one log line at level `info` containing the
   scenario name and the simulated timestamp of the window end.
7. IF a Publish_Interval summary reports a generated count greater than 0 and a published count of 0,
   THEN THE Simulator SHALL write one log line at level `warn` identifying that Publish_Interval and
   naming the publish-blocking condition among an unavailable MQTT connection, a scheduled dropout,
   and no active interface.
8. IF the configured log level is not one of `debug`, `info`, `warn`, or `error`, THEN THE
   Config_Loader SHALL reject the configuration with an error naming the supplied value and the four
   permitted values.
