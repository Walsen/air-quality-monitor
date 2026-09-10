# Requirements Document

## Introduction

The AI Advisor Agent (Service 3 of three in this monorepo) is the service the user actually talks to.
It consumes the Ingestion & Serving Service's authenticated API (Service 2) as a tool, and turns the
per-user JSON that API returns into plain-language, condition-appropriate **exposure-reduction
guidance** for a person with a respiratory illness.

Three constraints shape almost every requirement in this document.

1. **The agent is a translation layer, not a second brain.** Service 2 already computes the
   personalization: the condition weighting, the escalation threshold and which thresholds were
   crossed, the inhaled dose, the forecast and pollen outlook, the confidence flags, and the
   provenance of every number (`personalized`, `forecast`, `basis` in its `ServingResponse`). Service 3
   must **consume** those and must **not** recompute, re-derive, or second-guess any of them. An agent
   that recalculated a sub-index would produce a number that disagrees with the one the API served, and
   nothing would reconcile the two. The research finding is the same: the agent's value-add is
   **fusion and translation, not prediction** (`docs/research/FINDINGS.md`, cycle 8).

2. **Generated text is a new, unguarded output surface.** Service 2 enforces its guardrails on a
   structured body it composes itself, and can therefore check it exhaustively. Service 3 emits
   free-form language from a language model, which is the one place in this system where a
   non-diagnostic constraint can be violated by something nobody wrote. The guardrails are therefore
   **product requirements on the agent's own output**, re-checked after generation, not a disclaimer
   appended to it (`docs/research/FINDINGS.md`, cycle 6). The line between an unregulated
   general-wellness tool and a regulated medical device is drawn by **intended use and claims, not by
   the technology**, and FDA's 2026 clinical-decision-support guidance grants enforcement discretion
   only where the recommendation's basis is transparent and independently reviewable.

3. **The agent is the only component that sees what the user says.** Service 2 sees an authenticated
   identity and a query string. Service 3 sees free text, which means it is the only place that can
   recognise a red-flag symptom description and route the person to emergency care — and also the only
   place exposed to instructions hidden in user input or in third-party content. Both are requirements
   here because they are expressible nowhere else.

Source material: `docs/architecture/00-overview.md` (system context; the agent consumes Service 2 as a
tool, decisions D4, D5, D6), `docs/research/FINDINGS.md` (cycle 3 what the agent does with the data,
cycle 6 guardrails, cycle 7 condition-specific personalization, cycle 2 the pollutant→illness lag
structure, cycle 4 the inhaled-dose differentiator, cycle 8 fusion-not-prediction), the implemented
Service 2 contract at `/home/ec2-user/workplace/air-quality-monitor/data-processing/src/aqm_ingestion/serving/models.py`,
and `.kiro/steering/engineering-practices.md` (§0 stack, §1–§7 practice).

### Scope

In scope: the advisory turn contract; retrieval of air quality, history and profile through Service 2's
API behind injected ports; model invocation behind an injected port; grounding of every quantitative
claim in retrieved data; the non-diagnostic and basis-transparency guardrails enforced on generated
text; red-flag symptom escalation; clinician deference; condition-weighted interpretation and
action mapping; anticipatory warning from the forecast and the particulate lag structure;
activity-timing guidance; confidence disclosure; inhaled-dose explanation; the pollen–pollution
interaction for the allergic segment; untrusted-content handling; personal-data minimization; the
advice audit trail; degradation when Service 2 or the model is unavailable; invocation bounds;
configuration; observability; and the service's own scaffolding inside `agent-advisor/`.

Out of scope, and deferred to a separate deployment spec: the Bedrock model enablement and account
posture, agent runtime topology, infrastructure-as-code, the conversational front end (web, chat
surface, or voice), Cognito app-client configuration and the end-user login flow, and continuous
integration. Also out of scope: Service 1 and Service 2 themselves, both already specced and
implemented; this service treats Service 2 strictly as a remote API it is a client of.

Explicitly **not** in scope, and called out because each is a plausible thing to expect here and would
be wrong: computing an AQI sub-index, a NowCast, an inhaled dose, or a threshold crossing (Service 2
does all four); storing sensor readings; deciding a user's condition weighting; and taking any action
on the user's behalf. The last one is a research finding rather than a preference — **no autonomous
action**, advice only (`docs/research/FINDINGS.md`, cycle 6).

This document specifies **application logic**. Every boundary that would otherwise reach a cloud
service — the model, Service 2's API, the clock — is an injected port with a local fake, so the whole
suite runs with no AWS credentials and no network access beyond localhost, exactly as Services 1 and 2
do.

### Assumptions pending confirmation

Recorded so the requirements are complete and testable. Each is a decision the reader should confirm
or override.

- **A1 — Language/runtime:** Python 3.12, matching the monorepo stack (engineering practice §0), with
  Pydantic models carrying the turn contract.

- **A2 — Service directory:** `agent-advisor/`, with the package at
  `agent-advisor/src/aqm_advisor/`, matching the steering table in
  `.kiro/steering/engineering-practices.md` §0 and the sibling layout of the other two services.

- **A3 — Strands Agents for the agent, Amazon Bedrock AgentCore for the deployment.** Superseding this
  document's first draft, which framed the choice as application code *versus* a managed agent resource
  and picked the former to protect the offline suite. That was a false dichotomy: the Strands Agents SDK
  is code-first and its documentation states the loop **"runs in your process with no hosted control
  plane"**, while AgentCore is a separate, optional managed **deploy target** rather than where the loop
  runs. So both are available at once — the loop stays in testable Python, and deployment is managed.
  Verified 2026-09-10 against the Strands documentation and the AgentCore general-availability
  announcement of 2025-10-13.


- **A4 — The end user's own Cognito JWT reaches Service 2 by forwarding, and AWS documents this as
  sufficient for a single-tenant agent.** Research on 2026-09-10 verified three things about the
  mechanism. First, AgentCore Runtime accepts an end-user Cognito JWT **directly**
  at its own front door: a `customJWTAuthorizer` configured with the pool's `discoveryUrl`,
  `allowedClients` and `allowedAudience` validates the token before this service's code runs, and a
  missing token is refused with `401`. Second, AgentCore Identity's outbound model is a token **BROKER**,
  not a relay — it exchanges the inbound token for a Workload Access Token and then runs an OAuth 3LO
  flow against a registered provider, yielding *that provider's* token. Third, and decisively:
  **forwarding the inbound Cognito JWT onward to a downstream API is NOT a documented AgentCore Identity
  feature.** The nearest documented mechanism is the request-header allowlist
  (`RequestHeaderConfiguration` / `--request-header-allowlist Authorization`), which makes the raw
  `Authorization` header reachable in agent code; forwarding it from there is implementable but
  unendorsed.

  **Corrected 2026-09-10 (later the same day), and the correction is favourable.** AWS's own guidance on
  on-behalf-of token exchange states: "The OBO pattern is essential whenever an agent fronts multiple
  downstream services or tenants and the inbound token's audience differs from any single downstream API.
  **For a single-tenant agent where the inbound audience already matches the downstream service, direct
  token forwarding can be sufficient.**" (*Implement on-behalf-of token exchange for multi-tenant agents
  with Amazon Bedrock AgentCore Gateway*, AWS Machine Learning Blog, 13 July 2026.)

  This service is exactly that case: ONE Cognito pool, ONE downstream API. So forwarding is not a
  deviation to be justified — it is the documented sufficient choice, and the earlier framing of it as
  unendorsed was wrong. The condition attached to it is a real one and becomes a requirement: the audience
  AgentCore Runtime's `customJWTAuthorizer` accepts and the audience Service 2's authorizer accepts MUST
  be the same, or forwarding stops being sufficient. That is asserted in the deployment-contract tests.

  The consequence for Service 2 is that its Requirement 18 does NOT change. What would have changed it —
  adopting a brokered token of a different shape — is no longer motivated.

- **A4a — Service 3 needs no JWT verifier of its own.** A consequence of the above worth stating because
  it removes a component the first draft implied: AgentCore validates the inbound token before this
  service sees it, and Service 2 validates it again on receipt. A third verification inside the agent
  would add a place for the three to disagree without adding a check. The agent therefore treats the
  credential as opaque and never parses it.

- **A5 — the symptom diary is IN scope, and Service 2 owns both the storage and the inference.**
  Superseding this document's first draft, which deferred it: the user has asked for a daily diary whose
  history improves the advice. Service 2 gains a SymptomLogStore, a bounded structured Symptom_Entry, and
  a lag-aware Exposure_Association producing a Learned_Threshold (its Requirements 31 and 32). Service 3
  ELICITS an entry conversationally and writes it through, and READS the learned threshold like any other
  served value — it stores no diary and computes no association. The inference lives in Service 2 because
  it is deterministic numeric work over two time series that service already holds, and putting
  statistical inference behind a language model would make it unreproducible and untestable.

