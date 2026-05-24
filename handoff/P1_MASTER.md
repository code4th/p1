# P1 Master Document

Date: 2026-04-04
Status: Single source of truth for P1 external-core work

## Final Mainline Artifact

As of 2026-04-21, this file is the canonical P1 entry point.

Use this document first, then the code and operational docs below:

- code root: [p1-core](/Users/satojunichi/Documents/openclaw/p1-core)
- architecture: [architecture.md](/Users/satojunichi/Documents/openclaw/p1-core/docs/architecture.md)
- operator runbook: [p1-bootstrap-runbook.md](/Users/satojunichi/Documents/openclaw/p1-core/runbooks/p1-bootstrap-runbook.md)
- future handoff: [p1-future-handoff-2026-04-05.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p1/p1-future-handoff-2026-04-05.md)

Historical P1 notes remain useful as background, but they do not override this file.
When a scattered note conflicts with this master document, update this master document first.

Main-thread catch-up:

- [p1-main-thread-catchup-2026-04-04.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p1/p1-main-thread-catchup-2026-04-04.md)

## 0. Purpose Before Means

This document should be read purpose-first.

If a tool, runtime, worker, or interface choice conflicts with the intended purpose, the purpose wins and the means should be changed.

The user-facing purpose for P1 is:

- to help make the world better
- by thinking and acting on its own
- by noticing what is missing in itself
- by improving itself in auditable and rollbackable ways
- while using OpenClaw only as a temporary substrate, not as the authority

This master purpose is mutable, but the source of truth remains singular:

- `state/autonomy/runtime-state.json`
- if the purpose changes, it should change there first and everywhere else should derive from it

### 0.1 Intended Outcome

The intended outcome is not merely a set of modules.

The intended outcome is that:

- the manager can talk to P1 as a distinct agent alongside the main OpenClaw agent
- P1 can think with LLM-backed reasoning as its main loop
- P1 can observe, judge, propose, evaluate, and improve over time
- P1 can preserve memory, governance, comparison trails, and rollback paths
- P1 can continue to evolve without being trapped inside a single runtime

### 0.2 Intended Daily Experience

The intended day-to-day experience is:

- P1 can be talked to directly, but it does not exist only when spoken to
- P1 preserves continuity through its own state even when the manager is silent
- OpenClaw may still expose a P1-facing interface, but OpenClaw is only a temporary execution substrate
- P1 uses OpenClaw selectively for conversation, tool use, and operations when that is the practical path
- P1's long-horizon memory, governance, audit, and rollback remain outside OpenClaw

### 0.3 Layer Roles

The target layer split is:

- OpenClaw-side P1 agent interface
  - the front door
  - conversation, tool use, and practical runtime behavior
- external core
  - memory, policy, governance, audit, comparison, rollback
- local or cloud model providers
  - reasoning supply, not identity

### 0.4 Current Reading Rule

When deciding what to build next:

- prefer choices that make P1 easier to use as a separate OpenClaw-visible agent
- prefer choices that keep governance and rollback outside OpenClaw
- treat bootstrap wrappers and temporary CLIs as scaffolding, not the final product
- keep a single source of truth for runtime coordination: `state/autonomy/runtime-state.json`
- do not let dashboard, scheduler, or operator tooling invent their own timing contract
- if coordination becomes ambiguous, encode the resolution in the master state first, then implement from that state

Interface correction:

- the currently implemented external-core operator surface is useful, but it is not the final intended front-end
- the desired day-to-day interface is an OpenClaw-visible separate P1 agent, with the external core behind it as memory, governance, and rollback substrate

## 1. Purpose

This project is not primarily about building an AI framework.

Its top goal is to build a core loop through which an LLM can sustain long-term autonomous growth via:

- observation
- knowledge formation
- critique
- proposal
- evaluation
- governance
- self-modification

The intended long-term direction is an artificial-life-like agent substrate that is:

- runtime-independent
- model-independent
- OpenClaw-independent
- split between local-LLM auxiliary cognition and cloud-LLM high-quality judgment
- focused on growth in how knowledge is handled, not only on knowledge accumulation
- governed across short and long time scales
- auditable, comparable, and rollbackable in its self-modification

