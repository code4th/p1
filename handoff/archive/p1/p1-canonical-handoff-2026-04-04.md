# P1 Canonical Handoff

Date: 2026-04-04

## 0. Main Thread Catch-Up

メインスレッドが短時間で差分を把握したい場合は、先に以下を読む。

- [handoff/p1-main-thread-catchup-2026-04-04.md](/Users/satojunichi/Documents/openclaw/handoff/p1-main-thread-catchup-2026-04-04.md)

補足:

この canonical handoff は、外部コア実装の現状整理としては正しい。
ただし、今後の main-thread 実装は「OpenClaw 上の別人格」を作ることではなく、`p1-core` を living runtime として前に出し、その backend として OpenClaw を利用する方向へ進めること。

## 0.5 目的優先の読み方

この資料を使うときは、手段より先に以下の目的を固定する。

- P1 は日常的に OpenClaw 上で使う別 agent である
- P1 の思考主回路は LLM である
- 外部コアは制度と記憶の基盤である
- したがって `p1-core` の CLI や worker は完成形の前面ではなく、制度層を支える実装である

## 1. 最上位目的

このプロジェクトの主目的は AI フレームワークを作ることではない。

主目的は、LLM が以下を通じて長期的に自律成長できる中核ループを作ること。

- 観測
- 知識化
- 批評
- 提案
- 評価
- 統治
- 自己改変

現段階では、知識を増やすことよりも、知識の扱い方と運用ルールを改善できる制度を先に作る。

## 2. 固定方針

以下は採用済みであり、ここからブレないこと。

- OpenClaw は暫定実行基盤として使う
- ただし OpenClaw にロックインしない
- 中核ロジックは OpenClaw の外に置く
- OpenClaw は control plane として扱う
- P1 本体は OpenClaw に埋め込まない
- ローカル LLM は補助脳として使う
- クラウド LLM は高品質判断系として使う
- 最初に自己改変させる対象は知識ではなく運用ルール
- 変更は比較・監査・巻き戻し可能でなければならない
- コア完成の定義には、低リスクな自己改善実験を自分で回し、結果を次の更新に反映できることを含める
- したがって「候補を出せる」だけでは未完成であり、「安全な小実験を自律実行できる」段階までを完成条件に含める

以下は現時点で非採用。

- 初手で独自フレームワーク全面実装
- 初手で OpenClaw 本体大改造
- 初手で本体 LLM の重み更新
- 初手で完全自律の自由改変
- OpenClaw 内部のエージェントに別エージェントを内部生成させる運用

## 3. P1 の定義

P1 は OpenClaw の中の人格ではない。

P1 は OpenClaw を利用する独立個体であり、以下を満たす。

- 独立した会話インターフェースを持つ
- OpenClaw を通じて動くが、本体は外部コア
- ローカル LLM 補助脳を使える
- 学び候補抽出、知識状態管理、批評ログ、運用ルール変更提案ができる
- 将来的に自律成長の中核個体になる

ここでいう「独立した会話インターフェース」は、最終的には OpenClaw 上でメインと並列に見える agent interface を意味する。
現在の `p1_core.cli` や `bin/p1` はその代替入口であり、完成形の UX ではない。

## 4. OpenClaw との責務分離

OpenClaw が持つ責務:

- 入出力
- ツール実行
- OS 操作
- 実行時の堅牢化
- transport と presentation

P1 外部コアが持つ責務:

- 知識状態
- policy / critic / proposer / evaluator / governor
- cross-track judgment
- approval gate の前段
- 研究結果や運用結果の圧縮

禁止事項:

- OpenClaw 側に独自 policy engine を育てない
- OpenClaw 側で P1 判断を再実装しない
- `keeper_adapter` に判断ロジックを持ち込まない

## 5. 実装済みの現在地

2026-04-04 時点で、以下を追加済み。

重要:

