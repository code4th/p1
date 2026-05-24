# P4 Symmetry Audit 2026-04-26

## Purpose

P4 全体を [p4-coding-invariants-2026-04-24.md](p4-coding-invariants-2026-04-24.md) と [p4-event-contract-decisions-2026-04-24.md](p4-event-contract-decisions-2026-04-24.md) に照らして点検する。直近の judge fallback 改修で導入した非対称性を含めて棚卸しする。

判断基準:

- variation（理由・値だけが違う）は許容
- asymmetry（特定ケースだけ別状態・別表示・別終了条件）は対称化を検討する
- 対称化できない場合は、上位レイヤーで対称になるまで持ち上げる

## A. 直近改修で導入した非対称性（最優先で是正）

### A-1. `accepted_with_warning` という非正規 operation status

- 場所: [runtime.py:386](p4-core/p4_core/runtime.py:386), [runtime.py:1106](p4-core/p4_core/runtime.py:1106), [grounding.py:86](p4-core/p4_core/grounding.py:86)
- 違反: canonical operation status は `started|finished|failed|blocked` のみ ([p4-event-contract-audit:42-50](handoff/p4-event-contract-audit-2026-04-24.md))。`accepted_with_warning` は**新しい finished の variation**ではなく、**新しい primary status** として混入している。
- 影響: dashboard と `_status_is_accepted` の両方が新ブランチを覚える必要がある。事実が status 文字列と reason_code に二重に乗る。
- **対称化の方向**: operation status は `finished` のまま。「judge 不在で観測ベースに降格して受理した」という事実は `kind=decision` event に `reason_code: judge_unavailable_observation_accepted` として残す。

### A-2. `review_unavailable_observation_accepted` という非正規 semantic_status

- 場所: [grounding.py:85](p4-core/p4_core/grounding.py:85)
- 違反: semantic_status は judge の verdict 種別を表すべき。「verdict が出なかった」のは `review_unavailable` のままで正しく、それを caller が**どう扱うか**は別の決定。両者を同じフィールドに混ぜると、verdict と policy 判断が同居する。
- **対称化の方向**: semantic_status は `review_unavailable` で止める。observation acceptance は `kind=decision` event の variation として表現する。

### A-3. dashboard 「最終結果」カードが legacy duplicate を読む

- 場所: [templates.py:200,1151](p4-core/p4_core/dashboard/templates.py:200)
- 違反: [event-contract-decisions Decision 3](handoff/p4-event-contract-decisions-2026-04-24.md): `tool_result` / `session.last_tool_result` は legacy。dashboard は canonical `kind=tool` event を読むべき。
- 影響: 「最終結果」を新規追加した時に legacy ソースを正本扱いしてしまった。
- **対称化の方向**: snapshot 側で canonical event 列から最後の `kind=tool, status=finished` の `tool_result.stdout` を抽出した派生フィールド `last_canonical_tool_result` を作り、dashboard はそれを表示する。session メタの `last_tool_result` は触らない（migration 期の互換読み）。

## B. 既知の pre-existing 非対称性（事後是正対象）

[p4-event-contract-audit](p4-event-contract-audit-2026-04-24.md) で既に挙がっているもの。今回は触らないが点検結果として記録。

| 項目 | 場所 | 解消方向 |
|---|---|---|
| `system_note` に `finish_blocked` 等を載せる | runtime.py 各所 | canonical `kind=decision` に統一 |
| `runtime/status.json` が現在状態の正本 | workspace.py / dashboard/snapshot.py | canonical event から派生に縮小 |
| dashboard が時刻窓で operation 推測 | snapshot.py `_normalize_operation_rows` | operation_id で確定 |
| `tool_result` と `runtime_event.tool_call_finished` 二重 | workspace.py / runtime.py | canonical `kind=tool` のみ |
| stale running を UI が補正 | snapshot.py | runtime が `operation.failed` を出す |
| frame depth を UI が復元 | snapshot.py | runtime が `kind=frame.depth` を必ず出す |

## C. invariants ベースの一般点検

### Invariant 4 (variation/asymmetry) 違反候補

- `display_lines` vs `stream_lines` の分類後 sort: status の違いを sort 入力に反映する非対称。
  - 対症: 二次キー event_id 追加済み（局所整合）
  - 根治: kind+status の variation として同一 sort key で扱う

### Invariant 5 (正本を増やさない) 違反候補

- judge_metrics を session meta にキャッシュせず、canonical events から都度導出 → 現状 OK
- `last_tool_result` (session meta) と canonical `kind=tool` event の重複 → A-3 で扱う
- `last_finish_message` (session meta) と canonical `kind=operation finished` payload の重複 → migration 対象

### Invariant 6 (到達しないはず経路はアサート) 違反候補

- judge invalid_json は「ありうる経路」として fallback 扱いしている。本来は「judge プロンプトが壊れた / モデル不整合」の signal。
  - 対称化: invalid_json 連続 N 回 → assertion ではなく `kind=decision, decision_type=judge_health, status=failed, reason_code=invalid_json_repeated` を出して runtime が顕在化させる。dashboard はそれを表示する。

## D. 是正実装計画（このセッション内）

優先順位高 = A-1, A-2, A-3 の3点。pre-existing は今回は触らず、文書化のみ。

### 実装ステップ

1. **grounding.py**: `_finish_acceptance_evaluation` の戻り値を分離する。
   - `evaluation["status"]`: `success | partial_success | needs_revision`（accepted_with_warning を廃止）
   - `evaluation["semantic_status"]`: `reviewed | not_required | needs_revision | partial_success | review_unavailable`（observation_accepted を廃止）
   - 新しいフィールド `evaluation["acceptance_override"]`: `None | "judge_unavailable_observation_accepted"`
2. **runtime.py**: caller 側で `acceptance_override` を見て、observation 受理に降格する場合は
   - operation status は `finished` のまま
   - `kind=decision` event を emit: `decision_type="finish_acceptance", status="accepted", reason_code="judge_unavailable_observation_accepted"`
   - `_status_is_accepted` から `accepted_with_warning` を削除
3. **dashboard/snapshot.py**: canonical event 列から「直近の `kind=tool, status=finished` の result」を抽出して `last_canonical_tool_result` を提供
4. **dashboard/templates.py**: 最終結果カードを `last_canonical_tool_result` 優先、legacy `session.last_tool_result` は fallback のみ
5. **テスト追加**:
   - `test_grounding_acceptance_override_emits_decision_event`
   - operation status は常に canonical 値であることを assert
6. 既存 73 テスト緑維持

### 是正の効果

- judge fallback の事実が **decision event 1箇所に集約**される（A-1, A-2 解消）
- dashboard が canonical event を読むパスに揃う（A-3 解消）
- 後続の legacy 削除（B 表）が同じ canonical 経路で進められる

## E. 思想未反映の議論ポイント（要意思決定）

このセッションで決められない、上位の設計判断:

1. judge invalid_json への観測ベース受理を**仕様として認めるか**
   - 認める場合: design-spec に明記し、`reason_code: judge_unavailable_observation_accepted` を正規化
   - 認めない場合: judge 修復に振る（プロンプト・モデル・JSON抽出強化）
   - 当面は「decision event として記録した上で受理」とし、運用ログから判断する設計でブリッジ
2. step limit を超えた `consecutive_finish_blocks` の扱い
   - 現状: 3回連続で `accepted_with_warning` 強制（runtime.py に存在）
   - これも上記と同じ意思決定対象