## 2. Non-Negotiable Direction

These are fixed and should not drift:

- OpenClaw is a temporary execution substrate
- OpenClaw must not become the growth kernel
- the core logic stays outside OpenClaw
- OpenClaw is control plane, not identity
- P1 is an independent individual that uses OpenClaw
- P1 should not begin by permanently occupying a process
- LLM usage should begin conservatively and prefer local models whenever practical
- local LLMs are auxiliary cognition
- cloud LLMs are for final/high-stakes judgment
- the first self-modified target is operational rules, not model weights
- all changes must remain comparable, reviewable, and rollbackable

Not adopted at this stage:

- full custom framework first
- deep OpenClaw surgery first
- frequent base-model weight updates
- unconstrained autonomous free modification
- internal OpenClaw agent spawning as the foundation

## 3. What P1 Is

P1 is not a persona living inside OpenClaw.

P1 is an independent growth agent with:

- its own conversation identity in the future
- an external core outside OpenClaw
- access to a local LLM auxiliary brain
- a minimal conversation surface through the external core
- explicit world-observation and world-action interfaces
- knowledge-state management
- critique logs
- operational rule change proposals
- long-term potential to become the central self-growing individual

## 4. Responsibility Split

OpenClaw owns:

- I/O
- tool execution
- OS interaction
- runtime robustness
- transport and presentation

P1 external core owns:

- knowledge state
- policy / critic / proposer / evaluator / governor
- cross-track judgment
- pre-approval review
- compression of research and operational outcomes

Hard prohibition:

- do not grow an independent policy engine inside OpenClaw-side adapter code
- do not re-implement P1 judgment inside `keeper_adapter`

## 4.5 Interface Direction Correction

The external-core-first implementation was useful as a bootstrap path, but the intended operator experience is different.

The desired steady-state shape is:

- P1 appears as a separate agent interface on the OpenClaw side
- P1 uses LLM-backed reasoning as its main conversational and operational loop
- OpenClaw is used as the practical runtime surface for that agent
- the external core remains the place for memory, governance, audit, comparison, and rollback

Therefore:

- `p1-core` should be treated as the institutional substrate behind P1
- OpenClaw-facing P1 interface work is now first-class, not optional polish
- local worker infrastructure is auxiliary, not the final identity of P1

## 5. Current Architecture

### 5.1 Main Components

- `p1-core/`
  - external core workspace
- `keeper_adapter/`
  - thin OpenClaw-side bridge
- `handoff/`
  - planning, constraints, operating rules, and supporting notes

Current interpretation:

- `p1-core/` is already a viable governance and memory substrate
- the next implementation focus is the living P1 runtime that sits on top of that substrate
- `bin/p1` is the current operator/runtime surface for that living runtime
- OpenClaw backend calls should be routed through adapter code, not spread across the runtime

### 5.2 Worker

Local Ollama-backed HTTP worker:

- `/summarize`
- `/classify`
- `/draft_lessons`
- `/health`

Key files:

- [ollama_worker.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/worker/ollama_worker.py)
- [service.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/worker/service.py)
- [ollama_client.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/worker/ollama_client.py)

### 5.2.1 Local Model Role Split

The current local-model direction is now clearer from real-machine verification on:

- Apple M5
- 32 GB RAM
- Ollama 0.20.2

The practical split should be:

- lightweight local model
  - flexible judgment beyond fixed `if` statements
  - tagging, coarse classification, candidate extraction, log shaping, cheap routing
- medium local model
  - background summarization, counterexample enumeration, rough lesson drafting, nightly batch review
- heavy local model
  - slow background analysis, audit passes, proposal comparison, deferred-case re-review

Operational interpretation:

- local heavy models are not the main conversational brain
- local heavy models are suitable as asynchronous background cognition when latency is acceptable
- cloud models remain the final layer for high-stakes promotion or institutional change

### 5.2.2 Verified Ollama Model Findings

Real-machine checks in this fork validated the following:

- `qwen3:4b-instruct`
  - currently the strongest default candidate for fast auxiliary cognition
- `gemma4:e4b`
  - runs on this machine and is viable for slower background work