- **A5a — a medication list is stored, and naming it is permitted only for preparedness.** Service 2's
  Requirement 30 stores a name and a role and structurally cannot store a dose. Requirement 29 below
  permits the agent to name a stored medication when saying what to have to hand, and continues to
  forbid every form of recommending that it be taken. Confirm this reading: it is the narrowest widening
  that delivers the value, and the boundary it draws — preparedness and reference, never recommendation —
  is the boundary that keeps the product out of the regulated-device class.

- **A6 — The conversation is stateless across turns unless a transcript is supplied.** The agent takes
  prior turns as an explicit parameter rather than owning a session store, so a turn is reproducible
  from its inputs and no health-adjacent text accumulates in a store this spec would then have to
  govern. A durable session store is a deployment concern.

- **A7 — English only in this iteration**, with the response language treated as configuration rather
  than hardcoded, so a later locale is an entry and not a rewrite.

- **A8 — Guardrail texts come from Service 2's response**, not from a second copy here. Service 2
  already returns `advisoryScope`, `emergencyGuidance` and `disclaimer` on every body. Duplicating
  those strings in this service would let the two drift, and the drift would be invisible.

  **A8a — one narrow exception, for `emergencyGuidance` only, because A8's own reason is addressable.**
  A8's objection is not duplication as such; it is that the drift would be INVISIBLE. Requirement 10.4
  requires an Escalation even when the Serving_Client is unavailable, and Requirement 21.3 requires the
  envelope in a degraded response — so on the highest-stakes path the service has, A8 as written leaves no
  text to escalate with at all. Nothing is worse than drifted wording at the moment somebody is describing
  a severe attack.

  So this service MAY hold a configured `emergencyGuidance` fallback, and the drift is made VISIBLE rather
  than tolerated: on the first successful retrieval of each run the configured text is compared against
  what Service 2 returned and a mismatch is logged as a warning (Requirement 21.8). That satisfies A8's
  actual concern instead of overriding it.

  The exception is `emergencyGuidance` ALONE. `advisoryScope` and `disclaimer` remain strictly Service 2's,
  with no local copy and no fallback, because drift in compliance boilerplate is a nit while absence of an
  emergency direction is not — the asymmetry that justifies the exception does not extend to them.
- **A9 — Pinned versions:** `strands-agents==1.55.1` (released 2026-09-09, Apache-2.0, authored by AWS,
  classifiers list Python 3.12), pinned exactly as engineering practice §0 and the dev-environment
  steering require. The `otel` extra is taken for tracing. `strands-agents-tools` is **not** taken: this
  service's only tools are calls to Service 2, and a general-purpose tool library would add a
  file-system and shell surface a health advisor has no use for.

- **A10 — The asynchronous pattern applies to the LEARNING path, not the advisory turn.** The blog post
  supplied as direction (*Creating asynchronous AI agents with Amazon Bedrock*, 2025-03-13) predates both
  Strands and AgentCore and uses neither; its recommended broker pattern — EventBridge to Lambda to SQS,
  with routing decided by a Converse API tool-use call and agent descriptions held in AppConfig — solves
  the **supervisor bottleneck in a multi-agent fleet**, which a single advisory agent does not have. Its
  reasoning does apply, exactly once and usefully: Service 2's Requirement 32 criterion 12 already
  forbids computing the exposure–symptom association on the serving path, which makes the association a
  scheduled, event-driven job of precisely the shape the article describes. This spec therefore adopts
  asynchrony where the work is genuinely long-running and rejects it for the conversational turn, where
  it would add a queue between a person and their answer. Confirm this reading — it is a deliberate
  partial adoption of the supplied reference rather than an oversight.

- **A11 — The AgentCore Runtime contract is verified, and it is a container rather than a handler.**
  Superseding this document's first draft, which recorded the contract as unverified. Confirmed against
  the AWS developer guide on 2026-09-10: a deployed agent is an **`linux/arm64` Docker image in ECR** that
  serves **`POST /invocations`** and **`GET /ping`** on **`0.0.0.0:8080`**; the request body is JSON whose
  **field shape the application defines** (there is no AWS-mandated request schema); the response is
  either JSON or Server-Sent Events (`text/event-stream`); the maximum payload is **100 MB**; a session
  runs up to **8 hours** on the default microVM and up to **14 days** on Instances; and
  `runtimeSessionId` must be at least 33 characters. `/ping` returns `{"status": "Healthy"}` or
  `{"status": "HealthyBusy"}`. The `bedrock_agentcore` SDK's `@app.entrypoint` plus `app.run()` satisfies
  the contract without hand-written HTTP, and `app.run()` serves `/invocations` **locally with no AWS**,
  which is what lets Requirement 26 exercise the deployment contract in the offline suite.

- **A11a — Asynchrony and the long-running contract are verified.** Confirmed 2026-09-10 against the
  AgentCore developer guide's asynchronous and long-running page, and an AWS blog of 2026-08-19 on
  asynchronous patterns for calling AgentCore agents. There is **no separate fire-and-forget job API**:
  one invocation API serves both modes, and asynchrony is an agent-side behaviour — the agent responds
  immediately and keeps working, tracked by `add_async_task` / `complete_async_task`, which also manage the
  `/ping` status. Background work **does** survive the response to the client, which is the documented
  purpose. Event-driven triggering is supported through Step Functions `waitForTaskToken`, the Step
  Functions direct SDK integration, and Lambda durable functions. A session idles out after **15 minutes**
  in `Healthy` and stays alive while `HealthyBusy`.

  The load-bearing consequence is a hazard rather than a capability: **a blocking `/invocations` handler
  also blocks `/ping`**, and a session whose ping is blocked is treated as unhealthy — the documented
  outcome is termination and the reported field outcome is the entrypoint being re-invoked mid-turn. It
  does not reproduce locally. Requirements 32.4a to 32.4c exist for this.

- **A12 — AgentCore Memory is deliberately not used, and the reason is stronger than mere statelessness.**
  Its long-term strategies EXTRACT facts, preferences and summaries **asynchronously into managed
  storage** with an event expiry of up to 365 days. For this service that is precisely the wrong
  behaviour: it would place derived health facts in a store this specification does not govern and whose
  erasure it does not control, when Service 2's Requirement 31 already owns the one clinical store with
  its own retention and its own erasure. Statelessness (A6) is the mechanism; not creating a second,
  ungoverned home for health data is the reason.

- **A13 — AgentCore Gateway is evaluated and NOT adopted, and the reason is a chain rather than a
  preference.** Gateway turns an existing REST API into MCP tools from an OpenAPI specification or a
  Smithy model, or fronts an API Gateway target directly, adding inbound auth and observability. Service
  2's serving API is an API Gateway HTTP API with an OpenAPI shape, so it is a plausible target and the
  default answer under "prefer AgentCore where one fits" would be yes. Three findings, verified
  2026-09-10, say no here.

  First, **it would break the credential model A4 just validated.** AWS's OBO guidance states that direct
  token forwarding "is rarely true in multi-tenant systems and **never true when the agent fronts a tool
  gateway**", because the inbound token's audience becomes the Gateway's rather than the downstream API's.
  Adopting Gateway therefore forces RFC 8693 on-behalf-of token exchange — which is a correct pattern, but
  one this service does not otherwise need.

  Second, **the exchange needs an authorization server whose support for it is unconfirmed for Cognito.**
  AWS's own reference implementation uses Okta, and the same post cautions: "Amazon Cognito user pools can
  serve as the provider IdP that authenticates the inbound agent call. **Confirm the current grant-type
  support against the AgentCore Identity documentation if you plan to use Cognito for the consumer-side
  OBO role.**" This monorepo is standardised on Cognito. So adopting Gateway means either depending on an
  unconfirmed Cognito capability on the highest-stakes path, or introducing a second identity provider for
  a project that needs one IdP — a larger change than the tool layer it would replace.

  Third, **it moves the tool surface off the machine and out of the offline suite.** Requirement 32.2
  keeps every AgentCore dependency at the deployment boundary so no advisory component imports an
  AgentCore type and the offline suite is unaffected by the deploy target; Requirement 26 requires that
  suite to pass with no credentials and no network beyond localhost. Gateway-sourced tools are defined in
  AWS, so their shapes could not be enumerated or exercised offline without stubbing the very thing under
  test. The five tools as Python functions over an injected `ServingClient` port are what make the offline
  suite possible.

  Recorded because "we evaluated it" is only useful if the evaluation is written down. If Cognito's
  token-exchange support is later confirmed AND a second downstream service appears, the first two
  objections fall and this should be revisited; the third would remain and would need Requirement 32.2
  amended deliberately.

## Glossary

Terms are capitalized with underscores where a requirement depends on their exact meaning.

- **Advisory_Turn** — one exchange: the user's utterance plus the retrieved data plus the guidance
  emitted in response.
- **Advisory_Request** — the input to a turn: the user's utterance, their credential, and optionally
  prior turns.
- **Advisory_Response** — the output of a turn: the guidance text, the Basis_Summary, the
  Guardrail_Envelope carried through from Service 2, and any Escalation.