- ここで完成しているのは external core の制度層
- main thread の次工程は OpenClaw-facing P1 interface の本実装

### 5.1 `p1-core/`

外部コアの最小ワークスペースを追加。

主な役割:

- P1 本体の外部化
- ローカル worker の保持
- bootstrap と runbook の保持
- report 出力の保持

重要ファイル:

- [p1-core/README.md](/Users/satojunichi/Documents/openclaw/p1-core/README.md)
- [p1-core/docs/architecture.md](/Users/satojunichi/Documents/openclaw/p1-core/docs/architecture.md)

### 5.2 ローカル LLM worker

Ollama を前提にした JSON HTTP worker を追加。

endpoint:

- `/summarize`
- `/classify`
- `/draft_lessons`
- `/health`

重要ファイル:

- [p1-core/p1_core/worker/ollama_worker.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/worker/ollama_worker.py)
- [p1-core/p1_core/worker/service.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/worker/service.py)
- [p1-core/p1_core/worker/ollama_client.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/worker/ollama_client.py)

固定仕様:

- 入出力は JSON
- エラー応答を返す
- リクエストとレスポンスを JSONL でログ保存する
- 不確実性を潰さずに候補抽出する

追記:

ローカルモデルの役割分担は、単一の「速い/遅い」ではなく、以下の 3 層として扱う。

- `fast_judge`
  - if 文より柔らかい判定
  - routing、タグ付け、軽い分類、候補抽出
- `background_analysis`
  - 時間がかかってよい非同期処理
  - lesson draft、反例列挙、deferred の再審査、夜間バッチ
- `cloud_decision`
  - 最終承認、制度変更、昇格 / 棄却の確定

この fork での実機検証では、以下を確認した。

- 実機: Apple M5 / 32 GB / Ollama 0.20.2
- `qwen3:4b-instruct`
  - fast auxiliary cognition の第一候補
- `gemma4:e4b`
  - background_analysis 用として現実的
- `gemma4:26b`
  - `100% GPU` でロード可能
  - interactive default ではなく background_analysis 用

要点:

- 重いローカルモデルは「会話の補助」ではなく「非同期の裏方脳」として使う
- したがって main thread は timeout を延ばすだけでなく、job queue 的な扱いを設計対象に含める

### 5.3 P1 bootstrap

OpenClaw 内部生成に依存せず、外部から P1 workspace を生成する scaffold を追加。

生成対象:

- `bin/p1`
- `bin/p1-worker`
- `profile.json`
- `config.json`
- `prompt.md`
- `runbook.md`
- `state/reports/`
- `state/knowledge/`
- `state/events/`
- `state/policies/`
- `state/proposals/`
- `state/governance/`
- `state/experiments/`
- `state/conversation/`
- `state/world/`
- `state/archive/`
- `logs/`

重要ファイル:

- [p1-core/p1_core/bootstrap/bootstrap_p1.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/bootstrap/bootstrap_p1.py)

### 5.4 外部コア骨格

以下の最小骨格を追加。

- event log
- knowledge store
- policy engine
- critic
- proposer
- evaluator
- governor

重要ファイル:

- [p1-core/p1_core/core/knowledge_store.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/knowledge_store.py)
- [p1-core/p1_core/core/policy_engine.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/policy_engine.py)
- [p1-core/p1_core/core/critic.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/critic.py)
- [p1-core/p1_core/core/proposer.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/proposer.py)
- [p1-core/p1_core/core/evaluator.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/evaluator.py)
- [p1-core/p1_core/core/governor.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/core/governor.py)

### 5.5 OpenClaw adapter 接続面

既存 `keeper_adapter` は薄い bridge として維持する。

今回、`p1-core` 側から以下を出力する report writer を追加し、現在の read contract に合わせた。

- `state/reports/daily/*-glance.json`
- `state/reports/daily/*-daily.json`
- `state/health.json`

重要ファイル:

