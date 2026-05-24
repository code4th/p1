# P4 Event Contract Audit 2026-04-24

## Purpose

P4 の出力とダッシュボード表示を、個別ケースのパッチではなく、対称なイベント契約に寄せる。

問題が起きた時に分岐を増やすのではなく、まずその分岐が以下のどちらかを判定する。

- バリエーション分岐: 同じ構造の中で理由や値だけが違うもの。許容する。
- 非対称分岐: 特定ケースだけ別のログ、状態、表示、終了条件を持つもの。原則として削るか、対称なイベントへ上げる。

非対称分岐が必要に見える場合は、実装前に相談する。

## Current Root Cause

現在の P4 は、実行の正本が複数ある。

- `state/sessions/<session>/events.jsonl`
- `state/runtime/status.json`
- operation event
- `current_stream_text`
- frame state
- dashboard の `flow_steps`

このため dashboard が状態を推測している。特に以下が非対称性を生んでいる。

- running operation を時刻や runtime-state から推測する
- `current_stream_text` を flow に後付けする
- `finish_blocked` だけ `system_note` から拾う
- `tool_result` と `runtime_event.tool_call_finished` が並存する
- operation id が無いイベントを時刻窓で operation に結びつける
- frame depth を frame event の並びから復元する

正しい方向は、dashboard が推測しないこと。実行側が正規イベントを出し、dashboard はそれを表示用に変換するだけにする。

## Symmetric Event Model

dashboard が直接理解するイベント種別は最小化する。

### Operation

```json
{
  "kind": "operation",
  "status": "started|finished|failed|blocked",
  "operation_id": "...",
  "title": "...",
  "reason_code": "...",
  "payload": {}
}
```

### LLM

```json
{
  "kind": "llm",
  "status": "started|stream|finished|invalid_output|failed",
  "operation_id": "...",
  "turn_id": "...",
  "step_index": 1,
  "payload": {
    "model": "...",
    "thinking_text": "...",
    "content_text": "...",
    "parse_issue": "...",
    "stream_metadata": {}
  }
}
```

### Tool

```json
{
  "kind": "tool",
  "status": "started|stream|finished|failed",
  "operation_id": "...",
  "turn_id": "...",
  "step_index": 1,
  "payload": {
    "tool_name": "run_command",
    "tool_args": {},
    "tool_result": {},
    "reason_code": "timeout|invalid_args|denied|not_found|nonzero_exit|unsupported_tool"
  }
}
```

`write_file` の chunk 超過、`replace_text` の match 0、`run_command` の timeout などは dashboard 分岐にしない。全部 `kind=tool,status=failed` の variation として扱う。

### Frame

```json
{
  "kind": "frame",
  "status": "opened|returned|blocked",
  "operation_id": "...",
  "turn_id": "...",
  "step_index": 1,
  "payload": {
    "frame_id": "...",
    "parent_frame_id": "...",
    "depth": 1,
    "goal": "...",
    "return_payload": {}
  }
}
```

frame depth は dashboard が stack 復元しない。runtime が `depth` を出す。

### Guard / Decision

```json
{
  "kind": "decision",
  "status": "accepted|blocked|failed",
  "operation_id": "...",
  "turn_id": "...",
  "step_index": 1,
  "payload": {
    "decision_type": "finish|grounding|decompose|command_guard|work_package",
    "reason_code": "...",
    "message": "...",
    "evidence": {}
  }
}
```

`finish_blocked`, `grounding_judge`, `command_blocked`, `work_package_invalid`, `step limit` は個別 event type ではなく `kind=decision` の variation として扱う。

## Branch Reduction

### Keep As Variations

これらは上位イベントを増やさず、payload の `reason_code` に閉じ込める。

- LLM parse issue: `missing_json_object`, `json_parse_error`, `thinking_only_output`, `empty_output`, `length_truncated`, `invalid_tool_envelope`
- tool failure: invalid args, denied command, unsupported shell, timeout, nonzero exit, missing file, replace miss, write too large
- finish block reason: missing command, missing artifact, grounding invalid, child frame must return
- decompose block reason: empty task plan, invalid work package, depth limit
- stale status reason: worker absent, no heartbeat, operation not active

### Must Become Symmetric

これらは現在の非対称性なので、正規イベントへ上げる。

- `current_stream_text` only live output
- dashboard time-window operation inference
- `finish_blocked` special parsing
- frame depth reconstruction from event order
- operation status derived from runtime-state instead of operation event
- `tool_result` and `runtime_event.tool_call_finished` double source
- native generate and terminal agent having different logging paths

### Remove From Dashboard Responsibility

dashboard は以下を判断しない。

- tool 名ごとの成功/失敗分類
- LLM JSON 修復の具体戦略
- grounding judge の判定アルゴリズム
- stale running の原因推定
- frame stack の復元

dashboard は正規イベントを表示するだけにする。

## Minimal Contract Tests

全分岐を dashboard 統合テストに持ち込まない。対称な契約だけを検証する。

1. operation started/finished/failed/blocked が混線しない
2. llm started/stream/finished がログ化される
3. llm invalid_output が variation としてログ化される
4. tool started/stream/finished がログ化される
5. tool failed が reason_code 付きでログ化される
6. frame opened/returned/blocked が depth 付きでログ化される
7. decision accepted/blocked/failed が reason_code 付きでログ化される
8. finish accepted/blocked は decision と operation status に反映される
9. dashboard は正規イベントだけで operation flow を作る
10. dashboard は operation_id で分離し、時刻推測を使わない
11. dashboard は depth を runtime 由来で表示する
12. 旧ログ互換が必要な場合は互換層だけで吸収する

## Proposed Next Step

次に実装するなら、既存コードへ追加パッチを重ねるのではなく、まず `emit_event(kind, status, ...)` を 1 箇所に作る。

その後、既存の `runtime_event`, `tool_result`, `system_note`, `frame_opened`, `finish` をすぐ消さず、互換ログとして残しながら、正規イベントを並行出力する。

dashboard は正規イベントが存在する場合は正規イベントだけを見る。正規イベントが無い古い operation のみ旧ログ互換表示に落とす。

この順なら非対称性を増やさず、移行中の壊れ方も限定できる。
