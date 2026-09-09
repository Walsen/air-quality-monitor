# Implementation Plan: Ingestion & Serving Service

## Overview

Implementation proceeds inside-out. The contract spine comes first (record models, Serializer, Parser),
then the boundaries that make everything deterministic and offline-testable (Clock, the nine port
protocols, and an in-memory adapter for each). With fakes in place, the domain is built bottom-up in the
order the Ingest_Pipeline runs it — archive, validation, deduplication, calibration, unit conversion,
sub-index, NowCast, driving pollutant, quality flags — because each stage's tests depend on the stage
before it and on nothing after it. The pipeline is assembled only once its steps exist, then the two
ingestion entry points. The serving side follows the same order: profile, then each personalization
component, then enrichment, then authentication, then the response assembler that applies the guardrails,
and only then the routes. Configuration, the cloud adapters with their shared port contract suites, and
the container-fenced integration checks come last, because none of the domain work should wait on them.

Every step is TDD: the failing test comes first, then the smallest clean change that passes it. Each of
the 40 correctness properties from the design gets exactly one `hypothesis` property test running at
least 100 examples, tagged `Feature: ingestion-and-serving-service, Property {n}`. All domain code takes
an injected Clock — no `datetime.now()` anywhere in `domain/` — and no domain module imports from
`adapters/`. Language: Python 3.12, FastAPI, Pydantic, pytest + hypothesis, all under
`data-processing/`, importing nothing from another service directory.

## Tasks

- [ ] 1. Project scaffolding and observability foundation
  - [ ] 1.1 Create the `data-processing/` project skeleton and pinned manifest
    - Create the `src/aqm_ingestion/` package tree (`contract/`, `domain/`, `domain/calibration/`,
      `domain/aqi/`, `domain/personalization/`, `ports/`, `adapters/memory/`, `adapters/dynamodb/`,
      `adapters/s3/`, `adapters/mqtt/`, `adapters/http_feed/`, `adapters/forecast/`, `adapters/auth/`,
      `ingest/`, `serving/`, `config/`, `observability/`) and the sibling
      `tests/{unit,properties,contracts,integration}/` trees plus `config/`
    - Write `pyproject.toml` declaring Python 3.12 with every direct dependency pinned to one exact
      version; configure pytest markers (including the container-fenced marker) and a hypothesis profile
      with a minimum of 100 examples; commit `uv.lock`
    - Add the `just` recipes `test`, `test-integration`, `lint`, `typecheck`, `run`, and `up`
    - _Requirements: 28.1, 28.2, 28.4, 28.7, 28.11_

  - [ ] 1.2 Implement the structured JSON logger with central redaction
    - `observability/logging.py`: one single-line JSON object per event to stdout, configured once at
      startup, carrying instant, level, event name, and the optional `SiteCode`, `Species`, interval
      start, and route context; no `print()`
    - Implement the redaction rule in this one place so no call site can emit a profile field, a bearer
      credential, a resolved secret, or a claim beyond the user identity
    - _Requirements: 29.1, 29.2, 29.3, 29.4_

  - [ ] 1.3 Implement the metrics and gauge interface
    - Counters for records per transport, quarantines per reason, Readings per Quality_Flag, sites per
      fault category, dedup conflicts, forecast degradations, auth rejections per category, and responses
      per route; a gauge for the age of the most recent accepted Reading per site; one internal interface
      a deployment adapter publishes
    - _Requirements: 29.6, 29.7, 29.10_

  - [ ] 1.4 Write unit tests for log level filtering, redaction, and rejection
    - Each configured level emits at that level and above and suppresses below it
    - A log call carrying a profile field, a credential, or an extra claim emits the event with those
      values redacted
    - An invalid log level is rejected naming the supplied value and the permitted values
    - _Requirements: 29.3, 29.4, 29.9_

- [ ] 2. Independent record contract, Serializer, and Parser
  - [ ] 2.1 Define the two Pydantic record models in declared field order
    - `SensorDataRecord` with exactly nine fields and the case-sensitive `Species` and
      `RatificationStatus` enumerations; `SensorMetadataRecord` with exactly twenty fields plus the
      `GeoLocation`/`PointGeometry` models; `Latitude`/`Longitude` as 7-decimal signed strings
      character-identical to `Location.coordinates`; every key present even when null; `ScaledValue`
      preserved as received with no rounding
    - Classify `NO2` and `PM25` as Mass_Concentration measurements in the unit `Units` names, and
      preserve every field unmodified so the archived payload stays byte-comparable with what arrived
    - Add a golden-payload test using a captured Service 1 payload so a drift between the two copies
      fails here
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.9, 1.10, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 28.3_

  - [ ] 2.2 Implement the Serializer
    - Render both record types in declared field order with the contract's JSON value types, for the
      archive round-trip and the round-trip properties
    - _Requirements: 3.4_

  - [ ] 2.3 Implement the Parser with strict validation and batch tolerance
    - Accept a single object or an array of 0 to the configured maximum, preserving order; reject
      missing, unknown, wrong-typed, and invalid-enumeration fields; reject malformed JSON and oversize
      payloads; continue past a failed element returning accepted records plus one rejection per failure
      identified by array index; never produce a partially populated record
    - _Requirements: 3.1, 3.2, 3.3, 3.5, 3.7_

  - [ ]* 2.4 Write property test for measurement record round-trip
    - **Property 1: Measurement record round-trip**
    - **Validates: Requirements 3.4, 1.1, 1.3, 1.5**

  - [ ]* 2.5 Write property test for metadata record round-trip
    - **Property 2: Metadata record round-trip**
    - **Validates: Requirements 3.4, 2.1, 2.2, 2.3**

  - [ ]* 2.6 Write property test for parser rejection totality
    - **Property 3: Parser rejection is total and reasoned**
    - **Validates: Requirements 3.3, 3.7**

  - [ ]* 2.7 Write property test for batch partial acceptance
    - **Property 4: Batch partial acceptance preserves accepted records**
    - **Validates: Requirements 3.5, 3.1**

  - [ ] 2.8 Write unit tests for the metadata cross-field rules
    - `Location.coordinates` disagreeing with the sibling `Latitude`/`Longitude` is quarantined naming
      both; `EndDate` earlier than `StartDate` is quarantined naming both; a non-`ug.m-3` unit on a
      concentration species is quarantined naming received and expected
    - _Requirements: 2.3, 2.6, 1.8_

