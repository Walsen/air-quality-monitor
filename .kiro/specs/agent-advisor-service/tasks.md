# Implementation Plan: AI Advisor Agent

## Overview

Implementation proceeds inside-out, and the order is chosen so that the checks come before the thing they
check.

Scaffolding and the observability foundation first, then the boundaries that make everything
offline-testable: the Clock, the four port protocols, and a local fake for each — including the scripted
`Model` subclass that makes the whole advisory path runnable with no AWS credentials. With fakes in place
the domain is built in the order a turn executes it, which is also the order of increasing dependency:
the red-flag matcher first because escalation runs first and must work when everything else is down, then
grounding, then the forbidden-claim and medication-closure rules, then the action registry.

**The verification layer is built before the generation layer.** That is deliberate and is the one
ordering decision worth defending: if the pipeline were assembled first, every subsequent test would run
against an unguarded path, and the guardrails would arrive as an addition to working code rather than as
a precondition for it. Building them first means the first generation the pipeline ever produces is
already checked.

Only then the Strands tools, the system prompt, the hooks, and the `TurnPipeline` that composes them.
After that the conversational features that write through to Service 2 — profile elicitation, diary
capture, medication-aware preparedness, association reporting — each of which depends on the pipeline
existing but on nothing after it. Configuration, the real Bedrock and HTTP adapters with their shared port
contract suites, the AgentCore entrypoint, and the container packaging come last, because no domain work
should wait on them.

Every step is TDD: the failing test first, then the smallest clean change that passes it. Each of the 20
correctness properties from the design gets exactly one `hypothesis` property test running at least 100
examples, tagged `Feature: agent-advisor-service, Property {n}`. Nothing in `domain/` reads the wall
clock, imports `random`, or imports from `adapters/` or `agentcore/`. Language: Python 3.12 with Strands
Agents, Pydantic, pytest + hypothesis, all under `agent-advisor/`, importing nothing from another service
directory.

## Tasks

- [x] 1. Project scaffolding and observability foundation
  - [x] 1.1 Create the `agent-advisor/` project skeleton and pinned manifest
    - Create the `src/aqm_advisor/` package tree (`domain/`, `ports/`, `agent/`, `adapters/model/`,
      `adapters/serving/`, `adapters/guardrail/`, `adapters/audit/`, `agentcore/`, `config/`,
      `observability/`) with a package docstring in every `__init__.py` recording its layering rule, plus
      `py.typed`; the `tests/{unit,properties,contracts,integration}/` tree; `pyproject.toml` naming
      Python 3.12 and pinning `strands-agents==1.55.1` with the `otel` extra, `bedrock-agentcore`,
      `pydantic`, `httpx`, `boto3`, and the dev group; the committed lockfile; `just` recipes for test,
      lint, typecheck, local run and the fenced suite; a `hypothesis` `ci` profile at the 100-example
      floor and a `nightly` profile at 1000
    - _Requirements: 26.1, 26.2, 26.3, 26.4, 26.6, 26.9, A1, A2, A9_

  - [x] 1.2 Implement the JSON logger with central redaction
    - Single-line JSON to stdout, never `print`; redaction configured once in the formatter covering the
      credential, condition, sensitivity, personal threshold, coordinate, medication name and utterance
      keys; a handled-error helper carrying exception type and stack as a string so redaction still
      applies to it; assert the pseudonymous identity remains loggable
    - _Requirements: 19.2, 19.3, 19.4, 19.5, 24.1, 24.2, 24.3_

  - [x] 1.3 Implement the metrics registry and OpenTelemetry wiring
    - Counters for turns answered, turns degraded, guardrail rejections by category, escalations
      returned, Serving_Client failures by kind, Model_Port failures by kind, model invocations and token
      usage; no metric label may carry a condition, coordinate or utterance substring; OTel traces and
      metrics through the Strands `otel` extra with no collector configured by this service
    - _Requirements: 22.5, 24.4, 24.5, 32.10, 32.11_

- [x] 2. Boundaries: Clock, ports, and local fakes
  - [x] 2.1 Implement the Clock port and its implementations
    - `Clock` protocol, `SystemClock` at the process edge only, `FixedClock` for tests; instants
      normalised to UTC and a naive instant refused
    - _Requirements: 1.3, 25.2, 25.5_

  - [x] 2.2 Define the four port protocols
    - `ServingClient`, `GuardrailChecker`, `AdviceAuditStore`, `AssociationTrigger`, transcribed from the
      design's signature block; no Bedrock, AgentCore or httpx type in any signature, asserted by a test
      that renders each signature and scans for SDK imports
    - _Requirements: 2.1, 3.1, 4.1, 20.1, 33.3, 34.2_

  - [x] 2.3 Implement the scripted `Model` subclass
    - A Strands `Model` subclass implementing ALL FOUR of the ABC's abstract methods — `stream`,
      `structured_output`, `get_config`, `update_config` — because a subclass missing any of them
      cannot be instantiated at all; `stream` yields a scripted sequence of stream events and
      performs no network call; supports scripting a tool-use request, a text generation, a
      truncation, a `guardrail_intervened` stop reason and a raised failure, so every branch of
      the pipeline is drivable offline; `structured_output` is scripted too, since Req 6.3b routes
      the response's structured fields through it and it is part of the abstract surface
    - _Requirements: 6.2, 6.3b, 6.5a, 35.1_

  - [x] 2.4 Implement the local fakes for the other three ports
    - Scripted `ServingClient` returning canned Service 2 bodies built from that service's real response
      shape; local `GuardrailChecker` applying the pattern set with no network; in-memory
      `AdviceAuditStore` with a read accessor for tests; recording `AssociationTrigger`
    - _Requirements: 26.5, 34.5_

  - [x] 2.5 Write the architecture enforcement checks
    - AST checks over real source: no `domain/` import of `adapters/` or `agentcore/`; no wall-clock read
      or `random` import in `domain/`; no whole-config parameter in a domain function; plus self-checks
      proving each detector can actually fail, so no rule can pass vacuously
    - _Requirements: 25.2, 25.3, 32.2_

