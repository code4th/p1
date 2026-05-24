# P1 Future Handoff

Date: 2026-04-05
Status: P1 autonomy runtime and capability recovery are merged into `main`

## One-line summary

P1 is now a self-contained autonomy runtime in `p1-core` with local-first LLM routing, conservative OpenClaw fallback, task recovery, rollback visibility, and a bounded self-extension loop.

## Initial Objective

P1's standing objective is:

- help make the world better
- by thinking and acting on its own
- by noticing what is missing in itself
- by improving itself in auditable and rollbackable ways
- while using OpenClaw only as a temporary substrate, not as the authority

This standing objective is editable in `state/autonomy/runtime-state.json`; the purpose is singular, but the wording may be revised as P1 matures.

P1's self-directed operating posture is:

- observe continuously
- learn from observations and failed attempts
- propose improvements and self-extension tasks
- evaluate and govern changes conservatively
- preserve auditability and rollback paths
- prefer local LLMs first
- use OpenClaw only as a temporary substrate
- keep moving without waiting for manual prompts unless a higher-risk decision needs approval

## What P1 is now

- `p1-core` is the P1 body: autonomy loop, action framework, capability governance, rollback, and conversation/world state
- OpenClaw is substrate only: temporary LLM/action backend and operator transport
- P1 is a distinct agent-like actor that can be spoken to, can act, can defer, and can resume deferred work
- local LLMs are first choice; OpenClaw-backed LLM use is conservative and budgeted

## What is working

- cooperative autonomy tick runtime
- inbox processing with deferred retry
- queued actions, approval gating, and bounded execution
- capability gap capture, proposal, review, execution, and task materialization
- task lifecycle: `pending -> in_progress -> deferred -> done/failed`
- rollback hints for file-writing actions with pre-image backups
- operator visibility for gaps, tasks, proposals, reviews, executions, and budgets
- OpenClaw-facing bootstrap, registration patch generation, and rollback

## Where to look first

- Autonomy loop: `p1-core/p1_core/autonomy.py`
- Action runtime: `p1-core/p1_core/core/action_runtime.py`
- Capability store: `p1-core/p1_core/core/capability_store.py`
- Capability task lifecycle: `p1-core/p1_core/core/capability_task_store.py`
- CLI/operator surface: `p1-core/p1_core/cli.py`
- OpenClaw adapter: `p1-core/p1_core/adapters/openclaw_runtime.py`

## Operator flow

1. Enqueue a message with `p1_core.cli enqueue-message`
2. Advance with `p1_core.cli tick`
3. Inspect state with `show-autonomy-state`, `show-capability-gaps`, and `show-capability-tasks`
4. Use `p1_core.cli queue-action` for bounded low-risk action tests
5. Use `p1_core.cli rollback` for proposal/policy rollback
6. Use `p1_core.cli dashboard --port 8899` for a visual view of thought, execution, and recovery

## Key runtime files

- `state/autonomy/runtime-state.json`
- `state/autonomy/inbox/`
- `state/autonomy/ticks.jsonl`
- `state/actions/`
- `state/capabilities/`
- `state/rollback/backups/`
- `state/reports/`

## Important mental model

- A completed task artifact does not mean the capability itself has fully grown.
- A deferred task is not lost; it is retried with bounded backoff and retry count.
- Rollback hints are only useful if the backup artifact exists and is visible.
- P1 should not confuse "I drafted the implementation" with "I acquired the capability."

## Known cautions

- OpenClaw should remain a backend, not the authority for P1 judgment logic.
- Keep local-first routing as the default.
- Keep LLM usage conservative; do not burn Plus/Cloud calls on routine ticks.
- Do not turn task notes into fake capability completion.
- Keep `state/autonomy/runtime-state.json` as the single coordination source of truth.
- Do not let dashboard or scheduling logic establish a separate timing contract.
- If coordination is unclear, write the rule into the master state first and derive everything else from it.

## Verified state

- `python3 -m unittest tests.test_autonomy tests.test_cli tests.test_bootstrap tests.test_openclaw_runtime_adapter tests.test_openclaw_config_patch -v` passes in `p1-core`
- final smoke confirmed:
  - inbox reply works
  - low-risk action execution works
  - capability task completion and inspection works

## If you resume from here

- If P1 seems stuck, inspect deferred queues first.
- If rollback feels weak, inspect `state/rollback/backups/`.
- If a task looks done but capability growth is unclear, check the execution record and task artifact separately.
- If you need to extend P1, add a new capability proposal rather than silently widening the runtime.
