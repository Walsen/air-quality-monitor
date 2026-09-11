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

- [x] 5. Grounding
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

  - [x] 5.2 Write the unavailable-value and forecast-attribution tests
    - Req 7.5 and Req 7.4's attribution clause were ALREADY covered by `test_reporting.py` (a degraded
      forecast yields no next-day value and still names its provider). Two gaps remained and are now
      closed in `domain/attribution.py` with `tests/unit/test_attribution.py`
    - Req 7.3 had no path at all. `unavailable_text` says a value is unavailable, and the load-bearing
      test is that the sentence CONTAINS NO NUMERAL: a number inside the message that refuses to estimate
      IS the estimate the requirement forbids, and it would also be an ungrounded claim since nothing was
      retrieved. So the subject is DESCRIBED rather than quoted — `PM2.5` and `PM25` contain digits, and
      echoing the user's words would put a numeral in the sentence. Estimate hedging ("roughly",
      "probably around") is excluded for the same reason: that is estimating with a disclaimer attached
    - Req 7.4's FIRST clause had no check. Reading a forecast faithfully does not stop a generation
      describing it as a reading, so `presents_forecast_as_measurement` looks for forecast tense PAIRED
      with a measurement verb. The pairing is the design: a measurement verb alone is correct for a
      measurement, forecast tense alone is correct for a forecast, and only together do they
      misrepresent one as the other. A check firing on either alone would reject the correct wording for
      both — the `you have` defect's exact shape — so the near-misses are tested explicitly
    - `is`/`was` are excluded from the measurement verbs deliberately: "tomorrow's index is 5" is how a
      forecast is legitimately stated, and including the copula would make every forecast a violation
    - DEFECT found by the first run: the token scan yielded `tomorrow's`, which is not the token
      `tomorrow`, so the highest-value phrasing slipped through. Possessives are now stripped as an
      ADDITIONAL form rather than apostrophes being dropped from the pattern, which would mangle
      contractions. Kept as a regression test
    - Req 7.3's new sentence was added to task 6.3's required-texts sweep when it was written, rather
      than left for a later sweep to discover — which is the point of having generalised that guard
    - _Requirements: 7.3, 7.4, 7.5_

  - [x]* 5.3 Write property test for grounding totality
    - **Property 1: Grounding totality**
    - **Validates: Requirements 7.1, 7.2, 34.8**

  - [x]* 5.4 Write property test for refusing ungrounded generations
    - **Property 2: Ungrounded generations are never returned**
    - **Validates: Requirements 7.2, 22.2**