- [x] 3. Domain models
  - [x] 3.1 Implement the turn contract models
    - `AdvisoryRequest` with the length bounds and `credential` as a `SecretStr` excluded from
      serialisation; `AdvisoryResponse` with the exact field set from the design; `PriorTurn`;
      assert rendering the request exposes no credential in `repr`, `str` or any f-string form
    - _Requirements: 1.1, 1.2, 1.4, 1.5, 1.6, 5.1, 5.6_

  - [x] 3.2 Implement the basis, envelope and escalation models
    - `SpeciesBasis`, `RecordReference`, `NowcastBasis`, `BasisSummary`, `GuardrailEnvelope`,
      `Escalation`; every field populated by copying a retrieved value, with no code path that computes one
    - `BasisSummary.nowcast` carries Service 2's `basis.nowcast` (window length, hours available, weight
      factor), so Req 9.3's traceable derivation accounts for the weighting and not only the breakpoint
      table; a test must show a partial window (hours available < window length) survives into the summary
      unchanged rather than being normalised or dropped
    - A test must pin that `nowcast is None` means NOT nowcast-derived and is a complete answer, not a gap
      (Req 9.3a) — assert the response is not marked degraded and no disclosure is added
    - `BasisSummary.records` carries Service 2's `basis.records`, so Req 20.2's "the identifiers of the
      retrieved records the Basis_Summary named" has something to name
    - `RecordReference.identifier()` composes site code, species, instant AND duration; a test must show
      two Readings from the SAME sensor differing only in species, and two differing only in instant, get
      DIFFERENT identifiers — a bare site code would collapse both and silently under-report provenance
    - Add `domain/instants.py` with this service's OWN `iso_z` (whole-second UTC, `Z` suffix) and a
      round-trip test; do not import Service 2's, and let the architecture check prove no such import
    - _Requirements: 9.1, 9.2, 9.3, 9.3a, 9.4, 9.5, 20.2_

  - [x] 3.3 Implement `RetrievedValues`, `SymptomEntryDraft` and `AdviceRecord`
    - `RetrievedValues` with the ordered `tool_calls` trajectory; `SymptomEntryDraft` with `confirmed`
      defaulting to False; `AdviceRecord` with the pinned field set, an `idempotency_key`, and a test
      asserting there is no field able to hold an utterance, guidance text, condition, threshold or
      coordinate
    - _Requirements: 20.2, 20.3, 28.1, 32.4c_

