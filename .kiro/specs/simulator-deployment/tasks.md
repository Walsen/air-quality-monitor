# Implementation Plan: Simulator Deployment

## Overview

Implementation proceeds outside-in from the parts that need nothing but this repository, to the parts
that need the Simulator's own source, to the parts that need an AWS account. The pure algorithms and
the deployment data models come first, then the provisioning and credential logic against injected
clients, then the read-only checks, then the CDK stacks and every Template_Assertion, then the
pipeline definition, and only then the entrypoint wiring that imports the Simulator.

Every step is TDD (practices §3): the failing test comes first, then the smallest clean change that
passes it. That ordering is stated in each task rather than deferred to a block of test tasks at the
end. Each numbered design Correctness Property gets one `hypothesis` or `aws_cdk.assertions` test
running at least 100 examples where it is generative, tagged
`Feature: simulator-deployment, Property {n}`.

Determinism rules (practices §2) apply to the deployment code as much as to the Simulator: the shard
planner, the Generation_Tag digest, and the authorization-sample pair selection take their inputs
explicitly and call no wall clock and no module-level random; every registry, parameter-store,
secret, and clock dependency arrives injected so the whole of it is offline-testable.

Language: Python 3.12 throughout, AWS CDK in Python, `pytest` plus `hypothesis`.

### Sequencing note — the sibling spec is a hard prerequisite for one block only

This spec deploys the Simulator specified by `sensor-simulator-service`. That spec's implementation
has not been executed, so no Simulator source exists in the repository yet. Decision DP4 has the
Fleet_Provisioner call the Simulator's own Swarm identity factory rather than reimplement the
`SiteCode` rule, and DP2 puts every runtime on the Simulator's single image, so some work here cannot
complete until that source exists.

- **Tasks 1 through 14 do not wait on it.** The data models, the shard planner, the provisioning and
  credential algorithms, the rotation and revocation operations, the read-only checks, every CDK
  stack, every Template_Assertion, the repository scan, and the pipeline definition are all
  independently testable with no Simulator source, because every boundary is injected and a template
  is a data structure.
- **Tasks 15 and 16 require it**, and each such task names the sibling component it needs with a
  `_Depends on:_` line. Until then those bindings stand as protocol definitions with fakes, which is
  what keeps tasks 1 through 14 green on their own.

### Placement note

The CDK app, its stacks, and every Template_Assertion live under `infra/`. The runtime-side
deployment modules — the provisioner, the shard planner, the Credential_Materializer, the entrypoint
dispatcher, and the read-only checks — also live under `infra/src/aqm_deploy/` and are carried into
the Simulator_Image by widening that image's build context to the repository root, so DP2's single
image definition still holds and no second image definition is introduced. Task 15.8 records the
one-line change to the sibling-owned image definition that this requires.

### Marker legend

- `*` — test sub-task, skippable for a faster path to a working deployment.
- `_Requirements: X.Y_` — acceptance criteria of **this** spec's requirements.md.
- `_Properties: N_` — Correctness Property N of this spec's design.md.
- `_Requires: AWS account (post-deploy verification)_` — cannot run offline; this is the credential
  boundary Requirement 11 draws.
- `_Depends on: sensor-simulator-service (component)_` — blocked on the sibling spec's source.

## Tasks

- [ ] 1. Infrastructure project scaffolding and the offline gate
  - [ ] 1.1 Create the `infra/` project skeleton and pinned manifest
    - `infra/pyproject.toml` declaring Python 3.12 with `aws-cdk-lib`, `constructs`, `boto3`,
      `mangum`, `pytest`, `pytest-xdist`, and `hypothesis` each pinned to one exact version, and
      declaring the CDK assertions module as a test dependency
    - Package tree `infra/src/aqm_deploy/{models,planning,provisioning,credentials,operations,checks,entrypoints,observability}/`,
      `infra/stacks/`, `infra/app.py`, and `infra/tests/{unit,properties,assertions,integration}/`
    - _Requirements: 11.1, 11.4_
  - [ ] 1.2 Configure the test runner, the marker split, and the gating property profile
    - Register the `docker` marker and make the default selection `-m "not docker"`; define
      `hypothesis` profiles `ci` (exactly 100 examples, no deadline), `dev` (20), and `nightly`
      (1000), with `ci` as the gating profile
    - Failing test first: a configuration test asserting the gating profile's example count is not
      below 100, that the `docker` marker is registered and excluded by default, and that the shared
      Swarm-size generator draws from 3 to 20 with the 500-sensor case reserved for example tests
    - _Requirements: 11.7, 11.8, 11.4_
  - [ ] 1.3 Implement the Offline_Suite runner
    - One command running lint, type check, unit tests, property tests, template synthesis, and every
      Template_Assertion, aggregating their exit statuses
    - Failing test first over stub check results: the runner exits zero if and only if every check
      passed, and treats a check that reports no result as a failure
    - _Requirements: 11.1, 11.12_
    - _Properties: 17_
  - [ ] 1.4 Implement the structured JSON logger for the deployment-side modules
    - One single-line JSON object per event to stdout, configured centrally, never `print()`; a
      redaction helper that names a configuration value and its `SiteCode` while excluding the value
    - Failing test first: the helper's output contains the configuration key name and the `SiteCode`
      and does not contain the supplied value, for a PEM block, a private key, and an API key
    - _Requirements: 10.12, 5.3_

- [ ] 2. Deployment data models
  - [ ] 2.1 Implement `SwarmIdentityConfig` and the Generation_Tag digest
    - Frozen dataclass carrying Seed, Swarm size, Geography_Profile name, `SiteCode` prefix,
      Publish_Interval, and the supplied site list where one is supplied; one instance is the single
      source supplied to the provisioner and to every runtime
    - `generation_tag` as a stable digest over Seed, Swarm size, profile name, prefix, and the
      derived `SiteCode` set; `site_code_digest` as the separate digest of the derived set alone
    - Failing test first: two configurations with different derived sets produce different tags, and
      one configuration produces the same tag across processes
    - _Requirements: 3.1, 3.5_
  - [ ]* 2.2 Write property test for digest stability and set sensitivity
    - *For any* `SwarmIdentityConfig`, the Generation_Tag is byte-identical across repeated
      computation and independent of field construction order, and *for any* two configurations whose
      derived `SiteCode` sets differ the tags differ
    - _Requirements: 3.5, 3.11_
  - [ ] 2.3 Implement `ShardDescriptor` and `TimeWindow`
    - Frozen dataclasses: shard index, contiguous ascending `SiteCode` slice, ordered consecutive
      Time_Window tuple, Generation_Tag; each window a half-open simulated interval with its interval
      count
    - Failing test first: a descriptor rejects a non-ascending slice, a non-consecutive window
      sequence, and a window whose count disagrees with its bounds; the round trip to and from the
      result record of a failed execution preserves index, slice, window sequence, and tag
    - _Requirements: 8.1, 8.10_
  - [ ] 2.4 Implement `DeviceIdentityRecord` with key material structurally excluded
    - `SiteCode`, Thing ARN, certificate identifier and ARN, parameter name, active flag, and no
      field capable of holding a private key or a certificate PEM block
    - Failing test first: constructing the record from a provisioning result that carries key material
      raises, and the serialized form crossing the template response boundary contains neither a
      private key nor a PEM block
    - _Requirements: 5.2, 5.3_
  - [ ] 2.5 Implement `FleetDiff` and the Fleet_Replacement predicate
    - to-create, to-retain, and to-orphan subsets in ascending order, with `is_fleet_replacement`
      true exactly when the retain subset is empty and the orphan subset is not
    - Failing test first: an additive diff, a subtractive diff, a partial-overlap diff, and a
      disjoint diff each classify as expected, and an empty-orphan diff is not a replacement
    - _Requirements: 3.9, 3.14_
  - [ ]* 2.6 Write property test for Fleet_Replacement and partial-overlap classification
    - *For any* pair of derived `SiteCode` sets, the diff's three subsets partition their union
      exactly once, and `is_fleet_replacement` is true if and only if the intersection is empty and
      the left-only subset is non-empty, so a partial overlap never trips the replacement guardrail
    - _Requirements: 3.9, 3.14_
    - _Properties: 16_
  - [ ] 2.7 Implement `MetricFilterBinding`
    - Log event name, field path within that event, metric name, and dimension tuple
    - Failing test first: a binding rejects an empty event name, an empty field path, and a dimension
      tuple that omits the emitting-runtime dimension or the Generation_Tag dimension
    - _Requirements: 10.4_