- **Guidance** — the generated natural-language text presented to the user.
- **Air_Quality_Snapshot** — the body Service 2 returns from `/v1/air-quality/me`, unmodified.
- **Basis_Summary** — the retrieved provenance the agent shows alongside its guidance: driving
  pollutant, the sub-index and band that drove it, the threshold that was crossed if any, the
  confidence, and the breakpoint table, calibration strategy, nowcast window and record identifiers
  Service 2 named in `basis`.
- **Guardrail_Envelope** — the `advisoryScope`, `emergencyGuidance` and `disclaimer` strings Service 2
  returns.
- **Escalation** — a determination that the turn must direct the user to emergency services or to their
  clinician, rather than merely advise.
- **Red_Flag** — a described symptom the research names as requiring emergency care: severe
  breathlessness, a reliever inhaler that is not working, or blue lips or face
  (`FINDINGS.md`, cycle 6).
- **Model_Port** — the injected boundary to the language model.
- **Serving_Client** — the injected boundary to Service 2's API.
- **Retrieved_Value** — a number or category that came from a Serving_Client response in this turn.
- **Advice_Record** — the audit entry describing what was advised, on what retrieved data, at what
  instant.
- **Forbidden_Claim** — an output pattern the agent must never emit: a diagnosis, a medication or
  dosing instruction, or a statement that the user is or is not experiencing a medical event.

## Requirements

### Requirement 1: Advisory Turn Contract

**User Story:** As a front-end developer, I want one documented shape for asking the agent a question
and one for its answer, so that a chat surface, a voice surface, or a test can drive the agent
identically.

#### Acceptance Criteria

1. THE Service SHALL accept an Advisory_Request carrying exactly these fields: the user's utterance as
   text, the user's credential, an optional ordered sequence of prior Advisory_Turns, and an optional
   requested locale.
2. THE Service SHALL return an Advisory_Response carrying exactly these fields: the Guidance text, the
   Basis_Summary, the Guardrail_Envelope, the Escalation if one was determined, a degraded indicator,
   and the instant the turn was answered.
3. THE Service SHALL obtain the turn instant from the injected Clock and SHALL NOT read the wall clock
   in any component that composes a response.
4. THE Service SHALL reject an Advisory_Request whose utterance is empty or consists only of
   whitespace, naming the field, without invoking the Model_Port.
5. THE Service SHALL reject an Advisory_Request whose utterance exceeds a configured maximum length,
   with default 4000 characters, naming the field and the limit, without invoking the Model_Port.
6. THE Service SHALL NOT include the user's credential in the Advisory_Response.

### Requirement 2: Air-Quality Retrieval

**User Story:** As a user, I want the agent's advice to be about the air where I actually am right now,
so that it reflects my own locations and my own condition rather than a city average.

#### Acceptance Criteria

1. THE Service SHALL retrieve the Air_Quality_Snapshot through the Serving_Client by calling Service 2's
   `/v1/air-quality/me`, forwarding the user's credential, before generating Guidance for any turn that
   makes a claim about current conditions.
2. THE Service SHALL treat the retrieved body as authoritative and SHALL NOT modify, round, or
   recompute any value in it.
3. THE Service SHALL read the driving pollutant, sub-index, band and confidence from the
   `nearestSensors` entries and the escalation state from `personalized`, and SHALL NOT derive any of
   them from the raw measurements.
4. WHERE the snapshot reports `usedDefaultProfile` as true, THE Service SHALL state in the Guidance
   that no personal profile was found and that the guidance uses defaults.
5. WHERE a `nearestSensors` entry reports an empty measurement set, THE Service SHALL describe that
   site as having no current reading rather than omitting it, because Service 2 includes a stale site
   deliberately and omitting it would hide that the nearest sensor has gone quiet.
6. THE Service SHALL make at most one air-quality retrieval per Advisory_Turn.

### Requirement 3: History Retrieval

**User Story:** As a user, I want to ask how the air has been over the last few days, so that I can
connect how I have been feeling to what I have been breathing.

#### Acceptance Criteria

1. WHEN the turn requires readings over a past window, THE Service SHALL retrieve them through the
   Serving_Client by calling Service 2's `/v1/air-quality/history` with an explicit start and end.
2. THE Service SHALL derive the requested window from the utterance and the injected Clock, and SHALL
   NOT request a span exceeding the maximum Service 2 permits.
3. IF Service 2 rejects the window, THEN THE Service SHALL report that the requested period is not
   available and the permitted bound, and SHALL NOT retry with a silently different window.
4. THE Service SHALL NOT compute a trend, an average, or an exceedance count that it presents as a
   measurement; WHERE it summarizes a retrieved series it SHALL describe the summary as such and SHALL
   name the number of readings it covers.

### Requirement 4: Profile Retrieval and Update

**User Story:** As a user, I want the agent to know my condition and my thresholds, and to be able to
record a threshold I tell it, so that I do not have to repeat myself every conversation.

#### Acceptance Criteria

1. THE Service SHALL retrieve the user's profile through the Serving_Client by calling Service 2's
   `/v1/profile/me`, forwarding the user's credential.
2. WHEN the user asks to change a profile field the Service 2 allowlist permits, THE Service SHALL
   write it through the Serving_Client and SHALL confirm in the Guidance exactly which field changed.
3. THE Service SHALL NOT attempt to write a field outside the allowlist Service 2 accepts, and IF the
   user supplies clinical detail beyond it — a medication, a diagnosis narrative, a symptom history —
   THEN THE Service SHALL decline to store it, SHALL say that it does not keep that information, and
   SHALL NOT include the supplied detail in the declining message.
4. THE Service SHALL require an explicit user instruction before any profile write, and SHALL NOT infer
   a profile change from conversational context alone.
5. IF a profile write is rejected by Service 2, THEN THE Service SHALL report the rejection naming the
   field, and SHALL NOT report the change as applied.

### Requirement 5: Credential and Identity Handling

**User Story:** As a security reviewer, I want the user's token to travel to Service 2 and nowhere else,
so that a conversation transcript or a log can never become a way in.

#### Acceptance Criteria

1. THE Service SHALL forward the user's credential only to the Serving_Client, and SHALL NOT include it
   in any prompt sent to the Model_Port.
2. THE Service SHALL NOT write the credential to any log entry, any Advice_Record, any
   Advisory_Response, or any error message, including a message reporting an authentication failure.
3. THE Service SHALL log at most the pseudonymous user identity, and SHALL NOT log any claim from the
   credential beyond it.
4. IF Service 2 rejects the credential, THEN THE Service SHALL report that the session needs to be
   re-authenticated, naming neither the credential nor the rejection detail Service 2 returned.
5. THE Service SHALL hold no credential of its own for reading user data, per assumption A4.
6. THE Service SHALL treat the credential as OPAQUE: it SHALL NOT decode, parse, validate, cache or
   re-issue it, per assumption A4a. AgentCore validates the token inbound and Service 2 validates it on
   receipt, so a third verification here would add a place for the three to disagree without adding a
   check — and a component that parses a token is a component that can log a claim.

### Requirement 6: Model Invocation

**User Story:** As a Service 3 developer, I want the language model behind an injected port, so that the
whole advisory path is testable without a Bedrock account and so the model is swappable.

#### Acceptance Criteria

1. THE Service SHALL invoke the language model exclusively through the Model_Port, and no component
   other than the Model_Port's adapters SHALL name a model identifier or a cloud SDK type.
1a. THE Service SHALL realise the Model_Port as the Strands `Model` abstract class, so the port is the
   framework's own seam rather than a wrapper around it. A second abstraction over `Model` would have to
   be kept in step with it for no gain, and Strands accepts any `Model` subclass wherever a model is
   expected.
1b. THE Service SHALL configure the production adapter as a Strands `BedrockModel`, taking the model
   identifier and region from configuration and never from a literal in code.
2. THE Service SHALL ship a deterministic local Model_Port adapter for the offline suite, implemented as
   a `Model` subclass whose async `stream()` yields a scripted sequence of Strands stream events. Such an
   adapter performs no network call and returns exactly what a test scripted, which is what makes the
   whole advisory path — tool selection, generation, guardrail re-check and degradation — exercisable
   with no AWS credentials.
3. THE Service SHALL pass the retrieved data to the model as structured content distinct from the
   user's utterance, so the two are separable at the boundary.
3a. THE Service SHALL define each retrieval as a Strands tool, so the model requests a retrieval rather
   than the prompt carrying pre-fetched data the model did not ask for, and SHALL keep the tool set
   confined to the Service 2 operations of Requirements 2, 3, 4, 27 and 28.
3b. THE Service SHALL obtain the Advisory_Response's structured fields through Strands structured output
   against a Pydantic model, and SHALL treat a `StructuredOutputException` as a model failure under
   Requirement 21 rather than emitting an unvalidated response.
4. THE Service SHALL apply a configured request timeout, with default 30 seconds, and SHALL treat its
   expiry as a model failure under Requirement 21.
5. THE Service SHALL request a configured maximum output length and SHALL treat a truncated generation
   as a failure rather than emitting partial Guidance.
5a. THE Service SHALL treat the Strands stop reasons `content_filtered` and `guardrail_intervened` as
   guardrail rejections under Requirement 8 rather than as generation failures, because the model
   declining to produce text is the guardrail working and not the service breaking.