- [ ] 3. Time boundary, port protocols, and in-memory adapters
  - [ ] 3.1 Define the Clock port and its implementations
    - `ports/clock.py` with `now()` returning a timezone-aware UTC instant; `SystemClock` at the process
      edge; `FixedClock` and `AdvanceableClock` for tests and backfill
    - _Requirements: 27.1, 27.8_

  - [ ] 3.2 Define the nine port protocols
    - `ReadingsStore`, `SensorRegistryStore`, `RawArchive`, `ProfileStore`, `MeteorologyProvider`,
      `ForecastClient`, `MqttTransport`, `FeedClient`, and `Authenticator` as `Protocol` definitions with
      no cloud types in any signature; `WindowResult` carries its `truncated` flag in the type
    - _Requirements: 14.1, 15.1, 17.1, 24.1, 18.1, 14.8_

  - [ ] 3.3 Implement an in-memory adapter for every port
    - Deterministic fakes sufficient for the whole offline suite: readings, registry, archive, profile,
      meteorology, forecast, MQTT, feed, and a local Authenticator resolving configured test credentials
      to configured identities
    - _Requirements: 28.5, 14.9, 15.10, 16.8, 17.11, 18.8, 24.9_

  - [ ] 3.4 Write a lint or test check enforcing the dependency direction
    - Assert no module under `domain/` imports from `adapters/`, and no domain function signature takes
      the whole configuration object
    - _Requirements: 14.1, 27.1_

- [ ] 4. Raw archive and archive-first ordering
  - [ ] 4.1 Implement the archive write with derived key and metadata
    - Write the payload byte-for-byte with `ArchiveMeta`; derive the key as
      `raw/{yyyy}/{MM}/{dd}/{HH}/{archive_id}.json`; derive the archive identifier from injected inputs so
      a replay reproduces it; append-only with no update or delete path; never archive a credential or a
      profile field
    - _Requirements: 16.1, 16.2, 16.3, 16.5, 16.6, 27.6_

  - [ ]* 4.2 Write property test for archive precedence and round-trip
    - **Property 5: Raw archive precedes processing and round-trips**
    - **Validates: Requirements 16.1, 16.4, 16.2, 6.11**

  - [ ] 4.3 Write unit test for archive write failure handling
    - A failed archive write leaves the message unacknowledged on the push path and fails the invocation
      on the pull path, logging one `error` naming the failure kind, and processes nothing
    - _Requirements: 16.7, 4.6_

- [ ] 5. Reading validation and quarantine
  - [ ] 5.1 Implement the Validator collecting every violated rule
    - Finiteness and sign; per-species plausibility ceilings; future-dating against the Clock plus skew
      tolerance; staleness against the Retention_Window; interval-boundary alignment; unit agreement.
      Collect all reasons rather than short-circuiting
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

  - [ ] 5.2 Implement quarantine retention and reporting
    - Retain the record as received with its reasons, ingestion instant, transport, and archive
      identifier; emit one `warning` per record; increment the per-reason counter; never store a
      quarantined record and never serve it
    - _Requirements: 6.8, 6.9, 6.10_

  - [ ]* 5.3 Write property test for quarantine completeness and exclusion
    - **Property 6: Quarantined records are fully reasoned and never stored**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.10**

  - [ ] 5.4 Write unit test for the unknown-site allowance
    - A record whose `SiteCode` is absent from the registry is stored with one `warning` logged, and the
      site is excluded from geographic results until its metadata arrives
    - _Requirements: 6.7_