- [ ] 3. Shard planning algorithm
  - [ ] 3.1 Implement the planner core against the Floor_Throughput budget
    - Pure function of `(SwarmIdentityConfig, range_start, range_end, budget_seconds)`; capacity
      derived from the Floor_Throughput and the budget alone, with the parallel axis the contiguous
      ascending `SiteCode` slice and the sequential axis consecutive Time_Window values; no injected
      clock and no random stream because it needs neither
    - Failing test first: the two worked plans of the design — one sensor per shard over the widest
      range, and twenty sensors per shard over a narrow range — assert shard count, slice widths, and
      window counts before the implementation exists
    - _Requirements: 8.1, 8.5, 8.6_
  - [ ]* 3.2 Write property test for exact partition of the request space
    - *For any* valid `(config, range_start, range_end)` triple, the plan's shards partition the
      `(SiteCode × interval-start)` space of the requested range exactly once: no two shards cover the
      same pair and every pair in the range is covered
    - _Requirements: 8.2_
    - _Properties: 5_
  - [ ]* 3.3 Write property test for per-`SiteCode` containment and window order
    - *For any* plan and *for any* `SiteCode` in the range, every interval of that `SiteCode` lies in
      exactly one shard, and within that shard the Time_Window values are consecutive and ascending
    - _Requirements: 8.3_
    - _Properties: 6_
  - [ ]* 3.4 Write property test for plan determinism
    - *For any* valid planning input, two independent planner calls produce identical
      Shard_Descriptor sequences, and the plan is unchanged when the worker count, the fan-out
      concurrency, and the wall clock are varied
    - _Requirements: 8.4, 8.8_
    - _Properties: 7_
  - [ ]* 3.5 Write property test for Floor_Throughput budget bounding
    - *For any* emitted Shard_Descriptor, the `SiteCode` count multiplied by the sum of the window
      interval counts, evaluated at the Floor_Throughput, is at or below the configured Shard_Budget,
      which is itself strictly below the Invocation_Ceiling
    - _Requirements: 8.5, 8.6_
    - _Properties: 7_
  - [ ] 3.6 Implement request validation and its refusals
    - Reject a range end at or before the range start, a span exceeding the maximum backfill span, and
      a range start unaligned to a Publish_Interval boundary, naming each offending value and its
      permitted form and producing no plan
    - Failing test first: one example per offending value, and one carrying all three, asserting the
      message names every offending value and that no plan is returned
    - _Requirements: 8.7_
  - [ ] 3.7 Implement the unshardable-range refusal
    - Where a single-`SiteCode` descriptor covering the whole requested range still exceeds the
      Shard_Budget at the Floor_Throughput, reject naming the requested range, the budget, and the
      estimated duration, and produce no plan
    - Failing test first: the boundary case one interval below the refusal threshold plans
      successfully and the case one interval above it refuses with all three values named
    - _Requirements: 8.12_
  - [ ]* 3.8 Write unit tests pinning the 500-sensor plans
    - The full-span 500-sensor plan and the narrow-span plan asserted exactly, as example tests rather
      than through generators, per the generator-range rule of task 1.2
    - _Requirements: 8.5, 8.6_

- [ ] 4. Checkpoint - pure algorithms and data models
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 5. Fleet provisioner
  - [ ] 5.1 Define the injected boundaries and their fakes
    - Protocols for the device registry, the parameter store, the durable operation-state store, and
      the `SiteCode` source, plus in-memory fakes that record every call in order and can be told to
      throttle, to fail a persist, or to present pre-existing inconsistent state
    - Failing test first: the fakes reject a call sequence that violates the registry's own contract,
      so a later test cannot pass against an over-permissive fake
    - _Requirements: 2.1, 3.2_
  - [ ] 5.2 Implement Fleet_Diff computation and the replacement guardrail
    - Classify the derived set against the provisioned set; refuse a Fleet_Replacement unless
      `allow_fleet_replacement` is explicitly set, naming the empty retain set and the flag, changing
      no registry state and no Credential_Parameter; where the flag is set, provision the new
      generation in full and deactivate the preceding generation's certificates
    - Failing test first: the size-increase case is purely additive, the size-decrease case orphans
      only removed values, the disjoint case refuses without the flag, and the flagged disjoint case
      deactivates exactly the preceding generation
    - _Requirements: 3.6, 3.7, 3.8, 3.9, 3.14_
  - [ ] 5.3 Implement the create pass
    - Ascending iteration; idempotent Thing creation treating an already-exists report as success;
      count that Thing's `ACTIVE` certificates and mint only when the count is zero; write the bundle
      to one standard-tier encrypted Credential_Parameter under the Generation_Tag prefix keyed by that
      device's own `SiteCode`; attach the Fleet_Policy and add the Thing to the Fleet_Generation group
    - Failing test first: the mint-only-when-zero guard, the already-exists continuation, the exact
      parameter path, and the ascending call order asserted against the recording fake
    - _Requirements: 2.1, 2.4, 2.5, 2.9, 2.10, 5.1, 12.7_
  - [ ]* 5.4 Write property test for provisioning idempotence
    - *For any* sequence of one or more provisioner runs over one `SwarmIdentityConfig`, including
      runs interrupted at any step, at most one `ACTIVE` certificate exists per derived `SiteCode` at
      every point during and after those runs, and a run whose to-create subset is empty leaves the
      set of certificate identifiers unchanged
    - _Requirements: 2.6_
    - _Properties: 1_
  - [ ] 5.5 Implement the persist-failure path
    - Where the Credential_Parameter write fails after the certificate exists, set that certificate
      inactive, log one line naming the `SiteCode` and the failure type, and fail with a non-zero
      result rather than continuing
    - Failing test first: the fake fails the write, and the test asserts the deactivation call, the log
      line's contents, the absence of key material from it, and that no later `SiteCode` was processed
    - _Requirements: 2.7_
  - [ ] 5.6 Implement throttling retry with bounded attempts
    - Retry the throttled `SiteCode` with exponential backoff and randomized jitter under a bounded
      concurrency limit up to a configured maximum attempt count, then fail naming that `SiteCode`
      and the attempt count; the jitter source is injected so the test is deterministic
    - Failing test first: a throttle sequence that succeeds on the last permitted attempt, and one
      that exhausts them, asserting attempt counts and the failure message
    - _Requirements: 2.8_
  - [ ] 5.7 Implement the orphan pass
    - Ascending iteration over the to-orphan subset: set the certificate inactive, delete that
      device's Credential_Parameter, remove the Thing from the Fleet_Generation group, delete no Thing
      and no certificate, and leave every retained Device_Identity untouched
    - Failing test first: the retained devices' recorded call sets are empty and the orphaned devices'
      contain exactly the three steps in that order
    - _Requirements: 3.7, 6.8_
  - [ ]* 5.8 Write property test for orphan cleanup
    - *For any* pair of configurations differing in Swarm size or Seed, after the provisioner runs
      there is no `ACTIVE` certificate and no Credential_Parameter for any `SiteCode` the change
      removed from the derived set
    - _Requirements: 3.7, 6.8_
    - _Properties: 16_
  - [ ] 5.9 Implement the over-ceiling refusal
    - Support a derived set up to the supported ceiling inside one invocation; above it, fail naming
      the derived set size and the ceiling before creating any Thing, minting any certificate, or
      writing any Credential_Parameter
    - Failing test first: the ceiling case proceeds and the ceiling-plus-one case refuses with an
      empty recorded call list on the registry and parameter fakes
    - _Requirements: 2.11, 2.12_
  - [ ] 5.10 Implement the concurrent-invocation guard
    - Claim the Generation_Tag in the durable operation-state store at entry and release it at exit;
      a second invocation for a tag already claimed fails naming that tag before any write
    - Failing test first: the second invocation fails with an empty recorded call list, and a claim
      left by a crashed invocation is distinguishable from a live one
    - _Requirements: 2.14_
  - [ ] 5.11 Implement the inconsistent pre-existing-state refusal
    - Where a derived `SiteCode` holds more than one `ACTIVE` certificate, or holds one with no
      readable Credential_Parameter under the Generation_Tag prefix, fail naming that `SiteCode` and
      the observed condition and mint nothing and write nothing for it
    - Failing test first: both conditions, each asserting the named condition and that no mint or
      write was recorded for that `SiteCode`
    - _Requirements: 2.15_
  - [ ] 5.12 Implement the revoked-`SiteCode` guard
    - Where a `SiteCode` is recorded as revoked in durable state, mint no certificate for it in the
      create pass even though its `ACTIVE` count is zero
    - Failing test first: a revoked code is skipped with no mint recorded, while its unrevoked
      neighbours in the same run are provisioned
    - _Requirements: 6.12_
  - [ ]* 5.13 Write property test for credential material never crossing a boundary
    - *For any* provisioning run over any configuration and any failure injection, the returned record
      set carries parameter names, certificate identifiers, and the derived-set digest and carries no
      private key and no certificate PEM block, and no emitted log line contains either
    - _Requirements: 5.2, 5.3, 10.12_
    - _Properties: 11_
  - [ ] 5.14 Implement the supplied-site-list derivation path
    - Where a site list is supplied, derive the set from the supplied entries with a size equal to the
      entry count in strictly ascending order and create exactly one Device_Identity per entry
    - Failing test first: a supplied list of unordered entries yields an ascending derived set of the
      same size and exactly that many create passes
    - _Requirements: 3.13_
  - [ ]* 5.15 Write unit test for the profile-value-change no-op
    - A change to the active Geography_Profile's values, with its name, the prefix, the Seed, and the
      Swarm size unchanged, records no create, no deactivate, and no delete
    - _Requirements: 3.10_