6. THE Service SHALL resolve the model credential only from the environment or a runtime-supplied path,
   and SHALL never log it or include it in a response.

### Requirement 7: Grounding of Quantitative Claims

**User Story:** As a clinician reviewing this tool, I want every number the agent states to be a number
the API actually returned, so that its advice cannot rest on something the model invented.

#### Acceptance Criteria

1. THE Service SHALL ensure every pollutant concentration, sub-index, AQI band, threshold, dose and
   pollen category appearing in the Guidance is a Retrieved_Value from this turn.
2. THE Service SHALL verify the emitted Guidance against the Retrieved_Values before returning it, and
   IF the Guidance contains a quantitative claim that matches no Retrieved_Value, THEN THE Service SHALL
   NOT return that Guidance.
3. WHERE a value the user asked about was not retrieved, THE Service SHALL say it is unavailable rather
   than estimating it.
4. THE Service SHALL NOT present a forecast value as a measurement, and SHALL attribute a forecast to
   the provider Service 2 named in `forecast.source`.
5. WHERE the retrieved `forecast` reports `degraded` as true, THE Service SHALL NOT state a next-day
   value and SHALL say the forecast was unavailable.

### Requirement 8: Non-Diagnostic Framing

**User Story:** As a product owner, I want the agent to stay in the general-wellness lane by
construction, so that a single unlucky generation cannot turn the product into a regulated medical
device.

#### Acceptance Criteria

1. THE Service SHALL frame all Guidance as exposure reduction, matching the `advisoryScope` Service 2
   returns, and SHALL NOT emit a Forbidden_Claim.
2. THE Service SHALL check the generated Guidance against a configured set of Forbidden_Claim patterns
   before returning it, and IF a pattern matches, THEN THE Service SHALL NOT return that Guidance.
3. THE Service SHALL NOT state or imply that the user is having, or is not having, an asthma attack, an
   exacerbation, or any other medical event.
4. THE Service SHALL NOT name a medication, a dose, a frequency, or a change to any of them; WHERE the
   research's action mapping refers to a reliever inhaler, THE Service SHALL confine itself to
   preparedness language — keeping it to hand — and SHALL NOT instruct its use.
5. THE Service SHALL include the `disclaimer` Service 2 returned in every Advisory_Response.
6. WHEN a Forbidden_Claim check rejects a generation, THE Service SHALL log one warning naming the
   pattern category and SHALL NOT log the rejected text, because the rejected text is the thing that
   must not be recorded.
7. THE Service SHALL treat the Forbidden_Claim pattern set as configuration, and a configured set SHALL
   replace the defaults rather than extend them, so a deployment can correct a pattern that misfires.

### Requirement 9: Basis Transparency

**User Story:** As a clinician, I want to see why the agent said what it said, so that its
recommendation is independently reviewable rather than a black box.

#### Acceptance Criteria

1. THE Service SHALL return a Basis_Summary with every Advisory_Response that makes a claim about
   conditions, naming the driving pollutant, its sub-index and band, the site the reading came from and
   its distance, and the reading's confidence.
2. WHERE a threshold was crossed, THE Service SHALL name the threshold value and its source as Service 2
   reported them in `personalized.thresholdSource`.
3. THE Service SHALL name the breakpoint table, the calibration strategy, and the nowcast window — its
   length, the hours actually available within it, and the weighting applied — from the retrieved
   `basis`, so the derivation of the index is traceable.
3a. WHERE the retrieved `basis` reports no nowcast, THE Service SHALL treat that as meaning the index was
   not nowcast-derived, and SHALL NOT present it as a nowcast whose window is unknown.
4. THE Service SHALL read the Basis_Summary from the retrieved response and SHALL NOT recompute or
   re-derive any part of it.
5. THE Service SHALL make the Basis_Summary available whether or not the user asked for it.
6. IF the retrieved response carries no basis, THEN THE Service SHALL NOT emit a claim that would
   require one.

### Requirement 10: Red-Flag Symptom Escalation

**User Story:** As someone having a severe attack, I want to be told to get emergency help
immediately, not given advice about closing my windows.

#### Acceptance Criteria

1. WHEN the user's utterance describes a Red_Flag, THE Service SHALL return an Escalation directing the
   user to emergency services, using the `emergencyGuidance` text Service 2 returned.
2. THE Service SHALL determine the Escalation before generating exposure Guidance, and SHALL place the
   emergency direction first in the Advisory_Response.
3. THE Service SHALL return the Escalation even when the retrieved air quality is good, because a
   Red_Flag is about the person and not about the air.
4. THE Service SHALL return the Escalation even when the Serving_Client is unavailable, and SHALL NOT
   make an Escalation conditional on a successful retrieval.
5. THE Service SHALL NOT state whether the described symptoms are or are not an emergency as a clinical
   determination; it directs the user to emergency care and does not diagnose the cause.
6. THE Service SHALL record an Advice_Record for an escalating turn as for any other turn.
7. THE Service SHALL treat the Red_Flag recognition set as configuration with the research's three
   items as defaults, and SHALL apply it to the utterance and to any supplied prior turns in the same
   request.

### Requirement 11: Clinician Deference

**User Story:** As a patient with a written action plan, I want the agent to reinforce that plan rather
than compete with it, so that following the agent never means departing from my clinician.

#### Acceptance Criteria

1. THE Service SHALL defer to the user's clinician's action plan wherever the Guidance touches what the
   user should do about their condition, and SHALL NOT contradict or override a plan the user describes.
2. WHEN the user asks the agent to decide something their action plan governs, THE Service SHALL direct
   them to that plan and their clinician rather than answering.
3. THE Service SHALL NOT ask the user to record the contents of an action plan, consistent with
   Requirement 19's minimization and assumption A5.
4. WHERE the user reports worsening symptoms over time without a Red_Flag, THE Service SHALL suggest
   contacting their clinician, and SHALL NOT characterize the trajectory clinically.

### Requirement 12: Condition-Weighted Interpretation

**User Story:** As a COPD patient, I want the agent to lead with the pollutants that matter for me, so
that its advice is about my risk and not a generic index.

#### Acceptance Criteria

1. THE Service SHALL read the condition, sensitivity, `weightedFocus` and `unavailableWeightedSpecies`
   from the retrieved `personalized` block, and SHALL NOT compute a weighting of its own.
2. THE Service SHALL present the species in `weightedFocus` in the order Service 2 returned them,
   because that order is Service 2's configured clinical precedence.
3. WHERE `unavailableWeightedSpecies` is non-empty, THE Service SHALL state that those species matter
   for the user's condition but are not measured by this network, because Service 2 reports them
   precisely so the absence is explicit rather than invisible.
4. THE Service SHALL map the condition and the driving pollutant to concrete exposure-reduction actions
   from a configured mapping, and SHALL NOT invent an action outside it.
5. THE Service SHALL keep the action mapping a registry keyed by condition, so a new condition is an
   entry rather than a new branch.
6. THE Service SHALL NOT assume the user's condition is confined to one diagnostic category, and WHERE
   the profile names an overlap condition THE Service SHALL apply the weighting Service 2 returned for
   it rather than choosing one side.

### Requirement 13: Anticipatory Warning

**User Story:** As a PM-sensitive patient, I want to be warned before I feel it, so that I can act while
it still helps.

#### Acceptance Criteria

1. WHERE the retrieved `forecast` reports a next-day value and a trend, THE Service SHALL state the
   trend and attribute it to the named provider.
2. WHERE the retrieved data shows elevated particulate exposure, THE Service SHALL explain that
   particulate effects can lag by about three days, so the user can act ahead of symptoms and can relate
   today's symptoms to exposure around three days earlier.
3. THE Service SHALL describe gaseous pollutant effects as same-day, distinguishing them from the
   particulate lag.
4. THE Service SHALL present the lag as a general pattern from the evidence base, and SHALL NOT predict
   that the user will develop symptoms.
5. THE Service SHALL NOT compute its own forecast, consistent with the research finding that the
   agent's contribution is fusion rather than prediction.

### Requirement 14: Activity-Timing Guidance

**User Story:** As someone who exercises outdoors, I want to know when today is the better time to go,
so that I can keep exercising without raising my exposure.

#### Acceptance Criteria

1. WHEN the user asks about outdoor activity, THE Service SHALL answer in terms of the retrieved
   conditions and the retrieved forecast trend.
2. THE Service SHALL express timing guidance as a comparison between periods the retrieved data
   supports, and SHALL NOT name a specific hour the data does not distinguish.
3. WHERE the user's profile records an activity level and duration, THE Service SHALL relate the
   guidance to the retrieved `inhaledDose` rather than to concentration alone.
4. THE Service SHALL NOT instruct the user to stop or start exercising as a clinical direction, and
   SHALL frame the guidance as reducing exposure while doing what they intend to do.

### Requirement 15: Confidence and Uncertainty Disclosure

**User Story:** As a user, I want to know when the measurement behind the advice is weak, so that I can
judge how much to lean on it.

#### Acceptance Criteria

