# Requirements Document

## Introduction

The advisor's "what is the air right now?" answer depends on a **current** reading:
one whose interval start is within the serving freshness window
(`AQM_FRESHNESS_HOURS`, default 3h; Req 20.8 of ingestion-and-serving). The demo
sensors are not live — their readings come from the `seed_readings` job — so a
one-time seed goes stale within hours and the advisor honestly reports
"unavailable" thereafter.

For a hackathon demo that a judge may test at any time through **2026-10-08**,
`current` must stay fresh continuously without a human re-running the seed. This
feature keeps it fresh by re-running the existing, idempotent seed job on a
schedule. It changes no domain logic: it is the *deployment* of an existing job
on a cadence, mirroring `AssociationStack`.

## Requirements

### Requirement 1: A current reading is always available

**User Story:** As a judge testing the demo on any day through Oct 8, I want the
advisor to return a live air-quality reading for the demo user, so that the
headline "what's the air right now?" answer is not perpetually "unavailable".

#### Acceptance Criteria
1. WHEN the demo-data-refresh schedule fires THEN the system SHALL write demo
   readings whose newest interval start is at the current hour, for every demo
   site (CB0001–CB0003), for each demo species (PM25, NO2).
2. WHEN the serving API resolves `current` for the demo user at any time between
   deploy and 2026-10-08 THEN a reading SHALL fall within the freshness window,
   because the schedule cadence is shorter than that window.
3. The schedule cadence SHALL be at most the freshness window minus a safety
   margin (freshness 3h → cadence ≤ 2h), so a single missed or slow run cannot
   open a gap that reads as "unavailable".

### Requirement 2: Re-runs are idempotent and cheap

**User Story:** As the operator, I want repeated refreshes to add nothing beyond
a fresh tail, so the store does not grow unboundedly and history stays stable.

#### Acceptance Criteria
1. WHEN the refresh runs a second time THEN identical site metadata SHALL resolve
   to UNCHANGED and identical historical readings SHALL re-put to themselves (the
   `seed_readings` idempotence, Req 10.4 of ingestion), adding only the advancing
   hourly tail.
2. The refresh SHALL reuse `aqm_ingestion.jobs.seed_readings` unchanged; it SHALL
   NOT introduce a second, divergent copy of the seeding logic.

### Requirement 3: Least-privilege, offline-safe, disposable

**User Story:** As the operator, I want the refresher to follow the repo's infra
rules so it neither over-grants nor breaks the offline test guarantee, and is
easy to remove after the demo.

#### Acceptance Criteria
1. The refresher Lambda's IAM SHALL be scoped to exactly the `readings` and
   `sensor-registry` tables (read+write — the job upserts site metadata and writes
   readings), never `Resource: "*"`.
2. Synthesis of the stack SHALL resolve nothing from a live account (tables
   imported by name), so it belongs to the offline suite.
3. There SHALL be a documented teardown (its own `just` recipe / `cdk destroy`),
   because the stack is a demo-window artefact, not permanent infrastructure.
4. The stack SHALL carry no secret in any committed file (§7 of engineering
   practices).

## Glossary

- **Current reading**: a stored reading whose interval start is within the serving
  freshness window (`AQM_FRESHNESS_HOURS`, default 3h). Only a current reading makes
  the advisor's "right now" answer return live values instead of "unavailable".
- **Freshness window**: the max age, in hours, a reading may have and still count as
  current. Owned by the serving/selection config (Req 20.8 of ingestion-and-serving).
- **Recent tail**: the short run of HOURLY readings the seed job appends ending at the
  current hour, on top of the daily history, so a current reading exists at run time.
- **Demo site**: one of CB0001–CB0003 near demo-user-c's Cochabamba home, defined in
  `aqm_ingestion.jobs.seed_readings.DEMO_SITES`.