- [ ] 6. Credential materializer
  - [ ] 6.1 Implement batched reads and template-resolved writes scoped to owned `SiteCode` values
    - Read the Credential_Parameter values for exactly the `SiteCode` values the runtime owns — the
      whole derived set for the resident task, exactly the Shard_Descriptor's slice for a worker — in
      batches of at most ten per call, and write one certificate file and one key file per owned code
      at the paths resolved from the credential path template, before configuration validation runs
    - Failing test first: a 25-code fake asserts three batched calls, the exact resolved paths, and
      that a code outside the owned set is neither read nor written
    - _Requirements: 5.4_
  - [ ] 6.2 Implement the memory-backed mount discipline and file mode
    - Every credential file is written inside the declared memory-backed mount and none outside it,
      each with a mode readable and writable by the runtime's own user alone
    - Failing test first: a write targeting a path outside the mount raises, and the created files'
      modes are asserted exactly
    - _Requirements: 5.5_
  - [ ] 6.3 Implement the missing-path diff log and the fail-fast handoff
    - After materialization, log one line naming every owned `SiteCode` absent from the materialized
      set, then let configuration validation exit non-zero with one message per affected value naming
      the configuration value and the `SiteCode` and excluding the value itself
    - Failing test first: with two of five parameters missing, the diff line names exactly those two,
      it precedes the exit, and the exit status is non-zero
    - _Requirements: 5.6, 5.7_
    - _Properties: 15_
  - [ ] 6.4 Implement parameter-read throttling retry
    - Retry with backoff up to a configured maximum attempt count, then exit non-zero naming the
      affected `SiteCode` values
    - Failing test first: a throttle sequence succeeding on the last permitted attempt, and one
      exhausting them, asserting the named codes in the failure
    - _Requirements: 5.9_
  - [ ] 6.5 Implement the no-material-outlives-the-process rule
    - Remove every materialized certificate and key file on process exit, including the failure exits
      of tasks 6.3 and 6.4, so a reused execution environment presents no earlier Shard_Descriptor's
      or Fleet_Generation's material
    - Failing test first: after a successful run, after a failed run, and after a run for a different
      Shard_Descriptor in the same environment, no file remains at any template-resolved path
    - _Requirements: 5.13_
  - [ ] 6.6 Resolve the broker certificate authority path from image material
    - Supply the configured authority path as a value pointing at material already present in the
      image, and materialize no authority material from any Credential_Parameter
    - Failing test first: the materializer's recorded parameter reads contain no authority entry and
      the configured path resolves to an image path rather than a mount path
    - _Requirements: 5.12_

- [ ] 7. Rotation, revocation, and API key rotation operations
  - [ ] 7.1 Implement single-`SiteCode` rotation in its documented order
    - Create, activate, attach the Fleet_Policy and the Thing, write the new bundle as a new parameter
      version, restart the runtimes holding superseded material, then deactivate and delete the
      superseded certificate; replace no credential inside a running process, and treat the restart
      step as satisfied where no such runtime is running
    - Failing test first: the recorded call order is asserted step by step, and no hot-swap path exists
      for a running process
    - _Requirements: 6.1, 6.4_
  - [ ] 7.2 Implement the bounded two-certificate window and its durable record
    - Hold exactly two `ACTIVE` certificates for the rotating `SiteCode`, both attached to that one
      Thing and neither to another, keep the superseded one `ACTIVE` until the restart step is
      satisfied, record the rotation as in progress in durable state readable by read-only calls from
      the create step until the delete step completes, and bound that state by the configured
      Maximum_Rotation_Window; on completion leave exactly one `ACTIVE` certificate and one
      Credential_Parameter holding its material
    - Failing test first: at every intermediate step the count of `ACTIVE` certificates is never zero
      and never above two, the in-progress record exists across exactly that span, and the window
      bound is enforced against an injected clock
    - _Requirements: 6.2, 6.3_
  - [ ] 7.3 Implement the mid-rotation failure rollback
    - On any failed step, or on the restart step going unsatisfied inside the window, leave the
      superseded certificate `ACTIVE` and undeleted, set the new certificate inactive, restore the
      Credential_Parameter to the superseded material where new material was written, log one line
      naming the `SiteCode`, the failed step, and the failure type, continue with the remaining
      targeted codes, and exit non-zero naming every failed code
    - Failing test first: one injected failure per step, each asserting the rolled-back end state, the
      log line, and that the device is left serviceable
    - _Requirements: 6.11_
  - [ ] 7.4 Implement single-device revocation
    - Set that device's certificate revoked, delete that device's own Credential_Parameter, record the
      `SiteCode` as revoked in durable state readable by read-only calls, and change no other device's
      certificate, no Fleet_Policy statement, and no other device's Credential_Parameter; no log line
      carries key material or a PEM block
    - Failing test first: the recorded call set touches exactly one device, the revocation record is
      readable afterwards, and the log lines are asserted free of key material
    - _Requirements: 6.5, 6.6, 6.10_
  - [ ] 7.5 Implement reinstatement
    - Provision a new Device_Identity for a revoked `SiteCode` through the rotation steps, clear the
      revocation record only after that identity is in place, and return no revoked certificate to
      `ACTIVE`
    - Failing test first: the revoked certificate's status is unchanged, a new certificate is active,
      and an injected failure before the identity is in place leaves the revocation record intact
    - _Requirements: 6.13_
  - [ ] 7.6 Implement whole-fleet rotation
    - Perform create, activate, attach, and parameter-write for every targeted `SiteCode` in ascending
      order under the bounded concurrency limit, then restart each affected runtime exactly once
      rather than once per code, then deactivate and delete every superseded certificate
    - Failing test first: a 20-code fleet records one restart call, ascending per-code ordering, and
      the three phases in that order
    - _Requirements: 6.14_
  - [ ] 7.7 Implement coordinated API key rotation
    - One operation that replaces the managed secret's value and restarts the runtimes resolving it,
      leaving exactly one valid key at any instant, with the superseded-key outcome documented in the
      operation's own help text
    - Failing test first: at no observed point are two key values simultaneously valid, and the
      operation fails without replacing the value if the restart step cannot be issued
    - _Requirements: 7.8_

