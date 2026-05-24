# P4 要求仕様書 (改訂版)

日付: 2026-04-21

## 目的

P4 は、安定した P3 の session loop を土台に、P2 で設計された **再帰階層コンテキスト** を安全に実装することを目的とする。P3 の動作を維持したままフレーム階層を導入し、LLM が問題を階層的に分解し、局所問題に集中して結果を親に戻せるようにする。

## P4 の定義

P4 = リファクタリング済み P3 + P2 の再帰階層コンテキスト。ただし、リファクタリングは行数削減を目的とせず、責務分離と機能差分ゼロを重視する。

### 手順

1. P3 をそのままコピーして P4 プロジェクトを作成する（P3 は凍結）
2. P4 内でリファクタリングして `runtime.py` の責務を分離する
3. P4 に P2 のフレーム階層コンテキストを移植する（LLM からはツールとして見せ、runtime 内部では制御アクションとして処理）

## 中心テーマ

**LLM が問題を階層的に分解し、局所問題に集中し、必要な情報が揃ったら親フレームに戻れるようにすること。**

## 機能要求

### FR-1: フレーム階層と遷移

- LLM が `open_child_frame` アクションを選択することで子フレームを開ける。実装ではこれは kernel control action として処理する。
- 子フレームは親フレームの goal とコンテキストを継承しつつ、独自の局所 goal を持つ。
- LLM が `return_to_parent` アクションを選択することで親フレームに戻れる。`summary` と `findings` が親フレームに返される。こちらも実装では kernel control action として処理する。
- フレームの深さは最大 4 階層に制限する。深さ超過時はエラーが返る。
- フレーム遷移はログに `frame_opened` / `frame_returned` として記録される。

### FR-2: フレーム間コンテキスト継承

- 親から子へは `inherited_context` を通じて親の goal と重要な文脈（概要）を渡す。
- 子から親へは `return_payload` に `summary` と `findings` を格納して返す。
- 各フレームの session events は独立しており、子のイベントが親を汚染しない。親は `child_return` イベントを通じて子の結果を受け取る。

### FR-3: Frame Working Memory（最小構成）

フレームごとに保持される理解状態。初期段階では以下の4項目とする。

- `observations`: 読んだファイル・実行したコマンド・得られた事実など確認済みの情報
- `current_focus`: 今このフレームで注目している対象
- `unresolved_questions`: まだ答えが出ていない問い
- `avoid_repeating`: 失敗したコマンドや繰り返すべきでない操作

推論や仮説、意思決定は session events に残し、working memory には含めない。必要に応じて後続フェーズで分類を拡張する。

### FR-4: フレームの戻り条件

子フレームが親に戻る条件は以下のいずれか。

1. 局所 goal を満たした。
2. 局所 goal は未達でも、親が次の判断をするのに十分な情報が得られた。
3. これ以上その階層で進めても情報利得が小さいと判断した。
4. この問題は親の goal 設定自体が誤りであると判断した。

### FR-5: 既存機能の維持と互換境界

- P3 の全機能（session loop、tool executor、dashboard、observer、grounding judge）はそのまま動作する。再帰フレームを使わないタスクは P3 と同じ結果になることを保証する。
- フレーム化により影響を受ける既存機能について境界を明確にする。
  - `_conversation_messages`: 現在フレームの `session_events` と親から受け取った context summary を合成して返す。
  - `grounding judge`: フレーム内の evidence でのみ判定し、親の evidence は参照しない。
  - `observer`: tool 呼び出し→結果の流れがフレーム遷移で断絶するため、`frame_opened` / `frame_returned` 用の解説パスを追加する。
  - `finish` 処理: 子フレームでは `finish` が呼ばれた場合、ブロックして「子フレームでは return_to_parent を使用してください」という理由を返す。リダイレクトはしない（暗黙的な動作変更を避ける）。root フレームでのみ `finish` が許可される。
  - `dashboard snapshot`: `current_frame_id` を含め、表示をフレームごとに切り替える。

### FR-6: 新規ユーザーメッセージによるフレームリセット

- 子フレームで作業中にユーザーから新しいメッセージが送信された場合、現在のフレーム階層をすべて abandon（status: abandoned）し、root フレームに戻ってから新しいメッセージを処理する。
- abandon されたフレームの session_events はログとして残すが、新しい turn のコンテキストには含めない。
- これにより、ユーザーの意図変更が即座に反映され、古いフレーム階層に捕まることがない。

### FR-7: フレーム safety valve

- 1フレームあたりの最大ステップ数を 15 に制限する。超過した場合、runtime が自動的に `return_to_parent` を発行し、「ステップ上限に達したため親フレームに戻ります」というメッセージとともに現時点の observations を return_payload として返す。
- root フレームでステップ上限に達した場合は、finish を強制し、「ステップ上限に達しました。現時点の結果を報告します。」とする。
- これにより、P2 の「63回分解0回帰還」のような無限ループを構造的に防止する。

## 非機能要求

### NFR-1: コードの責務分離

`runtime.py` は session loop の核のみを担い、その他のロジックを周辺モジュールへ分離する。行数削減自体を目標としないが、モジュール境界を明確にすることで将来の変更が容易になる。

### NFR-2: 観測可能性

- Dashboard にフレーム階層の表示を追加し、現在フレームの working memory や goal を確認できるようにする。
- Observer がフレーム遷移 (`frame_opened` / `frame_returned`) を解説する。
- 各フレームの working memory が snapshot から参照できる。

### NFR-3: 段階的実装

フレームの最小実装（root → child → return の流れ）を in-memory で先に動かし、working memory や永続化、dashboard などの周辺機能は後続フェーズで追加する。永続化（JSONL 保存）はフレーム遷移が安定してから実装する。

### NFR-4: フレームと queue の関係

1 queue item = 1 ユーザーメッセージ = 1 turn。フレーム遷移（open_child_frame / return_to_parent）は turn 内のステップとして発生する。1 turn の中で複数回のフレーム遷移が起きうる。新しい queue item（ユーザーメッセージ）が追加された場合は FR-6 に従いフレームをリセットする。

## 今回の非対象

- 自己改善ループ（candidate / validation / promotion）
- Cross-Attempt Memory（試行をまたぐ戦術記憶）
- 自動的なフレーム発火条件（LLM の判断に任せる）
- skill / memo の統合

## 受け入れ条件

P4 の機能を受け入れるための基準は以下の不変条件で構成される。

1. 子フレームの `session_events` が親フレームに混入しない。
2. `return_to_parent` 後に親フレームが次の action を継続できる。
3. 子フレームの `return_payload` が親フレームの events に `child_return` として記録される。
4. 深さ制限（4階層）を超えるとエラーが返る。
5. 再帰フレームを使わないタスクが P3 と同じ結果になる（35/35 のテストがパスする）。

帰還率や行数などの数値目標は運用上のモニタリング指標とし、受け入れ条件とはしない。