- [ ] 6. Deduplication
  - [ ] 6.1 Implement the Dedup_Key and the resolution function
    - Key from `SiteCode`, `Species`, interval start, and `Duration`; resolution as a pure function of the
      two candidates: store when absent, no-write on equality, ratified supersedes provisional, provisional
      never displaces ratified, and equal-status value conflict keeps the greater with a
      `suspect_conflict` flag and a `warning`
    - Apply deduplication before calibration so a duplicate consumes no correction work
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.9_

  - [ ]* 6.2 Write property test for deduplication order independence
    - **Property 8: Deduplication is order-independent**
    - **Validates: Requirements 7.7, 7.6**

  - [ ]* 6.3 Write property test for the resolution precedence
    - **Property 9: Deduplication resolution follows the stated precedence**
    - **Validates: Requirements 7.4, 7.5, 7.6**

  - [ ] 6.4 Write unit test for archive-regardless-of-dedup
    - Every received payload is archived even when the record is a discarded duplicate
    - _Requirements: 7.10_

- [ ] 7. Calibration strategy registry
  - [ ] 7.1 Implement the strategy protocol, registry, and the `identity` strategy
    - `CalibrationStrategy` with `name`, `domain`, and `correct(reported, rh)`; a registry resolving by
      name; `identity` as the NO2 default and the no-RH fallback
    - _Requirements: 8.2, 8.6, 8.12_

  - [ ] 7.2 Implement the `rh_linear` strategy
    - `max(0, a*reported + b*rh + c)` with configurable coefficients defaulting to 0.524, -0.0862, and
      5.75, and a declared Calibration_Domain of 0 to 250 µg/m³ and 20 to 90 percent RH
    - _Requirements: 8.3_

  - [ ] 7.3 Implement RH resolution and domain flagging
    - Resolve RH from the meteorology channel mapping, then the MeteorologyProvider, then none; apply the
      strategy out of domain but flag `calibrated_extrapolated` with a `warning`; flag `uncalibrated` with
      `low` Confidence when no RH exists; retain the reported value and record the strategy name and RH
      source on the reading
    - _Requirements: 8.4, 8.5, 8.7, 8.10, 8.11, 3.6_

  - [ ]* 7.4 Write property test for humidity monotonicity
    - **Property 10: Calibration humidity monotonicity**
    - **Validates: Requirements 8.8**

  - [ ]* 7.5 Write property test for corrected-value bounds
    - **Property 11: Corrected values are finite and non-negative**
    - **Validates: Requirements 8.9, 8.3, 8.10**

  - [ ]* 7.6 Write property test for out-of-domain flagging
    - **Property 12: Out-of-domain calibration is flagged, not refused**
    - **Validates: Requirements 8.7, 8.6, 13.1**

- [ ] 8. Unit conversion
  - [ ] 8.1 Implement the temperature- and pressure-parameterized conversion
    - The ideal-gas relation with the molar gas constant 8.314462618 J/(mol·K) and NO2 molar mass
      46.0055 g/mol, in both directions, taking temperature and pressure as explicit parameters
    - Resolve temperature and pressure from the channel, then the provider, then the configured defaults
      of 15 °C and 740 hPa; record the values and their source; cap Confidence at `medium` when defaulted;
      treat a non-positive resolved value as an unavailable conversion with an `error` and no NO2
      Sub_Index
    - _Requirements: 9.1, 9.2, 9.3, 9.7, 9.8, 9.9_

  - [ ]* 8.2 Write property test for conversion round-trip
    - **Property 13: Unit conversion round-trip**
    - **Validates: Requirements 9.4**

  - [ ]* 8.3 Write property test for temperature and pressure response
    - **Property 14: Conversion factor responds correctly to temperature and pressure**
    - **Validates: Requirements 9.5**

  - [ ]* 8.4 Write property test for the pinned reference factors
    - **Property 15: Conversion factor is pinned at reference conditions**
    - **Validates: Requirements 9.6, 9.2**

- [ ] 9. Breakpoint tables and sub-index computation
  - [ ] 9.1 Implement versioned Breakpoint_Table loading and validation
    - Load tables as data by identifier with `epa-2024-05-06` the default; reject on load any overlapping
      band, gap, non-increasing breakpoint or index sequence, or unit mismatch, naming the offending band
    - Ship the default table with the PM2.5 bands over 24-hour µg/m³ and the NO2 bands over 1-hour ppb
      exactly as Requirements 10.3 and 10.4 state
    - _Requirements: 10.2, 10.3, 10.4, 10.11_

  - [ ] 9.2 Implement sub-index interpolation, banding, truncation, and the ceiling
    - Truncate PM2.5 to one decimal and NO2 to whole ppb before locating the band; interpolate linearly;
      round half away from zero; map to the six band names; extrapolate the top band's slope above the
      table, cap at the configured ceiling, and report `Hazardous`; compute a Sub_Index only for species
      the table defines
    - _Requirements: 10.1, 10.5, 10.6, 10.9, 10.10, 10.12_

  - [ ]* 9.3 Write property test for sub-index monotonicity
    - **Property 16: Sub-index monotonicity**
    - **Validates: Requirements 10.7**

  - [ ]* 9.4 Write property test for breakpoint boundary exactness
    - **Property 17: Breakpoint boundary exactness**
    - **Validates: Requirements 10.8, 10.1, 10.6**

  - [ ]* 9.5 Write property test for band agreement and the ceiling
    - **Property 18: Sub-index and band agree, and the ceiling holds**
    - **Validates: Requirements 10.5, 10.9**

  - [ ] 9.6 Write unit tests pinning every published breakpoint boundary
    - Each PM2.5 and NO2 band's lower and upper breakpoint asserted as an exact concentration-to-Sub_Index
      pair, so a self-consistent wrong table cannot pass
    - _Requirements: 10.3, 10.4, 10.8_