- [ ] 8. Read-only verification checks
  - [ ] 8.1 Implement the fleet drift check
    - Compare the locally derived `SiteCode` set against the set holding Fleet_Generation group
      membership, exactly one `ACTIVE` certificate, and a Credential_Parameter; on difference, name
      every code present in only one of them together with which constituent is absent and exit
      non-zero; report a code recorded as revoked as revoked rather than as missing an identity; exit
      zero on equality; make no write
    - Failing test first: an equal case, a missing-Thing case, a missing-parameter case, a
      surplus-code case, and a revoked case, each asserting the message and exit status
    - _Requirements: 3.12, 6.12_
    - _Properties: 4_
  - [ ] 8.2 Implement the one-`ACTIVE`-certificate check
    - Walk the Fleet_Generation group asserting exactly one `ACTIVE` certificate per Thing, the Thing
      name equal to the `SiteCode`, and an injective certificate-to-Thing mapping; treat two `ACTIVE`
      certificates as a pass only where an in-progress rotation record exists for that code and the
      Maximum_Rotation_Window has not elapsed
    - Failing test first: the clean case, the shared-certificate case, the two-certificate case with a
      valid in-progress record, and the same case with an elapsed window
    - _Requirements: 2.3, 6.2, 6.3_
    - _Properties: 1_
  - [ ] 8.3 Implement the credential-correspondence check
    - For each `SiteCode`, compare the certificate identifier held by its resolved Credential_Parameter
      against the registry's reported single `ACTIVE` certificate; on mismatch name the code and both
      identifiers, disclose no key material and no PEM block, and exit non-zero
    - Failing test first: a matching case, a mismatching case asserting both identifiers appear and no
      PEM block does, and an unreadable-parameter case
    - _Requirements: 5.11, 5.3_
  - [ ] 8.4 Implement the certificate-age and detached-certificate audit
    - Report every `SiteCode` whose `ACTIVE` certificate's age from its registry creation time to the
      run time exceeds the configured Maximum_Credential_Age, and every `ACTIVE` certificate in the
      region attached to no Thing or to a Thing outside the Fleet_Generation together with its age;
      make no write; exit non-zero when at least one entry is reported
    - Failing test first: the boundary case at exactly the configured age, one above it, a detached
      certificate, a certificate on a foreign Thing, and the empty case exiting zero, all against an
      injected clock
    - _Requirements: 6.9_
  - [ ] 8.5 Implement the sampled per-device authorization check
    - Select a configured count of ordered pairs of distinct provisioned devices by a rule derived from
      the Seed so identical inputs select identical pairs; for each pair assert own-topic publish
      allowed for both topics, cross-device publish denied for both topics, and cross-device connect
      denied, through the read-only authorization-test path with no device connected; on any failed
      assertion name the ordered pair and the assertion and exit non-zero; retry throttled or failed
      calls within a bounded backoff loop and on exhaustion exit non-zero naming the pairs left
      unasserted and reporting no pass
    - Failing test first: pair selection is reproducible from the Seed and independent of iteration
      order; each of the five assertions fails the check when the fake denies or allows wrongly; and
      exhaustion reports the unasserted pairs and no pass result
    - _Requirements: 4.7, 4.11, 4.12_
    - _Properties: 2_
  - [ ] 8.6 Implement the exactly-one-attached-policy check
    - For every `ACTIVE` certificate in the Fleet_Generation assert exactly one attached policy and
      that it is the Fleet_Policy; on any extra policy name that certificate's `SiteCode` and the extra
      policy and exit non-zero
    - Failing test first: the clean case, an extra-policy case, and a wrong-single-policy case
    - _Requirements: 4.13_
  - [ ] 8.7 Implement the Cost_Posture_Check
    - Report the Push_Task desired count and every recorded interruption event for which no succeeded
      repair execution covers that event's Publish_Interval and `SiteCode` range; make no write; exit
      non-zero when the desired count is one or more outside every scheduled Demo_Window or when at
      least one uncovered event is reported; exit zero otherwise
    - Failing test first: inside-window with count one exits zero, outside-window with count one exits
      non-zero, an uncovered event exits non-zero, and a covered event exits zero, against an injected
      clock and a fake schedule
    - _Requirements: 12.10_
  - [ ] 8.8 Implement the post-deploy check runner
    - One command binding the drift check, the one-certificate check, the correspondence check, the age
      audit, the authorization sample, the attached-policy check, and the Cost_Posture_Check to real
      read-only clients, emitting each check's exit status as a machine-readable result the pipeline
      stage records
    - Failing test first, offline, against the fakes: the runner's aggregate status is non-zero if any
      check is non-zero and its result document names every check and its status
    - _Requirements: 11.6_
    - _Requires: AWS account (post-deploy verification)_

