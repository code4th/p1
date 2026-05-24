# P4 Follow-Through Recovery: Don't Throw Away Whole Turns

Date: 2026-05-03
Status: Adopted
Related: `p4-coding-invariants-2026-04-24.md`, `p4-judge-verdict-first-2026-05-03.md`

## 観測 (Observation)

`/tmp/p4-demo` ダッシュボードでユーザが「複雑な迷路を作成して実行して表示して」を投げたところ、以下が起きた:

1. 08:34:39 user request 受信
2. 08:35:02 qwen3-coder が `list_files .` を実行 (空ディレクトリ確認)
3. 08:35:27 LLM が **2 つの JSON object を連続出力**:
   ```text
   {"analysis":"...","tool_name":"run_command",...}
   {"analysis":"ワークスペースは空です。...
   ```
4. 1 ターン目が `json_extraneous_text` で失敗 → `_chat_with_repair` がリトライ
5. リトライも同じパターンで失敗 → **ターン全体を放棄して operation:failed**
6. ユーザの request は何の前進もなく終了

ダッシュボード表示には `last_error: "LLM output did not satisfy machine-control schema"` だけが残り、なぜ完了できなかったのか / 何ができたのかが分からない状態に。

## 分類 (Classification)

`p4-coding-invariants-2026-04-24.md` の不変条件と照合:

- **Invariant 2 違反 (付け焼き刃の局所対応で塞がない)**: 「やり切る」という上位
  invariant が implicitly に「LLM 出力が完璧であること」に置き換えられていた。
  完璧でないと turn を全捨て、という強い癖が、ユーザの request を放棄する方向に働く。
- **Invariant 4 違反 (variation と asymmetry の混同)**: 「LLM 出力が完璧」 vs
  「全部失敗」の二値しか出口がなく、「envelope は valid だが余計テキストが付いた」
  という variation を救う対称的な経路が無い。
- **Invariant 8 違反 (分解は実行責務に着地)**: LLM が複数手を一括予測して 2 個の
  JSON を出すのは、「分解を実行責務に着地」できていない兆候。runtime 側でも、
  最初の有効な envelope を 1 ステップに切り戻す責務を持つべき。

## 設計判断 (Decision)

### Concept

「machine-control invariant = 1 ターン 1 envelope」を保ったまま、graceful
degradation 経路を追加する:

- LLM 出力が valid envelope (schema 適合) を **含んでいる** なら、余計テキストが
  あっても **最初の有効な envelope を採用** して前進する (やり切る invariant)。
- 余計テキストの存在は破棄せず、`system_note` (`code: llm_output_recovered`)
  として可視化する。
- 二値出口 ("perfect success" / "total failure") に **第三の状態 = "warning-accepted"**
  を加える。reason_code (`json_extraneous_text_recovered`) で識別可能にする。

### Invariants (固定)

1. envelope に valid な `tool_name` + `tool_args` が含まれ、schema 適合なら、
   raw が strict machine-json でなくても採用する。
2. recovery 採用時は必ず `code: llm_output_recovered` の system_note を残し、
   ユーザ・監査・dashboard が「何が起きて、何を選んだか」を追えるようにする。
3. recovery と「LLM 出力 fully clean」は status と event 上で識別可能であり続ける
   (`raw_output_is_machine_json: false`, `last_llm_parse_issue: "json_extraneous_text_recovered"`)。
4. recovery で採用される envelope は、`_extract_json_object` の決定論的アルゴリズム
   (最長 valid object を選択) によって一意に決まる。

### 反対の選択肢と却下理由

- **判断却下: prompt をさらに強化して LLM を矯正する** — `_json_repair_prompt` は
  既に "Do not put prose, Markdown, code fences, or hidden reasoning" と書いている
  が、LLM の自然な出力傾向 (複数手の同時計画) を完全に抑えられない。
  prompt の精度向上は別軸の改善であり、recovery 経路はその軸と直交して
  「やり切る」を保証する。
- **判断却下: retry 回数を大きく増やす** — 同じ LLM が同じ prompt で同じ失敗を
  繰り返す確率は高い。retry を 1〜2 回に保ち、かつ recovery 経路で envelope を
  採用する方が ユーザの待ち時間と完了率の両方で優れる。
- **判断却下: 失敗時に明示的に user に「もう一度お願いします」と返す** — これは
  「やり切らない設計」の延命であり、user 価値を生まない。

