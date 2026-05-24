# P2 開発 調査報告書

日付: 2026-04-20
作成者: Antigravity（AIコーディングエージェント）
目的: 今後の実装AIエージェントが P2 の顛末を把握し、同じ失敗を避けるための参照文書

---

## 1. 概要

P2 は「自己改善AIエージェント」として 2026年3月末〜4月中旬に開発・運用された。中心テーマは **再帰階層コンテキストによるコンテキスト管理** の実験であり、自己改善ループはそのための実行環境だった。

最終的に P2 は 4月17日以降 `paused` 状態となり、教訓を引き継ぐ形で P3 が別カーネルとして切り出された。

本報告は、P2 の開発中に残されたセッションログ、handoff ドキュメント（12本）、会話記録、コード、状態ファイルを俯瞰し、**何が起き、何がうまくいかず、何が学びとして残ったか**を記録する。

---

## 2. タイムライン

| 日付 | 出来事 | 状態 |
|---|---|---|
| 3月末 | P1 から P2 を分離。自己改善ループの構築開始 | active |
| 4月4日 | P1 の handoff 文書群を作成。P2 のための基盤整理 | active |
| 4月6日 | qwen3-coder 切替。自己改善エージェントの最適化セッション | active |
| 4月7-8日 | P1 のリースデッドロック修正。ダッシュボード同期修正 | active |
| 4月9日 | **長時間 run の分析セッション**。220回試行、42回昇格、v0044到達。後半64回連続昇格なし。監視の誤停止を発見 | stalling |
| 4月11日 0:00-4:00 | **深夜集中セッション**。OSS比較、失敗診断、設計メモ9本を作成 | analysis |
| 4月11日 昼 | P2_INDEX 作成、コンテキスト設計メモ追加 | analysis |
| 4月11日 夕 | `session_action_loop_v1` 実装完了。テスト 36/36 全通過 | implemented |
| 4月12日 | P2 のシステムプロンプト微修正（`continue_or_return` の指示明確化） | tweaking |
| 4月16日 | P2 を完全リセット。bootstrap seed からやり直し | reset |
| 4月16日〜 | リセット後 13 試行で昇格ゼロ。c0001〜c0013 すべて失敗または未完 | failing |
| 4月17日〜 | goal status を `paused` に変更。P2 を停止 | paused |
| 4月18日 | P3 の設計仕様 4 本を作成。P3 の実装開始 | → P3 |

---

## 3. P2 のアーキテクチャ（最終状態）

### 3.1 コードベース規模

```
p2-core/p2_core/ 合計: 約 12,697 行
  loop.py:                    2,821 行（メインループ）
  loop_prompts.py:            補助プロンプト構築
  loop_delta.py:              差分管理
  loop_frame_helpers.py:      フレーム操作
  loop_frame_memory.py:       フレーム記憶
  loop_reference_context.py:  参照コンテキスト
  loop_response_parsers.py:   応答パーサー
  dashboard_presenter.py:     42,544 bytes
  dashboard_script.py:        61,633 bytes
  ...他20ファイル以上
```

### 3.2 runtime kernel

P2 には 2 つの kernel が存在した。

- `legacy_phase_loop_v1`（実際に使用されていた）
- `session_action_loop_v1`（4/11に実装、テスト通過、**一度もliveで使用されず**）

`self_model.json` の最終状態:
```json
{
  "runtime_kernel": "legacy_phase_loop_v1",
  "available_runtime_kernels": [
    "legacy_phase_loop_v1",
    "session_action_loop_v1"
  ]
}
```

### 3.3 legacy_phase_loop_v1 の動作

```
kernel が phase を固定
→ 各 phase ごとに大きな prompt を新規構築
→ LLM に JSON を返させる
→ revised_file_content で全文書き換え
→ validation を外部で固定実行
→ 結果を要約して次の phase prompt へ
```

### 3.4 session_action_loop_v1 の動作（未使用）

