# P4 Branch Inventory 2026-04-24

## Intent

このメモの目的は、分岐を増やすことではなく、現在の分岐を以下の3種類に仕分けること。

1. 残すべき対称分岐
2. variation として payload に押し込める分岐
3. 非対称なので削るか、設計相談が必要な分岐

ここでは「テストケース数を増やす」前に、「そもそもその分岐は必要か」を見る。

## Runtime Branch Surface

### Tool routing branch

`runtime.py` で明示的に分かれている tool 名:

- `decompose_tasks`
- `open_child_frame`
- `return_to_parent`
- `finish`
- `run_command`
- `final_answer`

### System note / reason code branch

現在コード上で明示されている reason / code:

- `finish_blocked`
- `child_frame_must_return`
- `missing_required_commands`
- `missing_expected_artifacts`
- `grounding_judge`
- `llm_output_issue`
- `command_blocked`
- `command_failed`
- `work_package_invalid`
- `decompose_tasks_blocked`
- `frame_open_blocked`
- `frame_return_blocked`
- `controller_finish`

### Current phase branch

runtime status の `current_phase`:

- `TASK_DECOMPOSED`
- `FRAME_OPENED`
- `FRAME_RETURNED`
- `RETURN_TO_PARENT_REQUIRED`
- `EXECUTE_MISSING_COMMANDS`
- `SYNTHESIZE_FROM_EVIDENCE`
- `FINISH`

### Runtime event branch

現在の `runtime_event.event_name`:

- `llm_call_started`
- `llm_stream_chunk`
- `llm_response_received`
- `llm_repair_requested`
- `llm_call_finished`
- `tool_call_started`
- `tool_stream`
- `tool_call_finished`

## Tool Layer Branch Surface

`tools.py` の tool 名:

- `list_files`
- `read_file`
- `search_code`
- `write_file`
- `append_file`
- `replace_text`
- `run_command`

代表的な failure variation:

- path missing
- path escapes root
- empty query
- `write_file` chunk over limit
- `append_file` chunk over limit
- `replace_text` match count != 1
- empty command
- multi command denied
- dangerous command denied
- unsupported shell
- PowerShell unavailable
- timeout
- nonzero exit

これらは dashboard 分岐ではなく、`tool failed` の variation に落とすべき。

## Frame Layer Branch Surface

frame 遷移:

- root frame created
- child frame opened
- child returned
- child task completed
- root cannot return
- depth limit exceeded
- step safety valve return

ここで重要なのは、frame depth と parent/child 関係を dashboard が推測している点。これは非対称性。

## Dashboard Branch Surface

現在 dashboard がやっている非対称な処理:

- operation 行を event stream から再構築する
- `current_operation_id`, `current_turn_id`, `current_started_at` で active 判定する
- stale running を time-based に失敗へ正規化する
- `current_stream_text` を `live_stream` として後付けする
- `finish_blocked` を `system_note` から後で検出する
- latest `tool_result` を output preview に上書きする
- frame depth を event 順序から復元する

この全てが dashboard だけのローカル推測であり、非対称性の源になっている。

## Classification

### A. Keep As Symmetric Branches

これらは上位の対称モデルとして残してよい。

- operation: `started|finished|failed|blocked`
- llm: `started|stream|finished|invalid_output|failed`
- tool: `started|stream|finished|failed`
- frame: `opened|returned|blocked`
- decision: `accepted|blocked|failed`

### B. Convert To Variations

上位イベントは増やさず `reason_code` / `payload` に落とす。

- `missing_required_commands`
- `missing_expected_artifacts`
- `child_frame_must_return`
- `command_blocked`
- `command_failed`
- `work_package_invalid`
- `decompose_tasks_blocked`
- `frame_open_blocked`
- `frame_return_blocked`
- LLM parse issues
- tool-specific failures
- grounding judge sub-results

### C. Non-symmetric Branches That Should Be Removed

以下は個別対応ではなく削る対象。

1. `current_stream_text` がログ正本と別の live 表示ソースになっている
2. operation 紐付けを時刻窓で推測する
3. stale running を dashboard が独自推測する
4. frame depth を dashboard が再計算する
5. `system_note` と `runtime_event` と `tool_result` が重複して同じ事実を表す
6. native generate と terminal agent で別ルートのログ形式を持つ

## Simplification Proposal

### Core Rule

dashboard は推測しない。

runtime が出す正規イベントだけを変換表示する。

### Minimal Event Contract

```
event = {
  kind: "operation" | "llm" | "tool" | "frame" | "decision",
  status: string,
  operation_id: string,
  turn_id: string | null,
  step_index: int | null,
  payload: object,
}
```

### Consequences

- `current_stream_text` は cache 扱いに下げる
- dashboard の `_event_in_operation_window` は最終的に不要にする
- `_latest_blocked_reason` の後処理も不要にする
- `_append_live_stream_step` も正規イベントが揃えば不要
- `tool_result` は正規 event の payload として持てばよい

## What Is Actually Ambiguous

ここは勝手に決めると危険なので確認が必要。

1. `system_note` を今後も user-facing commentary として残すか
2. `observer_note` を正規 event contract の外に置くか
3. `tool_result` を legacy compatibility event として残すか
4. `runtime/status.json` をどこまで cache に格下げするか
5. stale running を UI から消すのか、operation event で明示的に `failed` にするのか

## Recommended Next Step

次は実装ではなく、`event.kind` / `event.status` / `payload` の確定版を短く決めること。

その時に相談が必要なのは上の5点だけで、他は対称モデルに寄せる方針で進めてよい。