## 修正 (Implementation)

### 変更ファイル

- `p4-core/p4_core/llm_comm.py`
  - `_chat_with_repair` に **recovery 経路** を追加。`parse_issue ==
    "json_extraneous_text"` かつ envelope が valid + schema 適合なら、
    `code: llm_output_recovered` の system_note を出してから envelope を採用。
  - 戻り値の `parse_issue` を空文字に正規化し、後段の "turn 失敗" 経路に流さない。
  - `last_llm_parse_issue: "json_extraneous_text_recovered"` を runtime status に
    残し、識別可能性を担保。
- `p4-core/p4_core/workspace.py`
  - `DEFAULT_CONFIG.runtime.json_retry_limit` を 1 → 2 に変更。length_truncated
    などの recovery 不能 issue に対しても 2 回の repair 試行を許容。recovery
    経路と組合せて bounded best-effort を保証する。
- `p4-core/p4_core/dashboard/snapshot.py`
  - `_FAILURE_TRANSLATIONS` に `json_extraneous_text_recovered` を追加 (✓ マーク
    付きで "recovery 成功" と分かる説明)。
- `p4-core/p4_core/dashboard/templates.py`
  - `renderLlmOutputRecovered()` を新設し、recovery system_note を緑系の
    成功カラーで表示。
  - 「この recovery が必要な理由」を foldable details で説明。
  - SSE polling 間隔を 1.0s → 0.3s に短縮し、stream chunk の体感を改善。

### 変更されたテスト (旧契約 → 新契約)

旧テストは「markdown フェンス付き JSON は厳格失敗 + 1 回 retry」を固定していたが、
これは「やり切らない設計」の現れだったため、新契約に合わせて書き換え:

- `test_tool_action_recovers_json_wrapped_in_markdown_fence_without_retry` (旧
  `test_tool_action_retries_json_wrapped_in_markdown_fence`) — markdown フェンス
  付きでも recovery 経路で 1 回で前進することを固定。
- `test_markdown_wrapped_json_records_recovery_marker` (旧
  `test_markdown_wrapped_json_records_raw_machine_json_false`) — recovery 採用
  時の status flag (`json_extraneous_text_recovered`,
  `raw_output_is_machine_json: false`, `schema_validation_ok: true`) の組合せを固定。

### 不変条件追加 (`p4-coding-invariants-2026-04-24.md`)

- Invariant 12: ユーザ request の「やり切る」を最上位に置く。LLM 出力の
  完璧性を理由に turn 全体を放棄しない。valid な envelope が抽出できる場合は
  graceful degradation で前進し、警告は `system_note` として可視化する。

## ダッシュボード設計判断

「君 (Claude Code) の出力と同じ説得力」をユーザが要求。具体には:
- LLM stream のリアルタイム表示
- 何を判定し、なぜ、どう判定したかの可視化
- 折りたたみ可能な詳細

既存ダッシュボードは SSE (snapshot) + polling fallback で stream を扱い、
consolidated card (LLM/tool/finish) で foldable な details を出していた。
今回は以下を追加:

- **recovery 系 system_note を専用レンダラで表示** — 緑系の枠 + ✓ マーク +
  「この recovery が必要な理由」の details。失敗ではなく「乗り越えた」と
  見えるように。
- **SSE polling を 1.0s → 0.3s** — LLM stream chunk の更新頻度を上げ、
  「LLM がトークン出してる感」を体感できるように。

## 範囲外として記録した課題 (Open Issues)

`p4-judge-verdict-first-2026-05-03.md` の Open Issues は引き続き有効。今回の
recovery 経路追加で完了率が上がっても、以下は未解決:

1. **複数手一括予測の根本対策**: prompt 改善でも LLM が複数手を出す傾向は残る。
   recovery で前進はできるが、「最初の手だけで本当に良いのか」の判断は LLM 側の
   分解品質に依存。
2. **reflection の片道性**: recovery が起きたことを次ターンの prompt に注入する
   経路は無い。同じ LLM に「次回は 1 手だけ出して」と教える機構が無い。
3. **stagnation 分類器の粒度**: schema 失敗 / recovery / semantic 失敗を分けずに
   「停滞」として扱っている。recovery が連続するなら model 切替よりも prompt 強化
   が必要。

これらは P4 の次回 mainline 改修で取り上げる。