- [ ] 9. Checkpoint - provisioning, credentials, operations, and checks
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 10. CDK stacks
  - [ ] 10.1 Wire the CDK app with an explicit environment and no lookups
    - `infra/app.py` instantiating the six stacks with an explicitly supplied account and region, every
      cross-stack value passed as a constructor argument, and no environment lookup, no
      account-dependent context resolution, and no network call anywhere in synthesis
    - Failing assertion first: a synthesis test that fails if any construct performs a lookup and that
      runs with no credentials present
    - _Requirements: 11.2_
  - [ ] 10.2 Implement the image stack
    - Build the Simulator_Image from the single committed image definition already used locally, for
      exactly one CPU architecture with no second architecture variant, and expose it to consumers by
      digest rather than by any mutable tag
    - Failing assertion first: the synthesized template references a digest and no mutable tag, and
      declares exactly one architecture
    - _Requirements: 1.2, 1.4, 1.5_
  - [ ] 10.3 Implement the device fleet stack's policy and group
    - Exactly one Fleet_Policy for the whole generation and no per-device policy document; `iot:Connect`
      only on the client resource the Thing-name policy variable resolves to; `iot:Publish` on exactly
      the Measurement_Topic and the Housekeeping_Topic `aqm/sensors/{SiteCode}/housekeeping` and no
      other topic; the Fleet_Generation Thing Group named by the Generation_Tag; Credential_Parameter
      resources created in the standard parameter tier
    - Failing assertion first: policy statement count, action set, resource forms, and the absence of
      any alternative measurement topic name
    - _Requirements: 4.1, 4.4, 4.5, 4.6, 12.7_
  - [ ] 10.4 Implement the provisioning custom resource
    - A custom resource on the image, invoking the provision command, carrying the derived-set digest
      and the replacement flag as resource properties so a set change forces an update, with a retain
      policy on every fleet identity resource, and with every `mqtt`-selecting runtime declared
      dependent on it so provisioning completes before any such runtime starts
    - Failing assertion first: the digest is a resource property, the retention policy is retain on
      each fleet identity resource, and the dependency edges exist
    - _Requirements: 2.2, 2.13, 3.11_
  - [ ] 10.5 Implement the pull API stack
    - Managed HTTP endpoint with exactly one catch-all proxy route and no per-path route, no
      endpoint-level API key, usage plan, or authorizer, an explicit stage request-rate limit, and no
      response cache; the runtime on the image by digest with memory 2048 MB, timeout 29 seconds, the
      API key delivered as the secret ARN plus read permission on that ARN alone, the generated key
      drawn from printable non-space ASCII inside the permitted length range, the retention window set
      to 30 simulated days, and no read permission on any Credential_Parameter; no store of any kind
      provisioned for served records
    - Failing assertion first: route count, the absence of authorizer, key, usage plan and cache, the
      rate limit, both numeric values, the key's character set and length, and the absent credential
      grant
    - _Requirements: 1.6, 1.7, 1.8, 5.8, 7.1, 7.3, 7.4, 7.6, 9.2, 9.4, 9.7_
  - [ ] 10.6 Implement the push task stack
    - Task definition on the same image digest and architecture with a memory-backed volume mounted at
      the credential path prefix; every configuration value in the environment definition, including
      the MQTT endpoint host, the broker authority path, the port, both credential path templates, the
      reconnect backoff maximum, the publisher buffer maximum, the retention window, and the log level,
      with the API key delivered by the platform's native secret injection; service desired count zero
      by default with exactly two scheduled actions setting the absolute values one and zero on the
      configured Demo_Window, neither reading nor incrementing the current count and neither
      conditioned on the other; no scaling policy and at most one instance per generation; no NAT
      gateway, a public subnet, and a security group with no inbound rule and outbound TCP on the MQTT
      port alone; a deployment circuit breaker so a failed start rolls the deployment back
    - Failing assertion first: desired count, both scheduled actions and their absolute values, the
      absence of a scaling policy, the mount type, the security group rule sets, and the environment
      key set
    - _Requirements: 1.10, 5.5, 7.1, 7.3, 7.9, 7.10, 7.13, 12.1, 12.2, 12.3, 12.5_
  - [ ] 10.7 Implement the backfill stack
    - Planner and worker runtimes on the same image digest, an orchestration in which exactly one
      planner invocation precedes every worker invocation of that execution, an explicit maximum
      fan-out concurrency supplied to no planner invocation, per-shard retry of at most three attempts
      after the initial one with a five-second first delay doubling thereafter, a Shard_Budget strictly
      below the Invocation_Ceiling, and an execution result recording a failed descriptor's index,
      slice, window sequence, and Generation_Tag
    - Failing assertion first: the planner-before-workers ordering, the concurrency value's presence in
      the orchestration and absence from the planner's input, and the retry parameters
    - _Requirements: 1.1, 8.6, 8.8, 8.9, 8.10, 12.2_
  - [ ] 10.8 Implement the observability stack's metric filters
    - One binding for each of the four Publish_Interval summary counts, and one each for the
      publish-blocked warn event, the give-up event, and `error`-level log lines, all sourced from the
      push task's log group, published in the `AQM/Simulator` namespace with a default value of zero,
      each carrying one dimension naming the emitting runtime and one carrying the Generation_Tag, and
      no metric published for any other Simulator log event
    - Failing assertion first: filter count, namespace, default values, both dimensions on each metric,
      and that no additional metric filter exists
    - _Requirements: 10.2, 10.3, 10.4, 10.8_
  - [ ]* 10.9 Write the metric-binding correspondence test
    - *For any* `MetricFilterBinding` in the stack, the binding's event name is a member of the
      Simulator's emitted event-name set and its field path names a field that event carries; the
      Offline_Suite fails naming the binding otherwise
    - _Requirements: 10.5_
    - _Properties: 12_
    - _Depends on: sensor-simulator-service (log event vocabulary of the observability module)_
  - [ ] 10.10 Implement the alarm set
    - Exactly one alarm per row of the log-derived table with that row's comparison, threshold, and
      consecutive-period count at an evaluation period of one Publish_Interval, treating absent data
      as non-breaching so a zero desired count raises nothing; exactly one alarm per row of the
      platform-derived table, with the running-task alarm evaluated only while the desired count is one
      or more; exactly one operator notification destination on each alarm's transition into alarm
      state, no alarm with an empty action list, and no alarm on any condition outside the two tables
    - Failing assertion first: alarm count equals the two tables' row count, each alarm's parameters
      match its row, every alarm has exactly one action, and missing-data treatment is asserted on the
      log-derived alarms
    - _Requirements: 6.7, 10.6, 10.7, 10.13, 10.14_
  - [ ] 10.11 Implement log forwarding, retention, and tracing
    - Forward the standard output of all five runtimes to the log service unchanged with no parsing and
      no reformatting; set an explicit retention period on every log group created and create none with
      indefinite retention; enable request tracing on the pull runtime, the planner, the worker, and
      the orchestration execution, and enable no tracing on the push task unless its startup-tracing
      flag is set, in which case trace the startup path alone
    - Failing assertion first: no log group lacks a retention value, no forwarding transform is
      configured, tracing is on exactly the four intended targets, and the flag's set and unset cases
      differ only in the push task's startup tracing
    - _Requirements: 10.1, 10.9, 10.10, 10.11_
  - [ ] 10.12 Implement interruption recording and gap repair
    - Where the push task's capacity mode is interruptible, record one interruption event naming the
      in-flight Publish_Interval's start timestamp and the `SiteCode` range that instance owned, and
      republish that interval from no push task instance; start exactly one backfill execution whose
      requested range is exactly that Publish_Interval and whose `SiteCode` range is exactly that range
    - Failing test first: an interruption yields exactly one recorded event and exactly one started
      execution with matching range and slice, and a repeated notification for the same interruption
      starts no second execution
    - _Requirements: 12.4, 12.6_
  - [ ] 10.13 Implement the above-maximum residency partition
    - Where the derived Swarm size exceeds the per-process maximum, run several push task instances
      each owning a contiguous `SiteCode` range, reusing the parallel-axis partition of the shard
      planner for residency
    - Failing test first: a size just above the maximum yields instances whose ranges partition the
      derived set exactly once, and a size at or below it yields one instance
    - _Requirements: 12.9_