```
goal を持つ
→ LLM が次の action を選ぶ（read_file / apply_patch / run_validation 等）
→ action を tool として実行
→ raw result を session に戻す
→ 続けるか / 子フレームへ降りるか / 終えるか を LLM が決める
```

---

## 4. 何が起きたか（事実の記録）

### 4.1 長時間 run の結果（4月9日時点）

```
総試行: 220 回
昇格: 42 回（v0044 まで到達）
失敗: 115 回
途中終了: 63 回
```

- 最後の昇格: 2026-04-09T06:54
- その後 c0156 → c0220 まで 64 回連続昇格なし
- 後半の失敗パターン: 「対象ファイルが実質変わっていない」が 47 回

### 4.2 監視の誤停止（4月9日発見）

```
[monitor] 2026-04-09T06:39:17 worker probe failed 3x; restarting loop worker
...（63回反復）
```

63 回の監視リスタートと、63 回の途中終了が一致。LLM の長い推論中に、監視側が「止まった」と誤認して worker を kill していた。

### 4.3 リセット後の結果（4月16日）

```json
{
  "active_generation": 1,
  "active_version_id": "v0001",
  "last_candidate_id": "c0013",
  "last_promoted_candidate_id": null
}
```

- c0001: Broken pipe
- c0005: validation failed
- c0006: 子フレームに降りたまま未帰還
- c0007-c0013: attempt_started のまま最終結果なし

### 4.4 再帰フレームの使用状況

```
task_frame_recursing_count: 63
task_frame_returning_count: 0
```

63 回分解し、0 回帰還。

---

## 5. 失敗原因の診断

以下は 4/11 の集中分析セッションで特定された原因群。ソースは `p2-why-repeats-failures`、`p2-context-failure-ack`、`p2-context-vs-tool-loop`。

### 5.1 行為と結果の因果が間接的だった

P2 は失敗結果を **要約** して次のターンに渡していた。

渡していたもの:
- `recent_attempt_bundle`（再構成済み試行要約）
- `meta_diagnosis`（メタ的な診断文）
- `reasoning_summary`（推論の要約）

渡すべきだったもの:
- 何を書き換えたか（edit diff）
- その直後に何が壊れたか（validation stdout/stderr）
- それがどの変更と結びつくか（action → result の因果列）

> 要約は要約者の判断で情報が落ちる。因果列を直接渡さなかったことで、同じ種類の失敗を再導入しやすくなっていた。（p2-context-failure-ack）

### 5.2 全文書き換え（revised_file_content）の構造問題

P2 は `revised_file_content` に Python ファイルの全文を書かせていた。

実際に記録された壊れ方:
- `reasoning_summary` の内容がコードに混入
- JSON 風の文字列で Python を上書き
- `SyntaxError` が同一箇所で反復

> 1回の出力で壊せる範囲が大きすぎた。最小差分（apply_patch）ではなく全文置換のため、1箇所の逸脱がファイル全体を破壊する。（p2-why-repeats-failures）

### 5.3 再帰フレームが prompt 規律にとどまっていた

再帰フレームの「いつ戻るか」は設計メモ上で明確に規則化されていた:

1. 局所 goal を満たした
2. 局所 goal は未達でも、親が次を判断するのに十分な情報が出た
3. これ以上進めても情報利得が小さい
4. 親の goal 設定自体が間違っていたと分かった

しかし legacy_phase_loop_v1 では、これらの判断が phase ごとの prompt 再構築に埋もれており、LLM にとって「戻る」が first-class な操作になっていなかった。

> 再帰フレーム自体は実装されているが、P2 の支配的な loop は依然として phase-driven であり、自然に子フレームへ降りて親へ戻るという動作が主経路になっていない。（p2-why-repeats-failures）

### 5.4 LLM が行動主体になっていなかった

> 現在の P2 は、kernel が phase を決め、LLM は各 phase で JSON を返すだけなので、「自分で確認して直す」主体になり切れていない。（p2-meta-design-clarification）