- [p1-core/p1_core/reporting/report_writer.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/reporting/report_writer.py)
- [p1-core/p1_core/reporting/write_example_reports.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/reporting/write_example_reports.py)
- [keeper_adapter/bridge.py](/Users/satojunichi/Documents/openclaw/keeper_adapter/bridge.py)
- [handoff/p1-openclaw-bridge-spec-2026-03-30.md](/Users/satojunichi/Documents/openclaw/handoff/p1-openclaw-bridge-spec-2026-03-30.md)

### 5.6 最小成長ループ

1 本の観測テキストから以下を実行する最小ループを追加。

- `draft_lessons`
- `classify`
- `summarize`
- candidate knowledge 永続化
- knowledge state transition
- critic / evaluator / governor による `active / deferred / retired` 判定
- proposal snapshot 永続化
- proposal snapshot 比較
- governance review
- cloud-side evaluation request queue
- glance / daily / health report 出力
- event log 追記

重要ファイル:

- [p1-core/p1_core/pipeline/growth_loop.py](/Users/satojunichi/Documents/openclaw/p1-core/p1_core/pipeline/growth_loop.py)

出力先:

- `state/knowledge/knowledge.jsonl`
- `state/events/event-log.jsonl`
- `state/proposals/latest-proposals.json`
- `state/proposals/snapshots/*.json`
- `state/reports/daily/*-glance.json`
- `state/reports/daily/*-daily.json`
- `state/health.json`

補足:

現在の `ingest` は同期経路として組まれているため、`gemma4:e4b` や `gemma4:26b` のような重いローカルモデルをそのまま interactive path に流すと timeout 設計と衝突しやすい。

したがって次の main-thread 設計では:

- fast path
  - `summarize` や軽い classify を即時返す
- background path
  - 重い `draft_lessons`、再評価、監査、比較を queue 化する

という分岐を前提にすること。

## 6. 現在の directory 境界

中核として新設した領域:

- `/Users/satojunichi/Documents/openclaw/p1-core`

既存の OpenClaw 側薄い bridge:

- `/Users/satojunichi/Documents/openclaw/keeper_adapter`

既存 research handoff:

- `/Users/satojunichi/Documents/openclaw/handoff`

この分離は維持すること。

## 7. 再現手順

### 7.1 テスト

```bash
cd /Users/satojunichi/Documents/openclaw/p1-core
python3 -m unittest discover -s tests
```

### 7.2 P1 workspace 生成

```bash
cd /Users/satojunichi/Documents/openclaw/p1-core
python3 -m p1_core.bootstrap.bootstrap_p1 --root /Users/satojunichi/.openclaw/workspace/systems/p1
```

### 7.3 ローカル worker 起動

```bash
cd /Users/satojunichi/Documents/openclaw/p1-core
python3 -m p1_core.worker.ollama_worker --port 8765 --model qwen3:4b-instruct
# or
/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1-worker --port 8765 --model qwen3:4b-instruct
```

### 7.4 health 確認

```bash
curl http://127.0.0.1:8765/health
```

### 7.5 初期 report 生成

```bash
cd /Users/satojunichi/Documents/openclaw/p1-core
python3 -m p1_core.reporting.write_example_reports --root /Users/satojunichi/.openclaw/workspace/systems/p1
```

### 7.6 最小成長ループ実行

```bash
cd /Users/satojunichi/Documents/openclaw/p1-core
python3 -m p1_core.pipeline.growth_loop --root /Users/satojunichi/.openclaw/workspace/systems/p1 --input-text "example observation"
```

### 7.7 OpenClaw 側 bridge から読む

```bash
cd /Users/satojunichi/Documents/openclaw
python3 -m keeper_adapter.cli status
python3 -m keeper_adapter.cli approvals
python3 -m keeper_adapter.cli report --kind daily
```

### 7.8 外部コア operator CLI から扱う