- [ ] 10. NowCast
  - [ ] 10.1 Implement the NowCast calculator as a pure function of an ordered series
    - Weight factor `1 - (max - min) / max` bounded below at 0.5, and 1 when the maximum is 0; weighted
      average `sum(w**i * c_i) / sum(w**i)` skipping absent hours in both sums; default 12-hour window;
      return the value with its coverage metadata
    - Apply NowCast to PM2.5 only, leaving NO2 on its own 1-hour value
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.9, 11.10_

  - [ ] 10.2 Implement the insufficient-coverage fallback
    - With fewer than two of the three most recent hours available, report no NowCast, fall back to the
      Reading's own corrected hourly value, cap Confidence at `low`, and record the fallback in the Basis
    - _Requirements: 11.5_

  - [ ]* 10.3 Write property test for NowCast bounds
    - **Property 21: NowCast is bounded by its window**
    - **Validates: Requirements 11.6, 11.3**

  - [ ]* 10.4 Write property test for the constant-series case
    - **Property 22: NowCast preserves a constant series**
    - **Validates: Requirements 11.7, 11.4**

  - [ ]* 10.5 Write property test for recency weighting and determinism
    - **Property 23: NowCast weights recency and is deterministic**
    - **Validates: Requirements 11.8, 11.4, 11.11**

  - [ ]* 10.6 Write property test for the coverage fallback
    - **Property 24: Insufficient NowCast coverage falls back and downgrades confidence**
    - **Validates: Requirements 11.5, 13.2**

- [ ] 11. Overall AQI and driving pollutant
  - [ ] 11.1 Implement the overall AQI, driving-pollutant selection, and method reporting
    - Maximum across available Sub_Index values; argmax with the configured species precedence as
      tie-break; band from the overall value; report `nowcast` or `hourly` per contributing Sub_Index;
      report no overall value when none is available; set overall Confidence to the lowest contributing
      Confidence; declare in the Basis which species had no Sub_Index and why
    - Structurally exclude `NO2Index` and `PM25Index` from every index path
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 12.7, 12.8, 1.7_

  - [ ]* 11.2 Write property test for maximum and argmax
    - **Property 19: Overall AQI is the maximum and the driving pollutant is its argmax**
    - **Validates: Requirements 12.1, 12.2, 12.3**

  - [ ]* 11.3 Write property test for index-species exclusion
    - **Property 20: Index species never influence a computed index**
    - **Validates: Requirements 1.7, 12.1**

- [ ] 12. Quality assessment and fault detection
  - [ ] 12.1 Implement the Quality_Assessor as one total function
    - Map calibration outcome, RH source, conversion source, NowCast coverage, dedup conflict, and fault
      state to exactly one Quality_Flag and one Confidence, applying the `medium` cap for a defaulted
      conversion and the `low` cap for insufficient NowCast coverage
    - _Requirements: 13.1, 13.2, 13.12_

  - [ ] 12.2 Implement the Fault_Detector over stored history
    - Stuck value after the configured consecutive equal-within-tolerance intervals; dropout after the
      configured consecutive missing intervals; drift against the peer median with a minimum peer count
      and radius, skipped with a `debug` event when too few peers exist; flag raise and clear each logged;
      clear after the configured consecutive clean intervals; assign `suspect_fault` without quarantining
    - _Requirements: 13.3, 13.4, 13.5, 13.6, 13.7, 13.8, 13.9, 13.11_

  - [ ]* 12.3 Write property test for confidence derivation
    - **Property 31: Confidence derivation is deterministic and respects its caps**
    - **Validates: Requirements 13.2, 13.12, 9.8**

- [ ] 13. Readings store semantics
  - [ ] 13.1 Implement window and latest-per-species queries with retention and truncation
    - Half-open window from start inclusive to end exclusive, ordered by ascending interval start then the
      configured species precedence; latest-per-species for a set of sites; retention exclusion measured
      from the Clock; result bounded by the configured maximum with truncation reported to the caller
    - _Requirements: 14.2, 14.3, 14.4, 14.7, 14.8, 14.11_

  - [ ]* 13.2 Write property test for store round-trip
    - **Property 25: Readings store round-trip**
    - **Validates: Requirements 14.5, 14.2**

  - [ ]* 13.3 Write property test for window completeness and exclusivity
    - **Property 26: Window query completeness and half-open exclusivity**
    - **Validates: Requirements 14.6, 14.3**

  - [ ]* 13.4 Write property test for retention exclusion
    - **Property 27: Retention window excludes aged readings**
    - **Validates: Requirements 14.7, 14.8**