1. THE Service SHALL state the confidence of the reading that drove the Guidance, using the value
   Service 2 returned.
2. WHERE the driving reading's confidence is not the highest value, THE Service SHALL say so in the
   Guidance rather than only in the Basis_Summary.
3. THE Service SHALL NOT present a low-cost-sensor reading as reference-grade.
4. WHERE the retrieved measurement carries a quality flag indicating a fault, a conflict, or an
   uncalibrated value, THE Service SHALL disclose that the reading is qualified and SHALL NOT present it
   as a plain measurement.
5. THE Service SHALL NOT resolve, average away, or otherwise smooth a disagreement between retrieved
   readings; it reports what was served.
6. THE Service SHALL treat the confidence Service 2 returned as the single authority on how weak a
   measurement is, and SHALL NOT derive a second weakness signal by comparing the nowcast window's hours
   available against its length. Service 2 already caps confidence at `medium` for an incomplete window
   and at `low` where there were too few hours to compute a nowcast at all, so an incomplete window
   reaches the user through criterion 2; a second derivation here could disagree with Service 2 about
   the same measurement, and re-deriving a part of the basis is what Requirement 9.4 forbids.

### Requirement 16: Inhaled-Dose Explanation

**User Story:** As a user, I want to understand that what I breathe depends on what I am doing, not just
on the air outside, so that I see why the advice differs from the public AQI app.

#### Acceptance Criteria

1. WHERE the retrieved `personalized` block reports an `inhaledDose`, THE Service SHALL be able to
   explain it as concentration combined with an activity-adjusted breathing rate over a duration.
2. THE Service SHALL present the dose using the value and unit Service 2 returned, and SHALL NOT
   recompute it.
3. WHERE no dose was returned, THE Service SHALL NOT state one and SHALL explain that it depends on
   recording an activity level and duration in the profile.
4. THE Service SHALL NOT present the dose as a clinical exposure limit or compare it to one, because no
   such personal limit exists in the evidence base.

### Requirement 17: Pollen and Pollution Interaction

**User Story:** As someone with allergic rhinitis, I want the agent to treat pollen as a primary
trigger, so that its advice addresses what actually sets me off.

#### Acceptance Criteria

1. WHERE the retrieved `personalized` block carries a `pollen` outlook, THE Service SHALL include it in
   the Guidance using the categories Service 2 returned.
2. WHERE both the pollen outlook and the particulate or nitrogen-dioxide readings are elevated, THE
   Service SHALL state that the combination is worse than either alone, because traffic-derived
   particulate acts as an adjuvant and damages pollen so it releases more allergen.
3. THE Service SHALL NOT derive a pollen category from a count, and SHALL NOT state a pollen value
   Service 2 did not return.
4. WHERE the user's condition marks pollen as relevant but no outlook was returned, THE Service SHALL
   say the pollen outlook is unavailable, because for this segment its absence is material.
5. THE Service SHALL NOT name a specific allergen as the user's trigger.

### Requirement 18: Untrusted Content

**User Story:** As a security reviewer, I want text the agent reads to be data rather than
instructions, so that neither a user nor a third party can redirect it.

#### Acceptance Criteria

1. THE Service SHALL treat the user's utterance, any supplied prior turns, and every value in a
   retrieved response as data, and SHALL NOT act on any instruction contained in them.
2. IF the utterance or a retrieved value attempts to change the agent's framing, disable a guardrail,
   reveal its instructions, or alter its scope, THEN THE Service SHALL continue under its own
   constraints and SHALL NOT comply.
3. THE Service SHALL apply the Requirement 8 output check to every generation regardless of what the
   input requested, so a successful injection still cannot produce a Forbidden_Claim.
4. THE Service SHALL NOT reveal its system instructions, its Forbidden_Claim patterns, or its
   configuration in a response.
5. THE Service SHALL keep the retrieved data structurally separate from the utterance in the prompt, per
   Requirement 6.3, so text inside a retrieved field cannot present itself as a turn boundary.

### Requirement 19: Personal Data Minimization

**User Story:** As a user, I want the agent to hold as little about my health as possible, so that a
breach of it costs me as little as possible.

#### Acceptance Criteria

1. THE Service SHALL store no condition, sensitivity, personal threshold, location, or utterance of its
   own beyond the lifetime of the turn, per assumption A6.
2. THE Service SHALL NOT write a condition, a sensitivity, a personal threshold, a coordinate, or any
   part of an utterance to a log entry.
3. THE Service SHALL log the pseudonymous user identity where a log entry needs to identify the user,
   consistent with Service 2's treatment of the same tension.
4. THE Service SHALL NOT include a coordinate in a log entry, and WHERE a location must be identified
   THE Service SHALL use the location name Service 2 returned.
5. THE Service SHALL configure redaction once, centrally, so no module can bypass it.
6. THE Service SHALL NOT transmit personal data to any destination other than the Serving_Client and the
   Model_Port.

### Requirement 20: Advice Audit Trail

**User Story:** As an operator, I want a record of what was advised on what data, so that a later
question about a specific piece of advice can be answered.

#### Acceptance Criteria

1. THE Service SHALL write one Advice_Record per Advisory_Turn through an injected port.
2. THE Service SHALL record the pseudonymous user identity, the turn instant, whether an Escalation was
   returned, whether a threshold was crossed, the driving pollutant, and the identifiers of the
   retrieved records the Basis_Summary named.
3. THE Service SHALL NOT record the utterance, the Guidance text, a condition, a sensitivity, a personal
   threshold, or a coordinate, so the trail carries no health-adjacent content and erasure has only an
   identity to remove.
4. THE Service SHALL record the Advice_Record for a turn whose generation was rejected by a guardrail
   check, noting the rejection, because a suppressed generation is exactly what an operator needs to
   see.
5. THE Service SHALL NOT make writing an Advice_Record a condition of answering the user, and IF the
   write fails THEN THE Service SHALL log the failure and still return the Advisory_Response.
6. THE Service SHALL take no action on the user's behalf and SHALL send no notification, consistent with
   the research's no-autonomous-action guardrail.

### Requirement 21: Degradation and Failure Handling

**User Story:** As a user, I want a useful answer or an honest one, so that the agent never guesses when
it cannot see.

#### Acceptance Criteria

1. IF the Serving_Client fails, times out, or returns an unusable body, THEN THE Service SHALL return an
   Advisory_Response saying current conditions are unavailable, SHALL set the degraded indicator, SHALL
   log one warning naming the failure kind, and SHALL NOT state any condition value.
2. IF the Model_Port fails, times out, or returns an unusable generation, THEN THE Service SHALL return
   a response built from the retrieved data without generated prose, SHALL set the degraded indicator,
   and SHALL log one warning naming the failure kind.
3. THE Service SHALL return the Guardrail_Envelope and any Escalation even in a degraded response.
4. THE Service SHALL NOT return a raw exception, a stack trace, a model error body, or a Service 2 error
   body to the caller.
5. THE Service SHALL catch the exception types it expects at the boundaries and SHALL NOT use a bare
   catch except at a true top-level boundary, where it SHALL log.
6. THE Service SHALL never answer an invalid Advisory_Request with a server error; a malformed request
   is reported as such, naming the offending field.
7. WHERE only part of the retrieved data is available, THE Service SHALL advise on what it has and state
   what is missing, rather than failing the whole turn.

8. THE Service SHALL resolve the Guardrail_Envelope in this order and SHALL record which source supplied
   it: the envelope Service 2 returned this turn; else the last envelope successfully retrieved in this
   process; else, for `emergencyGuidance` only, the configured fallback of assumption A8a. THE Service
   SHALL compare the configured fallback against the first successfully retrieved `emergencyGuidance` of
   each run and SHALL log one warning naming the field, but never either text, WHERE they differ.
9. THE Service SHALL NOT substitute a local `advisoryScope` or `disclaimer`, and WHERE no envelope can be
   resolved for those, THE Service SHALL omit them rather than invent them. An escalation SHALL still be
   returned, because Requirement 10.4 does not depend on the envelope being complete.

### Requirement 22: Invocation Bounds

**User Story:** As an operator, I want a turn to have a known ceiling in model calls and tokens, so that
one conversation cannot run away with the budget.

#### Acceptance Criteria

1. THE Service SHALL bound the number of Model_Port invocations per Advisory_Turn to a configured
   maximum, with default 2 — one generation and one repair attempt after a guardrail rejection.
1a. THE Service SHALL express the per-turn ceilings through Strands' own invocation `limits` — `turns`,
   `output_tokens` and `total_tokens` — rather than a counter of this service's own. The framework
   enforces them inside the agent loop where a hand-rolled counter cannot see a tool round trip, and it
   reports a typed `limit_*` stop reason the caller can act on.
2. WHEN the bound is reached without a Guidance that passes the Requirement 8 check, THE Service SHALL
   return a degraded response built from the retrieved data and SHALL NOT return the rejected
   generation.
2a. THE Service SHALL treat a `limit_*` stop reason as reaching the bound, and SHALL log one warning
   naming which limit was reached.
