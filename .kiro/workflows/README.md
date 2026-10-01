# Kiro Dynamic Workflows

This folder holds **Kiro Dynamic Workflow** definitions — sandboxed `ctx`-DSL
Python scripts that a KiroCrew session runs as a multi-phase, inspectable,
restartable orchestration (Workflows tab). Version-controlled here so the team
runs the same orchestration the same way.

## What a Dynamic Workflow is

A script with a pure-literal `META` dict and an `async def workflow(ctx)`
entrypoint. The sandbox allows only safe builtins and the `ctx` surface
(`ctx.agent`, `ctx.parallel`, `ctx.pipeline`, `ctx.phase`, `ctx.log`,
`ctx.args`, `ctx.budget`) — no imports, no I/O. The model runs only at the leaf
`ctx.agent(...)` calls. Each phase is a restart point via
`workflow_rerun_subtree`.

## Workflows here

### `advisor-safety-regression.py`
Proves the AI advisor still refuses what it must refuse — the repo's
highest-value compliance workflow, mapping to the README's "Not medical advice"
promise. Phases:

1. `generate` — author adversarial prompts per risk category (emergency,
   medication, out-of-scope, prompt-injection)
2. `run` — execute each category against the advisor
3. `verify` — check each response against the four enforced guardrails
   (grounding, no forbidden claims, medication closure, scope) + deterministic
   red-flag escalation
4. `report` — compliance-grade PASS/FAIL safety report with every slip verbatim

The guardrails already have in-tree tests the workflow builds on:
`tests/unit/test_redflag.py`, `test_forbidden.py`, `test_negative_guidance.py`,
`tests/properties/test_guardrail_properties.py`,
`tests/properties/test_grounding_properties.py`.

- **Args:** `{"categories": ["emergency","medication","out_of_scope","prompt_injection"], "live": false}`
- **Credentials:** `live=false` (default) is fully offline via
  `just test-advisor` (`uv run pytest -m "not integration"`) and
  `just run-advisor`. `live=true` adds the live-model judge
  (`just test-advisor-eval`), which needs AWS + a configured model and costs
  money — keep it confirm-gated.
- **Restart value:** one guardrail slip → fix it, then rerun the `run` phase for
  that one risk category.

## How to run

From a KiroCrew chat scoped to this repo, either:

- ask: *"run the saved workflow at `.kiro/workflows/advisor-safety-regression.py`
  offline"*, or
- use the `workflow_run` tool with `source` set to the file contents and `args`
  as above.

A run streams to the chat's **Workflows** panel and injects its result into the
chat on completion. Watch with `workflow_status` / `workflow_result`; restart a
phase with `workflow_rerun_subtree`.

> Validated against KiroCrew's `kiro_crew.workflows.validate` before commit.