- [ ] 14. Sensor registry and geographic query
  - [ ] 14.1 Implement registry upsert, activity marking, and last-upserted tracking
    - Upsert by `SiteCode` writing only on difference; mark inactive on a past non-null `EndDate`; log a
      `warning` on a changed position; record the last-upsert instant
    - _Requirements: 15.2, 15.11, 2.8, 15.9_

  - [ ] 14.2 Implement great-circle distance and nearest-N selection
    - Distance with an Earth radius of 6,371.0088 km, taking each site's position from its `Latitude`
      and `Longitude` fields parsed as decimal degrees; nearest-N ordered by ascending distance with
      `SiteCode` tie-break, honoring an inclusive maximum radius, excluding inactive sites, and returning
      fewer than N when fewer exist
    - _Requirements: 15.3, 15.4, 15.5, 15.6, 15.8, 2.7_

  - [ ]* 14.3 Write property test for nearest-N ordering and radius inclusion
    - **Property 28: Nearest-N ordering and radius inclusion**
    - **Validates: Requirements 15.5, 15.6, 15.7, 15.8, 20.2**

  - [ ]* 14.4 Write property test for distance symmetry
    - **Property 29: Great-circle distance is symmetric and zero on identity**
    - **Validates: Requirements 15.4, 20.10**

- [ ] 15. Ingest pipeline assembly
  - [ ] 15.1 Implement the Ingest_Pipeline as a Template Method
    - The fixed sequence archive → parse → validate → deduplicate → calibrate → convert → NowCast →
      sub-index → driving pollutant → quality/fault flags → store, transport-agnostic, with per-record
      failure isolation and a batch summary event naming counts received, accepted, deduplicated,
      quarantined, and per Quality_Flag with elapsed duration
    - Assemble the `CalibratedReading` carrying every provenance field the Basis needs
    - _Requirements: 6.11, 7.9, 8.1, 29.5, 4.4_

  - [ ]* 15.2 Write property test for ingestion idempotence
    - **Property 7: Ingestion is idempotent**
    - **Validates: Requirements 7.8, 7.3**

  - [ ] 15.3 Write unit tests for the pipeline ordering guarantees
    - Calibration never runs on a record the Validator quarantined; the archive write precedes the first
      validation rule; deduplication precedes calibration
    - _Requirements: 8.1, 6.11, 7.9_

- [ ] 16. Ingestion entry points
  - [ ] 16.1 Implement the MQTT entry point
    - Subscribe to the configured topic filter, default `aqm/sensors/+/data`, logging the resolved filter;
      extract the site identifier from the topic pattern and quarantine a payload whose `SiteCode`
      disagrees; acknowledge only after the archive write; record the transport as `mqtt`; isolate
      per-message failures; reconnect with exponential backoff from 1 second to the configured maximum,
      indefinitely; open no subscription when disabled
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8_

  - [ ] 16.2 Implement the feed poller entry point
    - Derive the window from the last ingested interval minus the configured overlap, or the initial
      backfill span; send the credential in `X-API-KEY` resolved only from the environment or a supplied
      path and never logged; bound the invocation by the maximum record count, resuming without a gap;
      refresh the registry on its cadence; on failure leave the high-water mark unchanged and exit
      non-zero; log the poll summary; make no request when disabled
    - _Requirements: 5.1, 5.2, 5.3, 5.5, 5.6, 5.7, 5.8, 5.9_

  - [ ]* 16.3 Write property test for push and pull convergence
    - **Property 39: Push and pull ingestion converge on the same state**
    - **Validates: Requirements 27.7, 5.4, 27.3**

- [ ] 17. Profile store and data minimization
  - [ ] 17.1 Implement the User_Profile model as an allowlist with reduced-precision locations
    - Exactly the fields Requirement 17.2 declares; the Condition and Sensitivity_Level enumerations;
      locations rounded to the configured precision and capped at the configured count; a required
      Consent_Record with a recognized version; rejection naming any field outside the allowlist
    - _Requirements: 17.2, 17.3, 17.4, 17.5, 17.6, 17.7_

  - [ ] 17.2 Implement profile read, write, delete, and the default profile
    - Keyed by the verified identity only; deletion removing the profile and every identifying audit
      field while retaining de-identified counts; the configured default profile served with
      `usedDefaultProfile` declared when none exists
    - _Requirements: 17.1, 17.8, 17.12_

  - [ ] 17.3 Write unit tests for minimization and isolation
    - No profile field appears in any log entry, the archive, or an error message; no response contains
      another user's profile data or Readings selected by another user's locations
    - _Requirements: 17.9, 17.10_

- [ ] 18. Condition weighting
  - [ ] 18.1 Implement the Condition_Weighting registry and the default map
    - A registry keyed by Condition producing an ordered species list and a pollen-relevance flag, with
      the default map of Requirement 21.2; restrict the Weighted_Focus to available species preserving
      order; report every named-but-unavailable species; substitute no proxy and infer no value; order
      each site's measurements by focus then species precedence
    - _Requirements: 21.1, 21.2, 21.3, 21.4, 21.5, 21.6, 21.7, 21.8_

  - [ ]* 18.2 Write property test for ordering without value alteration
    - **Property 32: Weighting orders the response without altering any value**
    - **Validates: Requirements 21.3, 21.4, 21.7, 21.9**

