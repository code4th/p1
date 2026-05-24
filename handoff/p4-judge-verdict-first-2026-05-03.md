# P4 Judge: Verdict-First Schema Design

Date: 2026-05-03
Status: Adopted
Related: `p4-coding-invariants-2026-04-24.md`, `p4-symmetry-audit-2026-04-26.md`

## 観測 (Observation)

`/tmp/p4-demo` ワークスペースで「複雑な迷路を生成して、実行して表示して」というユーザ依頼を実行した際、event log と dashboard で以下が観測された:

1. judge LLM (`glm-4.7-flash:latest`) は次の生出力を返した:

   ```json
   {"verdict":"ok","reason_code":"supported_claim","unsupported_claims":[],
    "rationale":"完全に一致しており..."}
   ```

2. にもかかわらず P4 ランタイムは `decision: invalid_output` で完了をブロック。
3. 連続 `finish_blocked / judge_invalid_output` 3 回 → fallback 経路で
   `judge_fallback_finish / judge_unavailable` として失敗終了。
4. dashboard 上は「judge が利用できず…」と表示されたが、実際には judge は応答していた。

## 分類 (Classification)

`p4-coding-invariants-2026-04-24.md` の不変条件と照合:

- **Invariant 5 (正本を増やさない) 違反**: `JUDGE_VERDICT_SCHEMA` は完了制御の決定権を
  実質的に `verdict` と `reason_code` の **両方** に持たせていた。schema の
  `additionalProperties: false` + `reason_code: enum [...]` + `required: [...全部]` の
  組合せで、annotation 側 (reason_code) の文字列ミスマッチが decision 側 (verdict) の
  正本性を逆流的に棄却していた。
- **Invariant 4 (variation と asymmetry の混同) 違反**: `judge_invalid_output`
  (LLM 到達済 / 構造不一致) と `judge_unavailable` (LLM 到達不能) は別事象だが、
  fallback の最終ラベルが一律 `judge_unavailable` に丸められていた。症状=原因の
  対応が壊れ、デバッグと監視が誤誘導されていた。
- **Invariant 3 (局所整合) 違反**: prompt 文字列 (`grounding.py`) と schema enum
  (`schemas.py`) が別ファイルで二重管理され、整合検査が無かった。LLM が prompt の
  例示 (`supported`) に近い `supported_claim` を返す自然な揺れがそのまま破綻に
  直結していた。

## 設計判断 (Decision)

### Concept

judge の責務分離を schema レベルで強制する:

- **verdict / status** = 完了制御の **正本 (single source of truth)**。
  enum 拘束し、ここに不一致が出たら decision を棄却する。
- **reason_code / rationale / unsupported_claims / observed_mismatch** = 後段の
  人/監査が読むための **annotation**。決定権を持たない。表現揺れを許容する。

### Invariants (固定)

1. judge LLM 応答から `verdict` (または `status`) が抽出でき、enum 値の範囲内で
   あれば、その他のキー欠損や表現揺れに関わらず decision として採用する。
2. `reason_code` は自由記述。schema は型と最大長のみ拘束する (enum 拘束は禁止)。
3. fallback ラベルは trigger reason を保持する。`judge_invalid_output` 由来の
   fallback は `judge_invalid_output_observation_accepted` /
   `judge_invalid_output_observation_rejected` のように元因を継承する。
4. `judge_unavailable` は **LLM が真に到達不能** な場合のみに予約する。

### 反対の選択肢と却下理由

- **判断却下: prompt の enum をより精密にして LLM を矯正する** — 表現揺れは
  自然言語モデルでは構造的に避けられない。schema 側で吸収する方が局所整合
  (Invariant 3) を保てる。
- **判断却下: judge を 2 段階にして verdict 抽出器を別 LLM 呼び出しにする** —
  正本が分散し Invariant 5 にむしろ近づく。1 回呼び出しで verdict を required、
  他を optional とする方が責務分離として明快。

## 修正 (Implementation)

### 変更ファイル

- `p4-core/p4_core/schemas.py`
  - `JUDGE_VERDICT_SCHEMA.required = ["verdict"]` (旧: 4 キー全部)
  - `reason_code` の enum を削除し `_string_schema(200)` に
  - `FINISH_ACCEPTANCE_SCHEMA.required = ["status"]` (旧: 4 キー全部)
  - `reason_code` の enum を削除し `_string_schema(200)` に
- `p4-core/p4_core/grounding.py`
  - 両 judge prompt 末尾の JSON 例を「必須は verdict/status のみ、他は省略可」と
    明示するよう書き換え
- `p4-core/p4_core/runtime.py`
  - `_consecutive_finish_block_summary()` を新設し trigger reason を捕捉
  - judge fallback の最終 `reason_code` / `last_error` / `current_phase` /
    `last_system_note` / 戻り値 `error` キーに trigger reason を継承
- `p4-core/p4_core/workspace.py`
  - canonical decision mapping を `endswith("_observation_accepted")` に拡張
- `p4-core/p4_core/dashboard/snapshot.py`
  - `judge_invalid_output` の説明文を verdict-first 設計に合わせて修正

### 追加テスト (再発防止)

- `tests/test_runtime.py::test_grounding_judge_accepts_verdict_with_free_form_reason_code`
  — ダッシュボードで観測した実シナリオ (verdict=ok / reason_code=旧 enum 外) を
  そのまま入力してパスすることを固定する。
- `tests/test_runtime.py::test_finish_acceptance_review_accepts_status_with_free_form_reason_code`
  — `FINISH_ACCEPTANCE_SCHEMA` 側でも同じ不変条件を固定する。

既存 7 件の judge 関連テストはすべてパス (回帰なし)。

## 範囲外として記録した課題 (Open Issues)

ダッシュボード調査で同時に見つかった以下の構造的問題は本対応の範囲外。次の対応で
取り上げる:

1. **観測フォールバックの救済路非対称**: `_can_accept_general_knowledge_without_judge`
   は evidence 非空のとき自動拒否する (`grounding.py:278-282`)。「judge 走ったが
   verdict 抽出不能 + evidence 充足」を救う対称的な経路が無い。
   → 今回の verdict-first 化で発生頻度自体は大幅に減るが、構造的ギャップは残る。
2. **final_answer に observation 本体が含まれない**: `working_memory.observations`
   が空のまま finish に進める。frame コントラクトとして「観測 → 次フレーム引継ぎ」
   の対称性が緩い。
3. **reflection の片道性**: `state/runtime/reflections.jsonl` に Write はあるが、
   次ターン prompt への Read 注入経路が無い (Apply/Revert 対称破綻)。
4. **`judge_metrics` の不活性化**: `consecutive_finish_blocks` 等を計上するが、
   prompt 切替/サーキットブレーカ等のトリガに繋がっていない。装飾化している。
5. **stagnation 分類器の粒度**: 「停滞 = LLM 力不足」と暗黙に仮定して reasoning
   モデルへ昇格させるが、ランタイム起因の構造バグを覆い隠す方向に働く。
   `LLM 起因` / `ランタイム起因` の分離が必要。

## 配置 (Placement)

本ノートは P4 mainline の **設計判断ログ** として保管する。
一般原則化したものは `p4-coding-invariants-2026-04-24.md` に追記する。
