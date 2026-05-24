# P2 Minimal Kernel Cut

Date: 2026-04-11

Purpose:

- `claw-code`
- `OpenHands`
- `Letta`
- `AutoGen`
- `SWE-agent`
- `AutoGPT`
- `opencode`
- 初期仕様書

を突き合わせて、`P2` に今入れるべき最小 kernel を切り出す。

このメモは研究メモではなく設計判断メモである。

---

## 1. 結論

`P2` は一度仕切り直すべきである。
理由は単純で、今までの `P2` は「自己改善ループ」を作ろうとして、runtime の核より周辺制度を先に増やしすぎた。

今回の比較から見えたことは次の通り。

- 強い OSS は、まず「要求 -> 実行 -> 結果 -> 次の実行」の loop を安定させている
- その loop の上に、memory、state machine、subagent、compaction、dashboard などを足している
- 最初から全部を同時に持っているわけではない

したがって `P2` の次版は、次の二層に分けるべきである。

- 内側: 実行 kernel
  - request/session/tool-result loop
- 外側: 自己改善 loop
  - 現行版を複製し、候補版を検証し、通れば昇格し、元の目的へ戻す

この二層以外は、原則として後回しにする。

---

## 2. 今回の最重要判断

### 一言で言うと何をするか

`P2` は「自己改善をする session-based coding agent」に縮退させる。

つまり、

- goal manager を重く作らない
- generation 管理を主役にしない
- 再帰階層やメモやダッシュボードを主役にしない
- まず session loop を成立させる
- その outer loop として自己改善を載せる

これが今回の最重要判断である。

### なぜそうするか

`claw-code` と `opencode` が示しているのは、

- 目的達成の本体は、賢い planner ではなく
- `messages + tools + tool results` を回す loop

だということだ。

`OpenHands` も `SWE-agent` も結局は、

- request を持つ
- action を出す
- observation を戻す
- stop 条件まで繰り返す

という同じ骨格を持っている。

つまり、共通核はかなり小さい。

---

## 3. P2 に残すもの

以下は `P2` の最小 kernel として残す。

### 3.1 Goal Anchor

残す。

ただし重い goal planner ではなく、最低限これだけにする。

- 現在の高レベル目的を 1 つ保持する
- 自己改善の後に元の目的へ戻す
- 今回の自己改善が何のためかを紐付ける

これは初期仕様書の `Goal Persistence` と `Return to Original Goal` に対応する。

### 3.2 Session Store

強く残す。ここが中心。

最低限必要なのは次。

- user message
- assistant message
- tool call
- tool result
- validation result
- current system/context snapshot

重要なのは、`何をしたか` と `何が起きたか` を、次回の推論入力へ戻せる形で保存すること。

これは今までの `P2` の弱点だった。

### 3.3 System Prompt / Context Assembly

残す。

ただし minimal にする。

最低限:

- system prompt
- 現在の goal
- 現在の self-edit 対象情報
- 直近の変更 raw
- 直近の validation raw
- 必要なら instruction files / skill notes

ここで重要なのは、「分析済み要約を大量に積む」より、

- 自分が何を書き換えたか
- その直後に何が壊れたか

の raw を返すこと。

### 3.4 Provider Adapter

残す。

最低限:

- model に messages/system/tools を渡す
- streaming を扱う
- tool call を構造化で受ける

この層は `claw-code`、`OpenHands SDK`、`opencode` で共通している。

### 3.5 Tool Registry / Tool Executor

残す。

最小 tool はこれで十分。

- read
- write or apply_patch
- ls / grep / glob
- bash
- validation
- finish

`P2` の自己改善に必要なのは、まずこれだけである。

### 3.6 Permission Policy

残す。

ただし minimal にする。

- protected path は deny
- editable zone は allow
- 危険操作は ask or deny

これも kernel 側で持つ。

### 3.7 Runtime Loop

最重要。残す。

内側 loop は次で十分。

1. goal に対する user/request message を session に入れる
2. system + messages + tools を model に送る
3. model が text / tool call / finish を返す
4. tool call があれば実行し、tool result を session に戻す
5. finish または tool call なしまで続ける

これが `P2` の実行 kernel である。

### 3.8 Candidate Isolation / Validation / Promotion

外側 loop として残す。

ただしこれも minimal にする。

1. active 版を複製して candidate を作る
2. candidate 上で自己編集させる
3. validation を走らせる
4. pass なら promote
5. fail なら reject
6. 新 active で元 goal を再試行

これが `P2` の自己改善 loop である。

### 3.9 Artifact Logging

残す。

ただし log の主役は「観測できる raw」にする。

最低限保存するもの:

- before file
- after file
- unified diff
- validation stdout/stderr
- return code
- model output
- tool result
- decision reason

---