- [ ] 19. Personal thresholds and escalation
  - [ ] 19.1 Implement threshold resolution and crossing evaluation
    - Effective escalation Sub_Index from the precedence personal threshold, then Sensitivity_Level
      mapping of 101, 76, and 51, then the Orange_Band lower bound of 101; crossings on greater-or-equal;
      concentration thresholds converted through the same Breakpoint_Table; report every crossing with its
      site, species, Sub_Index, and threshold, the `thresholdSource`, and the effective escalation value;
      report the Confidence alongside a crossing; reject an out-of-range threshold at the profile write
    - _Requirements: 22.1, 22.2, 22.3, 22.4, 22.5, 22.6, 22.8, 22.9, 22.10_

  - [ ]* 19.2 Write property test for crossing at greater-or-equal
    - **Property 33: Threshold crossing holds exactly on greater-or-equal**
    - **Validates: Requirements 22.3, 22.7, 22.6, 22.1**

  - [ ] 19.3 Write unit tests pinning the escalation points
    - 101, 76, and 51 for `standard`, `elevated`, and `high`, and the Orange_Band default of 101
    - _Requirements: 22.1, 22.2_

- [ ] 20. Inhaled dose
  - [ ] 20.1 Implement the dose calculator
    - Corrected concentration times the activity-adjusted breathing rate times duration, with the rate map
      defaulting to 0.5, 1.0, 2.0, and 3.2 m³/h; `null` when activity inputs are absent; never derived
      from the reported value or a Sub_Index; the concentration and rate named in the Basis; Confidence
      capped at the concentration's; duration range validated at the profile write; no clinical framing
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 23.6, 23.7, 23.8_

  - [ ]* 20.2 Write property test for dose scaling
    - **Property 34: Inhaled dose scales correctly and vanishes at zero duration**
    - **Validates: Requirements 23.5, 23.1, 23.3**

- [ ] 21. Geographic selection for serving
  - [ ] 21.1 Implement the Geo_Selector with freshness and fallbacks
    - Nearest-N per User_Location within the inclusive radius, default 3 and 10 km; distances rounded to
      the configured precision with their location names; a site serving several locations included once
      naming all of them; a location with no site in radius declared rather than silently widened; the
      configured fallback selection when the profile has no location, declared as used; readings limited
      to the freshness window with `asOf` reported; a site with no fresh reading included with an empty
      measurement set and no overall AQI
    - _Requirements: 20.1, 20.2, 20.3, 20.4, 20.5, 20.6, 20.7, 20.8, 20.9, 20.10_

- [ ] 22. Forecast and pollen enrichment
  - [ ] 22.1 Implement enrichment with caching and graceful degradation
    - Retrieve forecast and pollen for the primary location through the port; report the provider and
      retrieval instant; on failure or timeout serve without them, set `degraded`, log a `warning`, and do
      not fail the request; cache by rounded coordinates for the configured time-to-live; derive the trend
      against the configured band width; report pollen per taxon only when the weighting marks pollen
      relevant; pass nothing but the rounded coordinates; resolve any credential from the environment only
    - _Requirements: 24.1, 24.2, 24.3, 24.4, 24.5, 24.6, 24.7, 24.8, 24.10_

- [ ] 23. Authentication and authorization
  - [ ] 23.1 Implement credential resolution and per-user scoping
    - Resolve the bearer credential through the Authenticator before any handler logic; 401 for an absent,
      malformed, or empty header and for an unverifiable, expired, or wrong-audience credential reporting
      only the category; 403 for a cross-identity request; the identity taken only from verified claims,
      never from a parameter, path, or body; no credential or extra claim in any response, log, or audit
      entry; a `warning` logged per rejection; the configured per-identity rate limit with 429
    - _Requirements: 18.1, 18.2, 18.3, 18.5, 18.6, 18.7, 18.9, 18.10, 18.11_

- [ ] 24. Response assembler, basis, guardrails, and audit
  - [ ] 24.1 Implement the Basis assembly
    - Populate the Breakpoint_Table identifier, the per-species Calibration_Strategy, the RH source, the
      conversion source, the NowCast window and coverage, and one record reference per contributing
      Reading, from the provenance fields the pipeline recorded
    - _Requirements: 25.5, 10.2, 8.11, 9.7, 11.9_

  - [ ] 24.2 Implement the Guardrail_Enforcer and the guardrail envelope
    - Attach the non-empty disclaimer deferring to the clinician's action plan, the `exposure-reduction`
      advisory scope, and the emergency guidance naming the red-flag symptoms, on every data-bearing
      route including history; emit no diagnosis, medication name, dose, schedule, or treatment change;
      run the forbidden-phrase check and fail with 500 rather than emit a violating body; take no
      autonomous action on a crossing
    - _Requirements: 25.1, 25.2, 25.3, 25.4, 25.6, 25.10, 25.11, 25.12_

  - [ ] 24.3 Implement the audit writer
    - Append one record per served response naming identity, instant, route, table identifier, strategy
      set, whether a crossing was reported, and the contributing record references; store no Condition,
      Sensitivity_Level, threshold value, location coordinate, forecast, or pollen value; append-only with
      the configured retention
    - _Requirements: 25.7, 25.8, 25.9_

  - [ ]* 24.4 Write property test for value accompaniment and member presence
    - **Property 30: Every served value carries a quality flag and a confidence**
    - **Validates: Requirements 13.10, 14.11, 19.12**

  - [ ]* 24.5 Write property test for the guardrail envelope
    - **Property 35: The guardrail envelope is always present**
    - **Validates: Requirements 25.1, 25.12, 25.2, 25.3**

  - [ ]* 24.6 Write property test for basis population
    - **Property 36: The basis is always populated**
    - **Validates: Requirements 25.5, 10.2, 8.11, 9.7, 11.9**

  - [ ]* 24.7 Write property test for forbidden phrasing and log hygiene
    - **Property 37: No forbidden phrasing in responses and no sensitive data in logs**
    - **Validates: Requirements 25.4, 25.11, 29.4, 17.9, 18.7**

