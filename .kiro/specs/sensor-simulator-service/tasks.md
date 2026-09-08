# Implementation Plan: Sensor Simulator Service

## Overview

Implementation proceeds inside-out: the contract spine first (record models, Serializer, Parser),
then the two injected boundaries that make everything deterministic and testable (Clock,
RandomStream), then geography and configuration, then the signal model bottom-up (meteorology →
humidity artifact → regional field → pollutants → index), then swarm, faults, and scenarios, then
the publish pipeline and the two interfaces, and finally packaging and wiring.

Every step is TDD: the failing test comes first, then the smallest clean change that passes it. Each
of the 42 correctness properties from the design gets exactly one `hypothesis` property test running
at least 100 examples, tagged `Feature: sensor-simulator-service, Property {n}`. All domain code
takes an injected clock and an injected per-`SiteCode` random stream — no `datetime.now()`, no
module-level `random`. Language: Python 3.12, FastAPI, pytest + hypothesis, all under
`sensor-simulator/`.

## Tasks

- [ ] 1. Project scaffolding and observability foundation
  - [ ] 1.1 Create the `sensor-simulator/` project skeleton and pinned manifest
    - Create `src/aqm_simulator/` package tree (`contract/`, `signal/`, `scenarios/`, `geography/`,
      `swarm/`, `pipeline/`, `time/`, `rng/`, `interfaces/mqtt/`, `interfaces/rest/`, `config/`,
      `observability/`), sibling `tests/{unit,properties,integration}/`, and `config/`
    - Write `pyproject.toml` declaring Python 3.12 with every direct dependency pinned to one exact
      version; configure pytest and hypothesis (min 100 examples profile) and the single documented
      test command
    - _Requirements: 16.1, 16.2, 16.3_

  - [ ] 1.2 Implement the structured JSON logger configured once at startup
    - `observability/logging.py`: one single-line JSON object per event to stdout with ISO-8601 UTC
      `Z` timestamp, level, and event name; central configuration, level filtering, no `print()`
    - _Requirements: 17.1, 17.5_

  - [ ]* 1.3 Write property test for log output shape
    - **Property 42: Log output is single-line JSON**
    - **Validates: Requirements 17.1**

  - [ ]* 1.4 Write unit tests for log level filtering and rejection
    - Each configured level emits at that level and above and suppresses below it
    - Invalid log level is rejected naming the supplied value and the four permitted values
    - _Requirements: 17.5, 17.8_

- [ ] 2. Canonical record contract, Serializer, and Parser
  - [ ] 2.1 Define the two Pydantic record models in declared field order
    - `SensorDataRecord` with exactly nine fields; `SensorMetadataRecord` with exactly twenty plus
      the `Location`/`PointGeometry` models; `Latitude`/`Longitude` as 7-decimal strings identical to
      `Location.coordinates`; every key present even when null
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.12_

  - [ ] 2.2 Implement the Serializer
    - Render both record types in declared field order with the specified JSON value types;
      `ScaledValue` as a JSON number at 2 dp half-away-from-zero
    - _Requirements: 3.1, 2.7_

  - [ ] 2.3 Implement the Parser with strict validation
    - Accept a single object or an array of 0–100,000 objects preserving order; reject missing,
      unknown, wrong-typed, and invalid-`Species` fields; reject malformed JSON and oversize payloads
    - _Requirements: 3.2, 3.5, 3.7, 3.8_

  - [ ]* 2.4 Write property test for sensor data record round-trip
    - **Property 1: Sensor data record round-trip**
    - **Validates: Requirements 3.3, 2.1, 2.5, 2.6, 2.7**

  - [ ]* 2.5 Write property test for sensor metadata record round-trip
    - **Property 2: Sensor metadata record round-trip**
    - **Validates: Requirements 3.4, 1.1, 1.2, 1.3, 1.7**

  - [ ]* 2.6 Write property test for serialize-parse-serialize stability
    - **Property 3: Serialize-parse-serialize stability**
    - **Validates: Requirements 3.9**

  - [ ] 2.7 Implement ratification status derivation from record age
    - Derive `P`/`R` from current simulated time minus `DateTime` against the configured
      Ratification_Lag (default 90 days), taking the reference time as a parameter
    - _Requirements: 2.8, 2.9_

  - [ ]* 2.8 Write property test for ratification status by age
    - **Property 6: Ratification status by age**
    - **Validates: Requirements 2.8, 2.9**

  - [ ]* 2.9 Write unit tests for Parser error cases
    - Missing field, unknown field, wrong JSON type, invalid `Species`, malformed JSON, oversize
      payload — each naming the offending field and producing no record
    - _Requirements: 3.5, 3.7, 3.8_

