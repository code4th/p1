# P3 全フェーズ計画

日付: 2026-04-18

## 結論

`P3` は次の順で進める。

1. deterministic core を固める
2. grounded answer を evidence 主導で安定化する
3. benchmark と observability を phase-aware にする
4. 軽量 reflection を導入する
5. 難問だけ selective deliberation を使う
6. その後に初めて planning/search を限定導入する

この順にする理由は、現時点の `P3` の失敗モードが

- first tool call 不能
ではなく
- command 実行後の grounded finish 遅延

だからである。

## 現在地

### 進捗メモ

- `current_phase` の runtime 実装と dashboard 表示は入った
- benchmark の phase 指標 (`all_required_commands_done_ms`, `answer_ready_ms`, `finish_ms`, `timed_out_phase`) も入った
- terminal finish の judge bypass 型 `deterministic fallback` は相談なしに完了判定を迂回するため戻した。機械判定は LLM judge の前段/補助として扱い、judge を迂回する場合は別途相談する
- terminal multi-step は controller-led に切り替え、live benchmark で `pwd_short`, `find + head`, `git status + pwd` の required command 実行までは到達した
- benchmark ケースの `find + head` は evidence と期待値が噛み合うよう `head -n 8 AGENTS.md` に修正した
- `expected artifact` が明示されている場合は、存在しなければ `finish blocked` にする guard を追加した
- dashboard 判断のため `system_note.content` は日本語表示を維持し、benchmark/test 用の機械判定は `code` / `reason_code` へ分離する方針にした
- dashboard は `p3_core.dashboard` package 構成に統一し、操作者が現象確認しやすいよう主要表示ラベルを日本語化した
- 失敗した同一コマンドの再試行は、再度 LLM に判断を渡さず `command_blocked/repeated_command` として turn を失敗終了する
- live `pwd -> ls` で、Evidence と一致して見える final answer が LLM-as-judge により `finish_blocked/grounding_issues` になった。過去実行時の judge 生応答は記録されていなかったため、以後は `grounding_judge` event に `judge_prompt/raw_response/content_text/thinking_text/decision` を残す
- grounding judge は `OK/NG` prefix 判定をやめ、JSON verdict を要求する。`ng` と `invalid_output`/`invalid_json`/`empty_output`/`error` は別 reason として記録し、judge が壊れたケースを「根拠不足」と混同しない
- P3実験用の受動的な実況解説者を `observer_enabled` で有効化できるようにする。実況解説者はAIエージェント研究開発のエキスパートとして、システムとLLMのやりとりを見て `observer_note` に日本語解説を出す。初期段階では介入・制御判断・完了判定には使わない
- 実況解説者は step 後の事後観測として動く。`tool_result` 後、`finish_blocked` 後、native chat 完了後に加え、LLM 応答が tool_call JSON として解釈できない場合も `llm_output_issue` として1ステップ扱いで解説する
- 実況解説者は、何が起きたかだけでなく、LLM がなぜブロックされる出力に至ったか、渡されたコンテキストに失敗誘発要因がないかを点検する
- dashboard の operation status は `running/success/failed/blocked` に分ける。`finish_blocked` が最後の決定的イベントなら外側ランナーが戻っても SUCCESS と表示しない
- 後追いの `observer_note` は `turn_id` / `queue_id` で operation に紐づけ、更新後に古い解説へ戻らないようにする
- reflection は `failure_class` を持つ構造化保存に変え、benchmark でも再発数を集計できるようにした
- stale な `running` は、worker が止まっていて `finish_blocked` がある場合は dashboard snapshot で `blocked` に正規化する
- ただし live dashboard では、長い model 応答中の細かい phase 表示と実際の進行の対応は引き続き確認対象である
- そのため `phase 遷移が live run で正しく変わることを確認する` は継続確認にする

### できていること