- [ ] 25. Serving API routes
  - [ ] 25.1 Implement the response Pydantic models mirroring the pinned shape
    - One model per nested member of the Requirement 19.2 shape so a missing member fails at construction;
      `null` emitted for an unavailable member with no key dropped; every timestamp an ISO-8601 UTC
      whole-second `Z` instant
    - _Requirements: 19.2, 19.12, 19.13_

  - [ ] 25.2 Implement `GET /v1/air-quality/me`
    - Compose profile, geo selection, readings, personalization, enrichment, basis, and guardrails within
      the configured latency budget
    - _Requirements: 19.1, 19.11_

  - [ ] 25.3 Implement `GET /v1/air-quality/history`
    - `siteCode`, optional `species`, `startTime`, `endTime`; readings ordered by ascending interval start
      each carrying corrected value, flag, confidence, sub-index, and band, with the guardrail envelope;
      the maximum span enforced
    - _Requirements: 19.3, 19.9_

  - [ ] 25.4 Implement the profile routes and the health route
    - `GET`, `PUT`, and `DELETE /v1/profile/me`; unauthenticated `GET /health` returning status, resolved
      table identifier, and resolved strategy names with no reading, profile, or credential
    - _Requirements: 19.4, 19.5, 19.10_

  - [ ] 25.5 Implement boundary error handling for every documented failure
    - Authenticate before validating parameters; 400 for the parameter failures naming each offending
      parameter and its permitted form; 404 for an unknown site; 429 for the rate limit; never 500 on
      input
    - _Requirements: 19.6, 19.7, 19.8, 18.4_

  - [ ]* 25.6 Write property test for response determinism
    - **Property 38: Serving responses are deterministic**
    - **Validates: Requirements 27.2, 27.4**

  - [ ]* 25.7 Write property test for authentication precedence
    - **Property 40: Authentication precedes validation and leaks nothing**
    - **Validates: Requirements 18.2, 18.3, 18.4, 18.5, 18.9**

  - [ ] 25.8 Write named failure-mode unit tests
    - One example test each asserting the documented status and body: absent header (401), malformed
      credential (401), cross-identity request (403), `startTime` after `endTime` (400), unparseable
      instant (400), one bound without the other (400), bad `species` (400), oversize span (400), unknown
      site (404), rate limit (429), and a forbidden-phrase response (500 with the body withheld)
    - _Requirements: 19.6, 19.7, 19.8, 19.9, 18.2, 18.3, 18.5, 18.11, 25.11_

- [ ] 26. Configuration loader
  - [ ] 26.1 Implement fail-fast configuration resolution and validation
    - Environment over file over defaults, with the resolved non-secret configuration logged once; every
      value validated before a listener opens, a subscription is created, or a store call is issued; all
      failures collected with one message per invalid value, then a non-zero exit and never a half-start
    - Validate registry-name resolution for every pluggable name, the breakpoint table shape, calibration
      coefficients and domain bounds, all durations and their ordering including
      Retention_Window ≥ maximum history span, every bound pair and positive limit, secret resolution for
      enabled interfaces without ever echoing a secret, the file's readability with no fallback, that at
      least one interface is enabled, and the log level
    - _Requirements: 26.1, 26.2, 26.3, 26.4, 26.5, 26.6, 26.7, 26.8, 26.9, 26.10, 26.11, 8.13, 10.11_

  - [ ] 26.2 Write unit tests for every documented configuration failure
    - One test per row of the design's configuration-error table, asserting the message names the value
      and the violated constraint, that all failures are reported rather than only the first, and that the
      exit status is non-zero
    - _Requirements: 26.3, 26.4, 26.5, 26.6, 26.7, 26.8, 26.9, 26.10, 29.9_

- [ ] 27. Cloud adapters and shared port contract suites
  - [ ] 27.1 Write the shared behavioral test suite per port
    - One suite per port parameterized over every adapter of that port, asserting the same behavior for
      each, so an adapter swap cannot change domain behavior
    - _Requirements: 14.10, 15.10, 16.8, 17.11, 28.10_

  - [ ] 27.2 Implement the DynamoDB adapters
    - Readings keyed by `SITE#{SiteCode}#SP#{Species}` with the interval start as sort key, using a
      conditional write for the dedup resolution; registry keyed by `SiteCode`; profiles keyed by user
      identity; quarantine and audit tables with their time-ordered sort keys
    - _Requirements: 14.9, 15.10, 17.11_

  - [ ] 27.3 Implement the S3 raw archive adapter
    - The time-ordered key layout, append-only writes, and byte-identical reads
    - _Requirements: 16.5, 16.8_

  - [ ] 27.4 Implement the MQTT, feed, forecast, and meteorology adapters
    - Real transports behind the same ports, with credentials resolved only at runtime
    - _Requirements: 4.1, 5.1, 24.9, 8.4_

