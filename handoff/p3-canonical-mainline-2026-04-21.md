# P3 Canonical Mainline

Date: 2026-04-21
Version: `0.3.0-mainline`

## Status

This document defines the current P3 mainline for future P4 development.

Use `p3-core/` as the source of truth. Older P3 notes, previous `/tmp` workspaces, dashboard logs, and experiment writeups are historical context, not competing versions.

This is the single P3 entry point for the completed P3 baseline. If another P3 note disagrees with this file, treat that note as historical until this file is updated.

## Canonical Code Root

- Code: `p3-core/`
- Package: `p3_core`
- Version marker: `p3-core/VERSION`
- Runtime version constant: `p3_core.__version__`
- Tests: `p3-core/tests/`
- requirements/design/task history:
  - [p3-requirements-2026-04-18.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p3/p3-requirements-2026-04-18.md)
  - [p3-design-spec-2026-04-18.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p3/p3-design-spec-2026-04-18.md)
  - [p3-phase-plan-2026-04-18.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p3/p3-phase-plan-2026-04-18.md)
  - [p3-task-list-2026-04-18.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p3/p3-task-list-2026-04-18.md)
  - [note-p3-experiment-2026-04-20.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p3/note-p3-experiment-2026-04-20.md)

Verification:

```bash
cd /Users/satojunichi/Documents/openclaw/p3-core
python3 -m unittest discover -s tests
```

Latest verified result:

```text
Ran 35 tests
OK
```

## What Is Mainline Now

P3 mainline is a local LLM agent runtime with:

- session queue and append-only JSONL events
- local Ollama chat/generate integration
- dashboard package `p3_core.dashboard`
- terminal agent mode
- tool loop with `list_files`, `read_file`, `search_code`, `write_file`, `append_file`, `replace_text`, `run_command`, `finish`
- structured failure classification for invalid LLM tool-call output
- dedicated per-turn LLM workspace under `workspaces/runs/<turn_id>/`
- compact action context that excludes stale observer notes and unrelated old tasks
- passive Japanese live commentator, observational only
- blocked/success/failed/running operation status separation
- controller fast paths for explicit user-requested commands only
- direct terminal evidence completion for command stdout results

## Important Behavioral Decisions

1. P3 state and LLM work products are separated.

   P3 state remains under the workspace root (`state/`, `logs/`, `config.json`). LLM file and command tools run in a dedicated turn workspace:

   ```text
   workspaces/runs/<turn_id>/
   ```

2. Long code should not be emitted as one large JSON tool argument.

   `write_file` and `append_file` reject chunks over 2000 bytes. Existing-file edits should use `replace_text` where possible.

3. LLM invalid output is first-class evidence.

   P3 records `llm_output_issue` with reason codes such as:

   - `empty_output`
   - `missing_json_object`
   - `json_parse_error`
   - `length_truncated`
   - `invalid_tool_envelope`
   - `json_contract_not_confirmed`

4. The commentator must not control the run.

   The commentator explains after events are recorded. It does not approve, block, finish, or intervene. Controller/fast-path steps do not call the commentator LLM because there is no LLM action output to explain and commentary must not block execution.

5. The grounding judge is not the only completion path.

   If terminal stdout is direct evidence for the final answer, the controller may finish without waiting for an LLM judge.

6. Coding tasks are not satisfied through fixed benchmark scaffolds.

   Requests like `迷路を実装して実行し表示して結果を見せて` must go through the normal LLM action loop. The controller fast path may execute explicit user-requested commands, but it must not synthesize a fixed `maze_gen.py` or other benchmark artifact.

## Latest Live Smoke Result

The previous maze scaffold smoke result has been retired because fixed benchmark-like scaffolds pollute capability evaluation. Current smoke coverage should use either explicit user-requested commands or LLM-driven coding turns.

## Remaining Work Before P4

The P3 baseline is good enough to become P4's parent, but these are not solved yet:

1. Promote/apply flow

   P3 can isolate generated files in a run workspace. It does not yet have a formal mechanism to promote selected artifacts into a target project.

2. General coding strategy

   The maze scaffold proves the mechanism, but generic coding still needs broader planner/editor behavior instead of one-off controller paths.

3. Artifact dashboard

   The dashboard shows the current/last LLM workspace, but it should show created/modified files, command outputs, and promote status more explicitly.

4. Judge boundary cleanup

   Direct terminal evidence completion exists, but the boundary between controller finish and LLM judge should be made explicit and tested more broadly.

5. Versioned workspace snapshots

   P3 now has a mainline marker, but it does not yet snapshot or tag live workspaces as promoted/rejected candidates.

## P4 Starting Point

P4 should start by copying or depending on `p3-core/` at `0.3.0-mainline`.

Do not start P4 from:

- `/tmp/p3-dashboard-commentary`
- old P2 docs
- `handoff/archive/p3/note-p3-experiment-2026-04-20.md`
- partial dashboard logs

Those are historical evidence. The current baseline is `p3-core/` plus this canonical handoff.
