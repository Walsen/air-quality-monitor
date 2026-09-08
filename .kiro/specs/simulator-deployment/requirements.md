# Requirements Document

## Introduction

This document specifies the deployment and operation of the Sensor Simulator Service (Service 1 of
three in this monorepo) on AWS. The Simulator's own behavior — signal generation, the record
contract, scenarios, geography, and configuration semantics — is specified in the sibling spec
`sensor-simulator-service`, which places deployment out of its scope and defers one open decision to
this spec (its assumption A6, the source of served `/SensorData` records). This document resolves
that decision and everything around it.

The whole design rests on one property the sibling spec already guarantees: every emitted value is a
pure function of the Seed, the `SiteCode`, and the simulated timestamp
(`sensor-simulator-service` Requirement 11, and Requirement 12 criterion 4 for mode equivalence).
Because per-sensor work is independently recomputable, an invocation-scoped runtime can reproduce a
completed Publish_Interval from scratch with no carried state. That is what makes the runtime
choice a **per-interface** choice rather than a single choice for the service: invocation-scoped
compute for the pull interface and for Backfill_Mode, and a resident container task for the
real-time MQTT push interface, which needs per-device TLS sessions and an in-memory buffer that
outlive an outage.

The counterweight is cost. A resident task bills continuously, and at demo scale it is the single
largest fixed line item in the system, so the push path is deployed scaled to zero and raised only
for a Demo_Window.

This document **cites** the sibling spec wherever a contract field, a topic, an endpoint, a
threshold, or a numeric default belongs to it, in the form
`sensor-simulator-service Requirement X criterion Y`. It redefines none of them.

**Known inconsistencies in the sibling spec.** Two inconsistencies surfaced while deriving these
requirements. Both are recorded here rather than fixed, because they belong to the other spec:
`sensor-simulator-service` Requirement 13 criterion 4 gives the MQTT port a documented default of
8883 while that spec's Requirement 15 criterion 6 lists the MQTT port among the values for which the
Config_Loader applies no documented default; and that spec's Requirement 15 criterion 2 enumerates
the accepted configuration keys without naming the log level, which its Requirement 17 criterion 5
nonetheless defines as configurable with default `info`.