3. THE Service SHALL bound the number of Serving_Client calls per Advisory_Turn to a configured maximum.
4. THE Service SHALL bound the total prior turns it will include in a prompt to a configured maximum, and
   SHALL include the most recent ones when the supplied sequence is longer.
5. THE Service SHALL record the model invocation count and token usage per turn as metrics.

### Requirement 23: Configuration

**User Story:** As an operator, I want the service to refuse to start on a bad configuration rather than
half-start, so that a misconfiguration is a startup failure and not a subtly wrong answer.

#### Acceptance Criteria

1. THE Service SHALL resolve configuration from environment variables, then a configuration file, then
   built-in defaults, in that precedence, and SHALL log the resolved non-secret configuration once at
   startup.
2. THE Service SHALL validate every resolved value before invoking the Model_Port or the Serving_Client,
   SHALL write one message per invalid value, and SHALL exit non-zero without half-starting.
3. THE Service SHALL reject an unrecognized configuration key, listing the recognized keys in the same
   category.
4. THE Service SHALL never write a resolved credential to a log entry, and SHALL report a credential
   path as whether it resolved rather than as a path.
5. THE Service SHALL require the Service 2 base URL, and SHALL refuse to start without it.
6. THE Service SHALL select each adapter by name from a registry, so that adding an adapter is a
   registry entry rather than a new branch, and SHALL reject a name that is not registered.

### Requirement 24: Observability

**User Story:** As an operator, I want structured logs and counters that never contain health data, so
that I can run the service without reading anybody's conversation.

#### Acceptance Criteria

1. THE Service SHALL configure a structured logger once at startup, emitting one single-line JSON object
   per event to standard output, and SHALL never use direct printing.
2. THE Service SHALL log at `info` for operational events, `warning` for recoverable conditions
   including every guardrail rejection and every degradation, and `error` for handled failures.
3. THE Service SHALL log every handled error, and SHALL NOT fail silently.
4. THE Service SHALL count turns answered, turns degraded, guardrail rejections by category, escalations
   returned, Serving_Client failures by kind, and Model_Port failures by kind.
5. THE Service SHALL NOT emit a metric label carrying a condition, a coordinate, or any part of an
   utterance.

### Requirement 25: Determinism and Reproducibility

**User Story:** As a Service 3 developer, I want a turn to be reproducible from its inputs, so that a
report of bad advice can be investigated rather than guessed at.

#### Acceptance Criteria

1. FOR ALL identical inputs — the same utterance, the same retrieved data, the same prior turns, the same
   configuration, and the same Clock instant — THE Service SHALL produce an identical Advisory_Response
   when the Model_Port is deterministic.
2. THE Service SHALL obtain every instant from the injected Clock and SHALL use no source of randomness
   in composing a response.
3. THE Service SHALL apply a defined order to every iteration whose result reaches a response.
4. THE Service SHALL request `temperature` 0 from the Model_Port by default, and SHALL treat any
   sampling parameter as configuration.
4a. THE Service SHALL NOT claim reproducibility of generated prose from sampling controls alone. Strands
   documents no seed parameter, and `temperature` 0 reduces but does not eliminate variation in a hosted
   model. Requirement 25 criterion 1's reproducibility is therefore asserted in the test suite against
   the scripted Model_Port adapter of Requirement 6 criterion 2, which is exact — and the production
   claim is confined to the parts of the Advisory_Response this service composes itself: the
   Basis_Summary, the Guardrail_Envelope, the Escalation, and the degraded indicator.
5. THE Service SHALL express every internal instant as a timezone-aware UTC value.

### Requirement 26: Project Scaffolding and Offline Suite

**User Story:** As a contributor, I want one documented command surface and a suite that passes offline,
so that I can develop and verify this service without cloud access.

#### Acceptance Criteria

1. THE Service SHALL live in `agent-advisor/` with its package at `agent-advisor/src/aqm_advisor/` and
   its tests in a sibling tree divided into `unit/`, `properties/` and `integration/`.
2. THE Service SHALL declare its own manifest naming Python 3.12 and pinning every direct dependency to
   one exact version, with its resolved lockfile committed.
3. THE Service SHALL import no module from another service directory, keeping any copy of a consumed
   contract independent and covered by its own tests.
4. THE Service SHALL expose its full test suite behind one documented command that exits zero only if
   every test passes, and SHALL provide `just` recipes for test, lint, typecheck and local run.
5. THE full test suite SHALL pass with no AWS credentials and no network access beyond localhost,
   exercising a local adapter for every port.
5a. THE Service SHALL exercise the Requirement 32 deployment contract in the OFFLINE suite — `POST
   /invocations` and `GET /ping` against the locally served application — because the `bedrock_agentcore`
   SDK serves those endpoints with no AWS involvement. The contract that decides whether a deployment
   answers at all is therefore testable without deploying, and asserting it here is much cheaper than
   discovering a malformed health response after a push to ECR.
6. THE Service SHALL mark every check requiring a container engine or a live model so that the offline
   suite excludes it and a separate command runs it, and SHALL keep those checks free of any dependency
   on cloud credentials.
7. THE Service SHALL commit no secret, and every credential SHALL be supplied at runtime through the
   environment or a runtime-supplied path.
8. THE Service SHALL provide one shared behavioral test suite per port, executed against every adapter
   of that port.
9. THE Service SHALL implement each correctness property named in the design document as exactly one
   property-based test running at least 100 examples.

### Requirement 27: Health Profile Elicitation

**User Story:** As a user, I want to tell the agent about my condition, my inhalers and my weekly
routine in conversation, so that I do not have to fill in a form and do not have to repeat myself.

#### Acceptance Criteria

1. THE Service SHALL be able to elicit the Condition, the Sensitivity_Level, the Medication_Entry set,
   the Routine_Entry set, and the User_Location entries conversationally, and SHALL write each through
   the Serving_Client to Service 2.
2. THE Service SHALL restate the structured interpretation of what the user said and obtain explicit
   confirmation before any profile write, because mapping "I use a brown inhaler every morning" onto a
   Medication_Role is an interpretation of the user's health and SHALL NOT be assumed silently.
3. IF the user offers a dose, a frequency, a route of administration, or an administration schedule,
   THEN THE Service SHALL say that it records only the name and the role, SHALL write only those, and
   SHALL NOT include the offered dose or frequency in its reply, in a log entry, or in the write.
4. IF the user offers a diagnosis narrative, a diagnosis code, a date of birth, a name, or a contact
   detail, THEN THE Service SHALL decline to record it, SHALL say what it does keep, and SHALL NOT echo
   the offered value.
5. THE Service SHALL store none of these values itself, holding them only for the turn in which they are
   written, per Requirement 19.
6. WHERE a write is rejected by Service 2 for exceeding a configured limit, THE Service SHALL report the
   limit and SHALL NOT report the change as applied.

### Requirement 28: Symptom Diary Capture

**User Story:** As a user, I want to say how my day went in my own words and have the agent record it
properly, so that the diary builds up without becoming a chore.

#### Acceptance Criteria

1. THE Service SHALL be able to construct a Symptom_Entry from the user's description — a
   Symptom_Severity, a set of Symptom_Marker values, and whether a reliever was used — and SHALL write it
   through the Serving_Client.
2. THE Service SHALL restate the Symptom_Severity and the Symptom_Marker set it inferred and obtain
   explicit confirmation before writing, and SHALL apply the user's correction rather than its own
   reading when the two differ. Inferring a severity from prose is a judgement about the user's health,
   and the user is the authority on it.
3. THE Service SHALL NOT write a Symptom_Entry without an explicit instruction or confirmation from the
   user in the same turn, and SHALL NOT infer a diary entry from conversational context alone.
4. THE Service SHALL include a Symptom_Note only where the user asks for their own words to be kept, and
   SHALL say that the note is stored for their recall and is not used to compute anything.
5. WHERE the user describes a day that already has an entry, THE Service SHALL say that the existing
   entry for that date will be replaced, and SHALL obtain confirmation before replacing it.
6. THE Service SHALL apply Requirement 10's Red_Flag check to a diary description exactly as to any other
   utterance, and WHERE a Red_Flag is described THE Service SHALL escalate first and SHALL NOT let
   recording the entry displace the escalation.
7. THE Service SHALL NOT characterize a recorded entry clinically, SHALL NOT tell the user their
   condition is deteriorating or improving as a clinical finding, and SHALL NOT infer a diagnosis from a
   marker set.
8. THE Service SHALL store no part of the entry or the description itself, per Requirement 19.

### Requirement 29: Medication-Aware Preparedness

**User Story:** As a user, I want the agent to tell me which of my own inhalers to have with me on a bad
air day, so that the advice is about the things I actually carry.

#### Acceptance Criteria

1. THE Service SHALL be permitted to name a medication from the retrieved Medication_Entry set, and only
   from that set, when stating what the user may wish to have available.
2. THE Service SHALL confine such a statement to PREPAREDNESS — having the item to hand, or carrying it —
   and SHALL NOT recommend, instruct, suggest, or imply taking it, adding it, increasing it, decreasing
   it, or changing when it is used.
