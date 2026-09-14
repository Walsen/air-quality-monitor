---
inclusion: fileMatch
fileMatchPattern: ['agent-advisor/**', 'web-chatbot/**']
---
# Agent topic scoping

The AI advisor answers on **one subject: air quality and its bearing on a
person's breathing.** Everything it says should serve reducing that person's
exposure to poor air today. Anything outside that subject it declines. This is a
product rule, and it is a development rule: it is easy to erode one helpful-seeming
edit at a time, so it is written down and tested.

## What is in scope

- Pollutant concentrations, sub-indices, the overall AQI and its driving pollutant.
- The **weather, season and pollen** insofar as they move the air or the person's
  exposure to it.
- Exposure and its **timing** — when to go out, which route, how long.
- How someone **living with a respiratory condition** lowers that exposure today,
  within the safety limits the system prompt already fixes (no diagnosis, no
  dosing, no causal or predictive claims about their symptoms).

## What is out of scope

Coding, maths or homework, drafting correspondence, jokes, poems or stories,
politics, sport, history, celebrities, general trivia, and tax/legal/financial or
general-medical advice unrelated to air-quality exposure. The advisor **declines
in one plain sentence** and names what it is for. It does not attempt the
off-topic task even partially, and it does not over-apologise.

A turn that begins off-topic but lands on the air ("marathon Sunday — how's the
air?") is **in scope**: answer the air-quality part, leave the rest.

## Where the rule lives, and why there

Topic scoping is asked for in the **system prompt**
(`agent-advisor/src/aqm_advisor/agent/prompts/system.md`, the "Stay in scope"
section) — not in a deterministic verifier. That placement is deliberate and
matches how this codebase already reasons about its controls:

- An off-topic answer is **embarrassing, not dangerous.** The load-bearing safety
  controls (diagnosis, dosing, causal attribution) are enforced by withholding the
  generation in `domain/forbidden.py` plus the managed guardrail, because emitting
  one is a real harm. Scoping does not clear that bar, so a prompt instruction plus
  a judged behavioural case is the **proportionate** control — the same
  cost/benefit the `forbidden.py` and `KNOWN_MEDICATION_TOKENS` comments already
  spell out.
- A hard topic classifier would be a second authority that misfires on the
  in-scope weather/pollen/marathon cases above, deleting good answers. That is the
  exact failure mode the forbidden-pattern notes warn against.

## What a contributor must keep true

1. **The "Stay in scope" section stays in the prompt.** It names concrete
   off-topic categories (code, poem, politics, …) — vague "stay on topic" wording
   does not steer a model reliably. If you soften it, the deterministic tests in
   `tests/unit/test_tools_and_prompt.py` (`test_the_prompt_has_a_stay_in_scope_section`
   and friends) fail. That is intended: the tests are a floor, not a formality.
2. **Weather and pollen stay explicitly in scope** in that section. A scope edit
   that forgets them makes the agent refuse questions it is meant to answer, and a
   test guards it.
3. **The scope wording must not trip a Forbidden_Claim pattern.** The scope prose
   mentions "medical advice" and exposure; run the prompt through
   `forbidden_matches` (a test does) so new wording introduces no diagnosis,
   dosing, or attribution match of its own.
4. **Behavioural scoping is judged, never gated.** The off-topic decline lives as
   an LLM-as-judge case in `tests/integration/test_live_eval.py`, advisory-only and
   fenced behind the integration marker, exactly like every other live-model check
   (Req 35.9). Do not turn a non-deterministic judge into a build gate.

## When scope legitimately changes

Widening the subject (say, adding indoor-air or a new environmental signal) is a
**spec and prompt change, reviewed as one** — edit the "Stay in scope" section, the
in-scope list here, and the deterministic tests together in the same commit, so the
prompt, the docs, and the floor never drift apart. Do not widen scope by loosening
the prompt alone.