- [ ] 3. Checkpoint - contract spine
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. Time and randomness boundaries
  - [ ] 4.1 Implement the `Clock` protocol with real-time and backfill drivers
    - `SystemClock`: one tick per sensor per elapsed wall-clock minute, timestamps aligned to the
      minute start, `wait_until` sleeps. `BackfillClock`: advances in Publish_Interval steps,
      `wait_until` is a no-op
    - _Requirements: 12.1, 12.3_

  - [ ] 4.2 Implement `RandomStreamFactory` keyed by seed, `SiteCode`, and purpose
    - Independent `numpy.random.Generator` per `(seed, SiteCode, purpose)` with purposes for
      identity, signal, meteorology, artifacts, and faults; derived from `SiteCode`, never a
      positional index
    - _Requirements: 11.1, 11.4_

  - [ ]* 4.3 Write unit tests for clock and stream behavior
    - Backfill clock does not wait; fake system clock produces exactly one tick per simulated minute
    - Same `(seed, SiteCode, purpose)` reproduces the same draw sequence; different `SiteCode` values
      produce independent streams; a retained `SiteCode` is unaffected by swarm-size change
    - _Requirements: 11.4, 12.1, 12.3_

- [ ] 5. Geography profiles and registry
  - [ ] 5.1 Implement `GeographyProfile` and the registry with both built-ins
    - Frozen dataclass holding bounding box, timezone, sub-areas, elevation, `SiteCode` prefix,
      `SponsorName`, `SensorContract`, temperature/RH/pressure ranges, seasonal PM multiplier
    - Seed `cochabamba` (default) and `reference` into the registry at startup; lookup by name
    - _Requirements: 8.4, 8.5, 8.9, 1.5, 1.6, 1.7, 5.2, 5.5, 5.13_

  - [ ] 5.2 Implement configuration-declared profile registration
    - Merge declared profiles into the registry before profile-name validation; require the complete
      value set, a non-colliding name, a unique `SiteCode` prefix, and sub-areas inside the profile's
      own bounding box
    - _Requirements: 8.13, 8.14, 15.11, 15.9_

  - [ ]* 5.3 Write unit tests for profile registration and selection errors
    - Unknown profile name lists every registered name; incomplete value set, duplicate name,
      colliding prefix, and out-of-box sub-area each rejected naming the profile and value
    - _Requirements: 8.12, 8.14, 15.9_

- [ ] 6. Config_Loader with fail-fast validation
  - [ ] 6.1 Implement resolution in precedence order environment > file > documented default
    - Accept every key named in the requirements; reject unrecognized keys by name; apply per-profile
      value overrides while retaining unsupplied profile values; run with no config file present
    - _Requirements: 15.1, 15.2, 15.3, 15.6_

  - [ ] 6.2 Implement the single full validation pass
    - Validate all resolved values before the first tick, listener, or broker connection; write one
      single-line JSON message per invalid value naming the value and violated constraint; exit
      non-zero without half-starting
    - Cover signal windows and peaks, meteorology ranges and growth factor, breakpoint table, noise
      and drift and fault windows, swarm size and classification mix and site list, scenario schedule
      entries, seed range, backfill span, publish interval, interface selection, log level, and
      unreadable or unparseable config file
    - _Requirements: 15.4, 15.5, 15.7, 15.8, 4.10, 5.14, 6.7, 7.10, 8.10, 8.11, 10.11, 11.6, 12.6, 12.9, 17.8_

  - [ ] 6.3 Resolve MQTT credential paths and the REST API key secret
    - Expand the `{SiteCode}` credential path template into one certificate and one key path per
      sensor with per-`SiteCode` override, resolving before validation; load the API key from
      environment or injected secret only, never from a committed file, never echoed
    - Report one error per absent or unreadable value naming the configuration value and affected
      `SiteCode`, never the secret itself
    - _Requirements: 13.4, 13.9, 14.6, 14.9, 15.10, 16.9_

  - [ ] 6.4 Implement seed selection and disclosure when no seed is supplied
    - Select a seed in range and log it before the first record so the run is replayable
    - _Requirements: 11.7_

  - [ ]* 6.5 Write property test for configuration resolution precedence
    - **Property 41: Configuration resolution precedence**
    - **Validates: Requirements 15.1, 15.3, 15.11**

  - [ ]* 6.6 Write unit tests for each fail-fast configuration error case
    - One test per failure row in the design's configuration-error table, asserting the message names
      the value and constraint, that all failures are reported rather than only the first, and that
      the exit status is non-zero with no interface opened
    - _Requirements: 15.5, 15.7, 15.8, 4.10, 5.14, 6.7, 7.10, 8.10, 8.11, 10.11, 11.6, 12.6, 12.9_

