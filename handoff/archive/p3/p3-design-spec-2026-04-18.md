# P3 デザイン仕様

日付: 2026-04-18

## 設計方針

`P3` は「session runtime + tool executor + observability」の3層に切る。

## 1. ディレクトリ構成

`p3-core/`

- `p3_core/workspace.py`
  - path 解決
  - bootstrap
  - json/jsonl persistence
- `p3_core/models.py`
  - task 性質から model 選択
- `p3_core/ollama_client.py`
  - Ollama `/api/chat` adapter
- `p3_core/tools.py`
  - tool registry と execution
- `p3_core/runtime.py`
  - session loop
  - queue 処理
  - worker loop
- `p3_core/dashboard/`
  - `snapshot.py`: dashboard snapshot builder
  - `templates.py`: HTML renderer
  - `server.py`: HTTP endpoints and dashboard-triggered runners
- `p3_core/cli.py`
  - operator entrypoint

## 2. 実行モデル

### Session

- active session id は runtime state に保持
- event は JSONL に append-only
- event type:
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

### Turn loop

1. queue から message を取る
2. turn ごとに `workspaces/runs/<turn_id>/` の専用 LLM workspace を作る
3. goal と recent events と tool spec と LLM workspace path から prompt を組む
4. model router で active model を選ぶ
5. Ollama に送る
6. JSON action envelope を parse する
7. tool 実行 or finish
8. JSON action envelope として解釈できない LLM 応答は `llm_output_issue` として event 化し、`empty_output` / `missing_json_object` / `json_parse_error` / `length_truncated` / `invalid_tool_envelope` などに分類する。解説者はその時点で失敗要因とコンテキスト点検を記録する
9. tool 実行結果を event と prompt snapshot に保存
10. `tool_result` の後に、受動的な実況解説者が step 単位で `observer_note` を追加する
11. finish 前に required command / expected artifact / grounding judge を通し、失敗時は `finish_blocked` として日本語の理由を記録する
12. queue が空で finish なら idle

### LLM workspace

P3 の state / session / dashboard 用 root と、LLM が coding / command 実行に使う workspace は分離する。

- state root: `state/`, `logs/`, `config.json`
- LLM workspace: `workspaces/runs/<turn_id>/`
- `read_file` / `write_file` / `append_file` / `replace_text` / `search_code` / `run_command` は turn 専用 LLM workspace 配下で実行する
- `llm_workspace` は event、planning log、runtime status に残す
- dashboard は `current_llm_workspace` または `last_llm_workspace` を表示する
- 失敗した途中ファイルは run workspace に隔離され、P3 root や別 turn を汚さない

### Response contract

model には次の JSON を返させる。

```json
{
  "analysis": "brief reasoning",
  "assistant_message": "optional user visible text",
  "tool_name": "read_file",
  "tool_args": {
    "path": "README.md"
  }
}
```

`tool_name == "finish"` のときは task 完了。

JSON contract は provider-native tool calling ではなくテキスト JSON であるため、LLM が文章だけ・途中切れ JSON・Markdown code block を返す場合がある。その場合はツール実行済みとは見なさず、`system_note.code = llm_output_issue` として記録する。

長いコード全文を `tool_args.content` に詰める運用は禁止する。新規ファイルは小さな `write_file` で開始し、`append_file` で 2000 bytes 以下の chunk を追加する。既存ファイル編集は `read_file` 後に `replace_text` で最小差分を適用する。`write_file` / `append_file` は大きすぎる chunk を拒否し、dashboard には LLM parse failure と stream metadata を表示する。

controller fast path は、ユーザーが明示したコマンドを即時実行する用途に限定する。benchmark 的な coding prompt に対して固定成果物を合成する scaffold fast path は、能力評価を汚染するため持たない。

行動モデルに渡す文脈は、現在のユーザー依頼、直近の tool call/result、重要な system note、短い reflection に絞る。実況解説者の `observer_note`、古い別タスク、過去の失敗した長大な assistant output は action prompt に再投入しない。

## 2.5 判定と解説

### Grounding judge

