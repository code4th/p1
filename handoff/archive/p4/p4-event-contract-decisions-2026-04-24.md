# P4 Event Contract Decisions 2026-04-24

## Purpose

この文書は、2026-04-24 時点の P4 event contract 整理に関する案件固有の判断メモである。

恒久原則ではない。`1-5` はこの論点のための判断項目であり、一般的なコーディング原則として扱わない。

関連する監査メモ:

- [p4-event-contract-audit-2026-04-24.md](/Users/satojunichi/Documents/openclaw/handoff/p4-event-contract-audit-2026-04-24.md)
- [p4-branch-inventory-2026-04-24.md](/Users/satojunichi/Documents/openclaw/handoff/p4-branch-inventory-2026-04-24.md)

## Core Direction

この論点の結論は先に固定する。

- `system_note` に寄せて管理しない
- 観測は正式なイベントとして残す
- 同じ事実を複数形式で持たない
- `runtime/status.json` のような別系統の真実を持たない
- 実行状態の確定は runtime が責任を持つ

したがって、event contract の主語は `system_note` ではなく、**正規イベント種別** になる。

最低限必要な種別は次:

- `operation`
- `llm`
- `tool`
- `frame`
- `decision`
- `observation`

## Canonical Event Schema V1

```json
{
  "event_id": "uuid",
  "timestamp": "ISO-8601",
  "kind": "operation|llm|tool|frame|decision|observation",
  "status": "string",
  "operation_id": "string",
  "turn_id": "string|null",
  "step_index": 0,
  "parent_event_id": "string|null",
  "payload": {}
}
```

共通原則:

- append-only
- 同じ事実は一つの event にだけ書く
- dashboard はこの event だけを読む
- runtime の現在状態もこの event 列から導出される

## Decision 1: `system_note` をどう扱うか

### Current facts

- prompt では `system_note` を直近イベントとして LLM に再注入している
  - [prompts.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/prompts.py)
- `finish_blocked`、`command_failed`、`grounding_judge` など複数の別責務が `system_note` に混在している
  - [runtime.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/runtime.py)
- dashboard は `system_note` の文言を解釈して blocked 表示を作っている
  - [snapshot.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/dashboard/snapshot.py)
  - [templates.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/dashboard/templates.py)

### What matters

- 必要な情報そのものは消すべきではない
- ただし `system_note` という雑多な箱で持つと責務混在になる
- 文面依存の表示・判定は非対称性の原因になる

### Recommended direction

- `system_note` を正本にしない
- 中に入っている事実を、`decision` / `observation` / `operation` など正規イベントへ分解する
- 移行期だけ legacy event として残してよいが、正式な管理対象にはしない

### What this solves

- blocked 理由、実行メモ、運用者表示、LLM フィードバックの責務混在を解消する
- dashboard の文面解釈を不要にする

## Decision 2: `observer_note` を正規 contract の外に置くか

### Current facts

- `observer_note` は passive commentator 用であり、制御を持たないと README に明記されている
  - [README.md](/Users/satojunichi/Documents/openclaw/p4-core/README.md)
- prompt の通常コンテキストには `observer_note` を入れていない
  - [prompts.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/prompts.py)
- dashboard では独立した見た目と扱いを持つ
  - [templates.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/dashboard/templates.py)

### What matters

- 観測は必要
- ただし解説コメントと実行制御を同じ種別に混ぜると責務が崩れる

### Recommended direction

- 観測は **正規 contract に入れる**
- ただし `observer_note` という commentary 名ではなく、`observation` という正式イベント種別にする

### Observation payload example

```json
{
  "source": "commentator|judge|validator",
  "input_context": {},
  "result": {},
  "summary": "string",
  "related_event_id": "string|null"
}
```

### What this solves

- 観測結果を落とさず保存できる
- 実行フローと観測フローを区別したまま参照できる

## Decision 3: `tool_result` を legacy compatibility event として残すか

### Current facts

- runtime の思考材料は `steps[].tool_result` と session event `tool_result` の両方に依存している
  - [runtime.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/runtime.py)
  - [prompts.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/prompts.py)
- dashboard も `tool_result` を直接表示している
  - [snapshot.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/dashboard/snapshot.py)
  - [templates.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/dashboard/templates.py)
- すでに `runtime_event.tool_call_finished` も並行して存在する

### What matters

- 即削除すると影響範囲が広い
- しかし同じ事実を二重化したままにはできない

### Recommended direction

- `tool_result` を正本にしない
- ツール完了・失敗は `kind=tool` の canonical event に一本化する
- 移行中のみ legacy compatibility として残し、最終的には消す

### Canonical tool example

