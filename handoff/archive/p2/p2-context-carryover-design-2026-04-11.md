# P2 Context Carryover Design

日付: 2026-04-11

## 結論

今の `P2` のコンテキスト継承が悪い理由は単純である。

- `何をしたか` は一部残している
- しかし `何が分かったか` を状態として保持していない

OSS agent を見ると、強い実装は単にログを持っているのではなく、

- 直近の実行履歴
- 現在の作業状態
- 必要なら圧縮された記憶

を分けて持っている。

`P2` に必要なのも同じである。

## OSS から見えた共通構造

### 1. claw-code / opencode / OpenHands / SWE-agent

共通点:

- action / tool result を session に戻す
- 次の推論はその session を前提にする
- 直近の作業状態は明示的に残る

重要点:

- 生ログを全部読むことが本質ではない
- session の中で「今の作業に必要な履歴」が再構成される

### 2. Letta / AutoGen

共通点:

- 会話履歴とは別に memory / facts / plan / ledger を持つ
- 次の推論では、その場で必要なものだけ再注入する

重要点:

- raw history と working memory は別物
- memory は「再利用可能な理解」に圧縮されている

## P2 に足りないもの

今の `P2` は主にこれを渡している。

- goal
- frame goal
- target file の現在内容
- delta_context
- session_events の末尾
- frame_state

これは `履歴` と `直近状態` であって、`調査結果` ではない。

不足しているのは次である。

- もう観測済みの対象
- そこから得た結論
- 未解決の問い
- 今の焦点
- もう繰り返すべきでない観測

## P2 の context は 3 層に分ける

### 1. Raw Trajectory

append-only の事実ログ。

- action
- action_input
- result
- raw model output
- validation stdout / stderr

用途:

- 監査
- 再現
- 詳細確認

これは今の `session_events` でよい。

### 2. Frame Working Memory

各フレームが次の判断のために持つ、圧縮された調査結果。

最低限必要なのは次である。

- `observed_files`
- `observed_symbols`
- `observed_tests`
- `learned_findings`
- `focus_candidates`
- `current_focus`
- `unresolved_questions`
- `done_criteria`
- `avoid_repeating`

例:

- `goal_logic.py 全体確認済み`
- `self_check は確認済み`
- `tests/test_goal_logic.py は未確認`
- `validation 自体は成功している`
- `generic error という解釈は誤認の可能性がある`
- `次の焦点候補は self_check`
- `同じ粒度で goal_logic.py 全体を再読しない`

用途:

- 同じ調査の繰り返し防止
- 粗い探索から中位焦点への移行
- 戻る / 下る / 続けるの判断

### 3. Cross-Attempt Memory

attempt をまたいで残す、再利用可能な戦術記憶。

- repeated failure pattern
- successful tactic
- dangerous anti-pattern
- useful decomposition pattern

用途:

- 同じ壊れ方の再発防止
- モデル選択や探索方針の改善

これは今の `memo` と `delta_context` を整理して統合すべき領域である。

## P2 の prompt に何を入れるべきか

次の action を決める prompt には、raw log を大量投入するのではなく、次を入れる。

1. 高レベル goal
2. 現在の frame goal
3. frame working memory
4. 直近の 3-5 event
5. 必要な対象ファイル / テスト抜粋
6. cross-attempt memory のうち関連分だけ

つまり、

- session_events 全部
- 同じファイル全体の再投入
- 直近 failure の曖昧要約

ではなく、

- 何をもう知っているか
- 何がまだ分かっていないか
- 次にどこへ焦点を当てるか

を中心にする。

## 探索状態の遷移

`P2` は最初から細部へ行かず、次の遷移を取る。

1. `survey`
   - 粗く全体を見る
2. `focus`
   - 問いを 1 つに絞る
3. `local_work`
   - 観測または編集 + 検証
4. `return_ready`
   - 親へ返せるだけの結果が出た

この状態は frame working memory に持たせる。

## 実装方針

### A. session_events は残す

消さない。
ただし、次の思考の主役にはしない。

### B. read / search / validation のたびに working memory を更新する

例:

- `read_file(agent/goal_logic.py)` をしたら
  - `observed_files += goal_logic.py`
  - `learned_findings += "goal_logic.py 全体確認済み"`

- `search_code(def self_check)` をしたら
  - `observed_symbols += self_check`
  - `current_focus = self_check`

- `run_validation` が成功したら
  - `learned_findings += "validation 自体は成功している"`
  - `avoid_repeating += "generic error とだけ扱わない"`

### C. 同じ粒度の観測は working memory で禁止する

例:

- `goal_logic.py 全体確認済み` があるのに、同じ read を続けない
- 続けるなら理由が必要
  - 別行範囲
  - 新しい焦点
  - 子フレームでの別目的

### D. frame goal と current_focus を分ける

- frame goal
  - この階層で何を明らかにするか
- current_focus
  - いまその中のどこを見ているか

これを分けないと、広い goal のまま同じ観測を繰り返す。

## 一番重要な一文

`P2` に必要なのは「長いコンテキスト」ではない。
`P2` に必要なのは、「過去の行為ログ」から「現在の理解状態」へ変換された working memory である。
