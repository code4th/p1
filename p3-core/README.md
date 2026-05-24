# P3 Core

P3 Core is a minimal local-LLM agent runtime for purpose execution.

## Current mainline

- Mainline version: `0.3.0-mainline`
- Mainline date: `2026-04-21`
- Canonical handoff: `handoff/p3-canonical-mainline-2026-04-21.md`
- Code root for P4 inheritance: `p3-core/`
- Verification command: `python3 -m unittest discover -s tests`

This is the current P3 baseline. Older P3 notes and live workspaces are useful history, but P4 should inherit from this code root and the canonical handoff above.

It is intentionally smaller than P1/P2:

- session-based chat loop
- tool call -> tool result reinjection
- local Ollama model routing
- raw event persistence
- external dashboard for chat/control/visibility
- passive Japanese live commentary for step-by-step failure analysis
- blocked/success/failed operation status separation

## Supported models

- `gemma4:26b`
- `glm-4.7-flash`
- `qwen3-coder`
- `devstral`

## Quick start

```bash
cd /Users/satojunichi/Documents/openclaw/p3-core
python3 -m unittest discover -s tests
python3 -m p3_core.cli --root /tmp/p3-demo version
python3 -m p3_core.cli --root /tmp/p3-demo bootstrap --force
python3 -m p3_core.cli --root /tmp/p3-demo set-goal --text "READMEを読み、実行計画を返す"
python3 -m p3_core.cli --root /tmp/p3-demo chat --message "このworkspaceで最初に確認すべきファイルを読んで"
python3 -m p3_core.cli --root /tmp/p3-demo run-loop
python3 -m p3_core.cli --root /tmp/p3-demo dashboard --host 127.0.0.1 --port 8899
```

## Runtime observability

P3 records the agent loop as append-only JSONL events. Important event types:

- `user_message`
- `assistant_message`
- `tool_call`
- `tool_result`
- `finish`
- `system_note`
- `planning_note`
- `observer_note`
- `activity_update`
- `operation`

Terminal agent turns use a text JSON action contract. If the LLM returns prose, truncated JSON, or an invalid envelope instead of a valid `tool_name` / `tool_args` object, P3 records `system_note.code = llm_output_issue`. Parse failures are classified as `empty_output`, `missing_json_object`, `json_parse_error`, `length_truncated`, `invalid_tool_envelope`, or `json_contract_not_confirmed`. The latest class and stream metadata are also shown in runtime status and the dashboard.

Action prompts are intentionally compact. They include the current user request, recent tool evidence, important system notes, and a short reflection block. They do not replay old observer commentary, unrelated prior tasks, or large failed assistant outputs.

Coding/tool turns run inside a dedicated LLM workspace at `workspaces/runs/<turn_id>/`. P3 state, sessions, and dashboard logs stay in the root workspace, while `read_file`, `write_file`, `append_file`, `replace_text`, `search_code`, and `run_command` operate inside that turn workspace. The path is recorded as `llm_workspace` on events and is shown in the dashboard as the current or last work area.

For file edits, avoid putting long source code in one JSON argument. Use `write_file` only for small starter content, `append_file` for chunks of 2000 bytes or less, and `replace_text` for exact small edits to existing files. Oversized `write_file` / `append_file` calls fail with guidance instead of silently accepting brittle payloads.

The controller fast path is limited to executing commands that the user explicitly requested. It must not synthesize fixed coding artifacts for benchmark-like prompts; coding tasks should go through the normal LLM action loop.

Finish is guarded by required-command checks, expected-artifact checks, and a grounding judge. The grounding judge asks for JSON verdicts and separates `ng` from judge failures such as `invalid_output`, `invalid_json`, `empty_output`, and `error`.

## Dashboard commentary

Set `runtime.observer_enabled` in the workspace config to enable the passive live commentator. The commentator is observational only; it does not control, approve, block, or finish tasks.

It runs after:

- an LLM response cannot be parsed as a tool call (`llm_output_issue`)
- a tool result is recorded
- a finish attempt is blocked
- a native chat response completes

The dashboard shows commentary inside the operation flow. Commentary includes what happened, why the LLM may have produced the failing output, and whether the context passed to the LLM appears noisy or contradictory.

The commentator uses the runtime `chat_timeout_seconds`; there is no separate shorter commentator timeout.

Controller/fast-path steps do not call the commentator LLM because there is no LLM action output to explain, and commentary must not block the main execution path.

Operation status values are:

- `running`
- `success`
- `failed`
- `blocked`

`blocked` means the task did not pass P3's finish/governance checks, even if the outer runner returned normally.

## Commands

- `bootstrap`
- `version`
- `status`
- `ollama-status`
- `set-goal`
- `chat`
- `run-loop`
- `worker`
- `dashboard`