- [ ] 7. Checkpoint - boundaries and configuration
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 8. Meteorology signals and the humidity artifact
  - [ ] 8.1 Implement temperature, RH, and absolute pressure computation
    - Afternoon temperature maximum, 10–20 °C diurnal range, RH inversely tracking temperature,
      absolute station pressure at profile elevation, clamping to the nearer bound with the clamp
      event recorded
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.13_

  - [ ]* 8.2 Write property test for temperature diurnal shape and range
    - **Property 16: Temperature diurnal shape and range**
    - **Validates: Requirements 5.1, 5.3**

  - [ ]* 8.3 Write property test for meteorology value ranges
    - **Property 17: Meteorology value ranges**
    - **Validates: Requirements 5.2, 5.5, 5.6, 5.13**

  - [ ]* 8.4 Write property test for RH–temperature anticorrelation
    - **Property 18: RH–temperature anticorrelation**
    - **Validates: Requirements 5.4**

  - [ ] 8.5 Implement the hygroscopic growth factor and diagnostic recorder
    - `g(RH)` exactly 1.0 for RH ≤ 50, strictly increasing for 50 < RH < 85, ≥ 1.5 for RH ≥ 85,
      capped at the configured maximum; Reported_Concentration = Dry_Concentration × `g(RH)`
    - Record Dry_Concentration, Reported_Concentration, RH, and growth factor keyed by `SiteCode` and
      timestamp in diagnostic output only, never in the emitted contract
    - _Requirements: 5.7, 5.8, 5.9, 5.10, 5.11, 5.12_

  - [ ]* 8.6 Write property test for humidity-growth monotonicity
    - **Property 19: Humidity-growth monotonicity**
    - **Validates: Requirements 5.7, 5.8, 5.9, 5.12**

- [ ] 9. Regional field and pollutant signals
  - [ ] 9.1 Implement the shared `Regional_Field` with the seasonal multiplier
    - One city-wide latent series per simulated hour identical for every sensor, changing by at most
      5 µg/m³ per hour, scaled by the profile's seasonal PM2.5 multiplier
    - _Requirements: 9.1, 4.4, 4.8_

  - [ ]* 9.2 Write property test for regional-field variance share
    - **Property 27: Regional-field variance share**
    - **Validates: Requirements 9.1**

  - [ ]* 9.3 Write property test for the seasonal PM2.5 multiplier
    - **Property 15: Seasonal PM2.5 multiplier**
    - **Validates: Requirements 4.8**

  - [ ] 9.4 Implement the NO2 signal with diurnal traffic and classification scaling
    - Base plus non-negative diurnal component peaking in the rush-hour windows and ≤ 30 % of the
      daily maximum overnight, scaled Roadside ≫ Urban Background ≫ Suburban, plus drift and noise
    - _Requirements: 4.1, 4.2, 4.3_

  - [ ]* 9.5 Write property test for NO2 rush-hour diurnal shape
    - **Property 11: NO2 rush-hour diurnal shape**
    - **Validates: Requirements 4.1, 4.2**

  - [ ]* 9.6 Write property test for NO2 classification ordering
    - **Property 12: NO2 classification ordering**
    - **Validates: Requirements 4.3**

  - [ ] 9.7 Implement PM2.5 composition, clamping, and non-negativity
    - Regional baseline plus per-site local modifier and noise, weak diurnal component, clamp to the
      plausible range recording the clamp event, and never emit a negative `ScaledValue`
    - _Requirements: 4.4, 4.7, 4.11, 9.1_

  - [ ]* 9.8 Write property test for non-negative pollutant values
    - **Property 9: Non-negative pollutant values**
    - **Validates: Requirements 4.7**

  - [ ]* 9.9 Write property test for clean-scenario pollutant ranges
    - **Property 10: Clean-scenario pollutant ranges**
    - **Validates: Requirements 4.5**

  - [ ]* 9.10 Write property test for PM2.5 diurnal amplitude bound
    - **Property 13: PM2.5 diurnal amplitude bound**
    - **Validates: Requirements 4.4**

  - [ ]* 9.11 Write property test for signal autocorrelation floor
    - **Property 14: Signal autocorrelation floor**
    - **Validates: Requirements 4.9**