- [x] 4. Red-flag recognition (built first — escalation must survive every other failure)
  - [x] 4.1 Implement the deterministic `RedFlagMatcher`
    - `RedFlagRule` and `match_red_flags` over a normalised utterance, case-insensitive, with the
      research's three defaults; the rule set is configuration; matching applies to the utterance and to
      any supplied prior turns; no model call and no network
    - Normalisation must map the several apostrophe characters onto ASCII: phones and word processors
      emit U+2019, so "can't" arrives as "can’t" most times a person types it, and a matcher keyed on
      the ASCII form misses the likeliest spelling of the most important phrase it has
    - `match_request_red_flags` scans the user's prior UTTERANCES and NOT the agent's prior guidance.
      Service 2's `emergencyGuidance` contains all three default red flags, so scanning guidance would
      make every turn after an escalation re-escalate on the agent's own words while looking like caution
    - _Requirements: 10.1, 10.5, 10.7_

  - [x] 4.2 Write the escalation-asymmetry tests (matcher level)
    - Assert escalation fires for each configured rule; assert the matcher never diagnoses a cause; pin
      the deliberate bias toward escalating with a test naming the asymmetry in its docstring
    - Req 10.3 and 10.4 are asserted STRUCTURALLY at this level: `match_red_flags` takes an utterance and
      a rule set and nothing else, so it cannot consult an air-quality reading or a client. A signature
      cannot be bypassed by a later refactor the way a behavioural expectation can
    - Keep a test feeding Service 2's own `emergencyGuidance` wording back through the matcher. It found
      a real false negative: the clinical form uses a compound subject ("your lips or face look blue")
      matching neither "lips look blue" nor "face looks blue"
    - _Requirements: 10.3, 10.4, 10.5_

  - [x] 4.4 Assert escalation survives a failed retrieval, end to end
    - RESOLVED by assumption A8a and Req 21.8/21.9. A8's objection was that a second copy of the guardrail
      texts would drift INVISIBLY, not duplication as such — so the exception is bought with a detector
      (`emergency_guidance_drifted`) rather than by overriding the reason
    - `domain/envelope.py` resolves served -> cached -> configured fallback and records the source;
      `domain/turn.py` assembles the escalating response. Both are pure and parameterised, so escalation
      cannot be made conditional on a client, a model or a clock
    - The exception covers `emergencyGuidance` ALONE: `resolve_envelope` takes ONE configured string, so no
      local `advisoryScope` or `disclaimer` can be introduced without changing that signature and the test
      that pins it. Those two are omitted rather than invented (Req 21.9)
    - `escalation` now precedes `guidance` in `AdvisoryResponse`. Req 10.2 places the emergency direction
      first and Property 4 asserts it appears before any exposure guidance; with a structured response that
      is field ORDER, asserted on the DUMPED body so a later `model_config` change cannot reorder it quietly
    - _Requirements: 10.2, 10.4, 21.3, 21.8, 21.9, A8a_

  - [x]* 4.3 Write property test for unconditional escalation
    - **Property 3: Red-flag escalation is unconditional** — DISCHARGED, by two different means, and the
      difference is recorded in the test module because it is the interesting part
    - Retrieval success or failure is QUANTIFIED over, as the envelope source: served, the last envelope
      retrieved in this process, or A8a's configured fallback. Those are the three states a retrieval
      outcome leaves behind. Red-flag phrases are drawn from the rule set itself, and the flag is placed in
      the current utterance or an earlier one, so Req 10.7 is covered too
    - Model success-or-failure and air-quality band are discharged BY CONSTRUCTION:
      `determine_escalation` takes an utterance, prior turns, a rule set and the emergency text, so neither
      a model nor a reading can reach it. Asserted as a signature AND as a module-level dependency check.
      Quantifying over a dimension the code cannot observe would be an assertion that cannot fail
    - `determine_escalation` was extracted for this: the design's step 1 records the Escalation "before
      anything can fail", and making that a function with no port in its signature is what turns Req 10.4's
      reasoning into something a later refactor cannot quietly undo
    - Also asserts the converse, that ordinary text does not escalate — without it a matcher returning
      every marker would satisfy the property perfectly while directing every user to emergency care
    - **Validates: Requirements 10.1, 10.3, 10.4**

- [ ] 5. Grounding
  - [x] 5.1 Implement numeral extraction and the permitted-set comparison
    - `numerals`, `permitted_values`, `ungrounded`; normalisation of trailing zeros and thousands
      separators; the permitted set is retrieved values union a configured set of structural constants;
      tests pin that an invented number is caught, that a retrieved number in a different but equivalent
      rendering is accepted, and that the ~3-day lag constant is permitted as prose
    - Species names AND concentration units must be masked before extraction: `PM2.5`, `NO2`, `ug/m3` all
      contain digits. The unit case was found by a failing test and would have been intermittent, since
      `3` is also the lag constant — grounded when it was configured, ungrounded when it was not
    - Digit forms only. A spelled-out number is a claim this check cannot see; the mitigation is the
      system prompt (task 9.4) requiring digits, and the limit is documented in the module
    - _Requirements: 7.1, 7.2, 34.8_

  - [ ] 5.2 Write the unavailable-value and forecast-attribution tests
    - A value the user asked about that was not retrieved is reported unavailable rather than estimated;
      a forecast is never presented as a measurement and is attributed to the provider Service 2 named; a
      degraded forecast yields no next-day value
    - _Requirements: 7.3, 7.4, 7.5_

  - [ ]* 5.3 Write property test for grounding totality
    - **Property 1: Grounding totality**
    - **Validates: Requirements 7.1, 7.2, 34.8**

  - [ ]* 5.4 Write property test for refusing ungrounded generations
    - **Property 2: Ungrounded generations are never returned**
    - **Validates: Requirements 7.2, 22.2**

- [ ] 6. Forbidden claims and medication closure
  - [x] 6.1 Implement the forbidden-claim pattern check
    - `forbidden_matches` over diagnosis assertions, dosing instructions and administration verbs adjacent
      to a medication name; a configured set REPLACES the defaults rather than extending them; a rejection
      logs the pattern category and never the rejected text
    - _Requirements: 8.1, 8.2, 8.3, 8.6, 8.7_

  - [x] 6.2 Implement the medication-closure check
    - `unlisted_medications` asserting any drug name in the text is in the retrieved `Medication_Entry`
      set; the preparedness construction is the only one in which a medication name may appear; an
      administration instruction adjacent to any medication name is rejected regardless of listing; with
      no retrieved set, only generic role language is permitted
    - _Requirements: 29.1, 29.2, 29.3, 29.5, 29.6, 29.7, 29.8_

  - [x] 6.3 Write the guardrail-text self-consistency guard
    - Assert the required emergency guidance and disclaimer texts are not themselves rejected by the
      pattern set — a real risk, since the emergency text must mention a reliever inhaler, so a bare
      medication-word pattern would make the required text unpublishable
    - _Requirements: 8.4, 8.5_

  - [ ]* 6.4 Write property test for guardrail verdict totality
    - **Property 5: Guardrail verdict totality**
    - **Validates: Requirements 8.2, 34.4, 34.7**

  - [ ]* 6.5 Write property test for medication naming closure
    - **Property 6: Medication naming closure**
    - **Validates: Requirements 29.1, 29.3, 29.6**