```json
{
  "kind": "tool",
  "status": "finished",
  "payload": {
    "tool_name": "run_command",
    "args": {"command": "pwd"},
    "result": {"ok": true, "stdout": "/tmp\n"}
  }
}
```

### What this solves

- runtime / dashboard / prompt 再構成が同じ事実を参照できる
- `tool_result` と `runtime_event.tool_call_finished` の二重管理をやめられる

## Decision 4: `runtime/status.json` をどこまで cache に格下げするか

### Current facts

- `status.json` は current operation、current stream text、worker_running、last parse issue などを持つ
  - [workspace.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/workspace.py)
- dashboard はこれを使って running/stale/live_stream を推測している
  - [snapshot.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/dashboard/snapshot.py)
- CLI `status` も読む

### What matters

- 別ファイルの現在値が event log と独立した真実を持つと破綻する
- 履歴・監査・表示の正本は一つに寄せる必要がある

### Recommended direction

- `runtime/status.json` を正本として扱わない
- runtime の現在状態は canonical event 列から導出する
- もし別ファイルを残すなら、それは再計算可能な派生ビューに限定する

### What this solves

- event log と status file の不一致をなくす
- dashboard が status file を読んで推測する構造をやめられる

## Decision 5: stale running を UI で補正するのか、runtime が明示イベントを出すのか

### Current facts

- dashboard は `running` が古い時に time-based で failed に正規化している
  - [snapshot.py](/Users/satojunichi/Documents/openclaw/p4-core/p4_core/dashboard/snapshot.py)
- この判定は `worker_running`、`last_event_at`、`current_operation_id` など複数ソースを見ている

### What matters

- UI が実行意味を補正すると、表示層が実行責務を持ってしまう
- 実行の終了意味は runtime が確定すべき

### Recommended direction

- `operation.started|finished|blocked|failed` を runtime が必ず出す
- stale running の判定も runtime が持つ
- UI 補正は削除対象。移行期 fallback が必要なら最小限に留める

### What this solves

- 「本当に失敗した」のか「表示が古いだけ」なのかを runtime が一意に確定できる
- dashboard は表示責務だけに戻れる

## Current recommendation summary

1. `system_note` 中の事実は正規イベントへ分解し、正式管理を `system_note` に寄せない
2. 観測は `observation` として正式イベント化する
3. `tool_result` は canonical `tool` event に一本化する
4. `runtime/status.json` を別の真実にしない
5. 実行状態の最終確定は runtime が持つ

## Implementation order for this issue

1. canonical event schema を実装する
2. runtime が全状態遷移を canonical event として出す
3. dashboard を canonical event only に寄せる
4. legacy source を段階的に削除する

この順を崩して、UI 側だけで整合させない。

## Current Duplication Map

現状どこが「一つの事実」から外れているかを明示する。

### Operation state

- `type=operation` event
- `runtime/status.json`
- dashboard 内の stale running normalization

問題:

- operation の state が event, status file, UI 推測に分散している

canonical target:

- `kind=operation` event のみ

### Tool completion

- `type=tool_result`
- `type=runtime_event` with `tool_call_finished`
- `steps[].tool_result`

問題:

- 同じツール結果を 3 形態で持っている

canonical target:

- `kind=tool` event

### LLM output lifecycle

- `runtime_event.llm_call_started`
- `runtime_event.llm_stream_chunk`
- `runtime_event.llm_call_finished`
- `runtime/status.json.current_stream_text`
- `runtime/status.json.last_llm_*`

問題:

- lifecycle の一部が event、一部が status file にある

canonical target:

- `kind=llm` event

### Block / finish decisions

- `type=finish`
- `type=system_note` with `finish_blocked`
- `type=system_note` with `grounding_judge`
- dashboard の `_latest_blocked_reason`

問題:

- finish accepted / blocked / failed が複数経路で表現されている

canonical target:

- `kind=decision` event
- `kind=operation` event

### Frame hierarchy

- `frame_opened`
- `frame_returned`
- `child_return`
- dashboard の frame stack reconstruction

問題:

- frame の親子関係と depth を UI が再構成している

canonical target:

- `kind=frame` event with explicit `depth`, `frame_id`, `parent_frame_id`

### Observation / commentary

- `observer_note`
- `system_note`
- `activity_update`

問題:

- 観測、補助メモ、運用表示が同じ層で混ざっている

canonical target:

- `kind=observation` event

## Immediate Design Consequence

次の実装では、新しい canonical event emitter を作る時点で以下を満たす必要がある。

1. 同じ事実を legacy event と canonical event の両方に出す場合でも、canonical を明示的に唯一の正式表現とする
2. dashboard の新表示は canonical event だけを使う
3. status file は canonical event から再導出できる項目しか持たない
4. runtime が `started -> finished|blocked|failed` を閉じる