- [ ] 10. Index species derivation
  - [ ] 10.1 Implement the breakpoint table and `derive_index_band`
    - Configurable per-Species table defaulting to ten bands 1–10, validated contiguous,
      non-overlapping, and strictly increasing; lower-inclusive/upper-exclusive bounds; clamp above
      the top band and record the clamp event; derive from the emitted concentration `ScaledValue`
    - _Requirements: 6.3, 6.5, 6.6, 6.7_

  - [ ]* 10.2 Write property test for index monotonicity and determinism
    - **Property 20: Index monotonicity and determinism**
    - **Validates: Requirements 6.3, 6.4, 6.5, 6.6**

  - [ ]* 10.3 Write unit tests for index clamping and table validation
    - Boundary value lands in the higher band; above-table value clamps to the highest band with a
      recorded event; overlapping, gapped, non-increasing, and empty tables each rejected by band
    - _Requirements: 6.3, 6.6, 6.7_

- [ ] 11. Checkpoint - signal model
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 12. Swarm identity, factory, and spatial behavior
  - [ ] 12.1 Implement the Virtual_Sensor factory
    - Centralize identity assignment: prefixed zero-padded `SiteCode`, distinct `DeviceCode`,
      coordinates inside the bounding box, sub-area → `Borough`, classification mix, height and
      kerb distance; deterministic from `(seed, SiteCode)` so a restart reproduces every field
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [ ] 12.2 Implement the supplied-site-list path
    - Use supplied identity and location values in place of generated ones and set the swarm size
      from the entry count; reject duplicate or missing identity fields and out-of-box coordinates
    - _Requirements: 8.8, 8.11_

  - [ ]* 12.3 Write property test for swarm identity uniqueness and form
    - **Property 24: Swarm identity uniqueness and form**
    - **Validates: Requirements 8.1, 8.2**

  - [ ]* 12.4 Write property test for swarm placement within geography
    - **Property 25: Swarm placement within geography**
    - **Validates: Requirements 8.4, 8.5, 8.7**

  - [ ]* 12.5 Write property test for classification mix proportions
    - **Property 26: Classification mix proportions**
    - **Validates: Requirements 8.6**

  - [ ] 12.6 Implement `Swarm_Manager` orchestration with per-sensor isolation
    - Instantiate 1–500 sensors before the first tick in a defined order, then tick them; catch a
      per-sensor failure, log it at `error` with `SiteCode`, operation, and error type, and carry on
      with the rest of the swarm without stopping the process
    - _Requirements: 8.1, 17.4_

  - [ ]* 12.7 Write unit tests for per-sensor failure isolation
    - One sensor raising during tick and during publish leaves the remaining sensors emitting within
      the same Publish_Interval and the process running, with the error logged once
    - _Requirements: 17.4_

  - [ ]* 12.8 Write property test for near-sensor spatial correlation
    - **Property 28: Near-sensor spatial correlation**
    - **Validates: Requirements 9.2, 9.5**

  - [ ]* 12.9 Write property test for spatial decay
    - **Property 29: Spatial decay**
    - **Validates: Requirements 9.3**