- [x] 7. Action mapping and guidance content rules
  - [x] 7.1 Implement the condition-keyed action registry
    - `CONDITION_ACTIONS` and `actions_for`, keyed by condition so a new condition is an entry not a
      branch; every template is exposure-reduction phrasing; an AST test asserts resolution compares no
      condition-name literal; the registry never chooses a weighting
    - _Requirements: 12.4, 12.5_

  - [x] 7.2 Implement condition-weighted interpretation from the retrieved block
    - Read condition, sensitivity, `weightedFocus` and `unavailableWeightedSpecies` and present species in
      the order Service 2 returned; state that unavailable weighted species matter but are not measured;
      never compute a weighting; handle an overlap condition by using the returned weighting
    - _Requirements: 12.1, 12.2, 12.3, 12.6_

  - [x] 7.3 Implement anticipatory warning and activity timing
    - State the forecast trend attributed to its provider; explain the ~3-day particulate lag and the
      same-day gaseous effect as a general pattern, never a prediction about the user; answer activity
      questions in terms of retrieved conditions and the trend; relate guidance to the retrieved
      `inhaledDose` where the profile records an activity; never instruct starting or stopping exercise
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 14.1, 14.2, 14.3, 14.4_

  - [x] 7.4 Implement confidence, dose and pollen reporting
    - State the driving reading's confidence and say so in the guidance when it is not the highest value;
      never present a low-cost reading as reference-grade; disclose a qualified quality flag; explain the
      dose as concentration times an activity-adjusted breathing rate over a duration without recomputing
      it; include the pollen outlook with the returned categories and state the pollen-pollution synergy
      when both are elevated; say the outlook is unavailable where it matters
    - Req 15.6: the confidence Service 2 returned is the SINGLE authority on measurement weakness. An
      incomplete nowcast window reaches the user through 15.2, because Service 2 already capped its
      confidence — so an AST check must prove no code path compares `hours_available` against
      `window_hours` to gate a disclosure, with a self-check proving the detector can fail
    - A behavioural test: a partial-window body must produce exactly ONE weakness disclosure, not two.
      Double-disclosure is the failure a second derivation would cause, and it reads as thoroughness
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.6, 16.1, 16.2, 16.3, 16.4, 17.1, 17.2, 17.3,
      17.4, 17.5_

- [ ] 8. Basis assembly and clinician deference
  - [x] 8.1 Implement `BasisSummary` assembly from a retrieved snapshot
    - Read every field including `nowcast` and `records`; name the threshold and its source including
      `learned`; name the breakpoint table
      and calibration strategy; available whether or not the user asked; emit no claim requiring a basis
      when none was retrieved
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.6_

  - [x] 8.2 Implement clinician deference
    - Defer to the user's action plan wherever guidance touches what to do about their condition; direct
      them to the plan for a decision it governs; never ask them to record its contents; suggest
      contacting a clinician on reported worsening without a red flag, without characterising the
      trajectory clinically
    - _Requirements: 11.1, 11.2, 11.3, 11.4_

  - [ ]* 8.3 Write property test that nothing is recomputed
    - **Property 20: Nothing is recomputed**
    - **Validates: Requirements 2.2, 2.3, 9.4, 12.1, 16.2**

- [ ] 9. Strands tools and the system prompt
  - [ ] 9.1 Implement the five retrieval tools over `ServingClient`
    - `air_quality`, `history`, `profile_get`, `profile_put`, `symptom_entry_put` as `@tool` functions
      whose docstrings are the model-facing descriptions; each records its result into the
      `RetrievedValues` accumulator with an ordered `ToolCall` entry; the credential is forwarded and
      never interpolated into a prompt; at most one air-quality retrieval per turn
    - _Requirements: 2.1, 2.6, 3.1, 4.1, 5.1, 6.3, 6.3a_

  - [ ] 9.2 Implement history window derivation
    - Derive the window from the utterance and the injected Clock; never request a span beyond Service 2's
      maximum; on rejection report the period unavailable and the permitted bound with no silent
      re-request; never present a computed summary as a measurement, and name the reading count
    - _Requirements: 3.2, 3.3, 3.4_

  - [ ] 9.3 Implement the snapshot-reading rules
    - Treat the retrieved body as authoritative and modify nothing; read the driving pollutant, sub-index,
      band and confidence from the returned entries rather than deriving them; state when the default
      profile was used; describe a site with an empty measurement set as having no current reading rather
      than omitting it
    - _Requirements: 2.2, 2.3, 2.4, 2.5_

  - [ ] 9.4 Load the system prompt as configuration
    - Prompt content supplied as configuration rather than a literal embedded in a function, so its text
      is reviewable, diffable and testable as data; assert the prompt never contains a credential
      placeholder and never claims a capability the guardrails forbid
    - _Requirements: 31.4_