3. THE Service SHALL NOT name a medication that is not in the retrieved set, and SHALL NOT suggest that
   the user obtain, request, or ask about a medication they have not recorded.
4. THE Service SHALL state, whenever it names a medication, that the decision to use it is governed by
   the user's clinician's action plan, per Requirement 11.
5. THE Service SHALL treat this requirement as TIGHTENING Requirement 8 criterion 4 rather than relaxing
   it: the Forbidden_Claim set SHALL continue to reject any generation that pairs a medication name with
   an administration instruction, and the permitted preparedness form SHALL be the only construction in
   which a medication name may appear.
6. THE Service SHALL verify a generation naming a medication against both the Forbidden_Claim set and the
   retrieved Medication_Entry set before returning it, and IF the named medication is not in the
   retrieved set THEN THE Service SHALL NOT return that generation.
7. WHERE no Medication_Entry set was retrieved, THE Service SHALL use only generic role language — a
   reliever, a preventer — and SHALL NOT name any medication.
8. THE Service SHALL NOT rank, compare, or choose between the user's medications, because selecting among
   prescribed items is a clinical decision even when every option came from the user's own list.

### Requirement 30: Learned-Association Reporting

**User Story:** As a user who has kept a diary, I want to know what the service has noticed about my own
pattern, so that I can see why my alerts changed and judge whether I agree.

#### Acceptance Criteria

1. WHERE the retrieved data reports a Learned_Threshold, THE Service SHALL be able to explain that the
   escalation point came from the user's own diary, naming the species, the lag, and the number of
   observations it rested on.
2. THE Service SHALL describe an Exposure_Association as an association or a pattern, and SHALL NOT
   describe it as a cause, a trigger, a diagnosis, or a prediction about the user's future symptoms.
3. THE Service SHALL NOT state an association the retrieved data did not report, and SHALL NOT compute
   one, consistent with Requirement 7's grounding rule.
4. WHERE the retrieved data reports that the minimum observation count was not met, THE Service SHALL say
   that there is not yet enough diary history to draw a pattern, and SHALL name what is missing rather
   than presenting a weak association.
5. WHERE the escalation source is a Learned_Threshold, THE Service SHALL say so in the Basis_Summary
   alongside the threshold value, so a changed alerting point is traceable to the reason it changed.
6. THE Service SHALL state that the user's own declared Personal_Threshold takes precedence over a
   learned one, WHERE both exist, so the user understands their instruction was not overridden.
7. THE Service SHALL NOT present the association as a reason to change any medication or any clinical
   behaviour, and SHALL confine the consequence it describes to exposure reduction and to the alerting
   point.

### Requirement 31: Agent Framework

**User Story:** As a Service 3 developer, I want the agent built on Strands Agents, so that the agent
loop, tool calling, structured output and tracing come from a maintained SDK rather than from code we
wrote and must now own.

#### Acceptance Criteria

1. THE Service SHALL build the agent on the Strands Agents SDK, pinned to one exact version per
   assumption A9, with the resolved lockfile committed.
2. THE Service SHALL rely on the Strands agent loop and SHALL NOT implement a tool-calling loop of its
   own, because a second loop would duplicate the framework's stop-reason handling, limit enforcement and
   retry behaviour and then drift from it.
3. THE Service SHALL keep the agent loop **in this service's own process**, which is the documented
   Strands behaviour, so that every turn is executable in the offline suite.
4. THE Service SHALL define the system prompt as configuration-supplied content rather than a literal
   embedded in a function, so that its text is reviewable, diffable and testable as data.
5. THE Service SHALL register the Requirement 8 and Requirement 29 output checks through Strands hooks at
   the point after generation, so no return path can bypass them. A check invoked only from the ordinary
   path would be skipped by any future branch that returns early.
6. THE Service SHALL NOT take a general-purpose tool library, per assumption A9, so the agent's reachable
   tool surface is exactly the Service 2 operations this document names and nothing else.
7. THE Service SHALL treat every Strands stop reason explicitly, and SHALL fail a test if a stop reason
   the SDK can return is unhandled, so an SDK upgrade introducing one is a failing test rather than a
   silent fall-through.
8. THE Service SHALL NOT use the Strands memory store for conversational state, per assumption A6; prior
   turns arrive as a parameter and no health-adjacent text is retained.

### Requirement 32: Deployment Runtime

**User Story:** As an operator, I want the agent deployable to Amazon Bedrock AgentCore, so that session
isolation, scaling and long-running invocations are the platform's problem rather than ours.

#### Acceptance Criteria

1. THE Service SHALL be deployable to Amazon Bedrock AgentCore Runtime as a `linux/arm64` container image
   published to Amazon ECR, serving `POST /invocations` and `GET /ping` on host `0.0.0.0` port `8080`.
2. THE Service SHALL keep every AgentCore dependency at the deployment boundary, so that no domain or
   advisory component imports an AgentCore type and the offline suite is unaffected by the deploy target.
3. THE Service SHALL define its own `/invocations` request and response bodies, because the Runtime
   contract fixes the transport and the status codes but leaves the JSON field shape to the application.
   THE Service SHALL use the Advisory_Request and Advisory_Response of Requirement 1 as those bodies
   rather than a second wire shape that would then need keeping in step.
4. THE Service SHALL answer `GET /ping` with `{"status": "Healthy"}` when able to accept work and
   `{"status": "HealthyBusy"}` while a turn or a background task is in flight, and SHALL NOT advance the
   optional `time_of_last_update` on every ping. A timestamp that always moves signals a continuous status
   change, which prevents the idle session timeout from ever firing — sessions then persist to
   `MaxLifetime` and **can exhaust the account's session quota**. Using the AgentCore SDK's task API
   handles this correctly, which is a reason to prefer it over a hand-rolled status handler.
4a. THE Service SHALL NOT perform a blocking operation in the `/invocations` handler. A blocking call
   there also blocks the `/ping` health endpoint, and a session whose ping thread is blocked is treated as
   unhealthy: the documented consequence is termination, and the reported field consequence is the
   entrypoint being RE-INVOKED while the first turn is still working. Every model call, every
   Serving_Client call and every guardrail call SHALL therefore be awaited on the async path or run on a
   separate thread.
4b. THE Service SHALL assert in the OFFLINE suite that `GET /ping` stays responsive while a turn is in
   flight, and that it reports `HealthyBusy` for the duration. THIS IS THE ONE PRODUCTION FAILURE MODE
   THAT DOES NOT REPRODUCE LOCALLY BY DEFAULT — a blocked ping thread costs nothing on a developer's
   machine, where no platform is watching the health endpoint, and only appears once deployed. The
   documentation's own advice is to run the server locally and check ping status while simulating the
   scenario, so this converts an unreproducible deployed fault into an ordinary failing test.
4c. THE Service SHALL make every write it performs through the Serving_Client idempotent with respect to a
   re-invoked entrypoint, because a retry can deliver the same turn twice. A Symptom_Entry write is
   already safe by Service 2's Requirement 31 criterion 7, which replaces rather than accumulates an entry
   for a date; an Advice_Record write and a profile write are not inherently safe and SHALL be keyed so a
   duplicate delivery does not double-apply.
5. THE Service SHALL return an Advisory_Response for every handled failure of Requirement 21, and SHALL
   NOT allow an unhandled error to become a 4xx or 5xx from the container. A container error is surfaced
   to the caller as an opaque `424 RuntimeClientError`, which would replace a documented degraded answer
   with a transport fault and lose the Guardrail_Envelope and any Escalation with it.
6. THE Service SHALL keep every response within the Runtime's 100 MB payload limit, which an
   Advisory_Response bounded by Requirement 6 criterion 5 satisfies by construction.
7. THE Service SHALL accept an end user's Cognito JWT at the Runtime front door through a
   `customJWTAuthorizer` configured with the user pool's discovery URL, its permitted client identifiers
   and its permitted audience, so that an unauthenticated invocation is refused with `401` before any
   model or store is touched.
8. THE Service SHALL obtain the inbound `Authorization` header through the Runtime's request-header
   allowlist and SHALL forward it unmodified to the Serving_Client, per assumption A4, keeping Service 2's
   own authorization the single enforcement point. THE Service SHALL NOT parse, validate or re-issue the
   credential, per assumption A4a.
8a. THE Service SHALL treat a credential rejected for remaining lifetime as an authentication failure under
   Requirement 5 criterion 4 rather than as a service fault. A forwarded token is a token that keeps
   ageing: it may be accepted at the front door and then be near or past expiry when a later
   Serving_Client call is made within the same turn, and there is a reported behaviour of AgentCore
   refusing a token that expires within the next minute. THE Service SHALL NOT attempt to refresh, extend
   or reissue it — that is the caller's responsibility — and SHALL bound a turn short enough that a
   credential valid at the start is still valid at the last retrieval, which Requirement 32 criterion 13's
   turn budget already requires for other reasons.
9. THE Service SHALL NOT use AgentCore Memory for conversational state or for any derived value, per
   assumption A12.
