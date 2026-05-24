# P1 / OpenClaw Bridge Spec

Date: 2026-03-30

This document fixes the first adapter contract between the OpenClaw-side operator entrypoint and the P1 Keeper runtime.

## Goal

Expose P1 to OpenClaw as a thin bridge without re-implementing judgment logic in OpenClaw.

## Transport

Initial transport is intentionally simple:

- file-based report reads
- explicit script invocation for commands

OpenClaw reads:

- `/Users/satojunichi/.openclaw/workspace/systems/p1/state/reports/daily/*-glance.json`
- `/Users/satojunichi/.openclaw/workspace/systems/p1/state/reports/daily/*-daily.json`
- `/Users/satojunichi/.openclaw/workspace/systems/p1/state/health.json`

OpenClaw invokes:

- `/Users/satojunichi/.openclaw/workspace/systems/p1/intervene.js`

## Commands

The adapter exposes:

- `status`
- `report`
- `approvals`
- `intervene`
- `approve`
- `reject`
- `rollback`
- `risk`

## Risk Tiers

- `read_only`
  - report reading only
- `bounded_operational`
  - `continue`
  - `stop`
  - `priority change`
  - `weight change`
  - `skepticism up/down`
  - `exploration up/down`
  - `stability up/down`
- `approval_required`
  - `approve`
  - `reject`
  - `rollback`
  - unknown commands by default

## Responsibility Boundary

- OpenClaw presents and transports
- P1 interprets judgment and approval meaning
- Manager remains the final approval authority

## Explicit Prohibitions

The OpenClaw-side adapter must not:

- duplicate P1 scoring logic
- duplicate P1 tuning or repair policy
- grow an independent policy engine in meta-loop
- reinterpret research handoffs independently of P1

## Run Examples

```bash
cd /Users/satojunichi/Documents/openclaw
python3 -m keeper_adapter.cli status
python3 -m keeper_adapter.cli report --kind daily
python3 -m keeper_adapter.cli approvals
python3 -m keeper_adapter.cli intervene "skepticism up"
python3 -m keeper_adapter.cli approve new_monitor_target
python3 -m keeper_adapter.cli risk "rollback last_change"
```