- `gemma4:26b`
  - also runs on this machine and loads at `100% GPU`
  - should be treated as a background-analysis model, not an interactive default

Observed direct strict-JSON summarize timings:

- `qwen3:4b-instruct`: about 3.9s
- `gemma4:e4b`: about 10.7s
- `gemma4:26b`: about 38.7s

Observed worker-task timings with an expanded timeout:

- `qwen3:4b-instruct`
  - `summarize`: about 8.1s
  - `classify`: about 144.8s
  - `draft_lessons`: about 66.3s
- `gemma4:e4b`
  - `summarize`: about 52.5s
  - `classify`: about 63.4s
  - `draft_lessons`: about 60.2s
- `gemma4:26b`
  - `summarize`: about 35.7s
  - `classify`: about 106.8s
  - `draft_lessons`: timed out at 180s

Immediate consequence:

- current worker/CLI timeout assumptions are tuned for smaller models and should not be treated as a stable contract for background cognition
- heavy local models are still useful, but only when scheduled as queue-backed or batch-style work

### 5.2.3 Implementation Direction From These Findings

Main-thread work should treat local-model usage as three classes:

- `fast_judge`
  - low-latency local routing and flexible rule-like judgment
  - default candidate: `qwen3:4b-instruct`
- `background_analysis`
  - asynchronous lesson drafting, counterexample scans, deferred-case review, audit passes
  - candidate models: `gemma4:e4b`, `gemma4:26b`
- `cloud_decision`
  - final institutional approval, promotion, rejection, or policy change

Important:

- do not force heavier local models through the same synchronous interaction path as the fast auxiliary model
- instead, give them background-job semantics with queueing, longer time budgets, and delayed result pickup

Implemented minimum path:

- `p1_core.cli ingest --background-model ...`
  - runs fast judgment immediately
  - queues heavy local analysis under `state/background_jobs/queue/`
- `p1_core.cli run-background-job --job-id ... --model ...`
  - completes queued heavy analysis
  - runs lesson extraction and downstream proposal / governance work after the queue step

### 5.3 Bootstrap

External workspace scaffolding:

- `profile.json`
- `config.json`
- `prompt.md`
- `runbook.md`
- `state/reports/`
- `state/knowledge/`
- `state/policies/`
- `state/proposals/`
- `state/conversation/`
- `state/world/`
- `state/archive/`
- `logs/`

Key file:

- [bootstrap_p1.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/bootstrap/bootstrap_p1.py)

### 5.4 Core Modules

- event log
- knowledge store
- policy engine
- policy store
- critic
- proposer
- evaluator
- governor
- governance store
- experiment runner
- proposal store

Key files:

- [knowledge_store.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/knowledge_store.py)
- [proposal_store.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/proposal_store.py)
- [policy_engine.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/policy_engine.py)
- [policy_store.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/policy_store.py)
- [critic.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/critic.py)
- [evaluator.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/evaluator.py)
- [governor.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/governor.py)
- [governance_store.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/governance_store.py)
- [experiment_runner.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/experiment_runner.py)

### 5.5 Growth Loop

The current minimal growth loop already performs:

- lesson extraction
- classification
- summarization
- candidate knowledge persistence
- state transitions across `candidate / deferred / active / retired`
- proposal snapshot creation
- proposal comparison against previous snapshot
- governance review
- rollback of proposal snapshots
- policy-state application and rollback
- governance-profile loading
- bounded autonomous experiment execution
- report generation for bridge consumption

Key file:

- [growth_loop.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/pipeline/growth_loop.py)

Outputs:

- `state/knowledge/knowledge.jsonl`
- `state/events/event-log.jsonl`
- `state/proposals/latest-proposals.json`
- `state/proposals/snapshots/*.json`
- `state/policies/latest-policy.json`
- `state/policies/snapshots/*.json`
- `state/governance/latest-governance.json`
- `state/experiments/latest-experiment.json`
- `state/experiments/actions/*.json`
- `state/conversation/transcript.jsonl`
- `state/world/observations.jsonl`
- `state/world/action-requests.jsonl`
- `state/reports/daily/*-glance.json`
- `state/reports/daily/*-daily.json`
- `state/health.json`