- [ ] 13. Sensor artifacts, faults, and housekeeping
  - [ ] 13.1 Implement measurement noise and per-sensor drift
    - Zero-mean Gaussian noise per Species from the sensor's artifact stream clamped at 0, plus a
      drift term whose sign is fixed for a given seed and `SiteCode`, growing linearly with simulated
      elapsed time and capped at the configured maximum
    - _Requirements: 7.1, 7.2_

  - [ ] 13.2 Implement stuck-value and dropout windows
    - Dropout omits every record for the affected intervals with no placeholder while housekeeping
      continues; stuck value repeats the `ScaledValue` of the last interval completed before the
      window and derives the index from it; both resume from Signal_Engine values afterwards, and a
      suppressed concentration record suppresses its index record
    - _Requirements: 7.3, 7.4, 7.5, 6.8_

  - [ ]* 13.3 Write property test for stuck-value and dropout fault behavior
    - **Property 23: Stuck-value and dropout fault behavior**
    - **Validates: Requirements 7.3, 7.4, 7.5**

  - [ ] 13.4 Implement housekeeping telemetry
    - One record per sensor per Publish_Interval with signal quality 0–100 and an active-fault list;
      battery state of charge present if and only if `PowerTag` is `Solar`; published to a topic
      distinct from the measurement topic and excluded from every `/SensorData` response
    - _Requirements: 7.6, 7.7, 7.8, 7.9_

  - [ ]* 13.5 Write property test for housekeeping telemetry shape
    - **Property 22: Housekeeping telemetry shape**
    - **Validates: Requirements 7.6, 7.7, 7.8**

- [ ] 14. Scenario engine
  - [ ] 14.1 Implement the `Scenario` protocol, registry, and `clean` identity strategy
    - Exactly the five supported names selected from a registry with no conditional chain; `clean` is
      the identity strategy, and an uncovered timestamp applies no modifier
    - _Requirements: 10.1, 10.9_

  - [ ] 14.2 Implement the three atmospheric scenarios
    - `pollution_episode` ramps the regional PM2.5 field to ≥ 35.5 µg/m³ up to the episode peak;
      `wildfire_smoke` scales the field by its multiplier within the onset duration, caps at the
      wildfire peak, and leaves NO2 at baseline; `rush_hour_no2` multiplies roadside NO2 only and
      leaves other classifications at baseline. City-wide scenarios perturb the regional field;
      targeted scenarios leave it untouched
    - _Requirements: 10.2, 10.3, 10.4, 4.6, 9.4, 9.6_

  - [ ] 14.3 Implement the `sensor_fault` scenario
    - Apply stuck value, accelerated drift, or dropout to the named sensors only for the window
      duration, leaving every other sensor at its `clean` baseline
    - _Requirements: 10.5_

  - [ ] 14.4 Implement the schedule, ramp/recovery, and precedence composition
    - Validate and load up to the maximum entries with start/end timestamps and optional target list;
      taper each ended scenario to zero over the recovery duration; compose overlapping windows in
      configured precedence order deterministically and record applied names and affected `SiteCode`
      values in diagnostic output
    - _Requirements: 10.6, 10.7, 10.10_

  - [ ]* 14.5 Write property test for observable effect of scenarios
    - **Property 38: Observable effect of scenarios**
    - **Validates: Requirements 10.8, 10.9, 9.4, 9.6**

  - [ ]* 14.6 Write property test for scenario magnitude bounds
    - **Property 39: Scenario magnitude bounds**
    - **Validates: Requirements 10.2, 10.3, 10.4, 10.5, 10.10**

  - [ ]* 14.7 Write property test for scenario precedence composition
    - **Property 40: Scenario precedence composition**
    - **Validates: Requirements 10.7**

  - [ ]* 14.8 Write unit tests for scenario schedule validation errors
    - Unrecognized name, unrecognized fault type, unknown `SiteCode`, end at or before start,
      out-of-range multiplier, and over-maximum entries or targets — one message per offending entry
    - _Requirements: 10.11, 10.1_