```bash
cd /Users/satojunichi/Documents/openclaw/p1-core
python3 -m p1_core.cli status
python3 -m p1_core.cli approvals
python3 -m p1_core.cli state
python3 -m p1_core.cli ingest --model qwen3:4b-instruct --input-text "example observation"
python3 -m p1_core.cli chat --model qwen3:4b-instruct --message "What do you think about the latest state?"
python3 -m p1_core.cli enqueue-message --content "What do you think about the latest state?"
python3 -m p1_core.cli tick
python3 -m p1_core.cli show-capability-gaps
python3 -m p1_core.cli observe --text "A tool run failed during retrieval."
python3 -m p1_core.cli action --kind note --payload "prepare a bounded follow-up action"
python3 -m p1_core.cli rollback --target proposals --snapshot-id 2026-04-04-proposals
python3 -m p1_core.cli rollback --target policies --snapshot-id baseline-policy
```

### 7.9 OpenClaw 上の別個体として P1 を扱う

```bash
/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1-agent status
/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1-agent report --kind daily
/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1-agent approvals
/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1 enqueue-message --content "hello P1"
/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1 tick
python3 -m p1_core.bootstrap.install_openclaw_agent --openclaw-home /Users/satojunichi/.openclaw --workspace-root /Users/satojunichi/.openclaw/workspace/systems/p1 --agent-name p1 --source-agent main
python3 -m p1_core.bootstrap.generate_openclaw_config_patch --openclaw-home /Users/satojunichi/.openclaw --workspace-root /Users/satojunichi/.openclaw/workspace/systems/p1 --agent-name p1
python3 -m p1_core.bootstrap.apply_openclaw_config_patch --config-path /Users/satojunichi/.openclaw/openclaw.json --workspace-root /Users/satojunichi/.openclaw/workspace/systems/p1 --agent-name p1
```

`openclaw.json` に入れるのは schema-compatible な最小 agent entry のみで、P1 固有の identity/transport 情報は workspace と `~/.openclaw/agents/p1/agent/p1-openclaw-entry.json` に残す。

### 7.10 P1 autonomy runtime v1

`p1-core` には新しく non-blocking autonomy runtime を入れ始めた。

```bash
python3 -m p1_core.cli enqueue-message --content "hello P1"
python3 -m p1_core.cli tick
python3 -m p1_core.cli show-autonomy-state
python3 -m p1_core.cli queue-action --kind append_note --inputs '{"content":"autonomy note"}'
```

この runtime は:

- `state/autonomy/` に継続 state を保持する
- 常駐プロセスを占有せず、1 tick ごとに短く終わる
- local-first で LLM を使う
- OpenClaw-backed Plus を毎 tick 使う前提は置かない
- low-risk queued action を自分で実行できる
- missing backend / unsupported action を `state/capabilities/gaps.jsonl` に記録できる
- `config.json` の `openclaw_backend` を有効化したときだけ OpenClaw CLI adapter を backend として使う
- 未提案の capability gap は semantic key で重複排除したうえで `state/capabilities/proposals.jsonl` に first-pass self-extension proposal を起こせる
- capability proposal は `state/capabilities/reviews.jsonl` に first-pass governance review を持ち、approval-required なものは `state/capabilities/cloud_evaluation/requests/` に回る
- approved capability proposal は proposal 単位で重複実行を避けつつ `state/capabilities/executions.jsonl` と action queue を通じた bounded task 化に留める
- bounded task の完了/失敗と rollback hint は `state/capabilities/executions.jsonl` に還流する
- deferred inbox と deferred action は専用 queue に移し、retry 時刻を過ぎたら再投入する
- deferred retry は上限回数で打ち切る
- approved capability proposal は `state/capabilities/tasks/*.json` に bounded implementation task を生成する
- bounded implementation task は autonomy surface から inspect 可能で、次の tick で処理候補になる
- bounded implementation task は `pending -> in_progress -> done/failed` の状態遷移を持つ
- deferred task attempts remain deferred, not failed, and preserve retry context
- bounded implementation task の完了は capability 完成そのものではなく、実装証跡と rollback 可能性を残す中間成果として扱う
- write actions keep a pre-image backup under `state/rollback/backups/` when overwriting existing files