### 5.6 Autonomy Runtime v1

The newly added direction is a first non-blocking autonomy runtime inside `p1-core`.

It currently means:

- P1 keeps autonomy state under `state/autonomy/`
- P1 can receive queued messages through an inbox and answer them during a short `tick`
- P1 can queue and execute minimal low-risk actions through its own action framework
- P1 does not start as an always-on resource-owning daemon
- P1 decides its own `next_wake_at`, but an external scheduler is still expected to trigger `tick_once`
- LLM routing is local-first and budget-aware
- OpenClaw-backed Plus use must remain conservative and should not be the default path for every tick

New runtime outputs:

- `state/autonomy/runtime-state.json`
- `state/autonomy/ticks.jsonl`
- `state/autonomy/inbox/`
- `state/actions/queue/`
- `state/actions/history/`
- `state/budgets/llm-usage.json`

## 6. Current State Logic

Knowledge states:

- `raw`
- `candidate`
- `deferred`
- `active`
- `retired`

Current minimal routing:

- contradicted or high-impact proposal -> `deferred`
- new bounded proposal without counterexamples -> `active`
- obsolete or duplicate-against-previous-snapshot proposal -> `retired`

This is still heuristic and not the final governance quality target.

## 7. What Counts As Core Completion

P1 core is **not** complete merely because it can propose and compare.

For this project, core completion means all of the following:

- observation, knowledge formation, critique, proposal, evaluation, governance, and self-modification candidate handling all close inside the external core
- knowledge state is managed across `raw / candidate / deferred / active / retired`
- proposals and knowledge state are comparable, auditable, and rollbackable
- high-risk changes are approval-gated
- low-risk improvements can be executed autonomously
- small experiments can be run by the core itself
- experiment outcomes feed back into later updates
- the agent can converse through the external core
- the agent can observe and request bounded action toward the external world
- the agent can maintain a non-blocking autonomy state and advance itself without waiting for explicit manager instructions

Therefore, the current state is:

- minimal external core skeleton: complete
- bounded autonomous self-improvement core: complete as a minimum operational loop
- living autonomy runtime: started but not complete

## 8. Practical Operations Today

Current operational entrypoints:

1. Read P1 status
   - `cd /Users/satojunichi/Documents/openclaw`
   - `python3 -m keeper_adapter.cli status`
   - or `cd /Users/satojunichi/Documents/openclaw/p1-core && python3 -m p1_core.cli status`
   - or `/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1 status`

2. Read detailed report
   - `python3 -m keeper_adapter.cli report --kind daily`

3. Read approval-pending items
   - `python3 -m keeper_adapter.cli approvals`
   - or `python3 -m p1_core.cli approvals`

4. Advance P1 core
   - `cd /Users/satojunichi/Documents/openclaw/p1-core`
   - `python3 -m p1_core.pipeline.growth_loop --root /Users/satojunichi/.openclaw/workspace/systems/p1 --input-text "example observation"`
   - or `python3 -m p1_core.cli ingest --model qwen3:4b-instruct --input-text "example observation"`

5. Roll back proposal state
   - `python3 -m p1_core.pipeline.growth_loop --root /Users/satojunichi/.openclaw/workspace/systems/p1 --rollback-snapshot-id 2026-04-04-proposals`

6. Roll back policy state
   - `python3 -m p1_core.pipeline.growth_loop --root /Users/satojunichi/.openclaw/workspace/systems/p1 --rollback-policy-snapshot-id baseline-policy`

7. Inspect unified external-core state
   - `python3 -m p1_core.cli state`

8. Advance one autonomy tick without keeping a daemon alive
   - `python3 -m p1_core.cli tick`
   - `python3 -m p1_core.cli show-autonomy-state`
   - `python3 -m p1_core.cli show-capability-gaps`

9. Queue a message for the living P1 runtime
   - `python3 -m p1_core.cli enqueue-message --content "What do you think about the latest state?"`

10. Talk with P1
   - `python3 -m p1_core.cli chat --model qwen3:4b-instruct --message "What do you think about the latest state?"`
   - or `python3 -m p1_core.cli enqueue-message --content "What do you think about the latest state?"`
   - then `python3 -m p1_core.cli tick`