- local Ollama を native で呼べる
- `Terminal Agent` として `run_command` を打てる
- `1 step = 1 command` は enforced されている
- required command の 1 手目と 2 手目までは fast path でかなり速く打てる
- `tool_result` は `stdout / stderr / returncode / cwd / shell / duration_ms` を保持している
- benchmark を直列実行できる
- dashboard から benchmark 結果を読める
- dashboard で操作ごとの flow、blocked reason、observer commentary を日本語で確認できる
- LLM が JSON/tool contract を外した中間失敗も `llm_output_issue` として記録できる
- LLM 出力失敗は `empty_output` / `missing_json_object` / `json_parse_error` / `length_truncated` / `invalid_tool_envelope` に分類し、`done_reason` などの stream metadata を runtime status と dashboard に出す
- action prompt は現在のユーザー依頼、直近 tool evidence、重要 system note、短い reflection に絞る。`observer_note`、古い別タスク、失敗した長大 assistant output は行動モデルへ再投入しない
- 長いコード全文を単一 JSON tool 引数に入れる運用を避けるため、`append_file` と `replace_text` を追加した。`write_file` / `append_file` は 2000 bytes 超の content chunk を拒否する
- coding/tool turn は `workspaces/runs/<turn_id>/` の専用 LLM workspace で実行する。P3 の state/log と LLM の途中生成物を分離し、dashboard には current/last workspace を表示する
- 解説者の独自短縮 timeout は廃止し、runtime `chat_timeout_seconds` と同じ扱いにした

### 実測で分かっていること

短い live benchmark では、`devstral:latest` は 5 秒以内に required command に入れるケースがある。

- `pwd`
- `pwd -> ls`
- `find AGENTS.md -> head -n 8 AGENTS.md`
- `git status -> pwd`

一方で、直近に残っている runtime ログでは `finish` event まで到達せず、grounded final answer を返す前に停止または case timeout している。

したがって次の主目標は

- `first tool call latency`
ではなく
- `multi-step continuation and finish latency`

である。

## 設計原則

### 原則 1

`ACI` と `runtime orchestration` を強くする。

根拠:

- SWE-agent は ACI の改善が性能差に強く効くと示している
- OpenHands は stateless / event-driven / step 実行で組んでいる
- SWE-ReX は agent logic と execution runtime を分離している

### 原則 2

難しくないところを LLM に考えさせすぎない。ただし汎用性を削るほどの固定テンプレート化は避ける。

根拠:

- Agentless は複雑 agent より段階分離の方が強いことを示している
- 現在の P3 も command 実行後の finish で無駄に待っている
- ただし benchmark 固定ケースごとのハードコードは将来の汎用性を削る

### 原則 3

reflection / search / extra test-time compute は否定しないが、常時ではなく限定投入する。

根拠:

- Reflexion は試行間の軽量反省の有効性を示す
- Thinking Longer, Not Larger は open model でも追加 compute が効くことを示す
- SWE-Search は探索強化の効果を報告している
- ただし今の P3 の失敗は探索不足より finish 遅延である

### 原則 4

local LLM の遅さを理由に、システム側へ過剰な速度特化ロジックを積みすぎない。

根拠:

- 局所的な shortcut は有効だが、ケース追加のたびに分岐が増えると保守不能になる
- local LLM はそもそも一定の待ち時間を持つため、無理な高速化はスケールしない
- 速度改善は controller の責務分解、prompt 圧縮、phase 分離のような一般化可能な方法を優先する
- benchmark 固定ケース専用の高速経路は、汎用性と将来拡張性を損なう

## フェーズ 1: Deterministic Core

### 目的

required command の実行と evidence 取得を、LLM の気分ではなく controller の phase として安定化する。

### やること

1. terminal task を phase 化する
   - `DISCOVER_REQUIRED_COMMANDS`
   - `EXECUTE_MISSING_COMMANDS`
   - `SYNTHESIZE_FROM_EVIDENCE`
   - `FINISH`
2. `EXECUTE_MISSING_COMMANDS` 中は fast path を使う
3. `required command` が全部終わるまでは追加探索を抑える
4. `tool_result` を evidence store として整理する
5. `run_command` の各 step で `returncode / timeout / expected artifact` を評価する
6. エラー時は fast path を即中断し、通常の LLM 推論ループへ recovery fallback する
7. phase 可視化に必要な最小 observability をここで同時に入れる
8. fast path は一般化できる最小範囲に留め、benchmark 専用 shortcut を増やさない

### 実行チェック

- [x] `1 step = 1 command` を維持する
- [x] required command の fast path 継続を入れる
- [x] `current_phase` を runtime status に追加する
- [x] `run_command` 失敗時の recovery fallback note を入れる
- [x] 同一 failed command の再試行ブロックを入れる
- [x] expected artifact 不在での fallback 条件を実装する
- [x] `current_phase` を dashboard に表示する
- [ ] phase 遷移が live run で正しく変わることを確認する