- [ ] 28. Local stack and container-fenced integration checks
  - [ ] 28.1 Write the Docker Compose definition and the local stack recipe
    - The service, a local MQTT broker, and local store emulation sufficient to exercise the DynamoDB and
      S3 adapters without a cloud account; a git-ignored credential tree and no committed secret
    - _Requirements: 28.8, 28.9_

  - [ ] 28.2 Write the container-fenced integration checks
    - Broker connect-subscribe-ingest, the DynamoDB and S3 adapters against local emulation, and the
      Compose smoke check, each marked so the offline suite excludes it and none depending on cloud
      credentials
    - _Requirements: 28.6_

  - [ ] 28.3 Verify the offline guarantee
    - Assert the default suite passes with no AWS credentials present and no network beyond localhost,
      exercising the in-memory adapter for every port
    - _Requirements: 28.5_

- [ ] 29. Final wiring and checkpoint
  - [ ] 29.1 Wire the composition root
    - Build the pipeline, the entry points, and the FastAPI application from configuration, selecting
      every adapter by name, with the Clock and all ports injected at one place
    - _Requirements: 26.1, 26.5, 27.1_

  - [ ] 29.2 Verify determinism end to end
    - The determinism harness constructing two independent instances from the same configuration and Clock
      instant and asserting byte-identical serving bodies and identical stored readings
    - _Requirements: 27.2, 27.3, 27.5, 27.6_

  - [ ] 29.3 Final checkpoint — full suite green
    - Run the offline suite and the container-fenced suite; confirm every property test is present and
      running at least 100 examples; ask the user if questions arise
    - _Requirements: 28.4, 28.11_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. Skipping them means skipping the
  property tests that make calibration correctness, index correctness, determinism, and the guardrail
  invariants verifiable, so treat them as deferred rather than unnecessary.
- Each of the 40 design properties maps to exactly one property test sub-task, tagged
  `Feature: ingestion-and-serving-service, Property {number}` and running at least 100 examples.
- The property families the requirements name explicitly land as: contract round-trip → 2.4, 2.5;
  idempotency → 15.2; humidity monotonicity → 7.4; index monotonicity → 9.3; determinism → 25.6, 16.3;
  guardrail invariants → 24.5, 24.6, 24.7.
- Three numeric areas are pinned by example tests in addition to their properties, because a
  self-consistent wrong implementation would satisfy the properties: the breakpoint boundaries (9.6), the
  conversion reference factors (8.4), and the escalation points (19.3).
- Every domain component takes an injected Clock. No task should introduce `datetime.now()` in domain code
  or an import from `adapters/` into `domain/`; task 3.4 adds the check that enforces it.
- The in-memory adapters arrive in task 3, before any domain stage needs them, which is what lets tasks 4
  through 14 be built and tested without any cloud adapter existing.
- Checkpoints sit after the contract spine (task 2), the pipeline assembly (task 15), the serving routes
  (task 25), and the final wiring (task 29), so each layer is validated before the next depends on it.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1", "3.1"] },
    { "id": 2, "tasks": ["1.4", "2.2", "2.3", "3.2"] },
    { "id": 3, "tasks": ["2.4", "2.5", "2.6", "2.7", "2.8", "3.3"] },
    { "id": 4, "tasks": ["3.4", "4.1", "7.1", "8.1", "9.1"] },
    { "id": 5, "tasks": ["4.2", "4.3", "5.1", "7.2", "8.2", "8.3", "8.4", "9.2"] },
    { "id": 6, "tasks": ["5.2", "6.1", "7.3", "9.3", "9.4", "9.5", "9.6", "10.1"] },
    { "id": 7, "tasks": ["5.3", "5.4", "6.2", "6.3", "6.4", "7.4", "7.5", "7.6", "10.2", "11.1"] },
    { "id": 8, "tasks": ["10.3", "10.4", "10.5", "10.6", "11.2", "11.3", "12.1", "13.1", "14.1"] },
    { "id": 9, "tasks": ["12.2", "12.3", "13.2", "13.3", "13.4", "14.2", "15.1", "17.1"] },
    { "id": 10, "tasks": ["14.3", "14.4", "15.2", "15.3", "16.1", "16.2", "17.2", "18.1", "21.1"] },
    { "id": 11, "tasks": ["16.3", "17.3", "18.2", "19.1", "20.1", "22.1", "23.1", "24.1"] },
    { "id": 12, "tasks": ["19.2", "19.3", "20.2", "24.2", "24.3", "25.1"] },
    { "id": 13, "tasks": ["24.4", "24.5", "24.6", "24.7", "25.2", "25.3", "25.4", "26.1"] },
    { "id": 14, "tasks": ["25.5", "25.8", "26.2", "27.1"] },
    { "id": 15, "tasks": ["25.6", "25.7", "27.2", "27.3", "27.4"] },
    { "id": 16, "tasks": ["28.1", "28.2", "28.3", "29.1"] },
    { "id": 17, "tasks": ["29.2"] },
    { "id": 18, "tasks": ["29.3"] }
  ]
}
```