OSS の強い agent は LLM が read / grep / run / edit / finish を直接選ぶ。P2 では kernel が手順を固定しており、LLM の裁量が phase 内に限定されていた。

### 5.5 session_action_loop_v1 が切り替えられなかった

新 kernel は 4/11 に実装完了し、テスト 36/36 全通過。しかし live workspace への切り替えは行われなかった。

理由の推定:
- 12,700 行の legacy コードとの結合を解く必要があった
- dashboard、CLI、tests が legacy kernel を前提としていた
- リセット（4/16）は legacy kernel のまま行われた

---

## 6. 正しかった設計要素（再利用すべきもの）

### 6.1 3層コンテキスト設計

```
第1層: Raw Trajectory     — append-only の事実ログ（監査・再現用）
第2層: Frame Working Memory — フレームごとの圧縮された理解状態
第3層: Cross-Attempt Memory — 試行をまたぐ戦術記憶
```

> P2 に必要なのは「長いコンテキスト」ではない。P2 に必要なのは、「過去の行為ログ」から「現在の理解状態」へ変換された working memory である。（p2-context-carryover-design）

### 6.2 Working Memory のスキーマ

```json
{
  "observed_files": ["goal_logic.py"],
  "observed_symbols": ["self_check"],
  "learned_findings": ["validation自体は成功している"],
  "current_focus": "self_check",
  "unresolved_questions": ["generic errorの解釈は正しいか"],
  "avoid_repeating": ["同じ粒度でgoal_logic.py全体を再読しない"]
}
```

### 6.3 探索状態遷移モデル

```
survey → focus → local_work → return_ready
```

同じ粒度の観測を繰り返したら停滞とみなす。

### 6.4 フレーム戻り条件

「成功ではなく、親が次を判断するのに十分な結果が出たか」が戻る基準。子フレームは成功/失敗のどちらでも、情報を持って親に戻れるべき。

### 6.5 チャネル分離

```
goal:        目的
action:      コード編集（コードを書き換えられるのはここだけ）
observation: 実行結果・検証結果
narrative:   反省・説明・要約
```

---

## 7. 誤っていた設計・実装（避けるべきもの）

### 7.1 summary を主材料にすること

raw event ではなく summary を LLM への主入力にする設計は、因果の追跡を弱める。summary は補助であり、主材料は action/result の因果列であるべき。

### 7.2 全文生成による編集

`revised_file_content` 方式は被害範囲が大きすぎる。最小差分（patch / replace_text）を基本にすべき。

### 7.3 kernel が行動手順を固定すること

LLM を phase ごとの JSON 生成器にするのではなく、tool を持つ session loop の中で行動主体にすべき。

### 7.4 再帰を prompt 規律だけで実現すること

`open_child_frame` / `return_to_parent` は first-class な runtime 操作（tool）として提供する必要がある。説明だけでは使われない。

### 7.5 周辺を核より先に肥大化させること

dashboard コード（104KB）が runtime（loop.py 2,821行）より大きくなっていた。核が安定しないまま可視化や制御を積み重ねると、改修困難になる。

---

## 8. P2 → P3 で引き継がれたもの / 引き継がれなかったもの

### 引き継がれた

| 設計要素 | P2 での状態 | P3 での反映 |
|---|---|---|
| session/action/result loop | 設計済み・実装済み・未使用 | 中心アーキテクチャとして採用 |
| event-sourced 状態管理 | 一部使用 | JSONL append-only で全面採用 |
| LLM が tool を選ぶ | 新 kernel に実装 | 基本動作として採用 |
| patch/diff 編集 | 設計メモに記載 | `replace_text` / `append_file` として実装 |
| 観測可能性 | dashboard あり | dashboard + passive commentator |

### まだ引き継がれていない（将来の課題）

