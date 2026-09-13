# AI Advisor Agent (Service 3)

The advisor turns a signed-in user's utterance into an `Advisory_Response`: it
retrieves that user's air quality, history, and health profile from Service 2,
asks a language model for guidance, and then verifies that guidance before it
leaves the process. It is a non-diagnostic assistant — every response carries a
disclaimer, quantitative claims must be values that were actually retrieved, and
anything that reads as an emergency escalates deterministically before the model
is ever consulted.

The deployment target is Amazon Bedrock AgentCore Runtime. The advisory logic
itself imports no AWS SDK: every I/O boundary (the model, the Serving_Client,
the guardrail, the audit store, the clock) is a port with a local adapter, so
the whole offline suite runs with no credentials and no network beyond
localhost.

> **Not medical advice.** This service produces general air-quality guidance,
> not a diagnosis, prescription, or dosing instruction. Every response carries a
> non-diagnostic disclaimer.

## Commands

Every command is a `just` recipe in the repository-root `Justfile`, so a
contributor and CI invoke the same code path. Run them from the repository root.

| Recipe | What it does |
|---|---|
| `just test-advisor` | The single documented offline test command. Runs `pytest -m "not integration"` — no AWS, no network beyond localhost. |
| `just test-contracts-advisor` | The per-port contract suite alone (`pytest -m contract`). Part of the offline suite; run alone while adding an adapter. |
| `just test-integration-advisor` | The fenced checks (`pytest -m integration`): a live model or guardrail, or a container engine. |
| `just test-advisor-eval` | The live evaluation harness alone (the live-model smoke check and the LLM-as-judge). Skips cleanly with no live model configured. |
| `just test-advisor-nightly` | Every property at 1000 examples (`AQM_HYPOTHESIS_PROFILE=nightly`), offline. |
| `just lint-advisor` | `ruff check .` |
| `just fmt-advisor` | `ruff check --fix .` then `ruff format .` |
| `just typecheck-advisor` | `mypy` |
| `just run-advisor` | Serves `POST /invocations` and `GET /ping` on `:8080` with no AWS involvement. |
| `just synth-advisor-infra` | Synthesizes the advisor's AgentCore CDK stack offline: no account, no Docker daemon. |

## Routes

The service exposes exactly two routes, served by the AgentCore app
(`aqm_advisor.agentcore.app`):

| Method | Path | Body |
|---|---|---|
| `POST` | `/invocations` | The request is an `Advisory_Request`; the response body **is** the `Advisory_Response` (there is no second wire shape). |
| `GET` | `/ping` | The runtime health probe. |

## Configuration

Every value resolves environment first, then the config file, then the default
below. All environment variables carry the `AQM_ADVISOR_` prefix except the
shared Cognito client id, which is deliberately the same variable Service 2
reads. An empty **Default** cell means the value has no default: it is either
required (`serving_base_url`) or unset until configured.