### 受け入れ基準

- `pwd -> ls`
- `find -> head`
- `git status -> pwd`

の各ケースで required command が確実に全部実行される。

### 進まない理由

- required command 抽出が不安定
- redundant command loop が再発する
- shell 実行結果の event 化が粗い
- error propagation が弱く、失敗後も不要な command を続ける

## フェーズ 2: Evidence-Led Grounded Finish

### 目的

command 実行後の grounded final answer を、benchmark 固定テンプレートに過学習させず短く正確に返す。

### やること

1. `tool_result` から answer 用 evidence を正規化する
2. final answer は generic な evidence summarizer で作る
3. summarizer は小型高速モデルを優先し、context は `User Goal + normalized evidence` のみに絞る
4. benchmark 固定ケース専用のハードコードテンプレートは持たない
5. rule-based shortcut は本当に単純なケースに限定する
6. unsupported detail を answer に含めない validation を finish 前に必ず通す
7. shortcut を追加する場合も、特定ケース専用ではなく evidence schema 単位で再利用できる形に限定する

### 実行チェック

- [x] terminal finish 用の evidence normalization を実装する
- [x] summarizer の入力を `User Goal + normalized evidence` にする
- [x] terminal finish に lightweight summarizer path を追加する
- [x] summarizer 失敗時の deterministic fallback 文面を定義する
- [x] unsupported detail validation と summarizer 出力の相性を live で確認する
- [ ] `pwd -> ls` 系ケースで grounded final answer が返ることを live benchmark で確認する

### 受け入れ基準

- benchmark で `missing_commands = []`
- `missing_fragments = []`
- `final_answer` が evidence 内に根拠を持つ

### 進まない理由

- evidence normalization が弱く summary 品質が落ちる
- evidence 抽出が足りず fragment 判定が誤る
- 速度最適化の分岐が増えすぎ、設計が benchmark 依存になる

## フェーズ 3: Phase-Aware Benchmark And Observability

### 目的

失敗箇所を benchmark で正確に切り分ける。

### やること

1. benchmark 指標を phase 分離する
   - `first_tool_call_ms`
   - `all_required_commands_done_ms`
   - `answer_ready_ms`
   - `finish_ms`
   - `timed_out_phase`
2. benchmark ケースを 3 層に分ける
   - basic terminal grounding
   - multi-step terminal grounding
   - tool + synthesis
3. ranking と next target を phase ベースで決める
4. dashboard では phase 境界を同時に見えるようにする
5. `current phase` を runtime status に出す
6. `Start -> Finish Flow` に phase 境界を表示する
7. `blocked` / `failed` / `success` を dashboard operation status として分離する
8. 解説者が step 単位で、実行結果・ブロック理由・コンテキスト点検を表示する
9. tool_call JSON にならなかった LLM 応答も `llm_output_issue` として観測対象にする

### 実行チェック

- [x] `all_required_commands_done_ms` を benchmark に追加する
- [x] `answer_ready_ms` を benchmark に追加する
- [x] `finish_ms` を benchmark に追加する
- [x] `timed_out_phase` を benchmark に追加する
- [x] benchmark ケース定義を dashboard で表示する
- [x] `current_phase` を dashboard で表示する
- [x] `Start -> Finish Flow` に phase 境界を表示する
- [x] benchmark summary を phase ベースで並べ替える
- [x] operation status に `blocked` を追加し、外側ランナー成功と task 成功を混同しない
- [x] `finish_blocked` の理由を dashboard 操作カード上部と live output に表示する
- [x] `observer_note` を operation flow 内に表示し、上部の重複パネルは外す
- [x] `tool_result` 後の step commentary を日本語で表示する
- [x] `finish_blocked` 後に失敗要因とコンテキスト点検を表示する
- [x] `llm_output_issue` を記録し、tool_call JSON にならなかった中間失敗も解説する
- [x] `turn_id` / `queue_id` で後追い解説を operation に紐づける

### 受け入れ基準

- 「どこで詰まったか」が benchmark JSON だけでなく summary でも分かる
- first tool call の問題と finish 問題を取り違えない
- live turn と benchmark の両方で phase が見える
- dashboard refresh 後も最新の解説者コメントが該当 operation に残る
- `finish_blocked` が起きた turn は SUCCESS ではなく `blocked` として見える
- LLM 応答がツール呼び出しに変換できなかった時点で、その失敗が解説される

