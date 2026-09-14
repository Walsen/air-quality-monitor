---
inclusion: fileMatch
fileMatchPattern: ['agent-advisor/**']
---
# Agent engineering

How to build and change the AI advisor well. These are practices, not
ceremonies: reach for each when it earns its place, and prefer the simplest thing
that meets the requirement and its tests. They extend the monorepo engineering
practices; where this file is silent, that one still applies.

## 1. Agentic patterns — apply when the turn needs them, not by default

The advisor is a **tool-using agent**, not a chat wrapper. The patterns below are
the ones this service already leans on; add another only when a turn genuinely
needs it.

- **Tools as the only path to facts.** Every number the advisor states must come
  from a tool call in the same turn (grounding). Retrieval tools —
  `air_quality`, `history`, `profile_get/put`, `symptom_entry_put` — are the
  agent's senses; do not let the model narrate a value it did not retrieve.
- **The credential lives in a closure, never a tool parameter.** A tool takes only
  what the model may legitimately choose; the bearer token is captured when the
  tools are built (`build_retrieval_tools`) so it never enters a model-facing
  schema. Any new tool follows this: secrets and identity by closure, task inputs
  by parameter.
- **Deterministic pre-checks before the model, not after.** Emergency red-flags
  are evaluated and can short-circuit to seek-help **before** the model is
  consulted; forbidden-claim and grounding checks run on the **emitted** text and
  can withhold or degrade it. Put a control on the side of the model where it can
  actually hold: a safety act the model must never emit is checked on output; a
  routing decision that must not depend on the model is checked on input.
- **Degrade, don't crash.** A failed retrieval becomes "unavailable", an audit
  failure never blocks the answer, a guardrail-unavailable is recorded and the
  turn degrades. A turn returns a safe, honest response or a clean escalation —
  never a stack trace.
- **New sub-behaviours are strategies behind a port**, selected by name from the
  composition registry (`composition.py`), the same way models, stores, guardrails
  and serving clients already swap. Do not grow an `if provider == …` ladder.
- Document *why* a pattern is there when it is not obvious. Reserve multi-step
  planning, reflection loops, or a second agent for a turn that measurably needs
  them — most turns are one grounded pass and should stay that way.

## 2. Testing the agent — judge and student, offline and fenced

Two kinds of check, and they must not be confused.

- **The student is deterministic and gates the build.** Domain logic, tool
  wiring, grounding, forbidden-claim patterns, escalation, idempotency, redaction
  — example tests with `pytest` and invariants with `hypothesis`, all offline with
  no model, no network beyond localhost, no credentials. This is the verdict.
- **The judge is an LLM and never gates the build.** LLM-as-judge evaluation
  (`tests/support/eval_harness.py`) scores whether a generation is appropriate;
  its verdict is **advisory** and is reported, not asserted (Req 35.9). The
  un-gating is structural — `run_judged_evaluation` never raises on a negative
  verdict — so keep it that way: a non-deterministic judge cannot decide a
  deterministic suite.
- **The judge is injected**, so the harness itself is offline-testable against a
  stub. Live judging lives behind `@pytest.mark.integration`, keyed on
  `AQM_ADVISOR_MODEL_ID`, and **skips cleanly** when no model is configured. A new
  behavioural expectation about *what the agent says* (tone, scoping, helpfulness)
  belongs here as a judged case; a new rule about *what it must never say* belongs
  in the deterministic student suite.
- **Trajectory over wording where safety is at stake.** Assert which tools a turn
  called, not just the prose — a negative suite pins what must never be emitted,
  and trajectory assertions pin that a claimed retrieval actually happened. These
  are deterministic and gate.
- Write the failing test first (red → green → refactor), and for a judged
  behaviour add the judged case alongside the deterministic floor that guards its
  prerequisites.

## 3. Prompt caching

Prompt caching cuts latency and cost by reusing the model's work on a stable
prefix (the system prompt and tool schemas) across turns. It is a strong fit here
because the system prompt is large and fixed and only the user turn changes.

