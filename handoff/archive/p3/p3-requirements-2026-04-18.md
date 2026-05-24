# P3 要求仕様

日付: 2026-04-18

## 目的

`P3` は、ユーザーの自然言語要求を受け取り、ローカル `Ollama` LLM を使って目的達成まで進む最小エージェント実行基盤である。

`P2` の失敗を踏まえ、自己改善や重い階層制御を先に作らず、まず次を安定化する。

- 会話を受け取る
- 現在の目的を保持する
- モデルが tool を選ぶ
- tool 結果を次ターンへ戻す
- `finish` まで反復する
- 外部から状態・会話・制御を確認できる
- LLM とシステムのやり取りを step 単位で観測し、失敗理由とコンテキスト問題を後から確認できる

## 機能要求

### 1. 会話起点の実行

- 単一のアクティブ session を持つ
- ユーザーメッセージを session event として保存する
- message を queue に積み、worker が順に処理する
- 会話と tool 実行が同じ event 列に残る
- LLM 応答が tool_call JSON として解釈できない場合も、失敗イベントとして保存する
- LLM 応答失敗は `empty_output` / `missing_json_object` / `json_parse_error` / `length_truncated` / `invalid_tool_envelope` などに分類し、runtime status と dashboard から確認できる
- 行動モデルへ渡す文脈は現在タスク、直近 tool evidence、重要 system note、短い reflection に絞り、実況解説や古い別タスクを再投入しない
- coding/tool turn ごとに専用 LLM workspace を作り、途中生成物と実行結果を `workspaces/runs/<turn_id>/` に隔離する

### 2. ローカル LLM 実行

- 実行先は `Ollama` のみ
- モデル候補:
  - `gemma4:26b`
  - `glm-4.7-flash`
  - `qwen3-coder`
  - `devstral`
- ターンごとに task の性質から model を選ぶ
- 選択理由を trace に残す

### 3. 最小 tool 実行

- `list_files`
- `read_file`
- `search_code`
- `write_file`
- `append_file`
- `replace_text`
- `run_command`
- `finish`

制約:

- read/write/search は workspace 配下に限定する
- file tool と command tool の workspace は、既定では turn 専用 LLM workspace とする
- `write_file` / `append_file` は大きすぎる content chunk を拒否し、小分け編集へ誘導する
- 既存ファイル編集は `replace_text` で最小差分を適用できる
- command は workspace 配下で動かす
- 明白な破壊的 command は deny する

### 4. 状態永続化

- session meta
- session events
- prompt snapshots
- queue
- runtime status
- goal
- config

を JSON / JSONL で保存する。

### 5. ダッシュボード

- `P2` のようにブラウザから見えること
- snapshot API を持つこと
- SSE で更新できること
- message 投稿ができること
- goal 更新と worker start / stop は CLI/runtime 側の責務とし、現 dashboard API では message 投稿と snapshot/SSE 表示を主対象にする
- 直近 event, prompt, model, tool result, queue 状態を見られること
- 実行操作ごとに flow、phase、status、blocked reason を見られること
- 解説者の日本語 commentary を操作 flow 内で見られること
- dashboard 更新後も、後追いの解説者コメントが該当操作から外れないこと
- `finish_blocked` が発生した turn を SUCCESS と表示しないこと

### 6. CLI

- workspace bootstrap
- goal 更新
- chat message 送信
- loop 実行
- worker 実行
- dashboard 起動
- status 表示

### 7. 拡張性

- tool registry を差し替え可能にする
- LLM backend を差し替え可能にする
- session を複数化できる path 設計にする
- dashboard は snapshot builder を分離し、後で表示項目を増やせるようにする

## 非機能要求

- stdlib 中心で実装する
- 外部依存を増やさない
- audit しやすい raw event を主に残す
- failure 時にも途中状態が見える
- 外側ランナーの成否と task/judge の成否を混同しない
- judge の `ng` と `invalid_output`/`invalid_json`/`empty_output`/`error` を区別する
- 解説者は観測・解説のみを行い、初期段階では制御・完了判定・介入には使わない
- 解説者には独自の短い timeout を設けず、runtime の `chat_timeout_seconds` に揃える
- test は fake LLM で回る

## 今回の非対象

- 自己改善 loop
- generation 昇格
- 複雑な subagent runtime
- 長期 memory compaction
- provider native tool-calling 依存

## 要求レビュー

結論: この要求集合で実装開始してよい。

確認した点:

- `P2` の失敗要因だった「自己改善先行」を外している
- OSS 比較で共通だった `session -> tool -> result -> next turn` に収束している
- dashboard / CLI / persistence が最小でも運用可能な単位になっている

残リスク:

- 単一 session 前提なので multi-session は将来拡張
- command 実行は安全性より汎用性を優先しすぎると事故るため deny ルールを残す