### 進まない理由

- benchmark 自体が runtime 例外で partial evidence を失う
- phase 定義が曖昧で比較不能になる
- snapshot 更新の粒度が粗い
- phase 情報が runtime から UI へ十分渡っていない

## フェーズ 4: Light Reflexion

### 目的

失敗パターンを次 turn に短く持ち越す。

### やること

1. failed turn の末尾で 1-3 行の reflection を作る
2. 次 turn の system prompt に直近 reflection を 3 件まで差し込む
3. reflection は
   - repeated command
   - unsupported detail
   - missing finish
   のような failure class 単位で分類する

### 実行チェック

- [x] failed turn の reflection 保存を実装する
- [x] reflection を次 prompt に差し込む
- [x] reflection の failure class を構造化する
- [x] benchmark 上で再発率を見る指標を足す

### 受け入れ基準

- 同じ failure class の再発率が benchmark 上で下がる
- prompt 増加が小さい

### 進まない理由

- reflection が長くなり prompt 汚染を起こす
- 局所最適化で別 failure を増やす

## フェーズ 5: Metric-Triggered Selective Deliberation

### 目的

難しいケースだけ追加 compute を使う。

### やること

1. easy case では deterministic path を優先する
2. hard case だけ追加 reasoning を許す
3. selective routing 条件は自然言語の曖昧さ判定ではなく、物理メトリクスで決める
   - `missing_commands` が残る
   - `finish_blocked` が連続する
   - `tool_result.ok=false` が一定回数出る
   - `step_index` が閾値を超える
   - `search/read/list` が一定回数を超えても evidence が増えない
   - 同系統 command の再実行が起きる
4. 速度改善のために routing 条件を増やしすぎず、一般化可能なメトリクスのみ採用する

### 実行チェック

- [ ] `missing_commands` ベースの deliberation trigger を実装する
- [ ] `finish_blocked` 連続発生ベースの trigger を実装する
- [ ] `tool_result.ok=false` 回数ベースの trigger を実装する
- [ ] `step_index` ベースの trigger を実装する
- [ ] `search/read/list` 停滞ベースの trigger を実装する
- [ ] easy case の latency が悪化しないか benchmark で確認する

### 受け入れ基準

- easy case の latency を悪化させない
- hard case の success rate が上がる

### 進まない理由

- selective 条件が甘く、多くのケースで無駄に長考する
- local LLM の遅さで overall UX が悪化する
- 高速化ルールが増えすぎ、運用時に挙動説明ができなくなる

## フェーズ 6: Limited Planning / Search

### 目的

本当に必要な難問だけ探索を導入する。

### やること

1. terminal grounding の core が安定した後にだけ検討する
2. branch 数は小さく保つ
3. search は file localization や command candidate ranking のみに限定する

### 実行チェック

- [ ] hard bucket を定義する
- [ ] search を file localization に限定して試す
- [ ] command candidate ranking に限定して試す
- [ ] easy bucket のコスト悪化を benchmark で確認する

### 受け入れ基準

- benchmark の hard bucket でのみ有意差がある
- easy bucket のコスト悪化が小さい

### 進まない理由

- search の複雑性が benchmark 改善を上回る
- dashboard / observability が追いつかずデバッグ不能になる

## 優先順位

今すぐやる順序はこれで固定する。

1. フェーズ 1
2. フェーズ 2
3. フェーズ 3
4. フェーズ 4
5. フェーズ 5
6. フェーズ 6

## レビュー

### 結論

このフェーズ順は妥当。

### 理由

- 現在の失敗モードに直結している
- deterministic core を先に固め、search は後ろに追い出している
- local LLM の遅さを前提に、長考を常時使わない設計になっている
- benchmark 過学習を避けつつ、汎用 summarizer へ寄せている
- 速度改善を局所最適化ではなく、一般化可能な設計改善として扱っている

### 反対意見への回答

- 「もっと search を早く入れるべき」:
  いまの P3 は command 実行後に詰まっており、探索不足が主因ではない
- 「全部 controller 化しすぎでは」:
  evidence normalization と lightweight summarizer を使うため、固定スクリプト化には寄せていない
- 「benchmark 偏重では」:
  observability を benchmark 後回しにせず、core 実装と並走させる