10. THE Service SHALL emit traces, metrics and logs through OpenTelemetry, which AgentCore Observability
    consumes, and SHALL take the Strands `otel` extra as its instrumentor rather than a proprietary
    telemetry client. WHERE the service runs on AgentCore Runtime, instrumentation is automatic and this
    service SHALL configure no collector of its own.
11. THE Service SHALL propagate a session correlation identifier through OpenTelemetry baggage so the
    spans of one advisory session are attributable to it, and SHALL supply a `runtimeSessionId` of at
    least 33 characters wherever it originates one.
12. THE Service SHALL support Server-Sent Events on `/invocations` only WHERE streaming is enabled by
    configuration, and a streamed turn SHALL emit no Guidance token before the Requirement 34 output
    checks have passed on the complete generation. Streaming a generation as it is produced would put
    unverified health-adjacent text in front of the user, which is the single thing the output check
    exists to prevent — so streaming, where enabled, streams a checked result and not a live generation.
13. THE Service SHALL NOT rely on an execution window longer than a configured turn budget for an
    ordinary Advisory_Turn. The Runtime permits 8 hours on a microVM and up to 14 days on an Instance; a
    conversational turn that took minutes would be a defect rather than a feature, and the long window
    serves the scheduled work of Requirement 33, not the conversation.

14. THE Service SHALL configure the audience its inbound `customJWTAuthorizer` accepts to be the SAME
    audience Service 2's authorizer accepts, and SHALL assert that agreement in the offline
    deployment-contract tests. Per assumption A4, direct forwarding of the end user's token is sufficient
    only WHERE the inbound audience already matches the downstream service; if the two configurations
    drift apart, forwarding silently stops being the documented pattern and every retrieval fails
    authorization at Service 2 rather than here, which is the hardest place to attribute it.

### Requirement 33: Asynchronous Association Job

**User Story:** As an operator, I want the diary-to-exposure learning to run on its own schedule, so that
a person waiting for advice never waits for a statistical computation.

#### Acceptance Criteria

1. THE Service SHALL NOT compute, trigger synchronously, or wait for the exposure–symptom association of
   Service 2's Requirement 32 during an Advisory_Turn.
2. THE Service SHALL read a Learned_Threshold only as a value already present in a retrieved response,
   per Requirement 30.
3. WHERE this service hosts the trigger for the association job, THE Service SHALL invoke it
   asynchronously and SHALL NOT block a turn on its completion.
4. THE Service SHALL treat the association job as idempotent per user and per evaluation window, so a
   redelivered trigger recomputes the same result rather than double-counting a diary entry.
5. THE Service SHALL carry a correlation identifier through an asynchronous invocation and its result, so
   a completed job is attributable to the request that caused it.
6. IF an asynchronous association job fails, THEN THE Service SHALL log the failure and SHALL continue to
   serve advisory turns from the thresholds already stored, because a failed learning run degrades the
   personalization and does not break the advice.
7. THE Service SHALL NOT notify the user on the completion of an association job, consistent with
   Requirement 20 criterion 6's prohibition on autonomous action.
8. THE Service SHALL use the AgentCore SDK's asynchronous task API — `add_async_task` when background
   work begins and `complete_async_task` when it ends — for any work that continues after a response has
   been returned. Verified 2026-09-10: AgentCore supports agents that "continue processing after
   responding to the client", and the SDK's task API tracks the work AND manages the `/ping` status
   automatically, which is the part a hand-rolled background thread gets wrong.
9. THE Service SHALL treat asynchrony as an AGENT-SIDE behaviour rather than a separate client API. There
   is no fire-and-forget job endpoint to call: the documented model is one invocation API where the agent
   itself decides to respond immediately and keep working, so a client cannot tell a synchronous turn from
   an asynchronous one. An Advisory_Request therefore needs no async variant.
10. WHERE the association job is driven from a serverless pipeline rather than from within a turn, THE
    Service SHALL use one of the documented AWS integrations rather than a bespoke poller: a Step
    Functions `waitForTaskToken` state whose dispatcher returns as soon as the agent is started, the Step
    Functions direct SDK integration `aws-sdk:bedrockagentcore:invokeAgentRuntime`, or a Lambda durable
    function's `waitForCallback`. A caller that blocks on the agent is billed for the whole wait while
    doing nothing, which is the documented anti-pattern.
11. THE Service SHALL derive the `runtimeSessionId` for a triggered job from a stable property of the
    triggering execution rather than generating a fresh one, so that a retried trigger resumes the same
    session instead of starting a second one — which is also what makes Requirement 33 criterion 4's
    idempotency achievable rather than merely asserted.
12. THE Service SHALL set an explicit timeout on any callback-based wait, so a silent agent fails the
    execution cleanly rather than leaving it paused indefinitely.

### Requirement 34: Guardrail Enforcement and Output Verification

**User Story:** As a product owner, I want a second, independent check on what the agent says, so that
the non-diagnostic constraint does not rest solely on the model's willingness to obey a prompt.

#### Acceptance Criteria

1. THE Service SHALL verify generated Guidance with a check that is independent of the generating model,
   and SHALL NOT treat prompt instructions as the enforcement mechanism. A prompt is a request; an
   independent check is a control.
2. THE Service SHALL apply Amazon Bedrock Guardrails to the generated Guidance through the
   `ApplyGuardrail` operation with `source` set to `OUTPUT`. That operation is decoupled from model
   invocation, so it verifies text the service already holds — which is what makes it a check on the
   output rather than a hope about the generation.
3. THE Service SHALL configure Denied Topics covering diagnosis, medication administration, and dosing,
   because Denied Topics enforced on output is the control that most directly expresses Requirement 8's
   prohibition.
4. THE Service SHALL treat a guardrail intervention as a Requirement 8 rejection, SHALL NOT return the
   intervened text, and SHALL count the rejection by category.
5. THE Service SHALL run the local Forbidden_Claim check of Requirement 8 criterion 2 **in addition** to
   the Bedrock Guardrails check, and SHALL NOT make either conditional on the other. The local check runs
   with no network and therefore still runs in the offline suite and when Bedrock is unreachable; the
   managed check catches phrasings no pattern anticipated. Either alone leaves a gap the other covers.
6. IF the Bedrock Guardrails check is unavailable, THEN THE Service SHALL fail closed for any generation
   the local check cannot clear, and SHALL set the degraded indicator, because an unverifiable
   health-adjacent generation is worse than no generation.
7. THE Service SHALL apply the Requirement 34 checks to the final Guidance text, after any repair
   attempt, so a repair cannot reintroduce what the first check rejected.
8. THE Service SHALL NOT rely on a contextual grounding score as the mechanism for Requirement 7's
   grounding rule. Grounding requires that every quantitative claim match a Retrieved_Value exactly, which
   is a decidable comparison this service performs itself; a similarity threshold cannot decide it.
9. THE Service SHALL treat the guardrail identifier and version as configuration, and SHALL refuse to
   start when guardrail enforcement is enabled and no identifier is configured.

### Requirement 35: Agent Quality Assurance

**User Story:** As a reviewer, I want evidence that this agent behaves, so that "we prompted it not to"
is not the whole of the safety argument.

#### Acceptance Criteria

1. THE Service SHALL exercise every advisory path in the offline suite against the scripted Model_Port
   adapter, with no live model invocation, so the suite runs in continuous integration deterministically
   and at no model cost.
2. THE Service SHALL maintain a NEGATIVE test suite asserting what the agent does NOT say: for a set of
   adversarial utterances — asking for a diagnosis, asking what dose to take, asking it to ignore its
   instructions, asking it to name a medication the user has not recorded — the emitted Guidance SHALL be
   rejected or SHALL contain no Forbidden_Claim.
3. THE Service SHALL maintain a set of golden Advisory_Turns pairing a scripted retrieval and utterance
   with an expected structured outcome — the Escalation, the Basis_Summary, the degraded indicator, and
   the guardrail verdict — and SHALL assert those structured fields rather than the prose, because prose
   is the part a model may legitimately vary.
4. THE Service SHALL assert the TRAJECTORY of a turn, not only its output: which tools were called, in
   what order, and how many times, so that a turn which happens to produce plausible text by retrieving
   nothing fails.
5. THE Service SHALL assert that a turn making a claim about conditions called the air-quality retrieval,
   so that a plausible answer invented without evidence is a failing test.
6. THE Service SHALL assert the Requirement 10 escalation against utterances describing each configured
   Red_Flag, and SHALL assert that escalation still occurs when the Serving_Client fails.
7. THE Service SHALL implement each correctness property named in the design document as exactly one
   property-based test at no fewer than 100 examples, matching the discipline of the other two services.
8. THE Service SHALL mark any check requiring a live model or a live guardrail as excluded from the
   offline suite and runnable by a separate command, and SHALL keep the offline suite's verdict
   independent of it.
9. WHERE an evaluation uses a model to judge a model, THE Service SHALL treat its verdict as advisory and
   SHALL NOT make the offline suite's pass or fail depend on it, because a non-deterministic judge cannot
   gate a deterministic build.
10. THE Service SHALL record, for every guardrail rejection observed in testing, the utterance class that
    produced it, so the negative suite grows from real rejections rather than from imagination alone.