- [ ] 11. Cross-cutting Template_Assertion module
  - [ ] 11.1 Assert one image digest and one architecture across all five runtimes
    - *For all* runtimes — pull, planner, worker, provisioner, and the push task definition — the image
      is referenced by digest, no mutable tag appears, every digest is identical, and the platform value
      is set explicitly to the one built architecture with none left at a default
    - _Requirements: 1.4, 1.5_
    - _Properties: 14_
  - [ ] 11.2 Assert no plaintext secret in any synthesized template
    - *For all* stacks, no resource property, output, or metadata entry contains a plaintext secret or
      credential value; every secret appears only as an ARN, a platform secret reference, or a dynamic
      reference resolved at deploy or run time; no template output carries a private key, a PEM block,
      or the API key value
    - _Requirements: 7.5, 10.12_
    - _Properties: 9_
  - [ ] 11.3 Assert the IoT policy shape
    - *For all* IoT policy documents, no statement grants subscribe and none grants receive, and *for
      all* topic and client resources no `*` and no `#` wildcard segment appears and the
      device-identifying segment is expressed through the Thing-name policy variable
    - _Requirements: 4.2, 4.3_
    - _Properties: 3_
  - [ ] 11.4 Assert IAM scoping and the enumerated exceptions
    - *For all* statements attached to a role, resources are named ARNs or ARN patterns naming this
      deployment's account, region, and resource type, with Credential_Parameter resources confined to
      the Fleet_Generation path prefix, except exactly two enumerated statements — one granting only
      the certificate-creation action and one granting only the endpoint-description action — and no
      statement carrying an unscoped resource grants any other action; and no role in the deployment
      grants publish on the IoT data plane
    - _Requirements: 4.9, 4.10_
    - _Properties: 10_
  - [ ] 11.5 Assert the housekeeping topic sits outside the measurement filter
    - *For all* `SiteCode` values, `aqm/sensors/{SiteCode}/housekeeping` does not match the
      Measurement_Filter `aqm/sensors/+/data` while `aqm/sensors/{SiteCode}/data` does, asserted
      against a filter-matching implementation covered by its own failing test first
    - _Requirements: 4.6, 4.8_
    - _Properties: 13_
  - [ ] 11.6 Assert the cost posture has no standing capacity
    - No provisioned concurrency on the pull runtime, the planner, or the worker; no capacity
      reservation and no always-on runner for the push task; no scaling policy and no scheduled action
      other than the one start action that raises the desired count; the interruptible-window strategy
      carries no interruptible weight where the mode is non-interruptible; and no message-batching
      resource sits between the publisher and the broker
    - _Requirements: 12.1, 12.2, 12.5, 12.8_
  - [ ] 11.7 Assert log retention and unchanged forwarding everywhere
    - Every log group the app creates has an explicit retention period, none is indefinite, and no
      forwarding path applies a parse or reformat transform
    - _Requirements: 10.1, 10.11_
  - [ ] 11.8 Assert the configuration surface
    - *For all* runtimes, every configuration value the selected interface requires is present as an
      environment variable in that runtime's own environment definition and none is supplied by
      configuration file; no configuration file path is supplied by command argument or environment
      value; every key inside the configuration namespace is one the Config_Loader accepts; and every
      deployment-only value, including the API key secret reference, sits outside that namespace — the
      accepted-key set held as a committed fixture in this module until task 15.1 binds it to the
      Config_Loader's own set
    - _Requirements: 7.1, 7.2, 7.11_
  - [ ] 11.9 Assert the endpoint and serving shape
    - Exactly one catch-all route and no per-path route; no endpoint-level API key, usage plan, or
      authorizer; an explicit stage rate limit; the pull runtime's memory and timeout at their set
      values with neither at a default; no response cache; and no database, time-series store, or
      object store provisioned for served records
    - _Requirements: 1.6, 1.7, 1.8, 9.2, 9.7_
  - [ ] 11.10 Assert synthesis is credential-free and lookup-free
    - Synthesis of every stack completes with no AWS credentials present and no network access beyond
      localhost, performs no environment lookup and no account-dependent context resolution, and every
      cross-stack value is passed explicitly
    - _Requirements: 11.2_
    - _Properties: 17_

- [ ] 12. Repository credential hygiene scan
  - [ ] 12.1 Implement the tracked-file reader and the two recognizers
    - Read every file in the tracked file list at the commit under test as bytes with no extension
      filter and no path filter; recognize a prohibited value only by a PEM delimiter pair or by an
      assignment of a value other than a secret reference to the API key environment variable, with no
      length-based and no character-class heuristic; read no other commit
    - Failing test first: a fixture tree covering a private key block, a certificate block, a literal
      API key assignment, a secret-reference assignment that must pass, a binary file, and a
      high-entropy string that must not match
    - _Requirements: 13.1, 13.3_
  - [ ] 12.2 Implement the allowance list and its validation
    - Read the committed allowance list; exclude an allowed file from exactly the kinds its entry names
      and from no other kind; exit non-zero naming the entry, and exclude no file from any kind, when an
      entry names an absent file, names the private key kind, or omits the excluded kind or the stated
      reason
    - Failing test first: a valid certificate allowance passes while the same file still fails on the
      API key kind, and each of the three invalid entry forms exits non-zero with nothing excluded
    - _Requirements: 13.5, 13.6_
  - [ ] 12.3 Implement all-matches reporting
    - One message per matched file naming that file and which kinds matched, excluding the matched
      value and the bytes surrounding it, and exit non-zero after reporting every matched file rather
      than at the first match
    - Failing test first: with three offending files, all three appear in the output, no matched value
      appears in it, and the exit status is non-zero
    - _Requirements: 13.4_
  - [ ] 12.4 Exclude the credential path tree from version control
    - Ignore the whole directory tree named by the fixed prefix of the credential path template rather
      than the individual resolved paths, so the exclusion holds for every Swarm_Identity_Config
    - Failing test first: paths generated for three different Swarm sizes are all ignored, and the
      ignore rule names the tree rather than any resolved path
    - _Requirements: 13.2_
  - [ ] 12.5 Implement the disclosure remediation command
    - Given a scan result, apply the single-device revocation of task 7.4 to every affected `SiteCode`
      and the coordinated key rotation of task 7.7 where the affected material is the API key, and
      state in its own output that removing the file is insufficient because earlier commits retain the
      material
    - Failing test first: a result naming two device bundles issues exactly two revocations, a result
      naming the API key issues exactly one rotation, and a result naming both issues both
    - _Requirements: 13.7_
  - [ ]* 12.6 Write property test for repository credential hygiene
    - *For all* files in the tracked file list at the commit under test, the file contains no PEM
      private key block, no PEM certificate block, and no assignment of a value other than a secret
      reference to the API key environment variable, except where a valid allowance entry excludes that
      file from that kind
    - _Requirements: 13.1_
    - _Properties: 8_