- [x] 6. Forbidden claims and medication closure
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
    - EXTENDED for Req 30.2's generated-output enforcement: `ATTRIBUTION_PATTERNS`, a third category
      (`causal_attribution`) in the Forbidden_Claim set. Req 8.2 already checks generated Guidance against
      that set before returning it and Req 8.7 makes it configuration, so this needed entries rather than a
      new mechanism
    - IT MATCHES A CLAIM, NOT A WORD, and that distinction is load-bearing. Req 30.2 forbids describing an
      association "as a cause, a trigger, a diagnosis, or a prediction"; Req 21.5 forbids naming an allergen
      as "the user's trigger". Both offend on the ATTRIBUTION — the possessive that turns a correlation in
      someone's diary into a statement about their body. A word ban would have deleted a REQUIRED action
      template reading "Both irritant and allergic triggers can matter on the same day, so it is worth
      watching how you respond rather than assuming one cause", which uses both nouns to say the anti-causal
      thing the requirement wants said
    - THE ACTION TEMPLATES HAD NEVER BEEN IN THIS SWEEP. That was the gap which would have let the new
      patterns reject a required text with no test failing — the fourth time a broad guardrail has nearly
      made an obliged text unpublishable. Every template is now swept, with a test asserting a template
      really does use these nouns so the survival test cannot go vacuous
    - TWO DEFECTS FOUND BY ADVERSARIAL REVIEW AND FIXED. (1) FALSE NEGATIVES: the first pattern set caught
      6 of 77 plausible forbidden claims. Every synonym (`aggravates`, `worsens`, `irritates`, `provokes`,
      `sets off`, `brings on`), passive voice ("your cough was caused by"), nominalisation ("the cause of
      your symptoms"), reversed word order ("your trigger is ragweed") and prediction without the literal
      "will" escaped. The causal verb is now open-ended by STEM and passive, nominal and hedged shapes are
      covered. (2) A LIVE FALSE POSITIVE: `you will (have|get|experience|feel)` was unbound, so it rejected
      "you will get less exposure" and even "you will have your reliever with you" — the exposure framing
      the system prompt asks for and the preparedness language Req 8.4 permits. Req 8.2 DISCARDS a matching
      response, so those were good answers destroyed SILENTLY. Every prediction pattern is now bound to a
      symptom object, which is Req 30.2's own wording ("future symptoms")
    - The permitted-framing boundary is tested rather than hoped for, in a named `_MUST_REMAIN_SAYABLE`
      corpus: exposure framing, preparedness language, generic trigger talk, the association phrasing Req
      30.2 allows, Req 15's forecast attribution and Req 11's clinician deference. A guardrail that rejected
      those would push the model toward vaguer prose, which is a worse answer rather than a safer one
    - The caught-shapes corpus is named `_KNOWN_FORBIDDEN_SHAPES` and documented as a COVERAGE FLOOR, never
      as completeness — a test named "every causal claim is rejected" would be exactly the vacuous guarantee
      this codebase keeps getting bitten by. A test asserts the module docstring still says so and still
      names Req 34.5, because if that honesty erodes a later author will treat Req 30.2 as closed and drop
      the guardrail that is actually the authority
    - TWO SWEEP COVERAGE GAPS CLOSED, both found by review: the emergency-guidance and disclaimer constants
      were HAND-COPIED string literals in the test rather than imports, so the sweep asserted over a copy
      and a change to the shipped text would have gone unchecked — the same defect class as the duplicate
      `AdviceRecord`. And Req 27.2's PROFILE restatement was absent from the sweep while the diary one was
      present
    - The default-set completeness assertion was rewritten as a UNION over the category mapping instead of a
      sum of named lengths. The arithmetic version had to be hand-edited when this set landed, which is the
      shape of an assertion that stops being checked because updating it looks like fixing it
    - _Requirements: 8.4, 8.5, 8.2, 8.7, 30.2, 21.5_

  - [x]* 6.4 Write property test for guardrail verdict totality
    - **Property 5: Guardrail verdict totality**
    - **Validates: Requirements 8.2, 34.4, 34.7**

  - [x]* 6.5 Write property test for medication naming closure
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

- [x] 8. Basis assembly and clinician deference
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

  - [x]* 8.3 Write property test that nothing is recomputed
    - **Property 20: Nothing is recomputed**
    - **Validates: Requirements 2.2, 2.3, 9.4, 12.1, 16.2**

- [x] 9. Strands tools and the system prompt
  - [x] 9.1 Implement the five retrieval tools over `ServingClient`
    - `air_quality`, `history`, `profile_get`, `profile_put`, `symptom_entry_put` as `@tool` functions
      whose docstrings are the model-facing descriptions; each records its result into the
      `RetrievedValues` accumulator with an ordered `ToolCall` entry; the credential is forwarded and
      never interpolated into a prompt; at most one air-quality retrieval per turn
    - _Requirements: 2.1, 2.6, 3.1, 4.1, 5.1, 6.3, 6.3a_

  - [x] 9.2 Implement history window derivation
    - The window is derived from the utterance's requested span and the injected Clock, never the wall
      clock; a span beyond Service 2's maximum is refused BEFORE the call, naming the permitted bound,
      with no silent re-request; a drift guard pins the bound against Service 2's own constant, which is
      configurable there. `domain/history.py` describes a retrieved series without summarising its
      VALUES: it never reads the corrected value, reported value or sub-index at all, so it cannot
      compute a trend, an average or an exceedance count. The summary is labelled as a summary and names
      the reading count, and the `history` tool returns that label alongside the body so the model is
      handed the correct framing rather than trusted to add it
    - _Requirements: 3.2, 3.3, 3.4_

  - [x] 9.3 Implement the snapshot-reading rules
    - `domain/snapshot.py` reads every `nearestSensors` entry into a view, taking the driving pollutant,
      sub-index, band and confidence from the entry rather than deriving them from the raw measurements.
      The reader is TOTAL: a site with an empty measurement set, and a malformed entry, both become a
      view describing no current reading rather than being dropped, so the returned length always equals
      the number of entries served. The `air_quality` tool states each quiet site and the
      default-profile fallback in its result, because Req 2.5's real failure mode is the model reading
      straight past an empty array. AST guards assert no arithmetic and no reordering
    - _Requirements: 2.2, 2.3, 2.4, 2.5_

  - [x] 9.4 Load the system prompt as configuration
    - Prompt content supplied as configuration rather than a literal embedded in a function, so its text
      is reviewable, diffable and testable as data; assert the prompt never contains a credential
      placeholder and never claims a capability the guardrails forbid
    - _Requirements: 31.4_

- [x] 10. Verification hooks and the turn pipeline
  - [x] 10.1 Register the verification hooks
    - `agent/verification.py`. NOTE, established against the installed SDK: `AfterInvocationEvent`
      carries `result` and `resume` but NO `cancel` field, so a Strands hook can OBSERVE a response and
      cannot veto it. Bypass-proofing therefore cannot work by the hook blocking. It works by the hook
      RECORDING a verdict at the after-invocation point (which the SDK fires "regardless of whether it
      completed successfully or encountered an error") while the ledger starts UNVERIFIED and `release`
      raises instead of returning text. A return path that skips verification has no verdict, so
      assembly refuses — the new path does not have to remember to verify, because it cannot obtain the
      text without a verdict. A failed verdict is also final for the turn, so a retry cannot verify
      different text and publish on that verdict, and `reset` clears it between turns
    - The checks themselves are injected rather than imported, so the hook carries no policy: it
      guarantees they run. A verifier that raises records a FAILING verdict
    - _Requirements: 31.5, 34.7_

  - [x] 10.2 Implement the `TurnPipeline` Template Method
    - `agent/pipeline.py`. `run` owns the order; the six steps are abstract, so a subclass fills the
      sequence in and cannot reorder it. Generic in the retrieved payload and the response type rather
      than typed `Any`, so the steps carry real types
    - The order is guaranteed TWICE. Behaviourally, the steps record themselves through an injected
      `StepRecorder` and the tests assert over the sequence, because a response with the right shape can
      be produced by steps that ran in the wrong order or never ran. Structurally, an AST test reads
      `run` itself, so it holds for every subclass rather than for the one pipeline a test instantiated
    - DESIGN CORRECTION found by a test: the first version passed the escalation INTO `generate` and left
      each subclass to honour it. That is a guarantee every future subclass must remember, so it is not a
      guarantee. `run` now short-circuits and never calls `generate` on an escalating turn, so the model
      cannot hedge, cannot re-assess whether the emergency is real (Req 10.5 forbids the service doing
      that) and cannot fail in a way that loses the emergency direction. An escalating turn's step
      sequence OMITS generation, and that absence is the evidence
    - Every turn is verified, escalating ones included. Exempting that path would leave exactly one route
      to a user that no verifier inspected, and it would be the highest-stakes route; the emergency text
      passes because task 6.3's sweep already proves the required texts are not rejected
    - `run` raises BEFORE assembly on a verdict that did not pass, so task 10.1's fail-closed rule is
      control flow rather than a check somebody has to remember
    - _Requirements: 10.2, 20.4, 34.7_

  - [x] 10.3 Implement structured output and stop-reason handling
    - `agent/stop_reasons.py` classifies every reason, with `ALL_STOP_REASONS` DERIVED from the SDK's own
      `StopReason` literal rather than written out, so an upgrade that adds a thirteenth fails the test.
      The installed SDK has twelve, three of which the spec never anticipated (`cancelled`, `checkpoint`,
      `interrupt`). `content_filtered` and `guardrail_intervened` are rejections, not failures (Req 6.5a)
    - FINDING: the SDK has NO stop reason meaning "the model failed" — a failure arrives as an EXCEPTION
      (`ModelThrottledException`, `EventLoopException`, `StructuredOutputException`), so `MODEL_FAILED` is
      reachable only via the fallback and the exception path. The reachability test excludes it and says why
    - `agent/generation.py` obtains the structured fields against `ModelGeneration`, a Pydantic model whose
      FIELD SET is the statement of what the model may author. It carries the guidance alone, because every
      other `AdvisoryResponse` field has a different authority: `escalation` (Req 10.2, before generation),
      `basis` (Req 9.4, never derived), `envelope` (Service 2 or A8a), `answered_at` (the Clock),
      `degraded` (this service's own judgement). The disjointness is asserted against `AdvisoryResponse`
      itself, so adding a field to either side cannot quietly widen what the model may invent
    - `extra="forbid"`, so a model that tries to author `escalation` gets an ERROR rather than a silent
      drop — a silent drop is indistinguishable from the model never having tried
    - `StructuredOutputException` and a Pydantic `ValidationError` both become a model failure with NO
      generation. If the output could not be coerced there is no validated text, and returning the raw
      output "just this once" is the path Req 6.3b closes. Reason strings name a kind and never quote the
      provider's message, which can contain the prompt or the partial output (Req 21.4)
    - RECONCILED Req 6.5 with Req 22.2a: truncation is a "failure" in one and a "bound" in the other. Both
      DISCARD the partial text, which is the part that protects the user, so there is no conflict in
      behaviour. `BOUND_REACHED` is recorded because the same prompt would truncate again — a retry spends
      budget to reproduce the failure, where Req 22.2's degraded response is useful. A throttle is
      `MODEL_FAILED` for the mirror reason: nothing about the prompt caused it, so it IS worth retrying. A
      test pins that truncation classifies identically whether it arrives as an exception or a stop reason
    - `KeyboardInterrupt` and `SystemExit` are deliberately not caught; swallowing them would make the
      process unkillable mid-turn
    - _Requirements: 6.3b, 6.5, 6.5a, 31.7_

  - [x] 10.4 Implement the invocation bounds
    - `agent/bounds.py`. The model ceiling is expressed as Strands' `limits` (Req 22.1a's named mechanism),
      with `turns` defaulting to 2 — one generation plus one repair, so a default of 1 would make the
      repair path dead code. An AST test asserts the module keeps NO model-invocation counter alongside:
      one reintroduced next to the limits would drift from them and would miss exactly the tool round
      trips the framework exists to catch
    - Serving calls are the deliberate exception and ARE a counter of ours (`ServingCallBudget`), because
      they happen inside a tool body where the framework's limits cannot observe them. Recorded in the
      module so the inconsistency does not read as an oversight. A refusal degrades rather than raises,
      and does not consume budget, so the count stays truthful for Req 22.5's metric
    - Req 22.4's window keeps the MOST RECENT turns in order. A `PriorTurn` turned out to be a PAIR
      (utterance plus the guidance given back), not a message with a role, so the ceiling bounds exchanges
      rather than messages — the better unit, since truncating between an utterance and its answer would
      hand the model half an exchange
    - SPEC AMENDED — new Reqs 22.1b and 22.1c. 22.1a said the framework "enforces" all three ceilings, but
      the SDK documents `output_tokens` and `total_tokens` as APPROXIMATE: checked at turn boundaries
      rather than within a model call, so one oversized response can overshoot. 22.1b records that only
      `turns` is exact and forbids describing a token ceiling as a hard guarantee to an operator, who would
      otherwise not set the alarm that catches an overshoot. 22.1c records that an unset ceiling must be
      OMITTED, not zeroed: `Limits` is `total=False` and validates present keys as positive, so zero raises
      instead of lifting the cap — a bound that looks configured and is not
    - _Requirements: 22.1, 22.1a, 22.1b, 22.1c, 22.2a, 22.3, 22.4_

  - [x]* 10.5 Write property test for escalation precedence
    - **Property 4: Escalation precedes advice**
    - **Validates: Requirement 10.2**

  - [x]* 10.6 Write property test for invocation bounds
    - **Property 11: Invocation bounds hold**
    - **Validates: Requirements 22.1, 22.1a, 22.3**

  - [x]* 10.7 Write property test for turn reproducibility
    - **Property 10: Turn reproducibility**
    - **Validates: Requirements 25.1, 25.2, 25.3**

- [x] 11. Degradation and failure handling
  - [x] 11.1 Implement the degradation paths
    - `domain/degradation.py`. Req 21.8's warning is now EMITTED, which was the flagged gap:
      `emergency_guidance_drifted` existed and was tested, but nothing ever called it, so A8a's narrow
      exception was bought with a guard that was never armed. A detector nobody invokes is worse than no
      detector, because the design cites it as the reason the exception is safe
    - `DriftWatcher` holds three load-bearing properties, each tested. ONCE PER RUN, not per turn — a
      per-turn warning on a busy process is a flood, and an operator who filters it out has exactly the
      protection of one with no detector. The FIRST SUCCESSFUL retrieval of the run decides, so a failed
      retrieval must not consume the comparison or the run records having compared something it never saw
      and the drift goes unreported for the whole process lifetime. And the FIELD NAME ONLY, never either
      text. A blank served guidance is an unusable body rather than a comparison, so it neither reports
      spurious drift nor closes the latch. `has_compared` is observable so the absence of a warning does
      not have to stand for both "no drift" and "never checked"
    - The warning is asserted on the FORMATTED log line, not on `getMessage()`. Context arrives as `extra`
      and the formatter is what reaches stdout, so the first version of that test passed while the field
      never appeared in a real log at all
    - `degraded_response` keeps the envelope and any escalation (Reqs 21.3, 21.9) — a degraded turn is
      exactly when the user most needs the emergency direction, so dropping it under failure inverts the
      priority. `escalation` still precedes `guidance` in the dumped body; Req 10.2's field order does not
      relax under failure. `basis` is always None on this path: a basis is provenance for values, and
      there are no values. Every `ServingFailureKind` is quantified over, so a new kind without a path is
      a failing test
    - Req 21.7's partial case advises on what IS available and names what is missing, rather than failing
      the whole turn. `available_guidance` is the caller's ALREADY-VERIFIED text — this function never
      generates prose, since passing unverified text here would route around the pipeline's fail-closed rule
    - DESIGN CORRECTION found by a test: `missing_data_note` first left the digit-free property to the
      CALLER, and `("PM2.5", ...)` echoed the digits straight through. `describe_subject` was promoted from
      private in `attribution.py` to shared, so Reqs 7.3 and 21.7 get the same guarantee from one place
      rather than from a duty somebody must remember
    - Both new required texts joined task 6.3's sweep as they were written
    - _Requirements: 21.1, 21.2, 21.3, 21.7, 21.8_

  - [x] 11.2 Implement the boundary error handling
    - `agent/boundary.py`. Req 32.5 sets the shape: every handled failure becomes an `AdvisoryResponse`,
      NOT a status code, because an unhandled error becomes an opaque `424 RuntimeClientError` from the
      container that replaces a documented degraded answer with a transport fault and loses the envelope
      and any escalation with it
    - So even a MALFORMED REQUEST carries the emergency guidance. A user in trouble who typed something
      unparseable still needs to be told to call for help, and Req 10.4 does not depend on the request
      being well-formed
    - Req 21.4 holds by construction: every message is a fixed sentence chosen by KIND, never built from an
      exception's own words, and `BoundaryFault` has deliberately nowhere to put a provider message. The
      tests plant a marker string inside each exception and assert it does not surface — quantified over the
      expected types, because a test using one fixed error body would pass while a different one leaked
    - Req 21.6 names the offending FIELD but never the offending INPUT: a Pydantic error carries the value
      too, and echoing it would return the user's own utterance in an error message, which Req 19.2 keeps
      out of logs and an error body is no better a place for
    - Req 5.4's message says the session needs re-authenticating and nothing else. Req 5.2 names this case
      explicitly — "including a message reporting an authentication failure" — because it is where an author
      reaches for the token to debug with. Only UNAUTHORIZED maps to re-authentication; sending a user to
      sign in again because Service 2 timed out points them at something that is not broken
    - `fault_for` returns None for the UNEXPECTED, deliberately: that is what routes a surprise to
      `handle_at_top_level`, whose log is the only record one occurred. A `fault_for` answering everything
      would make that handler dead code and the log with it. The top-level log records the exception TYPE,
      never its message
    - TEST-DRIVEN CORRECTION: the first structural test expected the broad `except` inside
      `handle_at_top_level` and failed, because that function RECEIVES an already-caught error. The catch
      belongs at the entrypoint, so this module now asserts the stronger local property — it catches nothing
      broadly anywhere — and Req 21.5's placement clause is recorded against task 17.1 rather than left to
      be rediscovered
    - _Requirements: 5.4, 21.4, 21.6_

  - [x]* 11.3 Write property test for degradation completeness
    - **Property 12: Degradation is complete and honest**
    - **Validates: Requirements 21.1, 21.2, 21.4, 7.3**

  - [x]* 11.4 Write property test that malformed input never yields a server error
    - **Property 13: Malformed input never yields a server error**
    - **Validates: Requirements 1.4, 1.5, 21.6, 32.5**

  - [x]* 11.5 Write property test for envelope invariance
    - **Property 7: Envelope invariance**
    - **Validates: Requirements 8.5, 21.3**

- [x] 12. Audit trail
  - [x] 12.1 Implement the advice audit writer
    - `agent/audit.py`. Req 20.5 shapes the module: `write` returns a boolean and NEVER raises, because a
      user asking about the air they are breathing must not lose their answer because an audit table was
      unavailable. The audit exists for the operator; the answer exists for the user
    - Exactly one record per turn, enforced in the WRITER rather than trusted of the caller — a repair
      attempt after a guardrail rejection is still ONE turn, so recording per generation would double-count
      exactly the turns most worth counting accurately. The latch is per writer and a writer is per turn; a
      process-wide latch would record the first turn of a run and nothing after
    - A FAILED write does not close the latch, so a retry within the same turn can still record. Closing it
      on failure would let one transient error lose that turn's audit permanently
    - Req 20.4: a guardrail-rejected turn gets a record noting the rejection. Recording only successful
      turns would make the audit a log of things that went fine
    - Req 20.6 honoured by ABSENCE: a test pins the public surface to exactly `write` and `has_written`, so
      a `notify` somebody adds later has to change that test to land — a reviewable act rather than a quiet
      one. Only `OSError` and `ValueError` are caught, so a programming error in record assembly is not
      hidden as though it were an unavailable table. The failure log carries the error TYPE and no user
      identity: a store's message can quote the row it was writing, and that row IS the audit record
    - DEFECT FIXED (found while starting this task): `AdviceRecord` had TWO definitions — a frozen
      dataclass in `ports/protocols.py` and a Pydantic `_StrictModel` in `domain/records.py` — whose field
      sets happened to match. Luck, not a guarantee: two authorities drift the moment somebody edits
      whichever file they have open, and the `append` port would then accept a record the domain never
      validated. The port now RE-EXPORTS the domain's, and a test asserts object IDENTITY, which makes
      drift impossible rather than merely detectable. The domain's was kept because `extra="forbid"` makes
      Req 20.3's "exactly these fields" hold at CONSTRUCTION — an attempt to record an utterance now raises
      instead of being quietly dropped, and a dropped field looks identical to one never passed
    - A failing test also caught me re-deriving the record identifiers. `BasisSummary.record_identifiers()`
      already existed, and its docstring says why it lives there: Req 20.2 names "the retrieved records the
      Basis_Summary named", so deriving them elsewhere would let the audit claim provenance the response
      never cited. The identifier is a COMPOSITE of site, species, instant and duration, because a bare
      site code would fold together genuinely distinct records and under-report provenance while looking
      complete
    - _Requirements: 20.1, 20.2, 20.4, 20.5, 20.6_

  - [x]* 12.2 Write property test for exactly one audit record per turn
    - **Property 14: Exactly one audit record per turn**
    - **Validates: Requirements 20.1, 20.4, 20.5**

  - [x]* 12.3 Write property test for audit minimisation
    - **Property 15: Audit minimisation**
    - **Validates: Requirement 20.3**

- [x] 13. Untrusted content and data minimisation
  - [x] 13.1 Implement the untrusted-content rules
    - `domain/disclosure.py`. Reqs 18.1 and 18.2 are deliberately NOT implemented as detection: you cannot
      reliably recognise an instruction hidden in prose, and a service that believed it could would be
      trusting a filter that fails silently. Compliance is instead made impossible to EXPRESS
    - Req 18.3 holds because the output checks are unconditional — the verification ledger starts unverified
      and `TurnPipeline.run` refuses to assemble without a positive verdict, so a successful injection can
      only produce a WITHHELD turn, never a Forbidden_Claim
    - Req 18.5's mechanism is JSON ENCODING, not a filter. The tools return `json.dumps` output and JSON
      escapes newlines, so a retrieved field carrying a fake `Human:` turn boundary arrives as the two
      characters backslash-n rather than a line break. Tested over four hostile shapes, plus a round-trip
      test proving the value survives — escaping that lost the data would be a different bug. A structural
      test also asserts no prompt-assembly function takes both an utterance and retrieved data, so there is
      no string into which a retrieved field could be spliced beside the user's words
    - Req 18.4 needed a real check, and over-breadth was the trap: the prompt talks about asthma,
      emergencies and particulates, so a naive overlap test would reject every legitimate answer. The signal
      is a VERBATIM SPAN of 8+ consecutive words. Protected text is matched LITERALLY, never compiled — the
      Forbidden_Claim patterns are regexes, and compiling one would report disclosure whenever the
      generation merely MATCHED the pattern, so an answer describing symptoms would be flagged
    - A finding names which protected item was quoted and the run length, never the run or the protected
      text: these strings reach logs, and a log echoing the system prompt discloses it a second time (same
      reasoning as Req 21.8's field-name-only warning)
    - DEFECT CAUGHT BEFORE SHIPPING: the system prompt states the particulate-lag and gaseous-same-day
      explanations in the SAME WORDS the service is obliged to emit, because the prompt asks for that
      wording. Without an exemption, an answer correctly explaining the three-day lag was reported as
      disclosing the system prompt and the turn would have been withheld — the `you have` defect exactly.
      `required_texts()` assembles every obliged text so a caller cannot forget the exemption, and the
      sweep covers ALL of them rather than the one that failed, since narrowness was the root cause the
      first time. A non-vacuity test proves the exemption does not swallow the hard-limits section
    - A second test-side defect: the short-phrase threshold test used `str.split()` while the checker
      tokenises on non-alphanumerics, so seven whitespace tokens yielded eight word tokens. Built from the
      tokeniser's own output now
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5_

  - [x] 13.2 Implement the personal-data minimisation sweep
    - `tests/unit/test_minimisation_sweep.py`. Redaction is keyed on the KEY NAME, so the leak the sweep
      exists to catch is a sensitive VALUE arriving under an innocuous key — a condition logged as `detail`
      is invisible to `SENSITIVE_KEY_MARKERS`. The sweep therefore inspects no key names: it plants a
      distinctive sentinel in every field a turn can carry, runs the lifecycle at DEBUG, and asserts no
      sentinel reaches the log by any route. Assertions are on the FORMATTED output, because context
      arrives as `extra` and the formatter is what reaches stdout
    - The lifecycle run deliberately covers the paths a happy-path test would skip — the drift watcher, the
      degraded response, the top-level handler and a FAILING audit store — because those are where an author
      reaches for "just log the context so we can debug it"
    - COMPLETENESS is data-driven: sentinel keys are compared against `AdvisoryRequest.model_fields`,
      `PriorTurn.model_fields` and the canned profile body's own keys, so a new field fails this file until
      it has a sentinel. That test immediately earned its place — it caught `sensitivity_level` where I had
      assumed `sensitivity`, and a `routines` field I had not covered at all
    - SELF-CHECK included: one test plants a sentinel deliberately and asserts the sweep sees it, because a
      sweep that could not see a leak would pass on a service that logged everything. Sentinels are
      distinctive rather than realistic (`SENTINEL-CONDITION-Q7X`, not `asthma`) so they cannot collide
      with the service's own prose and report a leak that is really a docstring
    - Req 19.3 asserted POSITIVELY: the pseudonymous identity still reaches the log, since a sweep that
      redacted everything would look maximally safe while making an incident undiagnosable
    - DEFECT FIXED: Req 19.4 has two clauses, and the broad `location` marker satisfied the first while
      making the second IMPOSSIBLE — "WHERE a location must be identified THE Service SHALL use the
      location name Service 2 returned", yet `location_name` was redacted, so there was no way to say which
      of a user's sites an entry concerned. New `PERMITTED_LOCATION_KEYS` (`location_name`, `site_name`,
      `site_code`), exact names only exactly as `PERMITTED_COUNT_KEYS` is — a substring exception would
      re-open the marker it exists to narrow, and `location_coordinates` would pass. Eight near-miss keys
      are pinned as still redacted, and a test asserts the three permitted sets are disjoint
    - `locationName` in camelCase stays REDACTED deliberately: this service writes its own log keys in
      snake_case, so the camelCase form only appears when a whole retrieved body is passed — which is
      exactly what must not be logged
    - _Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.6_

  - [x]* 13.3 Write property test for credential non-disclosure
    - **Property 8: Credential non-disclosure**
    - **Validates: Requirements 5.1, 5.2, 5.4**

  - [x]* 13.4 Write property test that personal data never reaches a log
    - **Property 9: Personal data never reaches a log**
    - **Validates: Requirements 19.2, 19.4, 24.5**

  - [x]* 13.5 Write property test for injection resistance
    - **Property 18: Injection does not move the guardrails**
    - **Validates: Requirements 18.1, 18.2, 18.3, 18.4**

- [x] 14. Conversational writes to Service 2
  - [x] 14.1 Implement health profile elicitation
    - `domain/elicitation.py`. Reqs 27.3 and 27.4 both end with "SHALL NOT echo the offered value", and
      that is STRUCTURAL: `decline_message` takes a KIND, not the offered text, so there is no parameter
      through which a dose or a date of birth could reach the reply. A function that received the text and
      promised not to use it would be a promise; one that cannot see it is a guarantee. A signature test
      asserts no text-carrying parameter exists, and no decline message contains a numeral either — a dose
      and a date of birth are both numeric, so that would be the echo arriving by another route
    - `MedicationEntry` carries exactly `name` and `role` with `extra="forbid"`, so an attempt to record a
      dose RAISES rather than being silently dropped — a dropped field looks identical to one never
      offered, and this service must be able to say truthfully that it did not record it
    - The name is also checked for an embedded strength. "salbutamol 100mcg" puts the dose IN the name,
      which a field check alone would wave through, and a name with a strength in it is not only the name
    - Req 27.2's confirmation is a GATE: a draft starts unconfirmed and `write_body` raises, the same
      fail-closed shape as the verification ledger. The restatement names every field the write would
      apply, because one that omitted a field would obtain consent for less than the write does. An empty
      draft is refused — asking someone to confirm nothing obtains a confirmation authorising nothing
    - Req 27.5 keeps values for the turn only, so the draft is FROZEN and `confirm()` returns a new value.
      A mutable draft is a place to accumulate values across turns, which is how "for the turn" quietly
      becomes "for the session". It also has nowhere to put a name, date of birth, contact detail or
      diagnosis narrative: declining at the boundary is necessary but not sufficient, since a field able to
      hold one is a place a later author could put it
    - Reqs 27.6 and 4.5: the rejection message names the field and the limit, and is swept for every word
      that would read as success. Reporting an unapplied change as applied leaves someone believing their
      profile is something it is not, which then shapes advice they think was personalised
    - The decline messages joined task 6.3's required-texts sweep, and they are the highest-risk additions
      so far: the dose decline TALKS ABOUT doses ("never a dose or how often you take it"), exactly the
      shape the dosing patterns look for. They pass because those patterns require a medication object
      nearby and a message about what is NOT recorded names none — a property of the current patterns, so
      it is pinned rather than assumed
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 27.1, 27.2, 27.3, 27.4, 27.5, 27.6_

  - [x] 14.2 Implement symptom diary capture
    - `domain/diary.py`, plus `write_body` on the existing `SymptomEntryDraft`. Req 28.6 shapes the module
      and is enforced by CONTROL FLOW: the red-flag check runs on the description first, and when it fires
      the outcome carries the escalation with no confirmation request and `may_write` false. The write is
      UNREACHABLE on that path rather than something a caller must remember to skip — the same
      short-circuit as `TurnPipeline.run`, because a guarantee every caller must remember is not one
    - The urgency is not diminished by the framing: someone filing "my lips look blue today" as history is
      describing the same emergency as someone asking about it
    - Req 28.7 is enforced by ABSENCE: no arithmetic, no comparison, no aggregation, by AST test. A
      pre-merge review DEFEATED the first version of these guards and they were widened. It found that
      checking only `<`/`<=`/`>`/`>=` let `"changed" if this != last else "same"` through — a real
      deterioration classifier — so EQUALITY now counts too; that inspecting only bare-name calls never
      examined `statistics.mean(xs)` or `np.diff(xs)` at all, so ATTRIBUTE calls are now collected; and
      that `order.index(this) != order.index(last)` turned a severity into an ordinal to evade both, so
      `index`, `diff` and the ordering dunders are in the forbidden call set. The self-check is now
      PARAMETRISED over all eight known evasions — a self-check planting only `a > b` proves the detector
      catches the one thing nobody would write. Membership and identity stay permitted deliberately: they
      ask whether a thing is present, not how it relates to an earlier value
    - DEFECT FOUND BY REVIEW AND FIXED: `plan_diary_turn` passed `prior_turns=()`, dropping them. Req 10.7
      requires the recognition set be applied "to the utterance AND to any supplied prior turns in the
      same request", and dropping them broke exactly the case `match_request_red_flags` exists for — a
      user who says "my lips look blue" and THEN asks to log the day has described the emergency in the
      first message. The description alone is benign, so the entry would have been recorded and the blue
      lips never mentioned: Req 28.6's displacement, reached from the one direction a description-only
      test cannot see. My own tests all drove the description. Both directions are now tested
    - Every produced text is also swept for clinical wording
    - Req 28.2's restatement names the severity, every marker AND the reliever flag, since one that omitted
      a field would obtain confirmation for a different entry than the write applies — and the reliever
      flag is the field most easily inferred wrongly from prose. A test asserts the restatement of a
      CORRECTED draft shows the correction, or the second confirmation would restate the reading the user
      had just rejected
    - Req 28.4: an absent note is OMITTED from the write body rather than sent as null. Sending an explicit
      null would be this service asserting there is no note, where declining to send the field says only
      that it has none. `NOTE_PURPOSE_TEXT` says both halves — for recall, and computes nothing — because
      someone who believes their words feed a calculation words them for the machine rather than for
      themselves, which makes the diary worse at the one thing it is for
    - Req 28.5's warning names the DATE; "an entry will be replaced" leaves the user unsure which day they
      are overwriting, and a diary's value is that yesterday's entry is still yesterday's. It is absent
      when there is nothing to replace, because warning every time trains the user to click through it
    - Req 28.8: the outcome has nowhere to put the description, and a test asserts a distinctive marker in
      the description does not appear in its `repr`
    - All three new texts joined task 6.3's sweep. The restatement is the interesting one: it says "you
      used your reliever", NAMING a medication role, and passes only because
      `administration_near_medication` distinguishes reporting a past action from instructing one
    - _Requirements: 28.1, 28.2, 28.3, 28.4, 28.5, 28.6, 28.7, 28.8_

  - [x] 14.3 Implement learned-association reporting
    - `domain/association.py`. Nothing here derives an association: Service 2's `LearnedThreshold` carries
      species, lag and observation count with it, and its own docstring says why — "so Requirement 30's
      reporting obligation can be met without a second lookup, and so a threshold can never be surfaced
      without the basis it rests on". This module reads that and relays it. Checked Service 2's real shape
      rather than assuming field names
    - Req 30.2's TRAP is the word "trigger". It is the most natural word in the asthma vocabulary — people
      say "my triggers" — and it is exactly the word forbidden here, because a trigger is a causal claim
      about someone's body derived from a correlation in their diary. `CAUSAL_WORDS` is swept over EVERY
      text the module produces, not only the one that felt risky, and a test pins the set against Req
      30.2's own list. A separate sweep covers PREDICTION, which arrives through tense rather than a noun
    - Req 30.4 must name two numbers WITHOUT subtracting them. "You need 14 more observations" is a
      computation, and Req 30.3 forbids computing anything about an association — so both numbers are
      stated and the reader does the arithmetic. An AST test asserts the module performs none, which makes
      that phrasing a structural consequence rather than a stylistic choice
    - A PARTIAL learned block yields no view. Reporting a partial association would be worse than reporting
      none: the user would see a pattern whose derivation this service could not state, and Req 30.1 wants
      all three parts precisely so a threshold is never surfaced without its basis
    - Req 30.7's sweep covers medication, inhaler, reliever, preventer, dose and "see your doctor". A
      correlation in a diary is the weakest evidence in the system and the last thing that should move a
      clinical behaviour
    - TEST CORRECTED: the precedence test first asserted the literal word "learned" and failed. It was
      wrong, not the text — "learned threshold" is SPEC vocabulary, not user vocabulary, and Req 30.6's
      stated purpose is that the user UNDERSTANDS their instruction was not overridden. "A pattern from
      your diary does not replace it" carries that where the jargon would not, so the assertion now checks
      the substance rather than the spec's own wording
    - All three texts joined task 6.3's sweep; the explanation carries four numerals and the word
      "association", so it is exactly the kind of text a tightened pattern set could start rejecting
    - Req 30.2's GENERATED-OUTPUT enforcement now has a deterministic FAST PATH in
      `forbidden.ATTRIBUTION_PATTERNS` — see the note under task 6 below. It is NOT complete enforcement and
      is not recorded as such: an adversarial review composed 77 sentences an LLM would plausibly produce
      and the first version caught 6. The patterns were widened to close the highest-traffic holes, but the
      ceiling is the METHOD's — Req 30.2 forbids a semantic act with unbounded surface forms, and any regex
      set can be paraphrased around. Req 34.5's managed guardrail is the AUTHORITY; the pattern set is a
      cheap pre-filter in front of it, the same architecture `KNOWN_MEDICATION_TOKENS` already documents
    - `CAUSAL_WORDS` remains what it always was: a self-consistency sweep over the fixed strings THIS module
      authors, where a bare noun is avoidable. Its docstring now says so, so it is not mistaken for the
      runtime rule
    - CORRECTION: an earlier version of this note said the generated-output fix belonged to "task 18's
      guardrail wiring". That was wrong — task 18 is the asynchronous association trigger. The fix belongs
      in the Forbidden_Claim set, because Req 8.2 already checks generated Guidance against that set before
      returning it and Req 8.7 makes it configuration: Req 30.2 needed entries, not a new mechanism
    - _Requirements: 30.1, 30.3, 30.4, 30.5, 30.6, 30.7; 30.2 own texts met, generated output has a
      fast path with Req 34.5's guardrail as the authority_

  - NOTE spanning 14.1 and 14.2, from the pre-merge review: the `ProfileDraft` / `SymptomEntryDraft`
    confirmation gates are correct in isolation, but the wired `profile_put` / `symptom_entry_put` tools
    forward model-authored JSON and do not route through `write_body()`. So Reqs 27.2 and 28.3 are enforced
    only for a caller that uses the draft objects; nothing yet REQUIRES it. Closing that is the composition
    root's job (task 21.1), which must make the draft the only path to a write. Recorded here so the
    end-to-end guarantee is not assumed to exist already

  - [x] 14.4 Implement idempotent write keys
    - `domain/idempotency.py`. ONE derivation (`write_idempotency_key`) behind two named keys, so the two
      cannot drift apart
    - DEFECT FOUND AND FIXED: the existing `advice_idempotency_key` keyed on `user_id | turn_at | route`
      and COULD NOT SURVIVE THE REDELIVERY IT EXISTED FOR. `turn_at` is a clock reading taken when the turn
      is handled, so a re-invoked entrypoint produces a different key and writes a SECOND audit row for one
      turn — which Req 20 would then present as two separate pieces of advice. It had a test asserting the
      key was stable across the instant's *representation*, which passed and proved nothing about a
      re-read clock
    - The fix is `TurnIdentity` = `user_id` + `session_id`, with NO instant field, because a field able to
      hold one is a place a later author would key on it. Req 33.11 supplies the stable component: the
      `runtimeSessionId` must be derived from a stable property of the triggering execution rather than
      generated fresh, and states its own reason — that this "is also what makes Requirement 33 criterion
      4's idempotency achievable rather than merely asserted". The record still carries `turn_at` as DATA;
      when a turn happened is an audit fact, it is simply not what identifies the turn
    - The BODY is excluded too. A profile write's body is authored by the MODEL and a re-invoked entrypoint
      re-runs the model, so a redelivery can carry a patch meaning the same thing while differing in field
      order or phrasing. A body-derived key changes with it and the duplicate applies. (My first
      implementation included the body; the second still included the instant. Both are recorded in the
      module docstring so neither is re-attempted)
    - The key is CLOSURE-CAPTURED in `build_retrieval_tools`, never a tool parameter, for the credential's
      reason plus one of its own: a tool's `inputSchema` is part of the prompt, so a key the model authored
      would be freshly invented on each delivery. A test asserts no tool exposes `idempotency_key`
    - `idempotency_key` is a REQUIRED parameter on `ServingClient.profile_put`, so a new adapter cannot
      omit it and silently lose the protection — mypy refused 78 call sites, which is the forcing function
      working. It is transport metadata and never profile content: putting it in `patch` would store it as
      one of the user's own fields, where the Advice_Record carries its key as a FIELD precisely because
      that row IS the audit artefact
    - The local adapter COLLAPSES a repeated key rather than merely accepting it, so an offline test can
      observe the deduplication. An adapter that took the key and ignored it would let every idempotency
      test pass while the real protection existed nowhere. The collapsed call is still recorded, because a
      test needs to see the second delivery ARRIVED and was suppressed
    - Two profile writes in ONE turn now collapse to the first. Deliberate: `profile_put` replaces the
      confirmed state and `ProfileDraft` holds all of it, so a turn needs exactly one write — and
      collapsing the second removes the read-modify-write accumulation hazard outright
    - A key is NOT sufficient. `write_body()` on both drafts takes no argument beyond `self`, asserted by
      signature, so it has nowhere to receive current state and a read-modify-write cannot appear without
      changing a signature
    - The diary write stays UNKEYED, per Req 32.4c, and Service 2's Req 31.7 was read to confirm the claim
      rather than taken on faith. But "already safe" has a DEPENDENCY: the date must come from the
      confirmed draft, or a retry crossing midnight writes a second entry on a second date and
      replace-per-date collapses nothing. That precondition is now a test
    - DEFECT FOUND BY REVIEW AND FIXED: the key joined its components on a raw `|`, so
      `("alice", "|bob…")` and `("alice|", "bob…")` both rendered `alice||bob…` and produced the SAME
      digest — two DIFFERENT turns colliding onto one key, which reads as an already-applied duplicate and
      silently discards a real write to someone's health profile. Neither component is constrained to
      exclude `|`: a `runtimeSessionId` may arrive from the platform, where validation checks only length
      and non-blankness, and `user_id` comes from a federated identity whose character set this service
      does not choose. Components are now LENGTH-PREFIXED, which removes the class rather than banning one
      character, and the test compares the whole set of adversarial identities PAIRWISE — including two
      that imitate the length prefix itself
    - _Requirements: 32.4c_

  - [x]* 14.5 Write property test for idempotent writes under redelivery
    - **Property 17: Writes are idempotent under redelivery**
    - **Validates: Requirement 32.4c**
    - Both directions, because a key that never collided would satisfy idempotence by being useless: a
      redelivery yields the same key AND body, and distinct sessions/users/write-kinds yield distinct keys
      (asserted as `keys_differ == (left != right)` rather than one-sided)
    - Redelivery is simulated the way the fault happens — the identity and the draft are REBUILT from the
      same content, as a re-invoked entrypoint rebuilds them. Reusing one instance would prove only that
      the methods hold no internal state
    - One property goes end to end through the port and asserts the adapter applied the write ONCE, so the
      keys agreeing cannot be mistaken for the deduplication happening
    - Passes at the nightly 1000 examples/property

- [x] 15. Configuration
  - [x] 15.1 Implement the fail-fast configuration loader
    - `config/loader.py`, mirroring Service 2's loader in SHAPE rather than importing it (the practices forbid
      cross-service imports until a shared contract package is specced). Advisor 1177 -> 1242 tests
    - Fail-fast means NEVER HALF-START, not stop at the first error (Req 23.2): every validator runs, `_Problems`
      accumulates, and `ConfigError` carries every message. An operator fixing one setting per deploy is exactly
      what accumulation avoids
    - The loader does NOT re-check bounds `InvocationBounds` already owns. Service 2's loader records that
      duplicating them produced TWO messages for one invalid value, which Req 23.2 forbids; a test pins that a
      single bad value yields exactly one message. The rule lives wherever the value lives
    - `env` is a PARAMETER, not a read of `os.environ`, and an AST test asserts the module reads neither `environ`
      nor `getenv`. Resolving is not applying: a second AST test asserts it never calls `configure_logging`, and a
      source sweep asserts it calls nothing on the Serving_Client or the model (Req 23.2 wants validation complete
      BEFORE any such call, and a loader with a side effect has half-started)
    - DEFECT FOUND BY MY OWN TEST, same class as the `location_name` one: the redactor's broad markers
      (`utterance`, `credential`, `token`, `guidance`, `prompt`) made Req 23.1's REQUIRED startup line arrive
      almost entirely REDACTED — the one line an operator reads to see what was resolved would have said nothing.
      Fixed with `PERMITTED_CONFIG_KEYS`, an exact-match set exactly like `PERMITTED_LOCATION_KEYS`, because a
      LIMIT on utterance length is not an utterance and a BOOLEAN saying a credential resolved is not a
      credential. `modelCredentialPath` is deliberately absent, so an author who logs the path gets it redacted.
      The disjointness guard now compares all four sets PAIRWISE rather than hand-listing pairs
    - DESIGN CORRECTED BY MY OWN TEST: the first registry listed cloud names for every port and rejected the
      unimplemented ones through a second "registered but not implemented" tier. Incoherent — a registry that
      advertises a name the loader then refuses tells the operator two contradictory things, and Req 23.6's point
      is that the registry IS the answer to "what may I select". Now only names that resolve are registered; task
      16 adds each cloud name WITH its adapter, which is the one-entry change the requirement describes
    - `_nearest_category` resolves by CLOSEST WHOLE KEY (difflib) before falling back to a token head. A
      token-head-only version sent `model_regoin` to the `adapters` category, because that category holds a port
      literally named `model` and sorts first — so an operator who mistyped `model_region` was shown adapter
      names. `guardrail_checker` versus the `guardrail_*` settings is the same collision
    - Req 22.1c is honoured in one place: `_optional_int` keeps an unset ceiling ABSENT, and a configured zero is
      REJECTED. An operator writing 0 means "no limit"; the SDK means "invalid"; load time is the only place that
      difference can be explained to them
    - Req 23.5's base URL is required, refuses a blank (how a broken deploy template presents) and refuses a
      non-http scheme. Req 34.9 refuses to start when enforcement is enabled with no identifier — the worst of the
      three states, since the operator believes output is checked and it is not. Disabled is the default and Req
      34.5 keeps the LOCAL check running either way, so disabled does not mean unchecked
    - A credential is required only by the ADAPTER that needs one, never unconditionally: the scripted model needs
      none and demanding one would make the offline suite unstartable. The loader never reads a credential file —
      a test records every path the injected predicate was asked about and asserts nothing was opened
    - The file reader raises IMMEDIATELY rather than accumulating (a file that cannot be parsed yields nothing to
      accumulate over) and never falls back to defaults, which would come up with settings nobody chose. It names
      the PATH but never the CONTENTS, since those may hold a credential
    - THREE VALUES DELIBERATELY NOT CONFIGURABLE, recorded in the module docstring so a later author does not add
      them: the history window maximum (Reqs 3.2/3.3 make it Service 2's, learned from its rejection),
      `advisoryScope` and `disclaimer` (A8 and Req 21.9 give them no local copy and no fallback), and Req 27.6's
      write limits (that clause REPORTS Service 2's limit rather than creating one here)
    - THREE DEFECTS FOUND BY PRE-MERGE REVIEW AND FIXED:
      1. `redacted()` emitted `servingBaseUrl` and `jwtDiscoveryUrl` VERBATIM, and `urlparse` accepts
         `https://user:pass@host` — so an operator who embedded basic-auth would have had it published the
         moment the startup line was wired. Now REFUSED at validation rather than stripped for the log: this
         service authenticates to Service 2 by forwarding the caller's credential (A4a), so basic-auth in the
         base URL is a configuration it has no use for, and accepting it silently leaves a secret where no
         test looks. A test asserts the refusal does not echo the credential it refused
      2. `PERMITTED_CONFIG_KEYS` applied at EVERY nesting depth, so a key named `maxUtteranceLength` inside a
         retrieved Service 2 body escaped the `utterance` marker — in any casing, everywhere the walker went.
         Eight of the eleven entries exist precisely BECAUSE they neutralise a marker, so the hole was the
         size of the set. The set's own docstring claimed it was for Req 23.1's single startup line; it now
         is, via `is_sensitive(key, top_level=...)`. The other three sets stay unscoped, because an identity,
         a token count and a site name are equally publishable at any depth and a config key's name is not
      3. FOUR scalars this loader owns outright had NO validation: `request_timeout_seconds`,
         `turn_budget_seconds`, `max_utterance_length` and `model_max_output_tokens` all resolved clean at
         zero and negative, because none is an `InvocationBounds` field so nothing downstream refused them.
         Req 23.2 requires validating EVERY resolved value, and each has a concrete failure — a zero
         utterance cap rejects every request, so the service would start healthy and answer nothing. The
         sharpest part was the asymmetry: `max_output_tokens` was refused at zero while its neighbour
         `model_max_output_tokens` was not, which reads as deliberate. `locale` blankness and the temperature
         range are now checked too
    - Two AST guards were hardened after the review showed them evadable: the logging guard inspected only
      bare-name calls, so `logging.configure_logging()` passed, and the environment guard inspected only
      attributes, so `from os import environ` passed. Both now have parametrised self-checks over every
      spelling — the third time a guard's self-check has planted only the case nobody would write
    - Req 23.1 is PARTIAL, not met. `redacted()` is provided and proven publishable by the redactor that will
      publish it, but NOTHING LOGS IT: the once-at-startup emission needs the composition root (task 21.1).
      The loader deliberately performs no side effect, since Req 23.2 wants validation complete before any
      such call and a loader that logged would have half-started before finishing its own validation
    - _Requirements: 23.2, 23.3, 23.4, 23.5, 23.6, 34.9; 23.1 PARTIAL (payload provided, emission deferred
      to task 21.1)_

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
    - CARRIED FROM 11.2: Req 21.5's PLACEMENT clause must be asserted HERE. `agent/boundary.py` holds the
      handler but not the catch — `handle_at_top_level` receives an already-caught error — so the broad
      `except` lives at this entrypoint, and the AST test that it appears nowhere else belongs with it.
      `boundary.py` already asserts it catches nothing broadly, which is the other half
    - _Requirements: 21.5, 32.1, 32.3, 32.4, 32.4a, 32.5, 32.6_

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