- [ ] 15. Checkpoint - swarm, faults, scenarios
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 16. Publish pipeline, time modes, and determinism
  - [ ] 16.1 Implement the template-method publish pipeline
    - Fixed sequence tick → average over the Publish_Interval → apply artifacts → derive index
      species → serialize → publish/retain, with pluggable steps; emit `Duration` matching the
      configured interval; drop partial intervals whole; pair one index record with each
      concentration record
    - _Requirements: 12.2, 12.5, 12.8, 6.1, 6.2_

  - [ ]* 16.2 Write property test for interval averaging
    - **Property 33: Interval averaging**
    - **Validates: Requirements 12.2, 12.5**

  - [ ]* 16.3 Write property test for index species pairing
    - **Property 21: Index species pairing**
    - **Validates: Requirements 6.1, 6.2, 6.8**

  - [ ]* 16.4 Write property test for monotonic timestamps
    - **Property 32: Monotonic timestamps**
    - **Validates: Requirements 11.5**

  - [ ]* 16.5 Write property test for the emitted contract field set
    - **Property 5: Emitted contract field set**
    - **Validates: Requirements 2.1, 1.1, 5.11, 7.9, 8.9, 8.13**

  - [ ] 16.6 Implement the real-time and Backfill_Mode drivers
    - Both drive the same pipeline, differing only in the injected clock; real-time emits within 5
      seconds of the interval boundary; backfill covers `[start, end)` in non-decreasing `DateTime`
      order without waiting on wall-clock time
    - _Requirements: 12.1, 12.3, 12.7_

  - [ ]* 16.7 Write property test for backfill coverage
    - **Property 34: Backfill coverage**
    - **Validates: Requirements 12.3**

  - [ ]* 16.8 Write property test for mode equivalence
    - **Property 35: Mode equivalence**
    - **Validates: Requirements 12.4**

  - [ ] 16.9 Build the determinism and mode-equivalence test harness
    - Construct two simulator instances with fresh `RandomStreamFactory` objects from one seed to
      mimic separate processes, collect ordered records per `SiteCode`/`Species`, and compare
      Serializer bytes; drive one instance with `BackfillClock` and one with a fake `SystemClock`
      advanced without real sleeping
    - _Requirements: 11.2, 11.3, 11.4, 12.4_

  - [ ]* 16.10 Write property test for seeded determinism
    - **Property 30: Seeded determinism**
    - **Validates: Requirements 11.2, 11.4, 8.3, 7.2**

  - [ ]* 16.11 Write property test for seed sensitivity
    - **Property 31: Seed sensitivity**
    - **Validates: Requirements 11.3**

  - [ ] 16.12 Wire interval and scenario logging into the pipeline
    - One `info` summary per closed Publish_Interval with generated, published, buffered, and dropped
      counts; `info` lines on scenario window open and close with affected `SiteCode` values; a
      `warn` naming the blocking condition when generated > 0 and published = 0
    - _Requirements: 17.2, 17.3, 17.6, 17.7_

  - [ ]* 16.13 Write unit tests for interval summary and publish-blocked warning
    - Summary counts are non-negative integers and appear exactly once per interval; the blocked
      warning names an unavailable connection, a scheduled dropout, or no active interface
    - _Requirements: 17.3, 17.7_