## 4. P2 から一旦外すもの

以下は今の段階では kernel に入れない。

### 4.1 再帰階層システム

一旦外す。

理由:

- 発想はよい
- しかし runtime が不安定なまま入れると、原因追跡がさらに難しくなる
- まず flat な session loop で「変更 -> 失敗 -> 次で修正」が成立してから入れるべき

再帰は outer feature であり、kernel ではない。

### 4.2 generation 主体の世界観

弱める。

generation 管理自体は残してよいが、主役にしない。

主役は:

- 実行 session
- candidate validation

である。

generation はその結果として増えるだけでよい。

### 4.3 複雑な gap taxonomy

外す。

今必要なのはせいぜい次の区別だけ。

- 直接実行失敗
- 編集失敗
- validation 失敗
- protected path 違反
- context 不足

それ以上の taxonomy は、実データが溜まってからでよい。

### 4.4 高度な memory / 自己メモ

一旦外す。

今の段階では、永続メモより session と validation raw の方が重要である。

メモを増やすと、ノイズが増えやすい。

### 4.5 重い dashboard

後回し。

CLI とログ tail で十分。

必要なら簡易 dashboard を later phase で入れる。

### 4.6 複数モデル戦略

後回し。

モデル切り替えは有効だが、まずは kernel の問題を隠しやすい。
最初は 1 系統で loop を安定させた方がよい。

### 4.7 複雑な multi-agent orchestration

後回し。

subagent は効くが、まず単体が安定してからでよい。

---

## 5. 後回しにするが、将来入れる価値が高いもの

### 5.1 Compaction

`opencode` はここが強い。
長時間運用を考えるなら必要。

ただし kernel が安定してから。

### 5.2 Subagent

`AutoGen` と `opencode` はここが参考になる。
ただし単体 loop が安定した後で十分。

### 5.3 Event-state machine

`OpenHands` 的な execution status は有用。

ただし最初は簡単な status でよい。

- idle
- running
- waiting_tool
- validating
- promoted
- rejected
- finished
- error

### 5.4 Auditable trajectory

`SWE-agent` の良さ。

これは後から trace 表現として導入するとよい。

---

## 6. P2 の次版に必要な最小構成

次版 `P2` のディレクトリや機能は、これくらいでよい。

### 6.1 Core files

- `goal.json`
- `session.jsonl` or `messages.jsonl`
- `active/`
- `candidates/<id>/`
- `attempts/<id>/`
- `validations/<id>/`
- `version.json`

### 6.2 Inner loop

- request を session に入れる
- LLM に送る
- tool call を実行する
- tool result を session に戻す
- finish まで続ける

### 6.3 Outer loop

- active を複製
- candidate を自己編集
- validation 実行
- promote / reject
- 元の goal を新 active で再試行

### 6.4 Stop conditions

最低限これだけでよい。

- model が finish を返した
- validation が pass した
- self-improvement attempt 上限に達した
- protected path を触った
- 同一 candidate が no-op だった

---

## 7. P2 の最小 prompt 方針

今後の `P2` prompt は、機能誘導より観測誘導を優先する。

つまり、

- 「再帰しろ」
- 「こう考えろ」

を強く書くより、

- あなたが直前に変更した diff
- その直後の validation エラー
- 今回の goal
- 守ってはいけない path

を返す。

これによりモデルが、

- どの変更で壊れたか
- 次に何を戻すか
- 何を直すか

を自力で考えやすくする。

これはユーザーが繰り返し言っていた

- 何を書き換えたか
- その直後に何が壊れたか
- それがどの変更と結びつくか

を `P2` にも与える、という方針と一致する。

---

## 8. 初期仕様書との整合

今回の cut は、初期仕様書を捨てるものではない。
むしろ、初期仕様書の本筋だけを残す動きである。

残っている本筋は次。

- Goal Persistence
- Self Inclusion in Problem Framing
- Validation before Promotion
- Return to Original Goal
- Minimal Constitutional Fixity
- Safe and Efficient Local Improvement

逆に、まだ早いものを一旦後ろへ送っている。

これは仕様書の

- 「個別機能を先に固定しすぎない」
- 「壊れずに継続できる基盤を先に固定する」

と整合している。

---

## 9. 推奨する次の実装順

次に実装する順番はこれがよい。

1. minimal session runtime
2. minimal tool execution + tool result reinjection
3. minimal self-edit candidate loop
4. validation + promotion
5. raw diff/error reinjection
6. lightweight CLI visibility
7. その後に compaction / recursion / subagent

---

## 10. 最終判断

`P2` は「自己改善のための巨大システム」を目指すのではなく、

- session loop
- candidate validation loop

の二重 loop を持つ最小 coding agent として作り直すべきである。

これが今回の比較調査から得られた、最も実装価値の高い判断である。