11. Record a world observation
   - `python3 -m p1_core.cli observe --text "A tool run failed during retrieval."`

12. Queue a bounded world action request
   - `python3 -m p1_core.cli action --kind note --payload "prepare a bounded follow-up action"`
13. Queue a low-risk autonomy action
   - `python3 -m p1_core.cli queue-action --kind append_note --inputs '{"content":"autonomy note"}'`
14. Scaffold OpenClaw-facing registration without mutating global config automatically
   - `python3 -m p1_core.bootstrap.install_openclaw_agent --openclaw-home /Users/satojunichi/.openclaw --workspace-root /Users/satojunichi/.openclaw/workspace/systems/p1 --agent-name p1 --source-agent main`
   - `python3 -m p1_core.bootstrap.generate_openclaw_config_patch --openclaw-home /Users/satojunichi/.openclaw --workspace-root /Users/satojunichi/.openclaw/workspace/systems/p1 --agent-name p1`
15. Apply or roll back OpenClaw registration through the external bootstrap layer
   - `python3 -m p1_core.bootstrap.apply_openclaw_config_patch --config-path /Users/satojunichi/.openclaw/openclaw.json --workspace-root /Users/satojunichi/.openclaw/workspace/systems/p1 --agent-name p1`
   - `python3 -m p1_core.bootstrap.apply_openclaw_config_patch --config-path /Users/satojunichi/.openclaw/openclaw.json --agent-name p1 --rollback`
   - timestamped backups are created before mutation
   - `openclaw.json` keeps only a minimal slot registration; P1 identity details remain in the external workspace and `~/.openclaw/agents/p1/agent/p1-openclaw-entry.json`

Current limitation:

- P1 now has a direct temporary front door through `bin/p1` and a dedicated OpenClaw-facing wrapper through `bin/p1-agent`
- P1 can scaffold `~/.openclaw/agents/p1/` and apply or roll back `openclaw.json` registration through explicit external scripts
- registration is still an operator action, not an internal OpenClaw self-spawn
- `openclaw agent --agent p1` currently runs as an OpenClaw embedded agent against the P1 workspace; it does not yet route through `bin/p1-agent`, so direct OpenClaw turns do not automatically append to `state/conversation/` or `state/world/`
- until a deeper adapter exists, the verified external-core-backed entrypoint is `bin/p1` for autonomy and `bin/p1-agent` for status/report/approval transport
- OpenClaw still remains a thin control plane and should not absorb P1 judgment logic
- the new autonomy runtime is still cooperative rather than always-on, and OpenClaw-backed Plus use is intentionally conservative and not yet wired as the default deliberation lane
- capability gaps are now deduplicated semantically and recorded under `state/capabilities/gaps.jsonl`
- capability gaps now also yield first-pass self-extension proposals under `state/capabilities/proposals.jsonl`
- capability proposals now receive first-pass evaluation/governance reviews and can queue cloud approval requests under `state/capabilities/cloud_evaluation/requests/`
- approved capability proposals are first turned into bounded self-extension tasks, not direct unrestricted self-rewrites
- capability execution is deduplicated per proposal and reconciled after the queued task completes or fails, with rollback hints retained in `state/capabilities/executions.jsonl`
- deferred inbox items and deferred actions are now retried through explicit deferred queues instead of being silently dropped or permanently blocking the head of the queue
- deferred retries now stop after a bounded retry limit instead of looping forever on the same failure
- approved capability work is now materialized as a bounded implementation task artifact under `state/capabilities/tasks/`, not only as a free-form note
- the autonomy surface can inspect `state/capabilities/tasks/` and pick up pending bounded implementation tasks as first-class state
- bounded implementation tasks now move through `pending -> in_progress -> done/failed` instead of lingering as a single static note
- capability tasks also preserve a `deferred` branch and can be requeued after retry time, so deferred work does not disappear from the agent's perspective
- the operator task surface now shows both pending tasks and full task history for auditability
- task completion remains distinct from capability completion; completed task artifacts are visible, but they are not themselves proof that the underlying capability has grown
- failed or deferred task attempts now preserve a reason and keep execution/task state aligned instead of treating every pause as a failure
- file-writing actions preserve pre-image backups under `state/rollback/backups/` so rollback hints point to real recovery material
- the new `openclaw_backend` config block is an explicit opt-in adapter contract, not a signal to move default cognition away from local-first routing