- **Order context stable-prefix first.** Put the durable material — system prompt,
  tool definitions — ahead of the per-turn user content, so the cacheable prefix
  is contiguous and identical turn to turn. A caching win is destroyed by
  interpolating anything per-turn (a timestamp, the user id) into the prefix.
- **Never put per-user or per-turn data in the cached prefix.** Beyond breaking
  the cache, it risks leaking one turn's context into another's — the isolation
  the privacy tests protect. Retrieved readings and profile go in the per-turn
  section, not the shared prefix.
- **Prove it changed nothing.** Caching is a transport optimisation; the grounding,
  forbidden-claim and privacy properties must hold identically with it on or off.
  Gate it behind configuration and keep the offline suite passing with it disabled.
- **Status: implemented, config-gated, off by default.** The Bedrock model adapter
  takes `model_prompt_caching` (env `AQM_ADVISOR_MODEL_PROMPT_CACHING`, default
  `false`); when on it builds the model with `CacheConfig(strategy="auto")`, which
  places cache points over the system prompt and tool schemas. Off by default
  because it is a paid-tier behaviour and the offline suite must not depend on it —
  the adapter tests assert both the off (no `cache_config`) and on (`auto` strategy)
  states. `cache_prompt`/`cache_tools` string fields are deprecated in strands
  1.55.1; use `CacheConfig`.

## 4. Context engineering

What reaches the model is a designed artefact, not an accretion.

- **Retrieve, then answer — don't pre-load.** Fetch a fact when the turn needs it
  through a tool, rather than stuffing everything into the prompt on the chance it
  helps. Less irrelevant context is a better answer and a cheaper one.
- **The system prompt is data: reviewable, diffable, testable.** It lives in
  `prompts/system.md`, not an f-string, and is held to the same standards as code
  (no credential placeholder, trips no Forbidden_Claim pattern, instructs digits
  for numerals). Most of this service's safety behaviour is *asked for* there, so
  edits to it are reviewed as carefully as edits to `forbidden.py`.
- **A configured prompt or pattern set REPLACES the default, never extends it.**
  Extend-only configuration cannot remove a misfiring instruction; an operator
  needs to take one out, not pile another on. This matches Req 8.7 for forbidden
  patterns and the prompt loader's own contract.
- **Keep numerals as digits and provenance explicit.** The grounding verifier
  reads digits, so a spelled-out number bypasses it; a forecast names its
  provider and is never presented as a measurement. Context that carries a value
  carries where it came from.
- **Redact at the boundary, once.** The credential and tokens never enter a
  prompt, a log, or a tool schema; redaction is configured in one formatter, not
  re-decided per call site. New context that flows toward the model or the logs is
  checked against that boundary, not trusted to be clean.
- **Budget the window deliberately.** History windows are bounded
  (`_MAX_HISTORY_DAYS`); an unbounded fetch is a cost and a relevance problem.
  When a turn needs more context than fits, summarise or select — do not just
  enlarge the request.


## 5. Optimization roadmap — the six levers, and when

These are the cost/latency optimizations considered for the advisor, ranked and
sequenced. **The architecture that shapes the sequencing:** the advisor builds
**one `Agent` per turn, stateless**, because its tools close over that turn's
credential (Req 5.2 forbids a credential parameter). So there is no in-process
multi-turn conversation history today — which is exactly why some of the levers
below are "now" and others wait for a stateful-session design. Verify current SDK
support against the **Strands Agents MCP** and **Bedrock AgentCore MCP** servers
before implementing; the notes below were taken from them and the SDK moves.

Each optimization is its own config-gated, TDD change, off by default where it is
a paid-tier or behaviour-changing toggle, and the offline suite must pass with it
disabled. Never fold one silently into another edit.

### Doing now