- [ ] 17. Push mode over MQTT
  - [ ] 17.1 Implement the MQTT transport adapter and publisher
    - Narrow `MqttTransport` interface so a local broker and IoT Core are interchangeable; publish
      one record per message to `aqm/sensors/{SiteCode}/data`; TLS only with broker chain validation
      treated as a connection failure on mismatch and a per-sensor certificate and key
    - _Requirements: 13.1, 13.2, 13.3, 13.4_

  - [ ] 17.2 Implement Long_Running_Deployment retry, buffering, and ordered flush
    - Exponential backoff from 1 s to the configured maximum, retrying indefinitely while ticks keep
      generating; bounded in-memory buffer dropping the oldest record and incrementing the dropped
      counter on overflow; on restore publish every buffered record exactly once in non-decreasing
      `DateTime` order per sensor before any newer record
    - _Requirements: 13.5, 13.6, 13.7, 13.8_

  - [ ] 17.3 Implement invocation-scoped reporting and auth-rejection give-up
    - Report each record left unpublished with `SiteCode`, `Species`, and `DateTime` without relying
      on buffered records surviving the invocation; after the configured consecutive auth or
      certificate rejections stop attempts for that sensor, log the `SiteCode` and rejection
      category, and keep the rest of the swarm publishing
    - _Requirements: 13.10, 13.11_

  - [ ]* 17.4 Write property test for MQTT publish topic and single-record payloads
    - **Property 36: MQTT publish topic and single-record payloads**
    - **Validates: Requirements 13.1, 13.2**

  - [ ]* 17.5 Write property test for MQTT buffering and ordered flush
    - **Property 37: MQTT buffering and ordered flush**
    - **Validates: Requirements 13.6, 13.7, 13.8, 13.11**

  - [ ]* 17.6 Write unit tests for MQTT credential and rejection handling
    - Absent or unreadable CA, certificate, or key reported one message per affected value naming the
      configuration value and `SiteCode` with no signal generation; repeated rejection stops one
      sensor only; failed chain validation is treated as a connection failure
    - _Requirements: 13.3, 13.9, 13.10_

- [ ] 18. Pull mode over the authenticated REST API
  - [ ] 18.1 Implement the FastAPI app, `X-API-KEY` dependency, and `/health`
    - Auth dependency runs before query validation and returns 401 on missing, empty, or non-matching
      keys; 200 responses are `application/json` JSON arrays; `/health` is exempt and returns swarm
      size and current simulated timestamp; the key never appears in any response or log
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.8, 14.10_

  - [ ] 18.2 Implement `GET /ListSensors` with filtering and radius query
    - Whole-field case-insensitive matching for `SiteCode`, `Borough`, `Sponsor`, `Facility`;
      inclusive great-circle radius; unrecognized parameters ignored; ordered by ascending `SiteCode`;
      each documented failure mode returns 400 with a JSON body naming the offending parameter
    - _Requirements: 1.8, 1.9, 1.10, 1.11, 1.13, 1.14_

  - [ ] 18.3 Implement `GET /SensorData` with windowing, filtering, and retention
    - Default to the most recently completed Publish_Interval; half-open `[startTime, endTime)`
      window clipped to the retention window ordered by ascending `DateTime`; case-sensitive field
      filters and radius filter; each documented failure mode returns 400 naming the offending
      parameter while generation continues unchanged
    - _Requirements: 2.10, 2.11, 2.12, 2.13, 2.14, 2.15, 2.16, 2.17, 2.18, 14.7, 14.11_

  - [ ]* 18.4 Write property test for list filtering
    - **Property 7: List filtering matches supplied parameters**
    - **Validates: Requirements 1.8, 1.9, 1.10, 1.14**

  - [ ]* 18.5 Write property test for SensorData query windowing and filtering
    - **Property 8: SensorData query windowing and filtering**
    - **Validates: Requirements 2.10, 2.11, 2.12, 2.13, 14.7, 14.11**

  - [ ]* 18.6 Write unit tests for the five named REST failure modes
    - `RadiusKM` without both coordinates (400), `startTime` later than `endTime` (400), `Species`
      outside the four permitted values (400 listing them), omitted `X-API-KEY` (401), non-matching
      `X-API-KEY` (401) — each asserting the status and a JSON body naming the offending parameter,
      and confirming bad input never yields a 500
    - _Requirements: 16.4, 1.11, 2.14, 2.15, 14.3_

  - [ ]* 18.7 Write property test for push and pull byte-identity
    - **Property 4: Push and pull byte-identity**
    - **Validates: Requirements 3.6**