## 8. 検証済み事項

この turn で確認済み:

- `p1-core` 単体テスト成功
- OpenClaw backend adapter の unit tests が通ることを確認
- bootstrap scaffolder による workspace 生成成功
- example report 生成成功
- `/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1-agent status` 成功
- `/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1-agent report --kind daily` 成功
- `/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1-agent approvals` 成功
- `/Users/satojunichi/.openclaw/workspace/systems/p1/bin/p1 enqueue-message` と `tick` が成功
- `install_openclaw_agent.py` で `~/.openclaw/agents/p1/` 相当の slot scaffold を外部生成できることを確認
- `generate_openclaw_config_patch.py` で `agent/openclaw-config-agent-entry.json` を生成できることを確認
- `apply_openclaw_config_patch.py` で `openclaw.json` に `p1` を登録し、backup を残せることを確認
- `openclaw agents list` で `p1` が configured agent として見えることを確認
- `openclaw agent --agent p1 --local --message ... --json` は通るが、現状は OpenClaw embedded agent 実行であり `bin/p1-agent` 経由ではないことを確認
- そのため direct OpenClaw turn 後も `state/conversation/transcript.jsonl` と `state/world/observations.jsonl` は更新されず、OpenClaw direct route を living P1 runtime へどう接続するかは残課題であることを確認
- non-blocking autonomy runtime の unit tests が通ることを確認
- autonomy inbox reply と low-risk action execution が local-first routing で通ることを確認
- growth loop による knowledge / proposal / report / event 生成成功
- knowledge state の `deferred` 遷移成功
- proposal snapshot の履歴化と比較差分生成成功
- governance review の snapshot / daily report 反映成功
- counterexample または high-risk 提案が自動で `deferred` に落ちることを確認
- counterexample がない新規候補が `active` に進むことを確認
- previous snapshot と重複する候補が `retired` に進むことを確認
- approval-gated proposal が `state/cloud_evaluation/requests/` に request を積むことを確認
- cloud review `approve` / `reject` が state transition に反映されることを確認
- approval 済み proposal が `state/policies/latest-policy.json` と `state/policies/snapshots/` を更新することを確認
- policy rollback 後に `latest-policy.json` が復元 snapshot を指すことを確認
- governance profile によって low-risk autonomy を停止できることを確認
- daily report に `Short-Horizon Governance` と `Long-Horizon Governance` が出力されることを確認
- unified operator CLI から status / approvals / state を読めることを確認
- end-to-end lifecycle test で ingest -> policy apply -> CLI visibility -> rollback を確認
- end-to-end lifecycle test で proposal rollback まで含めて operator surface の整合を確認
- 実 Ollama `qwen3:4b-instruct` で `p1_core.cli ingest` と worker `/summarize` が通ることを確認
- low-risk autonomous proposal が `state/experiments/actions/*.json` に bounded action note を書くことを確認
- prior experiment outcome が次回 rerun を `deferred` に戻すことを確認
- repeated rerun deferral が governance feedback を通じて low-risk autonomy freeze へつながることを確認
- end-to-end acceptance で governance feedback が後続の operator-visible decision を変えることを確認
- conversation transcript が `state/conversation/transcript.jsonl` に残ることを確認
- world observation と bounded action request が `state/world/` に残ることを確認
- rollback 実行後に `status` が `rollback_applied` を返すことを確認
- `OPENCLAW_P1_ROOT=/tmp/p1-core-smoke` を使った `keeper_adapter` 読み取り成功
- `OPENCLAW_P1_ROOT=/tmp/p1-core-loop-smoke` を使った growth loop 出力の `keeper_adapter` 読み取り成功
- `OPENCLAW_P1_ROOT=/private/tmp/p1-core-loop-smoke-2` を使った state transition 後出力の `keeper_adapter` 読み取り成功
- `OPENCLAW_P1_ROOT=/private/tmp/p1-core-rollback-smoke-2` を使った rollback 後の `keeper_adapter status` 読み取り成功