| 設計要素 | P2 での状態 | P3 での状態 |
|---|---|---|
| 再帰階層コンテキスト | 規則化済み・部分実装 | 非対象（将来） |
| Frame Working Memory | 設計済み | 非対象（将来） |
| Cross-Attempt Memory | 設計済み | 非対象（将来） |
| 自己改善ループ | 中心機能 | 非対象（将来） |
| 探索状態遷移モデル | 設計済み | 非対象（将来） |

---

## 9. AI エージェントとの協働で分かったこと

### 9.1 AI エージェントが得意だったこと

- ログ分析と失敗原因の特定
- OSS 6製品の実装比較と本質的な差の抽出
- 設計メモの体系的な作成（一晩で12本）
- 新モジュールのゼロからの実装とテスト
- 批判的レビューと構造的盲点の指摘

### 9.2 AI エージェントが苦手だったこと

- 12,700 行の既存コードの中で壊さずに kernel を切り替える
- 「計画が正しい」から「動くデプロイ」への最後の一歩
- 「この方向はもうダメだ」と明示的に提言すること

### 9.3 構造的な教訓

- **AI の実行力は対象コードの複雑さに依存する**: 分析力はコードサイズに依存しないが、安全な修正の実行力は依存する。P2（12,700行）では修正が通らず、P3（5,500行）では通った。
- **解析→修正→また止まる→また解析 のループは人間が断ち切る必要がある**: AI は聞かれれば分析し、求められれば計画を出す。「やめよう」とは言わない。
- **コードベースのサイズ管理は設計判断である**: AI エージェントの実行力が届く範囲にコードを保つことが、協働の成功条件になる。

---

## 10. 関連ドキュメント索引

### Canonical（現時点の正本）

| ファイル | 役割 |
|---|---|
| `p2-meta-design-clarification-2026-04-11.md` | P2自身と実装者の主語分離、中心命題 |
| `p2-why-repeats-failures-2026-04-11.md` | 失敗反復の主因整理（正本診断） |
| `p2-session-kernel-implementation-2026-04-11.md` | session_action_loop_v1 の実装記録 |
| `p2-recursive-frame-rules-2026-04-11.md` | 再帰フレーム運用規則 |
| `P2_INDEX.md` | 全 P2 ドキュメントの正本インデックス |

### Supporting（補助）

| ファイル | 役割 |
|---|---|
| `p2-context-vs-tool-loop-2026-04-11.md` | コンテキスト粗さと tool loop 不在の関係整理 |
| `p2-context-failure-ack-2026-04-11.md` | 実装側の失敗認識 |
| `p2-what-to-do-next-meta-2026-04-11.md` | 次にやるべきことのメタ原則 |
| `p2-context-carryover-design-2026-04-11.md` | 3層コンテキスト設計 |
| `p2-stability-log-investigation-2026-04-09.md` | 長時間 run の統計分析 |
| `p2-minimal-kernel-cut-2026-04-11.md` | 最小 kernel 案（再帰の扱いに衝突あり） |
| `claw-code-purpose-achievement-analysis-2026-04-11.md` | claw-code 実装調査 |
| `open-source-agent-purpose-execution-comparison-2026-04-11.md` | OSS agent 横断比較 |

---

## 11. 今後の実装 AI エージェントへの指針

1. **P2 の handoff ドキュメントを読む順番**: `P2_INDEX.md` → `p2-meta-design-clarification` → `p2-why-repeats-failures` → `p2-session-kernel-implementation`
2. **再帰階層コンテキストは否定されていない**: session loop が安定した後に載せるべき機能として保留されている
3. **コードサイズを意識する**: 核が安定しないまま周辺を増やすと、修正困難になる。P2 の教訓
4. **summary より raw event**: LLM への主入力は要約ではなく action/result の因果列にする
5. **全文書き換えを避ける**: 最小差分の編集を基本にする
6. **「動くか」を最優先で確認する**: テスト通過 ≠ live で動く。なるべく早く live 環境で確認する
7. **解析ループに入ったら立ち止まる**: 「分析は正しいが動かない」が繰り返されたら、コードベースのサイズやアーキテクチャの根本を疑う