- [ ] 19. Checkpoint - both interfaces
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 20. Packaging, local development, and entry-point wiring
  - [ ] 20.1 Write the Dockerfile
    - Single image running the whole swarm in one process, defaulting to REST-only with no config
      file supplied and answering `GET /health` shortly after start
    - _Requirements: 16.5_

  - [ ] 20.2 Write the Docker Compose definition
    - Simulator plus a local MQTT broker, started with no AWS credentials present, broker credentials
      from runtime-supplied or locally generated material rather than committed files
    - _Requirements: 16.6, 16.8_

  - [ ] 20.3 Write the development credential generation script
    - `scripts/gen_dev_certs.py` generating certificate and key material for every sensor in the
      configured swarm at the paths resolved from the credential path template; git-ignore all
      generated material and commit no key, certificate, or API key
    - _Requirements: 16.11, 16.8, 13.4_

  - [ ]* 20.4 Write integration tests for the container, broker, and TLS transport
    - Credential generation runs first so per-`SiteCode` material exists at the template-resolved
      paths, then TLS/X.509 transport against a local broker, the container `/health` smoke check,
      and the Compose broker-connect-and-publish check as the sole test skippable when offline
    - _Requirements: 16.5, 16.6, 16.10, 16.11_

  - [ ] 20.5 Write the README
    - Real-time and backfill run commands, every configuration value with its default, every scenario
      name, both built-in profiles with their values, how to declare an additional profile, and the
      contract fields reproduced
    - _Requirements: 16.7_

  - [ ] 20.6 Wire the entry point end to end
    - Config → logger → profile registry → RandomStream factory and clock → swarm → pipeline →
      selected interfaces; activate only the selected interfaces, defaulting to `rest`, and reach a
      state serving `/health` with no config file and no MQTT settings
    - _Requirements: 15.6, 15.7, 14.1, 13.1_

- [ ] 21. Final checkpoint - full suite green
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. Skipping them means
  skipping the property tests that make determinism, mode equivalence, and contract fidelity
  verifiable, so treat them as deferred rather than unnecessary.
- Each of the 42 design properties maps to exactly one property test sub-task, tagged
  `Feature: sensor-simulator-service, Property {number}` and running at least 100 examples.
- The seven property families named in Requirement 16.3 land as: round-trip → 2.4, 2.5, 2.6;
  determinism and monotonic timestamps → 16.10, 16.4; index monotonicity → 10.2; spatial decay →
  12.9; mode equivalence → 16.8; humidity growth monotonicity → 8.6; observable effect → 14.5.
- Every domain component takes an injected clock and an injected per-`SiteCode` random stream. No
  task should introduce `datetime.now()` or a module-level random call in domain code.
- Checkpoints sit after the contract spine, configuration, the signal model, the scenario layer, and
  both interfaces, so each layer is validated before the next depends on it.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "2.1", "4.1", "4.2"] },
    { "id": 2, "tasks": ["1.3", "1.4", "2.2", "2.3", "4.3", "5.1"] },
    { "id": 3, "tasks": ["2.4", "2.5", "2.6", "2.7", "5.2", "10.1"] },
    { "id": 4, "tasks": ["2.8", "2.9", "5.3", "6.1", "8.1", "10.2", "10.3"] },
    { "id": 5, "tasks": ["6.2", "8.2", "8.3", "8.4", "8.5", "9.1"] },
    { "id": 6, "tasks": ["6.3", "8.6", "9.2", "9.3", "9.4", "12.1"] },
    { "id": 7, "tasks": ["6.4", "9.5", "9.6", "9.7", "12.2", "14.1"] },
    { "id": 8, "tasks": ["6.5", "6.6", "9.8", "9.9", "9.10", "9.11", "12.6", "13.1", "14.2", "14.3"] },
    { "id": 9, "tasks": ["12.3", "12.4", "12.5", "12.7", "13.2", "13.4", "14.4", "16.1"] },
    { "id": 10, "tasks": ["12.8", "12.9", "13.3", "13.5", "14.5", "14.6", "14.7", "14.8", "16.2", "16.3", "16.4", "16.5", "16.6"] },
    { "id": 11, "tasks": ["16.7", "16.8", "16.9", "16.12", "17.1", "18.1"] },
    { "id": 12, "tasks": ["16.10", "16.11", "16.13", "17.2", "18.2", "18.3"] },
    { "id": 13, "tasks": ["17.3", "18.4", "18.5", "18.6", "20.1", "20.3"] },
    { "id": 14, "tasks": ["17.4", "17.5", "17.6", "18.7", "20.2", "20.5", "20.6"] },
    { "id": 15, "tasks": ["20.4"] }
  ]
}
```