| Env var | Default | Meaning |
|---|---|---|
| `AQM_ADVISOR_SERVING_BASE_URL` | | **Required.** Service 2's base URL; the service can retrieve nothing without it. Must be an absolute http/https URL with no embedded credential. |
| `AQM_ADVISOR_REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout when calling Service 2. |
| `AQM_ADVISOR_MAX_UTTERANCE_LENGTH` | `4000` | The effective ceiling on an inbound utterance's length. |
| `AQM_ADVISOR_LOCALE` | `en` | The response locale. |
| `AQM_ADVISOR_MODEL_ID` | | The model identifier. In production this is the `us.anthropic.claude-sonnet-4-6` cross-region inference profile — the `us.` prefix is mandatory, as the bare id has no in-region on-demand support in any US region. |
| `AQM_ADVISOR_MODEL_REGION` | | The region the model is invoked in. |
| `AQM_ADVISOR_MODEL_TEMPERATURE` | `0.0` | Sampling temperature, between `0.0` and `2.0`. |
| `AQM_ADVISOR_MODEL_MAX_OUTPUT_TOKENS` | | The provider's per-call output cap, when set. |
| `AQM_ADVISOR_MODEL_CREDENTIAL_PATH` | | Optional credential path for the model adapter. When absent, the SDK's own credential chain is used (on AgentCore, the execution role). |
| `AQM_ADVISOR_MAX_MODEL_INVOCATIONS` | `2` | The per-turn model-invocation ceiling: one generation plus one repair attempt after a guardrail rejection. |
| `AQM_ADVISOR_MAX_OUTPUT_TOKENS` | | Soft per-turn output-token ceiling, when set. |
| `AQM_ADVISOR_MAX_TOTAL_TOKENS` | | Soft per-turn total-token ceiling, when set. |
| `AQM_ADVISOR_MAX_SERVING_CALLS` | `4` | The per-turn Serving_Client call budget. |
| `AQM_ADVISOR_MAX_PRIOR_TURNS` | `6` | How many of the most recent prior turns are kept as context. |
| `AQM_ADVISOR_GUARDRAIL_ENABLED` | `false` | Whether the Bedrock `ApplyGuardrail` check is enabled. When true, a `guardrail_identifier` is required. |
| `AQM_ADVISOR_GUARDRAIL_IDENTIFIER` | | The Bedrock guardrail identifier, required when the guardrail is enabled. |
| `AQM_ADVISOR_GUARDRAIL_VERSION` | | The Bedrock guardrail version. |
| `AQM_ADVISOR_EMERGENCY_GUIDANCE_FALLBACK` | | The only locally configurable envelope value: the emergency guidance used when Service 2's copy is unreachable. |
| `AQM_ADVISOR_SYSTEM_PROMPT_PATH` | | Path to the system prompt file. |
| `AQM_ADVISOR_LOG_LEVEL` | `info` | The log level; one of `debug`, `info`, `warning`, `error`, `critical`. |
| `AQM_ADVISOR_STREAMING_ENABLED` | `false` | SSE streaming. Not implemented; enabling it is refused at startup. |
| `AQM_ADVISOR_TURN_BUDGET_SECONDS` | `60` | The wall-clock budget for one turn; must be at least `request_timeout_seconds`. |
| `AQM_ADVISOR_JWT_DISCOVERY_URL` | | The OIDC discovery URL for the inbound authorizer. |
| `AQM_COGNITO_CLIENT_ID` | | The JWT audience the inbound authorizer accepts. Shares Service 2's variable on purpose, so the two authorizers cannot drift apart. |

Three sequence-valued settings — `forbidden_patterns`, `red_flag_rules`, and
`jwt_allowed_clients` — are file-only (no environment variable), because an
environment variable would need a separator convention this loader does not have.

## Adapters

Each I/O port selects a named adapter. The per-port environment variable is
`AQM_ADVISOR_<PORT>` (upper-cased), or the port key in the config file. The
**first** name in each row is the default, and it is always the offline one, so
the documented test command passes on a clean checkout.

| Port (`AQM_ADVISOR_<PORT>`) | Registered adapters (default first) |
|---|---|
| `serving_client` | `scripted`, `http` |
| `guardrail_checker` | `local`, `bedrock` |
| `advice_audit_store` | `memory`, `dynamodb` |
| `association_trigger` | `recording` |
| `model` | `scripted`, `bedrock` |
| `clock` | `system`, `fixed` |

## Tool set

The model retrieves data by requesting tools rather than being handed a
pre-stuffed prompt. There are exactly five, all over the Serving_Client, each
built per turn and closed over that turn's credential (the credential is never a
tool parameter):

- `air_quality` — current air quality for the user's saved locations (at most
  once per turn).
- `history` — the user's recent readings over a window of whole days.
- `profile_get` — the user's health profile, including any recorded medications.
- `profile_put` — update the profile with fields the user has confirmed.
- `symptom_entry_put` — record a symptom diary entry the user has confirmed.

## Guardrail and safety posture

The advisory turn is a fixed step order — red-flag check, retrieve, generate,
verify, assemble, audit — and safety lives at several points in it:

- **Deterministic escalation runs first.** The red-flag check is
  model-independent and runs before any retrieval or generation, so it still
  fires when both Service 2 and the model are unavailable. Either the
  deterministic matcher or the model reporting a red flag is sufficient to
  escalate, but the deterministic matcher is authoritative.
- **Dual guardrail enforcement.** Output is checked at two independent points:
  a local pattern/closure check (always on, offline) and an optional Bedrock
  `ApplyGuardrail` call (`guardrail_checker=bedrock`, `guardrail_enabled=true`).
  Neither is conditional on the other — the local check covers the offline and
  Bedrock-unreachable cases, and the managed check catches phrasings no pattern
  anticipated.
- **Grounding is a decidable numeral comparison,** never a similarity score:
  every quantitative claim in the guidance must be a numeral that was actually
  retrieved. An invented number is rejected.
- **Forbidden-claim and medication-closure checks** run in the verify step: no
  diagnosis, no dosing, and no medication named outside the user's stored list.
- **Repair, then degrade.** A failing verification gets one repair attempt; if
  it still fails, the turn returns a degraded response assembled from retrieved
  data rather than an unverified generation. The turn is audited either way.

The offline suite uses no live model: the scripted Model adapter and local
guardrail fake exercise the whole pipeline without a network call.