Source material: `.kiro/specs/simulator-deployment/design.md` (primary, decisions DP1–DP16),
`.kiro/specs/sensor-simulator-service/requirements.md` (the deployed service's own contract),
`docs/architecture/00-overview.md` (system context and cost envelope), and
`docs/architecture/02-ingestion-and-serving-service.md` (the consuming service's ingest filter and
deduplication).

### Scope

In scope: runtime topology and image integrity; device identity provisioning; fleet identity
derivation and drift detection; device authorization; credential storage and materialization;
credential rotation and revocation; secret and configuration delivery; backfill sharding; stateless
serving; observability wiring; the CI/CD pipeline; cost posture and scale-to-zero; and repository
credential hygiene.

Out of scope, and owned by `sensor-simulator-service`: the Simulator's signal generation, the
Sensor_Data_Record and Sensor_Metadata_Record contracts, the `Species` enum, scenario behavior,
geography profiles, and the Config_Loader's own resolution and validation semantics. Also out of
scope: the Ingestion & Serving Service (Service 2) and the AI Advisor Agent (Service 3) and their
deployments; the shared contract package; and the Bedrock agent.

### Assumptions pending confirmation

These assumptions are recorded so the requirements are complete and testable. Each is a decision the
reader should confirm or override.

- **A1 — Region and account posture:** a single AWS region and a single AWS account host both the
  dev and the demo deployments, as separate stack instances distinguished by their Generation_Tag
  and stack names. A separate demo account would change only the deploy identity's trust policy and
  the pipeline's stage targets, not the topology.
- **A2 — Pre-provisioning ceiling:** the ceiling for provisioning device identities from a single
  invocation is approximately 1,000 devices, bounded by the Invocation_Ceiling against
  rate-limited control-plane calls. City scale (approximately 5,000 sensors in
  `docs/architecture/00-overview.md`) changes the provisioning **mechanism** — the loop moves to a
  distributed map, and beyond roughly 5,000 devices to claim-certificate fleet provisioning — and
  does not change the runtime topology of Requirement 1.
- **A3 — Consuming-service deduplication:** the Ingestion & Serving Service deduplicates ingested
  records, so republishing an identical record is absorbed rather than double-counted. This is what
  makes the at-least-once shard retry of Requirement 8 acceptable. It is a real cross-service
  dependency: if it ceased to hold, backfill retry semantics would need revisiting.
- **A4 — Target CPU architecture:** ARM64 (Graviton) for the container image and for every runtime
  that consumes it, chosen for cost. A change here is a one-line change in the image build and in
  each runtime's platform setting, and Requirement 1 criterion 5 requires the two to agree whatever
  the value is.
- **A5 — Deploy identity:** deploys assume a role through a federated short-lived credential
  exchange (OIDC) with no long-lived access keys, from a hosted pipeline rather than an in-account
  one, because the expensive pipeline stages must run with no AWS credentials present at all. An
  in-account pipeline is the alternative where an external identity provider is unacceptable.
- **A6 — Key custody:** custody policy permits private keys generated by AWS IoT Core and stored
  encrypted in Parameter Store. The named alternative is registering a customer-managed CA and
  issuing certificates from a certificate signing request, so the private key never leaves the
  Fleet_Provisioner's memory; that costs a CA to operate and rotate and is not warranted for
  synthetic data.
- **A7 — Maximum credential age:** the design fixes no rotation interval, so the value in
  Requirement 6 criterion 9 is proposed rather than derived: a Maximum_Credential_Age of 365 days,
  audited as a read-only check. Confirm or override; the requirement holds at any configured value.
- **A8 — Error-rate alarm threshold:** the design names an alarm on the rate of `error`-level log
  lines per Publish_Interval but fixes no threshold, so Requirement 10 criterion 6 treats it as a
  configured value with no default asserted here.
- **A9 — Decommissioned Thing retention:** the design fixes no retention interval for the Things of
  `SiteCode` values that a Swarm change removes from the derived set, so this document proposes that
  such Things are retained indefinitely — certificate inactive and Credential_Parameter deleted per
  Requirement 3 criterion 7 — until an explicit operator action removes them. Confirm or override.
- **A10 — Maximum rotation window:** the design fixes no bound on how long a rotation may hold two
  `ACTIVE` certificates for one device, so the Maximum_Rotation_Window of Requirement 6 criterion 2
  is proposed rather than derived: 60 minutes, which must span a whole-fleet rotation under throttled
  control-plane calls. Confirm or override; the criteria hold at any configured value.
- **A11 — Backfill-written object store:** Backfill_Worker invocations already generate exactly the
  records a wide `/SensorData` query would want, so those workers could also write partitioned
  objects that Pull_Runtime range-reads. That path is deliberately not built. Adopting it would make
  the Simulator stateful and would require Requirement 9 criteria 1 and 2 to be reopened. The named
  trigger for revisiting it is a consumer with a stated wide-range latency requirement.
- **A12 — Metric dimension scheme:** the design fixes no dimension scheme for published metrics, so
  Requirement 10 criteria 2 and 3 propose one dimension naming the emitting runtime and one carrying
  the Generation_Tag. Under assumption A1's single account and single region, that scheme is what
  keeps a dev value and a demo value from landing on the same metric name indistinguishably and
  every threshold ambiguous. Confirm or override.
- **A13 — Alarm notification destination:** the design fixes no notification destination for alarms,
  so Requirement 10 criterion 13 requires exactly one operator destination per alarm without naming
  it. Confirm or override; the criterion holds at any configured destination.

**Note on cold starts.** The exclusion of cold-start invocations from the `/ListSensors` latency
obligation of Requirement 9 criterion 11 is deliberate: Requirement 12 criterion 2 configures no
provisioned concurrency and the design fixes no cold-start allowance, so a cold-start invocation has
no latency bound this document could assert.

## Glossary

- **Deployment**: the deployed form of the Simulator on AWS, comprising the components below. Where
  a criterion names THE Deployment, the obligation is on the infrastructure definition and the
  deployed resources rather than on the Simulator's own source.
- **Simulator**: the Sensor Simulator Service itself, as specified by `sensor-simulator-service`.
  Every component name carried over from that spec — Simulator, Virtual_Sensor, Swarm,
  MQTT_Publisher, REST_API, Config_Loader, Serializer, Geography_Profile, Species,
  Sensor_Data_Record, Publish_Interval, Seed, Backfill_Mode, Tick — keeps its definition there.
- **Simulator_Image**: the single container image built from the Simulator's own committed image
  definition, carrying every runtime entrypoint.
- **Image_Digest**: the content-addressed identifier of a specific Simulator_Image build.
- **Pull_Runtime**: the invocation-scoped runtime that serves the REST_API behind a managed HTTP
  endpoint.
- **Push_Task**: the resident container task that runs the Simulator's real-time MQTT push
  interface for the whole Swarm in one process.
- **Backfill_Planner**: the invocation-scoped component that computes a Shard_Plan for a requested
  backfill range.
- **Backfill_Worker**: the invocation-scoped runtime that executes exactly one Shard_Descriptor.
- **Fleet_Provisioner**: the deploy-time component that creates, retains, and orphans
  Device_Identity values for a Fleet_Generation.
- **Credential_Materializer**: the entrypoint step in Push_Task and Backfill_Worker that writes
  device credentials to local files before the Config_Loader validates them.
- **Swarm_Identity_Config**: the single value set that determines the `SiteCode` set — Seed, Swarm
  size, Geography_Profile name, `SiteCode` prefix, Publish_Interval, and the supplied site list where
  one is supplied.
- **Generation_Tag**: a short stable digest of the Seed, Swarm size, Geography_Profile name,
  `SiteCode` prefix, and derived `SiteCode` set, naming one Fleet_Generation.
- **Fleet_Generation**: the set of Device_Identity values provisioned under one Generation_Tag.
- **Device_Identity**: one device's registry entry, comprising a Thing whose name equals the
  `SiteCode`, exactly one ACTIVE X.509 certificate attached to that Thing, the Fleet_Policy
  attachment, Fleet_Generation group membership, and one Credential_Parameter.
- **Fleet_Policy**: the single IoT policy document serving every device in a Fleet_Generation,
  scoped by the Thing-name policy variable.
- **Credential_Parameter**: one encrypted parameter-store entry per device, holding that device's
  certificate and private key, under the Generation_Tag path prefix.
- **Fleet_Diff**: the classification of a derived `SiteCode` set against the provisioned set into
  to-create, to-retain, and to-orphan subsets.
- **Fleet_Replacement**: a Fleet_Diff whose to-retain subset is empty and whose to-orphan subset is
  non-empty, meaning a different fleet rather than a resized one.
- **Maximum_Credential_Age**: the configured age at or before which every device certificate is
  rotated.
- **Maximum_Rotation_Window**: the configured period within which a rotation in progress for a
  `SiteCode` must complete, and the only period during which that `SiteCode` may hold two `ACTIVE`
  certificates.
- **Measurement_Topic**: the MQTT topic `aqm/sensors/{SiteCode}/data`, owned by
  `sensor-simulator-service` Requirement 13 criterion 1 and cited, never redefined, here.
- **Housekeeping_Topic**: the MQTT topic `aqm/sensors/{SiteCode}/housekeeping`, named by this spec
  as the topic that `sensor-simulator-service` Requirement 7 criterion 9 requires to be distinct
  from the Measurement_Topic and leaves unnamed.
- **Measurement_Filter**: the MQTT topic filter `aqm/sensors/+/data` that the Ingestion & Serving
  Service subscribes to for measurement ingest.
- **Shard_Plan**: the ordered sequence of Shard_Descriptor values a Backfill_Planner invocation
  produces for one requested range.
- **Shard_Descriptor**: one unit of backfill work: a contiguous ascending `SiteCode` slice and an
  ordered sequence of consecutive Time_Window values, tagged with a Generation_Tag.
- **Time_Window**: a half-open simulated-time interval aligned to Publish_Interval boundaries, with
  an interval count.
- **Floor_Throughput**: the guaranteed backfill rate of `sensor-simulator-service` Requirement 12
  criterion 3 — at least 24 simulated hours of records per wall-clock second per Virtual_Sensor.
- **Shard_Budget**: the usable compute seconds a Backfill_Planner assumes per Backfill_Worker
  invocation, strictly less than the Invocation_Ceiling.
- **Invocation_Ceiling**: the 15-minute maximum duration of one invocation-scoped runtime
  execution.
- **Metric_Filter_Binding**: the mapping from a Simulator log event name and a field path within
  that event to a published metric name.
- **Offline_Suite**: the set of checks that run with no AWS credentials and no network access beyond
  localhost, including lint, type check, unit tests, property tests, template synthesis, and every
  Template_Assertion.
- **Template_Assertion**: an assertion made against a synthesized infrastructure template as a data
  structure, with no deployment and no credentials.
- **Deploy_Identity**: the role the pipeline assumes to deploy, obtained through a federated
  short-lived credential exchange.
- **Demo_Window**: the scheduled period during which Push_Task runs with a desired count of 1.
- **Cost_Posture_Check**: the read-only check of Requirement 12 criterion 10, which reports standing
  Push_Task capacity and every interruption gap left unrepaired and makes no write, mirroring the
  read-only drift check of Requirement 3 criterion 12.

## Requirements

### Requirement 1: Runtime Topology and Image Integrity

**User Story:** As a demo operator, I want each interface deployed on the runtime that suits it, all
running provably identical code, so that the Simulator's push/pull and real-time/backfill equality
guarantees hold in the deployed system and not only in the test suite.

#### Acceptance Criteria

1. THE Deployment SHALL run the REST_API on Pull_Runtime, Backfill_Mode on Backfill_Worker
   invocations orchestrated by a state machine in which exactly one Backfill_Planner invocation
   precedes every Backfill_Worker invocation of that execution, and the real-time MQTT push interface
   on Push_Task as a resident container task, and SHALL place no other Simulator interface on any of
   those three runtimes.
2. THE Deployment SHALL build the Simulator_Image from the same committed container image definition
   that the Docker Compose definition of `sensor-simulator-service` Requirement 16 criteria 5 and 6
   uses locally, and SHALL define no second container image definition for any runtime, so that the
   local and cloud builds cannot diverge.
3. THE Simulator_Image SHALL expose the runtime behavior selected by container command override,
   providing exactly the commands `serve-rest`, `backfill-shard`, `run-realtime`, `plan-backfill`,
   and `provision-fleet`, THE Deployment SHALL select a runtime's behavior by command override
   alone and by no separate build, and THE Simulator_Image SHALL provide no bootstrap
   credential-exchange command and no bootstrap credential-exchange path, so that the same build
   targets a local broker or AWS IoT Core with no source change as `sensor-simulator-service`
   Requirement 13 criterion 4 requires.
4. FOR ALL runtimes in the Deployment — Pull_Runtime, Backfill_Planner, Backfill_Worker,
   Fleet_Provisioner, and the Push_Task task definition — THE Deployment SHALL reference the
   container image by Image_Digest, SHALL reference no mutable image tag, and every referenced
   Image_Digest SHALL be identical, verifiable by Template_Assertion against the synthesized
   template, so that the push/pull byte-identity of `sensor-simulator-service` Requirement 3
   criterion 6 and the mode equivalence of that spec's Requirement 12 criterion 4 hold between
   deployed runtimes and not only between code paths inside one process.
5. THE Deployment SHALL build the Simulator_Image for exactly one CPU architecture, SHALL publish no
   additional architecture variant under that Image_Digest, and SHALL set the platform value of
   Pull_Runtime, Backfill_Planner, Backfill_Worker, Fleet_Provisioner, and the Push_Task task
   definition explicitly to that architecture, leaving none of the five at a platform default,
   verifiable by Template_Assertion.
6. THE Deployment SHALL front Pull_Runtime with a managed HTTP endpoint carrying exactly one
   catch-all proxy route and SHALL define no per-path route, verifiable by Template_Assertion, so
   that `/ListSensors`, `/SensorData`, and `/health` all reach the Simulator's own application and
   the application retains authority over the 401-before-parameter-validation ordering of
   `sensor-simulator-service` Requirement 14 criteria 2 and 3 and over `/health` being the one
   unauthenticated route of that spec's Requirement 14 criterion 8.
7. THE Deployment SHALL configure no endpoint-level API key, no endpoint-level usage plan, and no
   endpoint-level or route-level authorizer for the managed HTTP endpoint, so that `X-API-KEY`
   validation happens only where `sensor-simulator-service` Requirement 14 criterion 2 places it,
   the 401-before-parameter-validation ordering of that spec's Requirement 14 criteria 2 and 3 stays
   with the application, and `/health` remains the one unauthenticated route of that spec's
   Requirement 14 criterion 8.
8. THE Deployment SHALL apply an explicit stage-level request-rate limit to the managed HTTP
   endpoint, bounding abuse of the unauthenticated `/health` route, verifiable by
   Template_Assertion.
9. IF a container is started from the Simulator_Image with a command that is not one of the commands
   of criterion 3, THEN THE Simulator_Image SHALL exit with a non-zero status having written one log
   line naming the unrecognized command and the supported commands, and SHALL start no runtime
   behavior.
10. WHILE the derived Swarm size is at or below the per-process maximum of 500 that
    `sensor-simulator-service` Requirement 15 criterion 4 permits, THE Deployment SHALL run at most
    one Push_Task instance per Fleet_Generation and SHALL configure no scaling policy for it,
    verifiable by Template_Assertion, the multi-instance case being the exception of Requirement 12
    criterion 9.

### Requirement 2: Device Identity Provisioning

**User Story:** As a Service 2 developer, I want every virtual sensor to arrive at the broker with
its own X.509 identity already in place, so that my IoT ingestion path is exercised against
per-device mutual TLS rather than a shared credential.

#### Acceptance Criteria

1. THE Fleet_Provisioner SHALL create exactly one Device_Identity per `SiteCode` in the derived set,
   with the Thing name character-identical to the `SiteCode` and the MQTT client ID equal to the
   same value, so that Thing-name policy variables resolve for that device.
2. THE Deployment SHALL complete provisioning of every Device_Identity in the Fleet_Generation
   during the deploy that creates or updates that generation, before any runtime selecting the
   `mqtt` interface starts.
3. FOR ALL `SiteCode` values in the derived set, after a Fleet_Provisioner run completes with a
   success result there SHALL be exactly one certificate in `ACTIVE` status attached to exactly one
   Thing, that Thing's name SHALL equal the `SiteCode`, and no certificate SHALL be attached to more
   than one Thing, verifiable by read-only registry calls over the Fleet_Generation Thing Group
   without connecting a device.
4. WHEN the Fleet_Provisioner processes a `SiteCode`, THE Fleet_Provisioner SHALL count that Thing's
   `ACTIVE` certificates before minting a new one, SHALL mint only when that count is zero, and
   SHALL otherwise leave that device's certificate, Credential_Parameter, Fleet_Policy attachment,
   and Fleet_Generation Thing Group membership unchanged and continue with the next `SiteCode` in
   ascending order, so that a retry after a partial failure converges on exactly one certificate per
   device rather than accumulating one per attempt.
5. WHEN a Thing creation call reports that the Thing already exists, THE Fleet_Provisioner SHALL
   continue processing that `SiteCode` at the `ACTIVE`-certificate count of criterion 4, and that
   outcome alone SHALL fail no deploy.
6. FOR ALL sequences of one or more Fleet_Provisioner runs over the same Swarm_Identity_Config,
   including runs interrupted part way through, there SHALL be at most one `ACTIVE` certificate per
   derived `SiteCode` at every point during and after those runs, and FOR ALL runs whose to-create
   subset is empty the set of certificate identifiers in the Fleet_Generation SHALL be unchanged
   across that run, so that a repeat run over unchanged configuration is observably a no-op.
7. IF writing a device's Credential_Parameter fails after that device's certificate has been
   created, THEN THE Fleet_Provisioner SHALL set that certificate's status to inactive, SHALL write
   one log line naming the `SiteCode` and the failure type, and SHALL fail the deploy with a
   non-zero result rather than continuing with a partially provisioned fleet, because the private
   key is returned exactly once and a certificate whose key was lost is an unaccounted-for
   credential.
8. IF a control-plane call is throttled, THEN THE Fleet_Provisioner SHALL retry that `SiteCode` with
   exponential backoff and randomized jitter under a bounded concurrency limit up to a configured
   maximum attempt count per `SiteCode`, and IF those attempts are exhausted for a `SiteCode`, THEN
   THE Fleet_Provisioner SHALL fail the deploy with a non-zero result naming that `SiteCode` and the
   attempt count.
9. THE Fleet_Provisioner SHALL iterate `SiteCode` values in ascending order in both the create pass
   and the orphan pass, so that its log stream has a defined order for an operator diagnosing a
   failed deploy.
10. THE Fleet_Provisioner SHALL attach the Fleet_Policy to each created certificate and SHALL add
    each created Thing to the Fleet_Generation Thing Group.
11. THE Deployment SHALL support provisioning a derived set of up to 1,000 `SiteCode` values within
    a single Fleet_Provisioner invocation inside the Invocation_Ceiling.
12. IF the derived set contains more than 1,000 `SiteCode` values, THEN THE Deployment SHALL fail the
    deploy with a non-zero result naming the derived set size and the supported ceiling, before
    creating any Thing, minting any certificate, or writing any Credential_Parameter, so that the
    boundary is a named refusal rather than a discovered timeout.
13. IF a stack deploy fails or rolls back after a fleet change, THEN THE Deployment SHALL delete and
    deactivate no certificate and delete no Credential_Parameter as a side effect of that rollback,
    verifiable by Template_Assertion on the retention policy of every fleet identity resource.
14. IF a Fleet_Provisioner invocation starts for a Generation_Tag while another invocation for that
    same Generation_Tag has not completed, THEN THE Deployment SHALL fail the second invocation with
    a non-zero result naming that Generation_Tag, before creating any Thing, minting any certificate,
    or writing any Credential_Parameter.
15. IF a `SiteCode` in the derived set holds more than one `ACTIVE` certificate, or holds an `ACTIVE`
    certificate with no readable Credential_Parameter under the Generation_Tag path prefix, THEN THE
    Fleet_Provisioner SHALL fail the deploy with a non-zero result naming that `SiteCode` and the
    observed condition, and SHALL mint no certificate and write no Credential_Parameter for that
    `SiteCode`.

### Requirement 3: Fleet Identity Derivation and Drift

**User Story:** As a demo operator, I want the provisioned fleet and the generated Swarm to be
derived from one source, so that a configuration change can never leave some virtual sensors without
credentials.

#### Acceptance Criteria

1. THE Deployment SHALL hold the Seed, Swarm size, Geography_Profile name, `SiteCode` prefix, and
   Publish_Interval in exactly one Swarm_Identity_Config, which SHALL carry the supplied site list
   where one is supplied, and SHALL supply that same value set to the Fleet_Provisioner,
   Pull_Runtime, Backfill_Planner, Backfill_Worker, and Push_Task.
2. THE Fleet_Provisioner SHALL derive the `SiteCode` set by calling the Simulator's own Swarm
   identity factory, which owns the derivation rule of `sensor-simulator-service` Requirement 8
   criteria 2 and 3, the prefix-plus-zero-padded-four-digit form of that spec's Requirement 1
   criterion 7, and the supplied-site-list substitution of that spec's Requirement 8 criterion 8, and
   SHALL contain no second implementation of any of those rules.
3. FOR ALL Swarm_Identity_Config values, THE Fleet_Provisioner SHALL derive a strictly ascending
   `SiteCode` sequence whose length equals the configured Swarm size.
4. FOR ALL Swarm_Identity_Config values, the set of `SiteCode` values holding a Thing, an `ACTIVE`
   certificate, and a Credential_Parameter SHALL equal the set the Simulator's own Swarm identity
   factory generates for that Swarm_Identity_Config, with no extra value and none missing.
5. THE Deployment SHALL compute a Generation_Tag as a stable digest of the Seed, Swarm size,
   Geography_Profile name, `SiteCode` prefix, and derived `SiteCode` set, SHALL name the
   Fleet_Generation Thing Group with it, and SHALL prefix every Credential_Parameter path with it, so
   that a new generation is provisioned alongside its predecessor rather than by mutating it and two
   configurations with different derived sets can share no tag.
6. WHEN the Swarm size increases with the Seed and the `SiteCode` prefix unchanged, THE
   Fleet_Provisioner SHALL create Device_Identity values for the added `SiteCode` values only, and
   SHALL issue no new certificate and rewrite no Credential_Parameter for any retained `SiteCode`,
   the retained values being stable by `sensor-simulator-service` Requirement 11 criterion 4.
7. WHEN the Swarm size decreases with the Seed and the `SiteCode` prefix unchanged, THE
   Fleet_Provisioner SHALL, for each removed `SiteCode`, set that device's certificate status to
   inactive, delete its Credential_Parameter, and remove its Thing from the Fleet_Generation Thing
   Group, SHALL delete no Thing and SHALL delete no certificate, and SHALL leave every retained
   Device_Identity unchanged.
8. WHERE the `allow_fleet_replacement` flag is explicitly set, WHEN the Seed changes or the
   `SiteCode` prefix changes, THE Fleet_Provisioner SHALL treat the change as a Fleet_Replacement,
   SHALL provision the new generation in full, and SHALL set to inactive every certificate of the
   Fleet_Generation named by the Generation_Tag recorded by the preceding deploy of the same stack.
9. IF the computed Fleet_Diff is a Fleet_Replacement and the `allow_fleet_replacement` flag is not
   explicitly set, THEN THE Fleet_Provisioner SHALL fail the deploy with a message naming the empty
   retain set and the flag required to proceed, and SHALL change no registry state and no
   Credential_Parameter, so that a one-character Seed edit is a failed deploy rather than a fleet
   wipe.
10. WHEN the value set of the active Geography_Profile changes while that profile's name, the
    `SiteCode` prefix, the Seed, and the Swarm size are unchanged, THE Fleet_Provisioner SHALL
    create, deactivate, and delete nothing, because the derived `SiteCode` set is unchanged and only
    the runtimes' configuration values differ.
11. THE Deployment SHALL declare a digest of the derived `SiteCode` set as a property of the
    provisioning resource, so that a change to the derived set forces a provisioning update rather
    than being skipped as an unchanged resource, verifiable by Template_Assertion.
12. THE Deployment SHALL provide a read-only drift check that compares the locally derived `SiteCode`
    set against the set of `SiteCode` values holding Fleet_Generation Thing Group membership and
    exactly one `ACTIVE` certificate and a Credential_Parameter, and IF the two sets differ, THEN the
    check SHALL name every `SiteCode` present in only one of them together with which of those three
    constituents is absent and SHALL exit non-zero, and IF the two sets are equal, THEN the check
    SHALL exit zero.
13. WHERE a site list is supplied in configuration, THE Fleet_Provisioner SHALL derive the `SiteCode`
    set from the supplied entries per `sensor-simulator-service` Requirement 8 criterion 8, with a
    set size equal to the number of supplied entries and the values in strictly ascending order, and
    SHALL create exactly one Device_Identity per supplied entry.
14. FOR ALL Fleet_Diff values, including a Fleet_Diff whose to-create, to-retain, and to-orphan
    subsets are all non-empty, THE Fleet_Provisioner SHALL create a Device_Identity for every
    to-create value, SHALL apply the orphan steps of criterion 7 to every to-orphan value, and SHALL
    change no registry state and no Credential_Parameter for every to-retain value, so that a partial
    overlap is a classified case rather than an unclassified one and does not trip the
    Fleet_Replacement guardrail of criterion 9.

### Requirement 4: Device Authorization

**User Story:** As a security reviewer, I want each device able to publish only its own topics and
nothing else, so that a compromised or misconfigured device cannot impersonate another or reach the
consuming service's ingest path with the wrong payload.

#### Acceptance Criteria

1. THE Deployment SHALL define exactly one Fleet_Policy for the whole Fleet_Generation and SHALL
   define no per-device IoT policy document, verifiable by Template_Assertion, so that one document
   serves every device without one document per device.
2. FOR ALL IoT policy documents in the Deployment, no statement SHALL grant `iot:Subscribe` and no
   statement SHALL grant `iot:Receive`, verifiable by Template_Assertion.
3. FOR ALL topic resources and FOR ALL client resources in every IoT policy document in the
   Deployment, the resource SHALL contain no `*` and no `#` wildcard segment and SHALL express the
   device-identifying segment through the Thing-name policy variable, verifiable by
   Template_Assertion.
4. THE Fleet_Policy SHALL grant `iot:Connect` only on the client resource that the Thing-name policy
   variable resolves to, so that a certificate cannot open a session under another device's
   identity.
5. THE Fleet_Policy SHALL grant `iot:Publish` on exactly two resources for a given device — the
   Measurement_Topic owned by `sensor-simulator-service` Requirement 13 criterion 1 and the
   Housekeeping_Topic — and on no other topic.
6. THE Deployment SHALL name the Housekeeping_Topic `aqm/sensors/{SiteCode}/housekeeping`, being the
   topic that `sensor-simulator-service` Requirement 7 criterion 9 requires to be distinct from the
   Measurement_Topic and leaves unnamed, SHALL use for the Measurement_Topic exactly the name owned
   by that spec's Requirement 13 criterion 1, and SHALL define no alternative measurement topic name,
   verifiable by Template_Assertion.
7. WHEN a deploy of a Fleet_Generation completes, THE Deployment SHALL run a read-only authorization
   check over a sample of ordered pairs of distinct provisioned devices, of a configured pair count,
   the pairs selected by a rule derived from the Seed so that identical deploy inputs select identical
   pairs, and FOR ALL pairs in that sample the check SHALL assert that the first device's principal is
   authorized to publish to its own Measurement_Topic and its own Housekeeping_Topic, is denied
   publish to the second device's Measurement_Topic and to the second device's Housekeeping_Topic, and
   is denied connect with the second device's client ID, through the read-only authorization-test API
   without connecting any device. The universal claim over every ordered pair is carried structurally
   by criteria 1, 3, 4, and 5 through Template_Assertion, this check being a sample because an
   exhaustive enumeration at 500 devices is 249,500 rate-limited calls.
8. FOR ALL `SiteCode` values, the Housekeeping_Topic SHALL NOT match the Measurement_Filter while
   the Measurement_Topic SHALL match it, so that a mis-shaped housekeeping payload cannot reach the
   consuming service's measurement ingest path at all.
9. FOR ALL IAM roles in the Deployment — the Pull_Runtime, Push_Task, Backfill_Planner,
   Backfill_Worker, and Fleet_Provisioner roles — THE Deployment SHALL grant no `iot:Publish`
   permission, so that publish authorization is carried by the per-device certificate and the
   Fleet_Policy and by no IAM grant, verifiable by Template_Assertion.
10. FOR ALL IAM policy statements attached to a role in the Deployment, the statement's resources
    SHALL be named ARNs or ARN patterns naming this Deployment's account, region, and resource type,
    and for Credential_Parameter resources confined to this Fleet_Generation's path prefix, except for
    exactly two enumerated statements — one granting only the certificate-creation action and one
    granting only the endpoint-description action, whose APIs admit no resource scope — and no
    statement carrying an unscoped resource SHALL grant any action other than the single action it is
    enumerated for, verifiable by Template_Assertion.
11. IF any pair sampled by criterion 7 fails any assertion of that criterion, THEN the check SHALL
    name the ordered pair of `SiteCode` values and the assertion that failed and SHALL exit non-zero.
12. IF calls to the read-only authorization-test API are throttled or fail during the check of
    criterion 7, THEN the check SHALL retry within a bounded retry loop with backoff, and IF those
    bounded retries are exhausted, THEN the check SHALL exit non-zero naming the pairs left unasserted
    and SHALL report no pass result.
13. FOR ALL `ACTIVE` certificates in the Fleet_Generation, exactly one IoT policy SHALL be attached
    and it SHALL be the Fleet_Policy, verifiable by read-only registry calls without connecting a
    device, and IF any certificate carries another attached policy, THEN the check SHALL name that
    certificate's `SiteCode` and the extra policy and SHALL exit non-zero.

### Requirement 5: Credential Storage and Materialization

**User Story:** As a developer, I want the deployed simulator to see exactly the credential files it
sees locally, so that its startup path is identical in both places and no private key ever appears
where it can be read.

#### Acceptance Criteria

1. THE Fleet_Provisioner SHALL write each device's certificate and private key to one
   Credential_Parameter for that device, as an encrypted parameter whose path is composed of the
   Generation_Tag prefix and that same device's `SiteCode`, and SHALL write no device's material to
   any other store, so that a parameter's path names the device whose certificate it holds.
2. FOR ALL provisioning results that cross the infrastructure-template response boundary, the
   response SHALL carry parameter names, certificate identifiers, and the derived-set digest, and
   SHALL carry no private key and no certificate PEM block, because that boundary is readable in
   the deployment service's own console and API.
3. FOR ALL log lines the Fleet_Provisioner writes and FOR ALL outputs the Deployment's templates
   declare, none SHALL contain private key material and none SHALL contain a certificate PEM block,
   verifiable by an offline test against a faked registry client and by Template_Assertion.
4. WHEN a runtime selecting the `mqtt` interface starts, THE Credential_Materializer SHALL read the
   Credential_Parameter values for exactly the `SiteCode` values that runtime owns — the whole derived
   `SiteCode` set for Push_Task, and exactly the slice named in its own Shard_Descriptor for a
   Backfill_Worker — in batched reads of at most ten parameters per call, SHALL write one certificate
   file and one private key file per owned `SiteCode` at the paths resolved from the credential path
   template of `sensor-simulator-service` Requirement 13 criterion 4, and SHALL complete every such
   write before the Config_Loader validates those paths.
5. THE Deployment SHALL declare the mount holding materialized credentials a memory-backed volume
   rather than an image layer or a persisted volume, verifiable by Template_Assertion, and THE
   Credential_Materializer SHALL write every credential file inside that mount, SHALL write no
   credential file outside it, and SHALL set each file's mode to readable and writable by the
   runtime's own user and by no other user.
6. IF a credential path resolved from that template, or the configured broker certificate authority
   path of `sensor-simulator-service` Requirement 13 criterion 4, is absent or unreadable at the
   moment the Config_Loader validates it in a runtime selecting the `mqtt` interface, THEN the process
   SHALL exit with a non-zero status having written one message per affected value naming the
   configuration value and the affected `SiteCode` and excluding the value itself, which is exactly
   the fail-fast behavior of that spec's Requirement 13 criterion 9 together with its Requirement 16
   criterion 9.
7. IF one or more credential paths are absent after materialization, THEN THE Credential_Materializer
   SHALL write one log line naming every `SiteCode` that runtime owns and that is absent from the
   materialized set, before the Config_Loader exit of criterion 6, so that an operator sees which
   codes are missing rather than only that something is.
8. THE Deployment SHALL grant Pull_Runtime no read permission on any Credential_Parameter, because
   the pull path needs no device credential, verifiable by Template_Assertion.
9. IF parameter reads are throttled during materialization, THEN THE Credential_Materializer SHALL
   retry with backoff up to a configured maximum attempt count, and IF that attempt count is
   exhausted, THEN the process SHALL exit with a non-zero status naming the affected `SiteCode`
   values.
10. THE Credential_Materializer SHALL write to the same template-resolved paths that the local
    development credential generation command of `sensor-simulator-service` Requirement 16
    criterion 11 writes to, so that the Simulator's credential loading is identical between local
    and cloud, verified in the Docker-marked stage of Requirement 11.
11. IF the certificate held by the Credential_Parameter resolved for a `SiteCode` is not the
    certificate the registry reports as that `SiteCode`'s single `ACTIVE` certificate, THEN a
    read-only credential-correspondence check SHALL name that `SiteCode` and both certificate
    identifiers, SHALL disclose no private key material and no certificate PEM block on the terms of
    criterion 3, and SHALL exit non-zero.
12. THE Deployment SHALL supply the broker certificate authority path of `sensor-simulator-service`
    Requirement 13 criterion 4 as a value resolving to certificate authority material already present
    in the Simulator_Image, and THE Credential_Materializer SHALL materialize no certificate authority
    material from any Credential_Parameter, so that the chain validation of that spec's Requirement 13
    criterion 3 depends on no per-device parameter read.
13. WHILE no runtime selecting the `mqtt` interface is executing on a given execution environment, THE
    Deployment SHALL keep no materialized certificate file and no materialized private key file
    present at any path resolved from that template, so that credential material never outlives the
    process that materialized it and a reused execution environment cannot present another
    Shard_Descriptor's or another Fleet_Generation's material as its own.

### Requirement 6: Credential Rotation and Revocation

**User Story:** As a security reviewer, I want to rotate the fleet's credentials on a schedule and
revoke one device without touching the others, so that per-device identity delivers the containment
it exists to provide.

#### Acceptance Criteria

1. WHEN a documented rotation operation is invoked for one named `SiteCode` or for the whole
   Fleet_Generation, THE Deployment SHALL perform these steps in this order: create a new
   certificate, activate it, attach the Fleet_Policy and the Thing to it, write the new bundle to
   that device's Credential_Parameter as a new parameter version, restart the runtimes holding the
   superseded material — being Push_Task and any Backfill_Worker in flight for that `SiteCode` — then
   deactivate and delete the superseded certificate, the restart step being satisfied without action
   where no such runtime is running, Push_Task being scaled to zero outside a Demo_Window.
2. WHILE a rotation is in progress for a device, THE Deployment SHALL hold exactly two `ACTIVE`
   certificates for that `SiteCode`, both attached to that one Thing and neither attached to any
   other Thing, SHALL keep the superseded certificate in `ACTIVE` status until the restart step of
   criterion 1 is satisfied, SHALL record the rotation as in progress for that `SiteCode` in durable
   state readable by read-only calls from the create step until the delete step completes, and SHALL
   hold that two-certificate state for no longer than the configured Maximum_Rotation_Window, so that
   no interval exists in which the device has no `ACTIVE` certificate. The exactly-one-`ACTIVE`-
   certificate invariant of Requirement 2 criterion 3 is therefore a no-rotation-in-progress
   invariant: a checker observing two `ACTIVE` certificates for a `SiteCode` passes only where an
   in-progress record exists for that `SiteCode` and that window has not elapsed.
3. WHEN the delete step of criterion 1 completes for a `SiteCode`, THE Deployment SHALL leave exactly
   one `ACTIVE` certificate for that `SiteCode` and exactly one Credential_Parameter holding the
   material that certificate corresponds to, restoring the invariant of Requirement 2 criterion 3.
4. THE Deployment SHALL replace no credential inside a running process and SHALL apply every rotation
   through the restart step of criterion 1, because the Simulator resolves every credential path once
   at startup as a consequence of `sensor-simulator-service` Requirement 13 criterion 9.
5. WHEN a revocation is invoked for one named `SiteCode`, THE Deployment SHALL set that device's
   certificate status to revoked, SHALL delete that device's own Credential_Parameter, SHALL record
   that `SiteCode` as revoked in durable state readable by read-only calls, and SHALL change no other
   device's certificate, no Fleet_Policy statement, and no other device's Credential_Parameter.
6. WHEN one device's certificate is revoked, the observable consequence SHALL be exactly that of
   `sensor-simulator-service` Requirement 13 criterion 10 — that Virtual_Sensor's connection
   attempts are rejected, the MQTT_Publisher stops attempting to connect for that Virtual_Sensor
   after the configured number of consecutive rejections with default 5, an error identifying the
   `SiteCode` and the rejection category is reported, and every remaining Virtual_Sensor keeps
   publishing.
7. WHEN one device's certificate is revoked, THE Deployment SHALL surface the resulting give-up
   event through the give-up-event alarm of Requirement 10 criterion 6, because the Swarm
   continuing to publish means nothing else surfaces it.
8. FOR ALL `SiteCode` values that a Swarm change removes from the derived set, after the
   Fleet_Provisioner runs there SHALL be no `ACTIVE` certificate and no Credential_Parameter for
   that `SiteCode`, verifiable by read-only registry and parameter-store calls.
9. THE Deployment SHALL rotate every device certificate in the Fleet_Generation at or before the
   configured Maximum_Credential_Age, whose default is 365 days, and SHALL provide a read-only audit
   check that reports every `SiteCode` whose `ACTIVE` certificate's age, measured from that
   certificate's registry creation time to the audit's run time, exceeds that value, that also reports
   every `ACTIVE` certificate in the region attached to no Thing or attached to a Thing outside the
   Fleet_Generation together with that certificate's age, that makes no registry write and no
   parameter-store write, and that exits non-zero when it reports at least one entry, the
   detached-certificate case being exactly the artifact left by the persist-failure path of
   Requirement 2 criterion 7 and by an interrupted rotation, and caught by nothing else.
10. FOR ALL rotation and revocation operations, no log line and no template output SHALL contain
    private key material or a certificate PEM block, on the same terms as Requirement 5
    criterion 3.
11. IF any step of criterion 1 fails for a `SiteCode`, or the restart step of criterion 1 is not
    satisfied for that `SiteCode` within the Maximum_Rotation_Window, THEN THE Deployment SHALL leave
    the superseded certificate `ACTIVE` and undeleted, SHALL set the new certificate's status to
    inactive, SHALL restore that device's Credential_Parameter to the superseded material where new
    material was already written, SHALL write one log line naming the `SiteCode`, the failed step, and
    the failure type, SHALL continue with the remaining targeted `SiteCode` values, and SHALL exit
    with a non-zero status naming every `SiteCode` whose rotation failed, so that a failed rotation
    leaves the device serviceable.
12. WHILE a `SiteCode` is recorded as revoked, THE Fleet_Provisioner SHALL mint no certificate for
    that `SiteCode` in its create pass even though its `ACTIVE` certificate count is zero, and the
    fleet checks of Requirement 2 criterion 3 and Requirement 3 criterion 12 SHALL report that
    `SiteCode` as revoked rather than as missing an identity, so that a deploy cannot silently
    un-revoke a device and an intentionally revoked device is not a permanent check failure.
13. WHEN a reinstatement is invoked for a `SiteCode` recorded as revoked, THE Deployment SHALL
    provision a new Device_Identity for that `SiteCode` through the steps of criterion 1, SHALL clear
    the revocation record only after that identity is in place, and SHALL return no revoked
    certificate to `ACTIVE` status.
14. WHEN a rotation targets the whole Fleet_Generation, THE Deployment SHALL perform the create,
    activate, attach, and parameter-write steps for every targeted `SiteCode` in ascending order under
    the bounded concurrency limit of Requirement 2 criterion 8, SHALL then restart each affected
    runtime exactly once rather than once per `SiteCode`, and SHALL then deactivate and delete every
    superseded certificate.

### Requirement 7: Secret and Configuration Delivery

**User Story:** As a demo operator, I want the deployed configuration to be entirely visible in the
stack and every secret to be invisible in it, so that I can read what a runtime will do without
reading a secret.

#### Acceptance Criteria

1. FOR ALL runtimes in the Deployment, THE Deployment SHALL supply every configuration value that
   runtime's selected interface requires as an environment variable in that runtime's own environment
   definition, being the highest-precedence source in the Config_Loader's resolution order per
   `sensor-simulator-service` Requirement 15 criterion 1, and SHALL supply no configuration value to
   any runtime through a configuration file, verifiable by Template_Assertion.
2. THE Simulator_Image SHALL include no configuration file at any path the Config_Loader reads, and
   THE Deployment SHALL supply no configuration file path by container command argument and none by
   environment value, so that the deployed configuration is the stack's environment definition alone.
3. WHERE a runtime's selected interface includes `rest`, THE Deployment SHALL hold the REST API key in
   exactly one managed secret and SHALL deliver that key to that runtime by reference to the secret
   rather than by value, delivering it to Push_Task through the container platform's native secret
   injection resolved at task start, and SHALL deliver that key to no runtime whose selected interface
   excludes `rest` and grant such a runtime no read permission on that secret.
4. THE Deployment SHALL deliver the API key to Pull_Runtime as the secret's ARN in an environment
   variable, together with read permission on that ARN alone, and Pull_Runtime SHALL resolve the
   value during its initialization phase.
5. FOR ALL synthesized templates in the Deployment, no resource property, no output, and no metadata
   entry SHALL contain a plaintext secret or credential value, and every secret SHALL appear only as
   an ARN, as a platform secret reference, or as a dynamic reference resolved at deploy or run time,
   verifiable by Template_Assertion.
6. THE Deployment SHALL generate the API key with a length inside the 16-to-256-character range that
   `sensor-simulator-service` Requirement 14 criterion 6 permits, drawn from printable ASCII
   characters excluding the space character, containing no control character, no line break, and no
   leading or trailing whitespace, so that the value is transportable unchanged as a single
   `X-API-KEY` header value and matches under the exact case-sensitive full-value comparison of that
   spec's Requirement 14 criterion 2.
7. WHERE a runtime's selected interface includes `rest`, IF the API key value is absent or empty when
   that runtime initializes, THEN that runtime SHALL exit with a non-zero status before serving any
   request, as `sensor-simulator-service` Requirement 14 criterion 9 requires, and SHALL write one
   message naming the affected configuration value and excluding the value itself; for Pull_Runtime
   this SHALL be a failure raised during the initialization phase so that the invocation fails and no
   request is served on an unauthenticated path, and for Push_Task the task SHALL fail to start and
   the deployment circuit breaker SHALL roll the deployment back.
8. THE Deployment SHALL treat API key rotation as one coordinated operation with a single valid key
   at any instant, because `sensor-simulator-service` Requirement 14 criterion 2 admits exactly one
   configured key, and SHALL document that requests carrying the superseded key during the rotation
   window receive HTTP status 401 per that spec's Requirement 14 criterion 3.
9. THE Deployment SHALL set each of the following values explicitly in each runtime's environment
   rather than relying on the Config_Loader default, each to a value inside the range the cited
   criterion permits and each under a configuration key the Config_Loader accepts per
   `sensor-simulator-service` Requirement 15 criterion 2: the retention window, set to 30 simulated
   days inside the 1-to-365-simulated-day range of that spec's Requirement 14 criterion 7; the log
   level, set to `info` among the four values of that spec's Requirement 17 criterion 5; the MQTT
   port, set to 8883 inside the 1-to-65535 range of that spec's Requirement 13 criterion 4; the two
   credential path templates of that spec's Requirement 13 criterion 4, set to the paths of
   Requirement 5 criterion 10; the reconnect backoff maximum, set to 60 seconds per that spec's
   Requirement 13 criterion 5; and the publisher buffer maximum, set to 1000 inside the
   1-to-100,000-record range of that spec's Requirement 13 criterion 6.
10. IF a runtime's environment omits a value the Config_Loader requires for that runtime's selected
    interface, or carries a configuration key the Config_Loader does not recognize, THEN the runtime
    SHALL exit non-zero per `sensor-simulator-service` Requirement 15 criteria 2, 5, and 10, and THE
    Deployment SHALL surface that outcome as a failed task start or a failed invocation rather than as
    a running service serving requests.
11. FOR ALL environment variable keys the Deployment sets inside the key namespace the Config_Loader
    treats as configuration, the key SHALL be one the Config_Loader accepts per
    `sensor-simulator-service` Requirement 15 criterion 2, and THE Deployment SHALL place every
    deployment-only environment value the Simulator's configuration does not define — including the
    API key secret reference of criterion 4 — outside that namespace, verifiable with no AWS
    credentials and no network access by comparing each runtime's synthesized environment key set
    against the Config_Loader's accepted-key set.
12. IF resolving the API key from the managed secret fails at initialization for any reason other than
    an absent or empty value, THEN the runtime SHALL exit with a non-zero status before serving any
    request, SHALL write one log line naming the failure kind and the secret reference and excluding
    the resolved value, and SHALL serve no request on any unauthenticated path.
13. WHERE a runtime's selected interface includes `mqtt`, THE Deployment SHALL set that runtime's MQTT
    endpoint host and broker certificate authority path explicitly in its environment definition,
    these being values for which `sensor-simulator-service` Requirement 15 criterion 6 gives no
    documented default and whose absence that spec's Requirement 15 criterion 10 makes a non-zero
    exit, verifiable by Template_Assertion.

### Requirement 8: Backfill Sharding

**User Story:** As a Service 2 developer, I want a year of history generated by many parallel workers
without losing per-sensor ordering, so that I can seed my time-series store from the same code path
that produces real-time data.

#### Acceptance Criteria

1. THE Backfill_Planner SHALL partition backfill work using contiguous ascending `SiteCode` ranges
   as the parallel axis and consecutive Time_Window values within one Shard_Descriptor as the
   sequential axis.
2. FOR ALL valid triples of Swarm_Identity_Config, range start, and range end, the Shard_Plan SHALL
   partition the `(SiteCode × interval-start)` space of the requested range exactly once: no two
   Shard_Descriptor values SHALL cover the same pair, and every pair in the range SHALL be covered
   by some Shard_Descriptor.
3. FOR ALL Shard_Plan values and FOR ALL `SiteCode` values in the requested range, every interval of
   that `SiteCode` SHALL lie in exactly one Shard_Descriptor, and within that Shard_Descriptor the
   Time_Window values SHALL be consecutive and ascending, so that the non-decreasing `DateTime`
   order of `sensor-simulator-service` Requirement 12 criterion 3 and the per-Virtual_Sensor
   ordering of that spec's Requirement 13 criterion 8 survive the fan-out.
4. FOR ALL valid planning inputs, two independent Backfill_Planner invocations SHALL produce
   identical Shard_Descriptor sequences, and the Shard_Plan SHALL depend on no wall-clock value, no
   worker count, and no configured fan-out concurrency.
5. THE Backfill_Planner SHALL compute each Shard_Descriptor's estimated duration as that
   descriptor's `SiteCode` count multiplied by the sum of that descriptor's Time_Window interval
   counts, evaluated at the Floor_Throughput of `sensor-simulator-service` Requirement 12
   criterion 3 and at no measured rate, and SHALL emit no Shard_Descriptor whose estimated duration
   exceeds the configured Shard_Budget, so that every shard finishing inside its budget is true by
   construction.
6. THE Deployment SHALL configure the Shard_Budget strictly less than the Invocation_Ceiling of 15
   minutes, with default 600 seconds, reserving the remainder as headroom for cold start, credential
   materialization, broker connect, and the final flush.
7. IF a backfill request has a range end at or before its range start, a span exceeding the maximum
   backfill span whose default is 365 simulated days per `sensor-simulator-service` Requirement 12
   criterion 6, or a range start not aligned to a Publish_Interval boundary per that spec's
   Requirement 2 criteria 5 and 6, THEN THE Backfill_Planner SHALL reject the request with a message
   naming each offending value and its permitted form and SHALL produce no Shard_Plan.
8. THE Deployment SHALL bound backfill fan-out with an explicit maximum concurrency value, default
   25, verifiable by Template_Assertion, and SHALL supply that value to no Backfill_Planner
   invocation as an input, so that turning it changes wall time and downstream pressure and never
   changes which records land in which shard, the default being sized to the consuming service's
   ingest capacity.
9. IF a Backfill_Worker invocation fails, THEN THE Deployment SHALL retry that same Shard_Descriptor
   at most 3 times after the initial attempt, delaying the first retry by 5 seconds and doubling each
   subsequent delay, and each retry SHALL publish records identical to those the failed attempt
   published, so that delivery is at-least-once and a duplicate is a duplicate rather than a
   conflict, resolved by the consuming service's deduplication.
10. IF every retry of criterion 9 has failed for a Shard_Descriptor, THEN THE Deployment SHALL fail
    that backfill execution and SHALL record in that execution's result the failed
    Shard_Descriptor's shard index, `SiteCode` slice, ordered Time_Window sequence, and
    Generation_Tag, so that a Backfill_Planner re-run on the same inputs reproduces that descriptor
    per criterion 4 and that descriptor can be executed alone.
11. IF the MQTT connection is unavailable or a Sensor_Data_Record remains unpublished when a
    Backfill_Worker's Shard_Budget is exhausted, THEN the worker SHALL report each unpublished
    record with its `SiteCode`, `Species`, and `DateTime` per `sensor-simulator-service`
    Requirement 13 criterion 11, and SHALL exit with a non-zero status, so that the bounded retry of
    criterion 9 applies to that Shard_Descriptor.
12. IF the estimated duration of a single-`SiteCode` Shard_Descriptor covering the whole requested
    range, evaluated at the Floor_Throughput of `sensor-simulator-service` Requirement 12
    criterion 3, exceeds the configured Shard_Budget, THEN THE Backfill_Planner SHALL reject the
    request with a message naming the requested range, the Shard_Budget, and that estimated
    duration, and SHALL produce no Shard_Plan, one `SiteCode` per Shard_Descriptor being the finest
    partition the parallel axis of criterion 1 admits.
13. WHEN a Backfill_Worker executes its Shard_Descriptor, THE Backfill_Worker SHALL process that
    descriptor's Time_Window values in the ascending order that descriptor gives and SHALL publish
    each `SiteCode`'s records in non-decreasing `DateTime` order, so that the ordering of
    `sensor-simulator-service` Requirement 12 criterion 3 and that spec's Requirement 13 criterion 8
    holds in the published stream and not only in the Shard_Plan.
14. IF a Backfill_Worker's Shard_Descriptor carries a Generation_Tag other than the one derived from
    that runtime's own Swarm_Identity_Config, or carries a `SiteCode` outside the set derived from
    that configuration, THEN THE Backfill_Worker SHALL exit with a non-zero status naming the shard
    index and both Generation_Tag values or the offending `SiteCode`, SHALL read no
    Credential_Parameter, and SHALL publish no Sensor_Data_Record.

### Requirement 9: Stateless Serving

**User Story:** As a Service 2 developer, I want the pull interface to answer from computation rather
than from a store, so that the simulator costs nothing between demos and can never disagree with its
own seed.

#### Acceptance Criteria

1. THE Pull_Runtime SHALL compute every served Sensor_Data_Record from the Swarm_Identity_Config and
   the requested `SiteCode`, `Species`, and interval set at request time, SHALL hold no record
   store, SHALL retain no record across invocations, and SHALL retain every memoized intermediate
   value only for the invocation that computed that value and share it with no other invocation,
   which resolves assumption A6 of `sensor-simulator-service`.
2. THE Deployment SHALL provision no database, no time-series store, and no object store for served
   Sensor_Data_Record values, and SHALL configure no response cache on the managed HTTP endpoint,
   verifiable by Template_Assertion.
3. THE Pull_Runtime SHALL treat the retention window as a validity filter on `DateTime` per
   `sensor-simulator-service` Requirement 14 criterion 7, excluding from every response body every
   record whose `DateTime` falls outside that window while returning HTTP status 200 as that spec's
   Requirement 14 criterion 11 requires.
4. THE Deployment SHALL set the deployed retention window to 30 simulated days and SHALL set that
   window to no value outside the 1-to-365-simulated-day range that `sensor-simulator-service`
   Requirement 14 criterion 7 permits, verifiable by Template_Assertion.
5. WHEN a client issues an authenticated `/SensorData` request with no `startTime` and no `endTime`,
   THE Pull_Runtime SHALL serve the most recently completed Publish_Interval per
   `sensor-simulator-service` Requirement 2 criterion 10, computed from the current simulated
   timestamp and the configured Publish_Interval with no stored state.
6. THE Deployment SHALL provide Backfill_Mode over MQTT as specified in Requirement 8 as the
   supported path for seeding a consumer's store with history, SHALL add no REST route and no
   `/SensorData` query parameter beyond those that `sensor-simulator-service` Requirement 2
   specifies, and SHALL reject no `/SensorData` request on account of the width of that request's
   requested range.
7. THE Deployment SHALL set Pull_Runtime memory to 2048 MB and the Pull_Runtime invocation timeout to
   29 seconds, being the managed HTTP endpoint's integration ceiling, leaving neither value at a
   platform default, verifiable by Template_Assertion.
8. FOR ALL requests whose input is invalid, THE Pull_Runtime SHALL return the HTTP 4xx status and
   the JSON body naming the offending parameter that `sensor-simulator-service` Requirement 1
   criteria 11 and 13 and Requirement 2 criteria 14 through 18 specify, and SHALL return no HTTP 5xx
   status for any bad-input case.
9. IF an authenticated request with valid input cannot be completed inside the Pull_Runtime
   invocation timeout of criterion 7, THEN the managed HTTP endpoint SHALL return an HTTP 5xx status
   and that response body SHALL contain no Sensor_Data_Record and no Sensor_Metadata_Record, so that
   a partial result cannot be mistaken for a complete one, criterion 8 having made bad input a 4xx
   case.
10. WHEN two authenticated `/SensorData` requests carrying identical query parameters, each supplying
    `startTime` and `endTime` per `sensor-simulator-service` Requirement 2 criterion 11, are served
    while the Swarm_Identity_Config and the retention window are unchanged, THE Deployment SHALL
    return response bodies identical byte for byte, irrespective of which Pull_Runtime invocation
    serves each request and of the order in which the two are served.
11. WHILE the derived Swarm size is at or below 500, WHEN a client issues an authenticated
    `/ListSensors` request, THE Deployment SHALL return the complete response within the 2 seconds
    that `sensor-simulator-service` Requirement 1 criterion 8 fixes, measured at the managed HTTP
    endpoint on an invocation that is not a cold start.

### Requirement 10: Observability Wiring

**User Story:** As a demo operator, I want the simulator's own log contract turned into metrics and
alarms without changing that contract, so that a silently dead stream pages me instead of looking
healthy.

#### Acceptance Criteria

1. THE Deployment SHALL forward the standard output of Pull_Runtime, Push_Task, Backfill_Planner,
   Backfill_Worker, and Fleet_Provisioner to the log service unchanged, applying no parsing and no
   reformatting, verifiable by Template_Assertion, which the one-single-line-JSON-object-per-event
   contract of `sensor-simulator-service` Requirement 17 criterion 1 already makes
   machine-consumable, and that unchanged forwarding SHALL carry the per-tick diagnostics of that
   spec's Requirement 5 criterion 10 and Requirement 4 criterion 11 and the scenario window events of
   its Requirement 17 criteria 2 and 6 as log records queried in place.
2. THE Deployment SHALL define one Metric_Filter_Binding for each of the four counts in the
   Publish_Interval summary of `sensor-simulator-service` Requirement 17 criterion 3 — records
   generated, records published, records buffered, and records dropped — with the Push_Task log group
   as the filter source, publishing four metrics in the `AQM/Simulator` namespace with a default
   value of zero, each metric carrying one dimension naming the emitting runtime and one dimension
   carrying the Generation_Tag, verifiable by Template_Assertion.
3. THE Deployment SHALL define one Metric_Filter_Binding on each of the publish-blocked warn event of
   `sensor-simulator-service` Requirement 17 criterion 7, that event being the case where generation
   continues while publishing has stopped, the give-up event of that spec's Requirement 13
   criterion 10, and the `error`-level log lines of its Requirement 17 criterion 4, with the
   Push_Task log group as the filter source, publishing each in the `AQM/Simulator` namespace with a
   metric value of 1 per matching event and a default value of zero, each metric carrying one
   dimension naming the emitting runtime and one dimension carrying the Generation_Tag, verifiable by
   Template_Assertion.
4. FOR ALL Metric_Filter_Binding values in the Deployment, the binding's event name SHALL be a
   member of the set of event names the Simulator emits, and the binding's field path SHALL name a
   field that event carries.
5. IF a Metric_Filter_Binding names an event name the Simulator does not emit or a field that event
   does not carry, THEN THE Offline_Suite SHALL fail naming the binding, because a filter bound to a
   renamed event yields a permanently zero metric and an alarm on a permanently zero metric never
   fires, which is worse than having no metric.
6. FOR ALL rows of the log-derived alarm table below, THE Deployment SHALL configure exactly one
   alarm on that row's metric with that row's comparison and threshold, breaching only after that
   row's number of consecutive periods, with an evaluation period of one Publish_Interval, verifiable
   by Template_Assertion.

   | Metric | Comparison and threshold | Consecutive periods |
   | --- | --- | --- |
   | publish-blocked event count | at or above 1 | 2 |
   | records dropped | above 0 | 1 |
   | records buffered | above 80 percent of the publisher buffer maximum | 1 |
   | give-up event count | at or above 1 | 1 |
   | `error`-level log line count | at or above the configured error-rate threshold | 1 |

   Records dropped and records buffered are the Metric_Filter_Binding values of criterion 2; the
   publish-blocked event count, the give-up event count, and the `error`-level log line count are
   those of criterion 3. The publisher buffer maximum is the configured value whose default is 1000
   per `sensor-simulator-service` Requirement 13 criterion 6. The error-rate threshold is a
   configured value with no default asserted here.
7. FOR ALL rows of the platform-derived alarm table below, THE Deployment SHALL configure exactly one
   alarm on that row's metric with that row's comparison and threshold, verifiable by
   Template_Assertion, and THE Deployment SHALL configure no alarm on any condition outside this
   table and the table of criterion 6.

   | Metric | Comparison and threshold | Evaluated |
   | --- | --- | --- |
   | Push_Task running task count | below 1 | only while the desired count is 1 or more |
   | managed HTTP endpoint 5xx count | at or above 1 | every period |
   | Backfill_Worker error count | at or above 1 | every period |

8. THE Deployment SHALL publish metrics only for the counts named in criteria 2 and 3 — the four
   Publish_Interval summary counts, the publish-blocked event count, the give-up event count, and the
   `error`-level log line count — and SHALL publish a metric for no other Simulator log event,
   because at 500 Virtual_Sensor instances promoting per-tick telemetry to metrics costs more than
   the compute producing it.
9. THE Deployment SHALL enable request tracing on Pull_Runtime, Backfill_Planner, Backfill_Worker,
   and the backfill orchestration execution, each of which has a request or task boundary, and WHERE
   the Push_Task startup-tracing flag is not set, THE Deployment SHALL enable no tracing on
   Push_Task, its diagnostics being the metrics of criterion 2 and the per-Virtual_Sensor error lines
   of `sensor-simulator-service` Requirement 17 criterion 4.
10. WHERE the Push_Task startup-tracing flag is set, THE Deployment SHALL trace the startup path
    only, being credential materialization and first broker connect, that path having a bounded
    operation to trace.
11. THE Deployment SHALL set an explicit retention period on every log group it creates, with
    default 14 days, and SHALL create no log group with indefinite retention, verifiable by
    Template_Assertion.
12. FOR ALL log lines, response bodies, and diagnostic output the Deployment produces or forwards,
    the API key value SHALL be absent and every private key SHALL be absent, on the terms of
    `sensor-simulator-service` Requirement 14 criterion 10 and Requirement 16 criterion 9.
13. FOR ALL alarms of the tables of criteria 6 and 7, THE Deployment SHALL attach exactly one
    operator notification destination to that alarm's transition into alarm state and SHALL create no
    alarm with an empty action list, verifiable by Template_Assertion, because an alarm no operator
    receives surfaces nothing.
14. WHILE the Push_Task desired count is 0, THE Deployment SHALL hold every alarm of the table of
    criterion 6 out of alarm state on absent data and SHALL treat absent data as non-breaching for
    those alarms, so that the scale to zero of Requirement 12 criterion 1 raises no alarm, Push_Task
    emitting no Publish_Interval summary at all outside a Demo_Window and those metrics being absent
    rather than zero.

### Requirement 11: Continuous Integration and Delivery

**User Story:** As a developer joining the monorepo, I want every safety-relevant check to run before
anything touches an AWS account, so that a broken deploy is caught on a runner with no credentials.

#### Acceptance Criteria

1. THE Deployment SHALL provide an Offline_Suite comprising lint, type check, the unit and error-case
   tests, every property test, template synthesis, every Template_Assertion, and every other check
   this document names that requires no AWS credentials and no network access beyond localhost,
   excluding the Docker-marked check of criterion 4, extending `sensor-simulator-service`
   Requirement 16 criterion 10 to the infrastructure code.
2. THE Deployment SHALL perform no environment lookup, no account-dependent context resolution, and
   no network call during template synthesis, and SHALL pass every cross-stack value explicitly, so
   that synthesis is possible under the conditions of criterion 12.
3. THE pipeline SHALL order its stages so that the lint and type check stage, the Offline_Suite
   stage, and the Docker-marked stage of criterion 4 each precede every stage holding the
   Deploy_Identity, and SHALL attach no AWS identity to and expose no AWS credential to any of those
   three stages.
4. THE pipeline SHALL run the Docker Compose integration check of `sensor-simulator-service`
   Requirement 16 criterion 6 in a stage isolated by a test marker that the Offline_Suite excludes,
   in a job that has a container daemon and localhost networking available and no AWS role attached,
   so that no network is relaxed while no AWS credentials remains enforced.
5. THE pipeline SHALL build the Simulator_Image exactly once per pipeline run, SHALL record its
   Image_Digest, and SHALL deploy every environment in that same run from that recorded digest, so
   that a demo deploy executes bytes identical to those the earlier stages verified.
6. THE pipeline SHALL run, after each deploy and using a read-only identity, the provisioned-versus-
   derived `SiteCode` set check of Requirement 3 criterion 12, the one-`ACTIVE`-certificate-per-device
   check of Requirement 2 criterion 3, and the per-device authorization check of Requirement 4
   criterion 7, and SHALL record each of those checks' exit status as the result of that post-deploy
   stage, so that a downstream stage can gate on it.
7. THE Offline_Suite SHALL execute every property test named in this document with a minimum of 100
   generated examples per property, matching the floor of `sensor-simulator-service` Requirement 16
   criterion 3, and the gating test-runner profile SHALL configure no property below that floor.
8. WHEN the Offline_Suite runs on a runner with four vCPUs, THE Offline_Suite SHALL complete within
   10 minutes, SHALL draw Swarm sizes in property-test generators from the range 3 to 20 rather than
   from the 1-to-500 configuration range, and SHALL pin the 500-Virtual_Sensor case in example tests
   rather than in generators.
9. WHILE the checks of criterion 6 have not all exited zero for the current run's dev deploy, or
   while no explicit manual approval has been recorded for that run, THE pipeline SHALL start no
   demo deploy stage.
10. THE pipeline SHALL trigger only on changes to files under the simulator directory or the
    infrastructure directory, so that a change to another service does not run it.
11. THE pipeline's deploy stages SHALL obtain the Deploy_Identity through a federated short-lived
    credential exchange and SHALL use no long-lived access key.
12. WHEN the Offline_Suite runs on a machine with no AWS credentials present and no network access
    beyond localhost, THE Offline_Suite SHALL exit with status zero if and only if every check the
    Offline_Suite comprises passed.
13. IF a stage that criterion 3 requires to precede every stage holding the Deploy_Identity reports
    no passed result, including a skipped result, a cancelled result, a timed-out result, and a
    result that is unavailable, THEN THE pipeline SHALL treat that stage as failed and SHALL start no
    stage holding the Deploy_Identity.
14. IF any check of criterion 6 exits non-zero after the dev deploy, THEN THE pipeline SHALL fail the
    run, SHALL start no demo deploy stage, and SHALL leave the Image_Digest deployed to the demo
    environment unchanged.

### Requirement 12: Cost Posture and Scale to Zero

**User Story:** As a demo operator, I want the deployed simulator to bill nothing when nobody is
using it, so that it can stay deployed between demos on a budget of tens of dollars a month.

#### Acceptance Criteria

1. WHILE the derived Swarm size is at or below the per-process maximum of 500 that
   `sensor-simulator-service` Requirement 15 criterion 4 permits, THE Deployment SHALL set the
   Push_Task service desired count to 0 by default and SHALL define for a Demo_Window exactly two
   scheduled actions — a start action setting the desired count to the absolute value 1 and an end
   action setting the desired count to the absolute value 0, with a default schedule of weekdays
   13:45 to 18:30 UTC — each action reading neither the current desired count nor incrementing it and
   neither action conditioned on the other having run, verifiable by Template_Assertion, so that a
   repeated firing is idempotent and a failed start action suppresses no end action.
2. THE Deployment SHALL configure no provisioned concurrency on Pull_Runtime, Backfill_Planner, and
   Backfill_Worker, no capacity reservation and no always-on runner for Push_Task, and no scaling
   policy and no scheduled action other than the start action of criterion 1 that sets the Push_Task
   desired count above 0, verifiable by Template_Assertion, so that between Demo_Windows the only
   standing charges are the log groups, the managed secret, the stored image, and the Push_Task
   public IP address.
3. THE Deployment SHALL provision no NAT gateway and SHALL place Push_Task in a public subnet with a
   security group carrying no inbound rule and carrying outbound rules permitting TCP only on the
   MQTT port that Requirement 7 criterion 9 sets, permitting outbound traffic on no other port and
   under no other protocol, verifiable by Template_Assertion.
4. WHERE the Push_Task capacity mode is set to interruptible for a Demo_Window, WHEN an interruption
   ends the Publish_Interval in flight, THE Deployment SHALL record one interruption event naming
   that Publish_Interval's start timestamp and the `SiteCode` range that task instance owned, and
   SHALL republish that interval from no Push_Task instance, that interval's records being omitted
   per `sensor-simulator-service` Requirement 12 criterion 8.
5. WHERE the Push_Task capacity mode is set to non-interruptible for a Demo_Window, THE Deployment
   SHALL configure that window's capacity provider strategy with non-interruptible capacity alone and
   with no interruptible weight, verifiable by Template_Assertion.
6. WHEN an interruption event of criterion 4 is recorded, THE Deployment SHALL start exactly one
   execution of the backfill state machine of Requirement 1 criterion 1 whose requested range is
   exactly the recorded Publish_Interval and whose `SiteCode` range is exactly the recorded
   `SiteCode` range, that execution reproducing the real-time output for that interval per
   `sensor-simulator-service` Requirement 12 criterion 4.
7. THE Deployment SHALL store per-device credentials in the parameter store as Requirement 5
   criterion 1 requires, SHALL create every Credential_Parameter in the standard parameter tier, and
   SHALL store no per-device credential in the managed secret store, so that per-device credential
   storage carries no per-device monthly charge.
8. THE Deployment SHALL insert no message-batching layer between the MQTT_Publisher and the broker,
   the one-record-per-message rule of `sensor-simulator-service` Requirement 13 criterion 2 and the
   Publish_Interval averaging of that spec's Requirement 12 criterion 2 already having fixed the
   message volume.
9. WHERE the derived Swarm size exceeds the per-process maximum of 500 that
   `sensor-simulator-service` Requirement 15 criterion 4 permits, THE Deployment SHALL run several
   Push_Task instances each owning a contiguous `SiteCode` range, applying the partitioning of
   Requirement 8 criterion 1 to residency.
10. THE Deployment SHALL provide a read-only Cost_Posture_Check that reports the Push_Task desired
    count together with every interruption event recorded under criterion 4 for which no succeeded
    execution of criterion 6 covers that event's Publish_Interval and `SiteCode` range, that makes no
    write, that exits non-zero when the reported desired count is 1 or more outside every scheduled
    Demo_Window of criterion 1 or when it reports at least one uncovered interruption event, and that
    exits zero otherwise.

### Requirement 13: Repository Credential Hygiene

**User Story:** As a security reviewer, I want the repository provably free of credential material,
so that a leaked clone leaks nothing and the check cannot be skipped by a missing local hook.

#### Acceptance Criteria

1. FOR ALL files in the tracked file list at the commit under test, the file SHALL contain no PEM
   private key block, no PEM certificate block, and no assignment of a value other than a secret
   reference to the API key environment variable of Requirement 7 criterion 4, extending
   `sensor-simulator-service` Requirement 16 criterion 8 to the infrastructure code.
2. THE Deployment SHALL exclude from version control the whole directory tree named by the fixed
   prefix of the credential path template of `sensor-simulator-service` Requirement 13 criterion 4,
   rather than the individual resolved paths that spec's Requirement 16 criterion 11 requires the
   local generation command to write, so that the exclusion holds for every Swarm_Identity_Config
   instead of decaying when the Swarm size changes.
3. THE Offline_Suite SHALL include a repository scan that reads every file in the tracked file list at
   the commit under test as bytes with no extension filter and no path filter other than the
   allowance list of criterion 5, that recognizes a prohibited value only by a PEM delimiter pair or
   by an assignment site of the API key environment variable and by no length-based and no
   character-class heuristic, that reads no commit other than the one under test, and that enforces
   criteria 1 and 2, so that enforcement runs in the pipeline where it cannot be bypassed rather than
   in a local hook alone.
4. IF the repository scan of criterion 3 finds a tracked file containing credential material of any
   kind that criterion 1 enumerates, THEN the scan SHALL write one message per matched file naming
   that file and which of those kinds matched, SHALL exclude the matched value and the bytes
   surrounding it from every message, and SHALL exit non-zero after reporting every matched file
   rather than at the first match.
5. WHERE a tracked file is named in the committed allowance list the repository scan of criterion 3
   reads, the scan SHALL exclude that file from the kinds that allowance entry names and from no
   other kind, each entry naming the file, the excluded kind, and a stated reason, so that a
   committed public certificate is a declared exception rather than a reason to disable the scan.
6. IF an allowance entry names a file absent from the tracked file list, names the PEM private key
   block kind, or omits the excluded kind or the stated reason, THEN the repository scan SHALL exit
   non-zero naming that entry and SHALL exclude no file from any kind.
7. IF the repository scan of criterion 3 finds any prohibited material, THEN THE Deployment SHALL
   treat that material as disclosed, SHALL apply the revocation of Requirement 6 criterion 5 to every
   affected `SiteCode`, and SHALL apply the coordinated key rotation of Requirement 7 criterion 8
   where the affected material is the API key, removal of the file being insufficient because the
   scan is scoped to the commit under test while earlier commits retain the material.