- finish 前に evidence と final answer を照合する。
- judge には JSON verdict を要求する。
- `ok` / `ng` / `invalid_output` / `invalid_json` / `empty_output` / `error` を区別する。
- judge が壊れたケースを根拠不足そのものと混同しないため、`grounding_judge` event に prompt / raw_response / content_text / thinking_text / decision を残す。
- 相談なしの deterministic answer bypass は行わない。機械判定は LLM judge の前段・補助として扱う。

### Passive live commentator

実況解説者は `runtime.observer_enabled` が true のときだけ動く。初期段階では介入・制御判断・完了判定には使わず、観測と説明だけを行う。

動作タイミング:

1. LLM 応答が tool_call JSON として解釈できなかった直後
   - `system_note.code = llm_output_issue`
   - `observer_note.reason_code = llm_output_issue_commentary`
   - 「なぜツール呼び出しに失敗したか」「コンテキストが失敗を誘発していないか」を解説する
2. tool 実行が終わり `tool_result` が記録された直後
   - `observer_note.reason_code = step_commentary`
   - 直近 LLM 応答、tool_result、prompt excerpt を見て step 単位で解説する
3. finish がブロックされた直後
   - `system_note.code = finish_blocked`
   - `observer_note.reason_code = system_judgement_commentary`
   - ブロック理由、judge の状態、LLM が失敗出力に至った仮説、コンテキスト点検を出す
4. native chat が完了した後
   - 入力、LLM回答、システム判定をまとめる

解説者は「1ステップ遅れの実況」であり、LLM とシステムのやり取りがログに残った後に動く。並列監視ではない。

解説者には独自の短い timeout を設けない。runtime の `chat_timeout_seconds` を使い、解説だけが短時間 timeout して観測ログを欠落させる状態を避ける。

controller / fast path のように LLM 応答が存在しない step では、解説者 LLM は呼ばない。tool evidence は event と dashboard flow で確認し、解説者が本処理の critical path を止めることを避ける。

## 3. Model 選択

- `qwen3-coder`
  - read/search/write など code-centric
- `devstral`
  - shell / command / build / env / execution-centric
- `glm-4.7-flash`
  - 軽い status / quick answer
- `gemma4:26b`
  - 一般 reasoning / planning / ambiguous ask

heuristics:

- 最新 user message と recent tool history から選ぶ
- 理由を `runtime/status.json` と prompt snapshot に保存する

## 4. ダッシュボード

### API

- `GET /api/health`
- `GET /api/snapshot`
- `GET /api/events`
- `POST /api/message`

現時点の dashboard HTTP API は message 投入と snapshot/SSE 表示を主対象にする。goal/control は CLI/runtime 側の責務であり、dashboard HTTP endpoint としては未実装。

### snapshot 要素

- goal
- runtime status
- current model
- queue counts
- latest session meta
- recent conversation
- recent tool results
- recent prompt snapshots
- recent operations
- flow steps
- activity updates
- blocked reason
- observer notes

### operation status

- `running`: 実行中
- `success`: システム判定上、完了または native chat が成功
- `failed`: ランナーまたは tool が失敗
- `blocked`: `finish_blocked` があり、成功完了していない

`finish_blocked` 後に解説者ログが後追いで追加されても、operation は `blocked` のまま維持する。operation と後追い event は時刻だけでなく `turn_id` / `queue_id` でも結びつける。

## 5. 安全境界

- file tool は workspace outside を拒否
- `write_file` / `append_file` は 2000 bytes を超える content chunk を拒否し、chunked write を促す
- command は workspace cwd 固定
- 危険 command pattern を deny

## 6. テスト方針

- fake LLM backend で runtime loop を deterministic に確認
- dashboard はローカル HTTP で health/snapshot/message を確認
- Ollama 実機依存テストは入れない

## デザインレビュー

結論: デザインは十分に小さく、拡張余地も残している。

良い点:

- `P2` から dashboard/SSE のみ借り、自己改善構造は持ち込んでいない
- backend/tool/runtime が分離されており fake backend test が可能
- session raw events を主記録にしている

注意点:

- JSON action contract は provider native tool-calling より脆い
- ただし今回は `Ollama + 複数モデル横断` を優先し、この tradeoff は妥当
- 一部モデルは `message.content` ではなく `thinking` に長い出力を出すため、client 側で両方を回収し、JSON repair retry を前提にする