- [ ] 13. CI pipeline definition
  - [ ] 13.1 Define the stage order and the credential boundary
    - Lint and type check, then the Offline_Suite, then the Docker-marked stage, each preceding every
      stage that holds the Deploy_Identity, with no AWS identity attached to and no AWS credential
      exposed to any of those three
    - Failing test first: a pipeline-definition test parsing the workflow file and asserting the stage
      order and that no credential-configuring step appears in the first three stages
    - _Requirements: 11.3_
  - [ ] 13.2 Define the Docker-marked stage
    - Run the marked integration check in a job isolated by the marker the Offline_Suite excludes, with
      a container daemon and localhost networking available and no role attached
    - Failing test first: the job's selection expression is the marker the offline job excludes, and the
      job configures no role
    - _Requirements: 11.4_
  - [ ] 13.3 Define build-once and promote-by-digest
    - Build the image exactly once per run, record its digest as a stage output, and have every deploy
      stage in that run consume that recorded digest
    - Failing test first: exactly one build step exists and each deploy stage's image input references
      the recorded digest output rather than a tag
    - _Requirements: 11.5_
  - [ ] 13.4 Define the post-deploy check stage
    - After each deploy, run the drift check, the one-`ACTIVE`-certificate check, and the per-device
      authorization check with a read-only identity, and record each check's exit status as the stage
      result
    - Failing test first: the stage invokes all three checks and its result document carries one status
      per check
    - _Requirements: 11.6_
  - [ ] 13.5 Define the gate before the demo deploy
    - Start no demo deploy stage while the post-deploy checks have not all exited zero for the run's dev
      deploy or while no explicit manual approval is recorded; on any non-zero check, fail the run,
      start no demo deploy, and leave the digest deployed to the demo environment unchanged
    - Failing test first: a non-zero check and an absent approval each block the demo stage, and the
      blocked path performs no deploy action
    - _Requirements: 11.9, 11.14_
  - [ ] 13.6 Define fail-closed handling of an absent stage result
    - Treat a skipped, cancelled, timed-out, or unavailable result from any stage that must precede the
      Deploy_Identity as a failure and start no stage holding that identity
    - Failing test first: each of the four absent-result forms blocks every credentialed stage
    - _Requirements: 11.13_
  - [ ] 13.7 Define the federated deploy identity
    - Obtain the Deploy_Identity through a federated short-lived credential exchange in the deploy
      stages and use no long-lived access key anywhere in the definition
    - Failing test first: the definition contains no static credential input and every deploy stage
      references the federated exchange
    - _Requirements: 11.11_
  - [ ] 13.8 Define path-filtered triggering
    - Trigger only on changes to files under the simulator directory or the infrastructure directory
    - Failing test first: a change under a third service's directory matches no trigger path while a
      change under each of the two named directories does
    - _Requirements: 11.10_
  - [ ] 13.9 Wire the Offline_Suite into the gating stage
    - The Offline_Suite stage runs the task 1.3 runner under the gating property profile and gates on
      its aggregate exit status
    - Failing test first: the stage's command names the gating profile and the stage fails when the
      runner's aggregate status is non-zero
    - _Requirements: 11.7, 11.12_
    - _Properties: 17_

- [ ] 14. Checkpoint - Offline_Suite green with no Simulator source present
  - Ensure all tests pass with no AWS credentials and no network access beyond localhost, ask the user
    if questions arise.

- [ ] 15. Runtime entrypoints and adapters
  - [ ] 15.1 Implement the command dispatcher
    - Provide exactly the five commands and select a runtime's behavior by command override alone with
      no separate build and no bootstrap credential-exchange command or path; on an unrecognized
      command, write one log line naming it and the supported commands, start no runtime behavior, and
      exit non-zero; surface a configuration validation failure as a non-zero exit rather than as a
      running service
    - Failing test first: each of the five commands dispatches to its own runtime, an unrecognized
      command's log line and exit status are asserted, no exchange command exists, and an omitted or
      unrecognized configuration key exits non-zero
    - _Requirements: 1.3, 1.9, 7.10_
    - _Depends on: sensor-simulator-service (Config_Loader)_
  - [ ] 15.2 Implement the REST adapter
    - Wrap the Simulator's own FastAPI application as an invocation handler, adding no route and
      changing no ordering, so the application retains authority over authentication before parameter
      validation and over the one unauthenticated route
    - Failing test first: the adapter forwards method, path, headers, query string, and body unchanged
      and returns the application's status and body unchanged for an authenticated request, an
      unauthenticated request, and a bad-parameter request
    - _Requirements: 1.3, 7.4_
    - _Depends on: sensor-simulator-service (FastAPI application)_
  - [ ] 15.3 Implement API key resolution at initialization
    - Resolve the key from the managed secret during the initialization phase; where it is absent or
      empty, exit non-zero before serving any request with one message naming the affected
      configuration value and excluding the value; where resolution fails for any other reason, exit
      non-zero with one log line naming the failure kind and the secret reference and excluding the
      resolved value, serving no request on any unauthenticated path
    - Failing test first: the absent, empty, and retrieval-failure cases each fail initialization, no
      request is served in any of them, and no message contains the value
    - _Requirements: 7.4, 7.7, 7.12_
  - [ ] 15.4 Bind the provision-fleet entrypoint to the Simulator's identity factory
    - Replace the injected `SiteCode` source's fake with the Simulator's own Swarm identity factory so
      the derivation rule, the prefixed zero-padded form, and the supplied-site-list substitution have
      exactly one implementation, and assert the derived sequence is strictly ascending with a length
      equal to the configured Swarm size
    - Failing test first: the binding's derived set equals the factory's for a range of Seeds and
      Swarm sizes, and the provisioner contains no second derivation implementation
    - _Requirements: 3.2, 3.3, 3.4_
    - _Properties: 4_
    - _Depends on: sensor-simulator-service (Swarm identity factory)_
  - [ ] 15.5 Implement the plan-backfill entrypoint
    - Read the requested range from the invocation event, run the planner, and return the plan or the
      refusal; on refusal produce no plan and fail the execution with the offending values named; on
      exhausted retry record the failed descriptor's index, slice, ordered window sequence, and
      Generation_Tag in the execution result
    - Failing test first: a valid request returns a plan, each refusal case fails with its values, and
      the recorded result round-trips into a descriptor equal to the original
    - _Requirements: 8.7, 8.10_
  - [ ] 15.6 Implement the backfill-shard entrypoint
    - Validate the descriptor's Generation_Tag against the one derived from this runtime's own
      configuration and every `SiteCode` against that derived set, exiting non-zero naming the shard
      index and both tags or the offending code, reading no Credential_Parameter and publishing
      nothing; otherwise materialize the slice's credentials, process the windows in the given
      ascending order publishing each code's records in non-decreasing timestamp order, and on an
      unavailable connection or an exhausted budget report each unpublished record with its `SiteCode`,
      `Species`, and timestamp and exit non-zero
    - Failing test first: a mismatched tag and an out-of-set code each exit before any read or publish;
      the publish order is asserted per code; and the unpublished-record report is asserted field by
      field
    - _Requirements: 8.11, 8.13, 8.14_
    - _Depends on: sensor-simulator-service (MQTT_Publisher, Config_Loader)_
  - [ ] 15.7 Implement the run-realtime entrypoint
    - Materialize the whole derived set's credentials into the memory-backed mount before configuration
      validation, then start the resident swarm on the selected `mqtt` interface with the endpoint host
      and broker authority path taken from the environment
    - Failing test first: materialization completes before validation runs, a missing endpoint host or
      authority path exits non-zero, and the mount is empty again after the process exits
    - _Requirements: 5.4, 7.13_
    - _Depends on: sensor-simulator-service (Config_Loader, MQTT_Publisher, Swarm_Manager)_
  - [ ] 15.8 Extend the single image definition to carry every command
    - Declare the five commands as entry points of the one committed image definition already used
      locally, widening its build context to the repository root so the deployment modules ship in the
      same image, and add no second image definition
    - Failing test first: an offline test asserts the image definition declares all five commands, that
      the repository contains exactly one image definition, and that the local composition file and the
      image stack reference that same definition
    - _Requirements: 1.2, 1.3, 1.5_
    - _Depends on: sensor-simulator-service (container image definition and local composition file)_
  - [ ]* 15.9 Write the Docker-marked materialization parity test
    - Marked `docker` and excluded from the Offline_Suite: against a faked parameter source, the
      materializer produces files at exactly the paths the local development credential generation
      command writes, so the Simulator's credential loading cannot tell local from cloud
    - _Requirements: 5.10, 11.4_
    - _Depends on: sensor-simulator-service (development credential generation command)_