- [ ] 10. Verification hooks and the turn pipeline
  - [ ] 10.1 Register the verification hooks
    - Grounding, forbidden-claim, medication-closure and guardrail checks registered through Strands hooks
      at the after-generation point rather than called from the ordinary path, so no return path can
      bypass them; a test asserts a deliberately added early return still gets checked
    - _Requirements: 31.5, 34.7_

  - [ ] 10.2 Implement the `TurnPipeline` Template Method
    - The six steps in the fixed order red-flag check, retrieve, generate, verify, assemble, audit;
      escalation determined before generation and placed first in the response; the order recorded by
      tests that observe the sequence of operations through injected ports rather than asserting on the
      result alone
    - _Requirements: 10.2, 20.4, 34.7_

  - [ ] 10.3 Implement structured output and stop-reason handling
    - Obtain the response's structured fields through Strands structured output against a Pydantic model;
      treat `StructuredOutputException` as a model failure; treat `content_filtered` and
      `guardrail_intervened` as guardrail rejections rather than failures; handle every stop reason the
      SDK can return, with a test that fails if one is unhandled
    - _Requirements: 6.3b, 6.5a, 31.7_

  - [ ] 10.4 Implement the invocation bounds
    - Express the per-turn ceilings through Strands' own `limits` for turns, output tokens and total
      tokens; treat a `limit_*` stop reason as reaching the bound with one warning naming it; bound
      `ServingClient` calls and the number of prior turns included in a prompt
    - _Requirements: 22.1, 22.1a, 22.2a, 22.3, 22.4_

  - [ ]* 10.5 Write property test for escalation precedence
    - **Property 4: Escalation precedes advice**
    - **Validates: Requirement 10.2**

  - [ ]* 10.6 Write property test for invocation bounds
    - **Property 11: Invocation bounds hold**
    - **Validates: Requirements 22.1, 22.1a, 22.3**

  - [ ]* 10.7 Write property test for turn reproducibility
    - **Property 10: Turn reproducibility**
    - **Validates: Requirements 25.1, 25.2, 25.3**

- [ ] 11. Degradation and failure handling
  - [ ] 11.1 Implement the degradation paths
    - Req 21.8's WARNING is not yet emitted anywhere: `emergency_guidance_drifted` exists and is tested, but
      the one-warning-per-run log naming the field (and neither text) belongs on this path. The detector
      without the log leaves A8a's guard unarmed, which is the whole basis of the exception
    - A `ServingClient` failure yields a degraded response stating conditions are unavailable with no
      condition value; a `Model_Port` failure yields a response built from retrieved data without prose;
      the envelope and any escalation are present in every degraded response; partial retrieval advises on
      what is available and states what is missing
    - _Requirements: 21.1, 21.2, 21.3, 21.7_

  - [ ] 11.2 Implement the boundary error handling
    - Catch the expected exception types at the boundaries with a broad catch only at the entrypoint,
      which logs; never return a raw exception, stack trace, model error body or Service 2 error body;
      report a malformed request as such naming the field, never as a server error; report a rejected
      credential as needing re-authentication without naming it or Service 2's detail
    - _Requirements: 5.4, 21.4, 21.5, 21.6_

  - [ ]* 11.3 Write property test for degradation completeness
    - **Property 12: Degradation is complete and honest**
    - **Validates: Requirements 21.1, 21.2, 21.4, 7.3**

  - [ ]* 11.4 Write property test that malformed input never yields a server error
    - **Property 13: Malformed input never yields a server error**
    - **Validates: Requirements 1.4, 1.5, 21.6, 32.5**

  - [ ]* 11.5 Write property test for envelope invariance
    - **Property 7: Envelope invariance**
    - **Validates: Requirements 8.5, 21.3**

- [ ] 12. Audit trail
  - [ ] 12.1 Implement the advice audit writer
    - Exactly one `AdviceRecord` per turn including escalating and guardrail-rejected turns, the latter
      noting the rejection; the write is never a condition of answering, and a failure is logged with the
      response still returned; no notification and no action on the user's behalf
    - _Requirements: 20.1, 20.2, 20.4, 20.5, 20.6_

  - [ ]* 12.2 Write property test for exactly one audit record per turn
    - **Property 14: Exactly one audit record per turn**
    - **Validates: Requirements 20.1, 20.4, 20.5**

  - [ ]* 12.3 Write property test for audit minimisation
    - **Property 15: Audit minimisation**
    - **Validates: Requirement 20.3**