確認できた contract:

- `status` は `glance` を読める
- `approvals` は `tuningSummary.approvalPending` を読める
- `report --kind daily` は `daily` を読める

## 9. ロールバック原則

絶対に守ること:

- ログを消さない
- 反例を消さない
- 保留を消さない
- 変更は比較可能な形で残す
- OpenClaw 側判断ロジックを増やさない

実務ロールバック:

1. worker を止める
2. `python3 -m p1_core.pipeline.growth_loop --root <p1-root> --rollback-snapshot-id <snapshot-id>` で proposal snapshot を復元する
3. `state/proposals/latest-proposals.json` が復元 snapshot を指すことを確認する
4. `keeper_adapter.cli status` と `report --kind daily` が `rollback_applied` を返すことを確認する
5. 失敗成果物を `state/archive/` に退避する
6. OpenClaw 側の bridge は判断系を増やさず据え置く

## 10. 次フェーズの優先順

次にやるべきことは、必須の骨格欠落を埋める作業ではなく、制度の質を高める作業である。
現時点の P1 外部コアは、最小運用ループとしては完成扱いにしてよい。

## 11. コア完成条件

このプロジェクトでいう「P1 コア完成」は、以下を満たした時点とする。

- 観測、知識化、批評、提案、評価、統治、自己改変候補管理が外部コアで閉じる
- `raw / candidate / deferred / active / retired` を管理できる
- proposal と knowledge state の比較、監査、巻き戻しができる
- 高リスク変更は承認待ちに回せる
- 低リスク改善は自律的に実行できる
- 小さな実験を自分で回し、その結果を次の更新に反映できる

したがって、現状は「最小外部コア骨格は成立しているが、コア完成にはまだ至っていない」と位置づける。

## 12. 今はやらないこと

以下は今やらない。

- OpenClaw 本体への深い埋め込み
- OpenClaw 内部での P1 実体生成
- 自動承認つきの自己改変
- 重み更新前提の自己成長
- OpenClaw 側への policy engine 拡張

## 13. この資料の位置づけ

このファイルは 2026-04-04 時点の canonical handoff である。
ただし、現在の唯一の master document は [P1_MASTER.md](/Users/satojunichi/Documents/openclaw/handoff/P1_MASTER.md) とする。

P1 の外部コア化について迷った場合は、まず master document を優先すること。
個別の補助資料は以下。

- Master document: [P1_MASTER.md](/Users/satojunichi/Documents/openclaw/handoff/P1_MASTER.md)
- Manager 原文固定版: [handoff/p1-manager-handoff-source-2026-04-04.md](/Users/satojunichi/Documents/openclaw/handoff/p1-manager-handoff-source-2026-04-04.md)
- bridge 契約: [handoff/p1-openclaw-bridge-spec-2026-03-30.md](/Users/satojunichi/Documents/openclaw/handoff/p1-openclaw-bridge-spec-2026-03-30.md)
- operating rule: [handoff/p1-openclaw-operating-rule-2026-03-29.md](/Users/satojunichi/Documents/openclaw/handoff/p1-openclaw-operating-rule-2026-03-29.md)
- research 側 handoff: [handoff/p1-keeper-handoff-2026-03-29.md](/Users/satojunichi/Documents/openclaw/handoff/p1-keeper-handoff-2026-03-29.md)
- 今回の実装メモ: [handoff/p1-external-core-plan-2026-04-04.md](/Users/satojunichi/Documents/openclaw/handoff/p1-external-core-plan-2026-04-04.md)