- [ ] 16. Stateless serving verification
  - [ ] 16.1 Implement and verify per-invocation memoization scope
    - Every memoized intermediate value is retained for the invocation that computed it and shared with
      no other invocation; no record store is held and no record is retained across invocations
    - Failing test first: two successive invocations in one reused environment share no memoized value,
      and the second invocation recomputes rather than reading the first's cache
    - _Requirements: 9.1_
    - _Depends on: sensor-simulator-service (REST_API request path)_
  - [ ]* 16.2 Write property test for byte-identical responses
    - *For any* two authenticated requests carrying identical query parameters that both supply a start
      and an end time, served while the Swarm_Identity_Config and the retention window are unchanged,
      the response bodies are identical byte for byte irrespective of which invocation serves each and
      of the order in which the two are served
    - _Requirements: 9.10_
    - _Depends on: sensor-simulator-service (REST_API request path)_
  - [ ] 16.3 Verify the retention window as a validity filter
    - Records whose timestamp falls outside the window are excluded from the response body while the
      response status stays 200
    - Failing test first: a request spanning the window boundary returns 200 with the outside-window
      records absent and the inside-window records present
    - _Requirements: 9.3_
    - _Depends on: sensor-simulator-service (REST_API request path)_
  - [ ] 16.4 Verify the stateless default window
    - A request with neither a start nor an end time serves the most recently completed
      Publish_Interval, computed from the current simulated timestamp and the configured
      Publish_Interval with no stored state
    - Failing test first: against an injected clock, the served interval is the expected one at three
      positions within an interval, and no state is read
    - _Requirements: 9.5_
    - _Depends on: sensor-simulator-service (REST_API request path)_
  - [ ] 16.5 Verify the unchanged request surface
    - No route and no query parameter beyond those the sibling spec specifies, and no request rejected
      on account of its requested range's width
    - Failing test first: the deployed route and parameter sets equal the specified sets exactly, and a
      request spanning the whole retention window is not rejected for its width
    - _Requirements: 9.6_
    - _Depends on: sensor-simulator-service (REST_API request path)_
  - [ ] 16.6 Verify the status-code boundary
    - Every invalid-input case returns its documented 4xx status with a JSON body naming the offending
      parameter and no bad-input case returns 5xx; a request that cannot complete inside the invocation
      timeout returns 5xx with a body carrying no record of either kind
    - Failing test first: each documented bad-input case asserts its 4xx status and body, and an induced
      timeout asserts a 5xx whose body carries no record
    - _Requirements: 9.8, 9.9_
    - _Depends on: sensor-simulator-service (REST_API request path)_
  - [ ]* 16.7 Write the metadata latency example test
    - At a derived Swarm size at or below the per-process maximum, an authenticated metadata request
      returns the complete response inside the fixed budget, measured at the managed HTTP endpoint on an
      invocation that is not a cold start
    - _Requirements: 9.11_
    - _Requires: AWS account (post-deploy verification)_

- [ ] 17. Final checkpoint - full suite green
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked `*` are test sub-tasks and can be skipped for a faster path to a deployment; every
  other sub-task carries its own failing-test-first step in its description, so skipping the starred
  ones does not leave the behavior untested.
- Tasks 1 through 14 complete with no Simulator source present. Tasks 15 and 16 each name the sibling
  component they need. Until that source exists, the bindings of tasks 15.4, 15.6, 15.7, 16.1, and
  16.3 through 16.6 stand as protocols with fakes, and task 11.8 holds the Config_Loader's accepted-key
  set as a committed fixture that task 15.1 replaces with the real set.
- Two tasks cannot run offline and are marked accordingly: 8.8, the post-deploy check runner, and 16.7,
  the metadata latency measurement. Their logic is unit-tested offline against fakes; only the live
  invocation needs an account.
- Areas of the design with no requirement behind them, and therefore no task: the object-store escape
  hatch for wide range queries (assumption A11 names it as deliberately not built); the alternative
  provisioning mechanisms considered and rejected in the design's options section; the city-scale
  distributed-map and claim-certificate provisioning paths, which assumption A2 records as a
  mechanism change beyond the ceiling that Requirement 2 criteria 11 and 12 make an explicit refusal;
  the interface endpoint alternative to a public subnet named alongside decision DP15; and the
  in-account pipeline alternative named in assumption A5.
- Requirement 12 criterion 8 is a negative obligation — no batching layer between publisher and broker
  — so it is covered by the absence assertion in task 11.6 rather than by an implementation task.
- Requirement 6 criterion 6 restates a sibling behavior; the deployment-side obligation covered in
  task 7.4 is that revocation touches exactly one device, with the give-up signal surfaced by the alarm
  of task 10.10.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4"] },
    { "id": 2, "tasks": ["2.1", "2.3", "2.4", "2.5", "2.7"] },
    { "id": 3, "tasks": ["2.2", "2.6", "3.1"] },
    { "id": 4, "tasks": ["3.2", "3.3", "3.4", "3.5", "3.6"] },
    { "id": 5, "tasks": ["3.7", "3.8", "5.1"] },
    { "id": 6, "tasks": ["5.2", "12.1"] },
    { "id": 7, "tasks": ["5.3", "12.2"] },
    { "id": 8, "tasks": ["5.4", "5.5", "12.3"] },
    { "id": 9, "tasks": ["5.6", "12.4"] },
    { "id": 10, "tasks": ["5.7", "12.5"] },
    { "id": 11, "tasks": ["5.8", "5.9", "12.6"] },
    { "id": 12, "tasks": ["5.10", "6.1"] },
    { "id": 13, "tasks": ["5.11", "6.2"] },
    { "id": 14, "tasks": ["5.12", "6.3"] },
    { "id": 15, "tasks": ["5.13", "5.14", "5.15", "6.4"] },
    { "id": 16, "tasks": ["6.5", "7.1"] },
    { "id": 17, "tasks": ["6.6", "7.2"] },
    { "id": 18, "tasks": ["7.3", "8.1"] },
    { "id": 19, "tasks": ["7.4", "8.2"] },
    { "id": 20, "tasks": ["7.5", "8.3"] },
    { "id": 21, "tasks": ["7.6", "8.4"] },
    { "id": 22, "tasks": ["7.7", "8.5"] },
    { "id": 23, "tasks": ["8.6", "10.1"] },
    { "id": 24, "tasks": ["8.7", "10.2"] },
    { "id": 25, "tasks": ["8.8", "10.3"] },
    { "id": 26, "tasks": ["10.4", "11.1"] },
    { "id": 27, "tasks": ["10.5", "11.2"] },
    { "id": 28, "tasks": ["10.6", "11.3"] },
    { "id": 29, "tasks": ["10.7", "11.4"] },
    { "id": 30, "tasks": ["10.8", "11.5"] },
    { "id": 31, "tasks": ["10.9", "10.10", "11.6"] },
    { "id": 32, "tasks": ["10.11", "11.7"] },
    { "id": 33, "tasks": ["10.12", "11.8"] },
    { "id": 34, "tasks": ["10.13", "11.9"] },
    { "id": 35, "tasks": ["11.10", "13.1"] },
    { "id": 36, "tasks": ["13.2"] },
    { "id": 37, "tasks": ["13.3"] },
    { "id": 38, "tasks": ["13.4"] },
    { "id": 39, "tasks": ["13.5"] },
    { "id": 40, "tasks": ["13.6"] },
    { "id": 41, "tasks": ["13.7"] },
    { "id": 42, "tasks": ["13.8"] },
    { "id": 43, "tasks": ["13.9"] },
    { "id": 44, "tasks": ["15.1"] },
    { "id": 45, "tasks": ["15.2", "15.3"] },
    { "id": 46, "tasks": ["15.4", "15.5"] },
    { "id": 47, "tasks": ["15.6", "15.7"] },
    { "id": 48, "tasks": ["15.8", "15.9"] },
    { "id": 49, "tasks": ["16.1", "16.3"] },
    { "id": 50, "tasks": ["16.2", "16.4", "16.5"] },
    { "id": 51, "tasks": ["16.6", "16.7"] }
  ]
}
```