## 9. Rollback Principles

Always preserve:

- logs
- counterexamples
- deferred items
- comparison trails

Operational rollback:

1. stop the worker
2. restore a proposal snapshot
3. confirm `latest-proposals.json` points at the restored snapshot
4. confirm `keeper_adapter.cli status` and `report --kind daily` reflect rollback state
5. archive failed artifacts only after restored state is confirmed

## 10. Verified So Far

Verified in implementation:

- `p1-core` unit tests passing
- cooperative autonomy runtime and OpenClaw backend adapter unit tests passing
- bootstrap scaffolding works
- worker contract works
- growth loop writes knowledge / proposal / report / event outputs
- `deferred` transitions work
- `active` promotion works for bounded counterexample-free proposals
- `retired` works for obsolete and duplicate-against-previous-snapshot proposals
- governance review is written into snapshots and daily reports
- rollback updates proposal latest pointer and bridge-visible state
- cloud review `approve` / `reject` responses are applied during governance
- approved proposals can mutate versioned policy state under `state/policies/`
- policy snapshots can be rolled back through a current-pointer restore path
- evaluator / governor now read a long-horizon governance profile from `state/governance/`
- short-horizon and long-horizon governance layers are written into daily reports
- a unified operator CLI now exposes ingest / status / approvals / state / rollback from `p1-core`
- an end-to-end lifecycle test now verifies ingest -> approval/policy apply -> operator visibility -> rollback
- the lifecycle acceptance path now covers both policy rollback and proposal rollback through the operator surface
- real Ollama verification has been completed with `qwen3:4b-instruct` through both `p1_core.cli` and the HTTP worker
- low-risk autonomous proposals can now execute a bounded external file action under `state/experiments/actions/`
- prior experiment outcomes now defer reruns until reviewed
- experiment feedback is now accumulated into long-horizon governance and can freeze low-risk autonomy after repeated rerun deferrals
- end-to-end acceptance now covers governance feedback changing a later operator-visible decision
- conversation transcript is persisted under `state/conversation/`
- world observations and queued action requests are persisted under `state/world/`
- `keeper_adapter` reads `glance / daily / approvals` from generated outputs
- `openclaw.json` registration for `p1` can now be applied and rolled back with backup files through external bootstrap scripts
- a non-blocking autonomy runtime now persists `state/autonomy/`, `state/actions/`, and `state/budgets/`
- autonomy can process queued inbox messages and low-risk queued actions without a permanently running daemon
- local-first LLM routing is now enforced for that autonomy runtime

## 11. Remaining Work

The next meaningful work is now:

- extending the autonomy runtime beyond inbox reply and low-risk local actions
- wiring OpenClaw as a true backend adapter rather than only a separate surface
- teaching P1 to promote capability gaps into self-extension proposals
- keeping Plus usage conservative while still making OpenClaw-backed deliberation available for high-value cases

## 12. Supporting Documents

These are now supporting docs, not the primary entrypoint:

- [p1-manager-handoff-source-2026-04-04.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p1/p1-manager-handoff-source-2026-04-04.md)
- [p1-canonical-handoff-2026-04-04.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p1/p1-canonical-handoff-2026-04-04.md)
- [p1-external-core-plan-2026-04-04.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p1/p1-external-core-plan-2026-04-04.md)
- [p1-openclaw-bridge-spec-2026-03-30.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p1/p1-openclaw-bridge-spec-2026-03-30.md)
- [p1-openclaw-operating-rule-2026-03-29.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p1/p1-openclaw-operating-rule-2026-03-29.md)
- [p1-keeper-handoff-2026-03-29.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p1/p1-keeper-handoff-2026-03-29.md)
- [p1-bootstrap-runbook.md](/Users/satojunichi/Documents/openclaw/p1-core/runbooks/p1-bootstrap-runbook.md)