- [ ] 13. Untrusted content and data minimisation
  - [ ] 13.1 Implement the untrusted-content rules
    - Treat the utterance, prior turns and every retrieved value as data; keep retrieved data structurally
      separate from the utterance so text inside a field cannot present itself as a turn boundary; never
      reveal system instructions, pattern sets or configuration; apply the output checks regardless of
      what the input requested
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5_

  - [ ] 13.2 Implement the personal-data minimisation sweep
    - A data-driven leak sweep running the full turn lifecycle at DEBUG with a distinctive sentinel in
      every request and profile field, plus a completeness test comparing sentinel keys against the model
      field sets so adding a field fails until it has a sentinel; a self-check proving the sweep can see a
      planted value; assert the pseudonymous identity is still logged
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.6_

  - [ ]* 13.3 Write property test for credential non-disclosure
    - **Property 8: Credential non-disclosure**
    - **Validates: Requirements 5.1, 5.2, 5.4**

  - [ ]* 13.4 Write property test that personal data never reaches a log
    - **Property 9: Personal data never reaches a log**
    - **Validates: Requirements 19.2, 19.4, 24.5**

  - [ ]* 13.5 Write property test for injection resistance
    - **Property 18: Injection does not move the guardrails**
    - **Validates: Requirements 18.1, 18.2, 18.3, 18.4**

- [ ] 14. Conversational writes to Service 2
  - [ ] 14.1 Implement health profile elicitation
    - Elicit condition, sensitivity, medication entries, routine entries and locations conversationally;
      restate the structured interpretation and obtain explicit confirmation before any write; decline an
      offered dose, frequency, route or schedule without echoing it; decline a diagnosis narrative, date
      of birth, name or contact detail without echoing it; store nothing locally; report a limit rejection
      without reporting the change as applied
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 27.1, 27.2, 27.3, 27.4, 27.5, 27.6_

  - [ ] 14.2 Implement symptom diary capture
    - Construct a `SymptomEntryDraft` from the user's description; restate the inferred severity and marker
      set and obtain confirmation, applying the user's correction over its own reading; never write without
      an explicit instruction in the same turn; include a note only when asked and say it computes nothing;
      warn before replacing an existing entry for a date; apply the red-flag check to a diary description
      and escalate first; never characterise an entry clinically; store nothing
    - _Requirements: 28.1, 28.2, 28.3, 28.4, 28.5, 28.6, 28.7, 28.8_

  - [ ] 14.3 Implement learned-association reporting
    - Explain that an escalation point came from the user's own diary, naming species, lag and observation
      count; describe it as an association or pattern and never a cause, trigger, diagnosis or prediction;
      say when there is not yet enough history rather than presenting a weak association; name a learned
      source in the basis; state that a declared threshold takes precedence over a learned one; confine
      the consequence to exposure reduction and the alerting point
    - _Requirements: 30.1, 30.2, 30.3, 30.4, 30.5, 30.6, 30.7_

  - [ ] 14.4 Implement idempotent write keys
    - Key every `ServingClient` write so a redelivered turn does not double-apply; record why a diary write
      is already safe by Service 2's replace-per-date rule while an audit and a profile write are not
    - _Requirements: 32.4c_

  - [ ]* 14.5 Write property test for idempotent writes under redelivery
    - **Property 17: Writes are idempotent under redelivery**
    - **Validates: Requirement 32.4c**

- [ ] 15. Configuration
  - [ ] 15.1 Implement the fail-fast configuration loader
    - Resolve from environment, then file, then defaults; validate every value before any model or store
      call; one message per invalid value and a non-zero exit with no half-start; reject an unrecognised
      key listing the recognised keys in the same category; never write a resolved credential, reporting a
      credential path as whether it resolved; require the Service 2 base URL; select each adapter by name
      from a registry and reject an unregistered name; log the resolved non-secret configuration once
    - _Requirements: 23.1, 23.2, 23.3, 23.4, 23.5, 23.6, 34.9_

- [ ] 16. Real adapters and shared port contract suites
  - [ ] 16.1 Write the shared behavioural test suite per port
    - One suite per port parameterised over every adapter of that port, so an adapter swap cannot change
      behaviour; cloud parameters skip when no endpoint is reachable and the skip must be provable, with
      a fenced assertion that no parameter skips when an endpoint IS present
    - _Requirements: 26.8_

  - [ ] 16.2 Implement the `BedrockModel` adapter configuration
    - Model identifier and region from configuration and never a literal; `temperature` 0 by default with
      any sampling parameter as configuration; the request timeout and maximum output length applied; the
      model credential resolved only from the environment or a runtime path and never logged
    - _Requirements: 6.1b, 6.4, 6.5, 6.6, 25.4_

  - [ ] 16.3 Implement the HTTP `ServingClient` adapter
    - `httpx` client over Service 2's routes forwarding the credential in the header; the credential folded
      into the client and not retained as an attribute; failures translated to typed errors naming the
      failure kind and never the credential; a token rejected for remaining lifetime reported as an
      authentication failure with no refresh attempt
    - _Requirements: 5.2, 5.4, 21.1, 32.8a_

  - [ ] 16.4 Implement the `ApplyGuardrail` adapter
    - `ApplyGuardrail` with `source` OUTPUT against a configured guardrail identifier and version; denied
      topics covering diagnosis, medication administration and dosing; an intervention treated as a
      rejection counted by category; unavailability failing closed for anything the local checks cannot
      clear
    - _Requirements: 34.2, 34.3, 34.4, 34.6, 34.9_

  - [ ] 16.5 Implement the audit store adapter and erasure
    - Append and `forget_user`; erasure deletes rather than de-identifies where the record carries clinical
      content, and reports the count
    - _Requirements: 20.1, 20.2_