1. **Prompt caching — DONE.** `CacheConfig(strategy="auto")` behind
   `AQM_ADVISOR_MODEL_PROMPT_CACHING` (default off). The win here is mostly
   *within a turn*: the agentic loop re-sends the ~1,700-token stable prefix
   (system prompt + tool schemas) on every tool round-trip, so caching pays even
   for a single user turn. The 5-minute cache TTL can also span rapid successive
   turns despite the per-turn agent. See §3.

2. **Tool executor — DONE, and it went the opposite way from the plan.**
   Investigating parallel tool calls revealed that Strands **already defaults to a
   `ConcurrentToolExecutor`**, and that our tools are **not** safe under it:
   `history` reads the `retrieved_site_code` that `air_quality` sets earlier in the
   same turn (and refuses to run without it), and the per-turn tools share a mutable
   `RetrievalRecorder` and `nonlocal` counters whose read-modify-write is not atomic.
   So the correct change was to **pin `SequentialToolExecutor` explicitly**
   (`Agent(tool_executor=...)`, behind `AQM_ADVISOR_TOOLS_CONCURRENT`, default off),
   turning an ordering assumption that held only by GIL luck into one that holds by
   construction. **True parallelism is deferred**, and earning it needs two things
   first: make the recorder thread-safe (a lock, or per-tool accumulation merged
   after), and break the `air_quality -> history` data dependency (e.g. let `history`
   resolve the site itself). Only then does flipping the flag to concurrent buy
   anything — and even then the two main tools are data-dependent, so the win is
   smaller than it first looked. The write tools (`profile_put`, `symptom_entry_put`)
   stay confirmation-gated and are never candidates for speculative parallelism.

3. **Context offloading — SOON, applies now.** A wide `history` window can return
   a large tool result that bloats the loop on every subsequent step within the
   turn. Strands' context-offloader keeps a compact preview in context and stores
   the full result externally. This applies even to a stateless per-turn agent
   because the bloat is *within* the loop, not across turns. Pairs with keeping
   `_MAX_HISTORY_DAYS` tight. **Trigger:** measured large tool results, or a turn
   that legitimately needs a long window.

### Doing later — when a trigger is met

4. **`service_tier` (flex/priority) — LATER, cheap when wanted.** Strands exposes
   `service_tier`; `"flex"` trades latency for lower cost, `"priority"` the
   reverse. The advisor is not hard-real-time, so `flex` is a clean cost lever —
   one config flag, no behaviour change to verify beyond a passthrough test.
   **Trigger:** a cost target, or a latency SLO that makes `priority` worth the
   premium. Deferred only because it is a knob, not a need, today.

5. **Streaming — LATER, perceived-latency not cost.** AgentCore Runtime supports
   response streaming (Strands `agent.stream_async` yielded from the
   `@app.entrypoint`), and the Bedrock model has a `streaming` flag. This improves
   how fast the **web chatbot** feels, not token cost, and it changes the
   response contract (SSE / chunked) end to end — the chatbot, the serving hop,
   and the advisor all have to agree. **Trigger:** a UX pass on the chatbot where
   time-to-first-token matters. Not before, because it touches three services.

6. **Conversation-history management (`context_manager="auto"` / summarizing) —
   LATER, blocked on a stateful design.** Strands ships one-line `auto` context
   management (offloading + summarization + proactive compression at 85%; their
   benchmark reports ~55% lower cost and higher accuracy). But it manages
   **carried conversation history**, which this advisor deliberately does not keep
   in-process — each turn is a fresh, stateless agent. So this lever has **little
   to offer until we adopt multi-turn session state** (carrying prior turns into
   the agent). **Trigger:** a decision to make the advisor stateful across a
   session; at that point `auto` is the default to reach for, with the summarizer
   pointed at a cheaper model. Until then, item 3 (offloading) covers the part
   that applies.

### Not on the list (yet): model routing

Routing trivial turns (greetings, off-topic declines that need no retrieval) to a
cheaper model (Haiku) and real advice to Sonnet is a real lever, but it adds a
routing decision and a second model path — complexity only justified at volume.
Revisit if turn volume grows enough that the cheap-turn fraction is material.
