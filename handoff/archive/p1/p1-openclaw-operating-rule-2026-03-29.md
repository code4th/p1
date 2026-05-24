# OpenClaw / P1 Operating Rule

Date: 2026-03-29

This note fixes the working boundary between the live OpenClaw execution layer and the P1 integration layer.

## Decision

- Keep `~/.openclaw/workspace` as the live execution substrate.
- Keep `systems/p1` as the single integration window for Manager.
- Do not continue independent policy-engine growth inside `meta-loop`.
- Keep execution hardening inside OpenClaw itself.

## Role Split

### OpenClaw Owns

- live loop execution
- collection pipelines
- browser / CDP runtime behavior
- cron scheduling and run plumbing
- delivery pipeline behavior
- runtime fallback, retry, timeout handling
- execution-time self-repair and robustness fixes

Examples:

- source-specific fallback
- timeout guard tuning
- session id collision fixes
- format repair for summaries used by existing execution scripts

### P1 Owns

- cross-track judgment
- priority decisions
- approval gates
- repair-vs-growth policy
- research sandbox ingestion
- bounded tuning policy
- compressed Manager reporting

Examples:

- whether recurring timeout is drift or degradation
- whether a sandbox is report-only or promotion-ready
- whether a structural live-loop mutation must wait for approval
- how research handoffs are summarized across tracks

### Meta Loop Owns

- adapter behavior only
- reading P1 outputs and relaying them to existing tooling

The following are explicitly out of scope for future `meta-loop` growth:

- independent escalation policy evolution
- independent model-shift policy beyond adapter use
- duplicate research-handoff reasoning
- duplicate approval-gate logic

## Working Rule

Use this decision rule going forward:

1. If the issue is breaking live execution right now, fix it in OpenClaw.
2. If the issue is prioritization, approval, sandbox interpretation, or cross-track coordination, route it to P1.
3. If the issue is a summary or recommendation for existing meta tooling, read from P1 instead of re-implementing policy in `meta-loop`.

## Manager Rule

- Manager can continue to talk to either side during transition.
- New integration instructions should be treated as P1 work by default.
- New live execution fixes should be treated as OpenClaw work by default.

## Safety Rule

- OpenClaw runtime robustness changes do not require P1 approval if they do not change external action scope.
- Structural coupling between research tracks and live execution still requires explicit approval through P1.