- [ ] 17. AgentCore entrypoint and the deployment contract
  - [ ] 17.1 Implement the entrypoint and health endpoint
    - `@app.entrypoint` async throughout so no blocking operation can block `/ping`; `@app.ping` reporting
      `Healthy` or `HealthyBusy`; `time_of_last_update` omitted or set only on a real status change; the
      `AdvisoryRequest` and `AdvisoryResponse` as the `/invocations` bodies; every handled failure returned
      as a response rather than a container status
    - _Requirements: 32.1, 32.3, 32.4, 32.4a, 32.5, 32.6_

  - [ ] 17.2 Implement inbound identity and credential forwarding
    - Read the inbound `Authorization` header from the request-header allowlist and forward it unmodified;
      never parse, validate, cache or reissue it; document the `customJWTAuthorizer` configuration the
      deployment supplies
    - _Requirements: 5.5, 5.6, 32.7, 32.8_

  - [ ] 17.3 Implement asynchronous task tracking
    - `add_async_task` and `complete_async_task` bracketing any work continuing after a response, so the
      SDK manages the ping status; no fire-and-forget job API is assumed and no async variant of the
      request exists
    - _Requirements: 33.8, 33.9_

  - [ ] 17.4 Write the offline deployment-contract tests
    - Drive `POST /invocations` and `GET /ping` against the locally served application with no AWS; assert
      the health response shape; assert `/ping` stays responsive and reports `HealthyBusy` for the whole
      time a turn is in flight
    - Req 32.14: assert the inbound authorizer's configured audience equals the audience configured for
      Service 2. A4's direct forwarding is documented as sufficient only WHERE those match, so this is the
      test that keeps the assumption true rather than merely asserted. Drift here fails at Service 2, not
      here, which is the hardest place to attribute it
    - _Requirements: 26.5a, 32.4b, 32.14_

  - [ ]* 17.5 Write property test that ping stays live during a turn
    - **Property 16: Ping stays live while a turn is in flight**
    - **Validates: Requirements 32.4, 32.4a, 32.4b**

- [ ] 18. Asynchronous association trigger
  - [ ] 18.1 Implement the association trigger
    - Never computed, triggered synchronously or awaited during a turn; a learned threshold read only from
      a retrieved response; invoked asynchronously where this service hosts the trigger; idempotent per
      user and evaluation window; a correlation identifier carried through; a failure logged with turns
      continuing against stored thresholds; no user notification
    - _Requirements: 33.1, 33.2, 33.3, 33.4, 33.5, 33.6, 33.7_

  - [ ] 18.2 Document and implement the chosen trigger mechanism
    - Choose among a Step Functions `waitForTaskToken` state, the direct SDK integration, and a Lambda
      durable function's `waitForCallback`; derive the session identifier from a stable property of the
      triggering execution so a retry resumes the same session; set an explicit timeout on any
      callback-based wait
    - _Requirements: 33.10, 33.11, 33.12_

- [ ] 19. Quality assurance suites
  - [ ] 19.1 Write the negative suite
    - For a corpus of adversarial utterances — asking for a diagnosis, asking what dose to take, asking it
      to ignore its instructions, asking for a drug not in the list, describing a red flag obliquely — the
      guidance is either rejected or contains no forbidden claim; the corpus grows from observed
      rejections, and each entry records the utterance class that produced it
    - _Requirements: 35.2, 35.10_

  - [ ] 19.2 Write the golden turns and trajectory assertions
    - Golden turns pairing a scripted retrieval and utterance with an expected structured outcome —
      escalation, basis, degraded flag, guardrail verdict — asserting those fields and not the prose;
      trajectory assertions over which tools were called, in what order and how many times; a turn making
      a conditions claim must have called the air-quality retrieval
    - _Requirements: 35.3, 35.4, 35.5, 35.6_

  - [ ] 19.3 Write the evaluation harness and mark the live checks
    - Any check needing a live model or live guardrail marked so the offline suite excludes it and a
      separate command runs it; an LLM-as-judge evaluation may run but its verdict is advisory and never
      gates the build
    - _Requirements: 35.8, 35.9_

  - [ ]* 19.4 Write property test that claims require retrieval
    - **Property 19: Claims require retrieval**
    - **Validates: Requirements 2.1, 35.5**

- [ ] 20. Container packaging
  - [ ] 20.1 Write the Dockerfile and packaging assertions
    - `linux/arm64` base, dependencies installed from the committed lockfile with a frozen resolve, an
      unprivileged user, no baked secret, `0.0.0.0:8080` exposed; packaging assertions read the parsed
      structure rather than raw text so a comment cannot satisfy them
    - _Requirements: 26.2, 26.7, 32.1_

  - [ ] 20.2 Verify the offline guarantee
    - Assert the default suite passes with no AWS credentials and no network beyond localhost, exercising
      a local adapter for every port; a scrubbed-environment run with every AWS variable removed and the
      SDK config files pointed at nonexistent paths; no offline test names a non-reserved host
    - _Requirements: 26.5, 26.6_

- [ ] 21. Final wiring and checkpoint
  - [ ] 21.1 Wire the composition root
    - Build the pipeline, the tools, the hooks and the entrypoint from configuration, selecting every
      adapter by name, with the Clock and all ports injected at one place; a test asserting the adapter
      factory table agrees exactly with the loader's registry, so a name the configuration permits but
      nothing builds fails offline
    - _Requirements: 23.6, 25.1, 31.1, 31.2, 31.3, 31.6, 31.8, 32.2_

  - [ ] 21.2 Write the README from values read in code
    - Commands, every configuration default, the routes, the tool set, and the guardrail posture, with a
      drift guard pinning each stated default to the constant that owns it
    - _Requirements: 26.4_

  - [ ] 21.3 Final checkpoint — full suite green
    - Run the offline suite, the fenced suite and the nightly property profile; confirm every property test
      is present, unique per module and running at least 100 examples; confirm the negative suite and the
      trajectory assertions pass; ask the user if questions arise
    - _Requirements: 26.4, 26.9, 35.7_

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP. Skipping them means skipping the
  property tests that make grounding, escalation, guardrail totality, minimisation and determinism
  verifiable, so treat them as deferred rather than unnecessary.
- Each of the 20 design properties maps to exactly one property test sub-task, tagged
  `Feature: agent-advisor-service, Property {number}` and running at least 100 examples.
- **The verification layer is built before the pipeline that produces text to verify** (tasks 4 to 8
  precede task 10). This is the plan's one non-obvious ordering choice: building the pipeline first would
  mean every subsequent test ran against an unguarded path, and the guardrails would arrive as an addition
  to working code rather than as a precondition for it.
- **Red-flag recognition is task 4, before any retrieval or generation exists.** Requirement 10.4 demands
  escalation when the Serving_Client is unavailable, and the same reasoning applies to the model — so the
  matcher is built and tested when neither exists, which is the strongest possible demonstration that it
  depends on neither.
- Property 16 (ping liveness) is the one property covering a failure mode that does not reproduce locally
  by default. It is nonetheless an offline test, because `app.run()` serves both endpoints with no AWS.
- No task introduces `datetime.now()` into `domain/`, an import from `adapters/` or `agentcore/` into
  `domain/`, or a cloud type into a port signature; task 2.5 adds the checks that enforce it.
- Three areas are pinned by example tests in addition to their properties, because a self-consistently
  wrong implementation would satisfy the properties: the guardrail texts' self-consistency (6.3), the
  structural constants permitted by grounding (5.1), and the red-flag rule set (4.2).
- Service 2's Requirements 30, 31 and 32 must be implemented before tasks 14.2, 14.3 and 18 can be tested
  against a real Service 2; against the scripted `ServingClient` they can be built and tested immediately,
  which is why they are not blocked here.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1", "2.2"] },
    { "id": 2, "tasks": ["2.3", "2.4", "2.5", "3.1", "3.2", "3.3"] },
    { "id": 3, "tasks": ["4.1", "5.1", "6.1", "7.1"] },
    { "id": 4, "tasks": ["4.2", "4.3", "5.2", "5.3", "5.4", "6.2", "6.3", "6.4", "6.5"] },
    { "id": 5, "tasks": ["7.2", "7.3", "7.4", "8.1", "8.2", "8.3", "9.1"] },
    { "id": 6, "tasks": ["9.2", "9.3", "9.4", "10.1"] },
    { "id": 7, "tasks": ["10.2", "10.3", "10.4"] },
    { "id": 8, "tasks": ["10.5", "10.6", "10.7", "11.1", "11.2", "12.1"] },
    { "id": 9, "tasks": ["11.3", "11.4", "11.5", "12.2", "12.3", "13.1", "13.2"] },
    { "id": 10, "tasks": ["13.3", "13.4", "13.5", "14.1", "14.2", "14.3", "14.4"] },
    { "id": 11, "tasks": ["14.5", "15.1", "16.1"] },
    { "id": 12, "tasks": ["16.2", "16.3", "16.4", "16.5"] },
    { "id": 13, "tasks": ["17.1", "17.2", "17.3"] },
    { "id": 14, "tasks": ["17.4", "17.5", "18.1", "18.2"] },
    { "id": 15, "tasks": ["19.1", "19.2", "19.3", "19.4"] },
    { "id": 16, "tasks": ["20.1", "20.2", "21.1"] },
    { "id": 17, "tasks": ["21.2"] },
    { "id": 18, "tasks": ["21.3"] }
  ]
}
```
