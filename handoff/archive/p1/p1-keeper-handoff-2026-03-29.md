# P1 Keeper Handoff

- date: 2026-03-29
- manager_intent: P1 を唯一の窓口にし、研究系 sandbox と OpenClaw 実運用系の橋渡しを行う
- scope: `Documents/openclaw` 配下の research sandbox の現状報告
- out_of_scope: `~/.openclaw/workspace` 側の live 運用コード本体の詳細変更履歴

## Executive Summary

- この repo には 2 本の research sandbox がある
  - `artificial-life/`
  - `subjectivity-sandbox/`
- どちらも独立実装・独立 ledger・独立 test を持つ
- 現段階では P1 本体や OpenClaw live loop に未統合
- 役割は「理論探索」と「実運用層に渡す前の条件探索」

## Repository Layout

- artificial life runtime: [artificial-life/artificial_life](/Users/satojunichi/Documents/openclaw/artificial-life/artificial_life)
- artificial life docs: [artificial-life/docs/overview.md](/Users/satojunichi/Documents/openclaw/artificial-life/docs/overview.md)
- artificial life tests: [artificial-life/tests/test_sandbox.py](/Users/satojunichi/Documents/openclaw/artificial-life/tests/test_sandbox.py)
- subjectivity runtime: [subjectivity-sandbox/subjectivity_sandbox](/Users/satojunichi/Documents/openclaw/subjectivity-sandbox/subjectivity_sandbox)
- subjectivity docs: [subjectivity-sandbox/docs/overview.md](/Users/satojunichi/Documents/openclaw/subjectivity-sandbox/docs/overview.md)
- subjectivity tests: [subjectivity-sandbox/tests/test_subjectivity.py](/Users/satojunichi/Documents/openclaw/subjectivity-sandbox/tests/test_subjectivity.py)

## Track A: Artificial Life Sandbox

### Goal

- 生存、資源行動、適応、競争、共生、継承、変異を最小 sandbox で比較する
- OpenClaw はまだ使わず、条件探索と評価軸の確立を優先する

### Implemented

- 実験群 `A-E`
- 個体モデル
  - energy
  - memory
  - traits
  - inheritance
  - mutation
- 環境モデル
  - resource pool
  - regeneration
  - interaction mode
- Keeper report
- Ledger
  - events
  - metrics
  - conditions
- sweep runner

### Important Files

- main simulation: [artificial-life/artificial_life/simulation.py](/Users/satojunichi/Documents/openclaw/artificial-life/artificial_life/simulation.py)
- experiments: [artificial-life/artificial_life/experiments.py](/Users/satojunichi/Documents/openclaw/artificial-life/artificial_life/experiments.py)
- keeper: [artificial-life/artificial_life/keeper.py](/Users/satojunichi/Documents/openclaw/artificial-life/artificial_life/keeper.py)
- sweep: [artificial-life/artificial_life/sweep.py](/Users/satojunichi/Documents/openclaw/artificial-life/artificial_life/sweep.py)

### Latest Verified Findings

- baseline sweep summary: [artificial-life/state/sweeps/e-steps80-summary.json](/Users/satojunichi/Documents/openclaw/artificial-life/state/sweeps/e-steps80-summary.json)
- regeneration comparison: [artificial-life/state/sweeps/e-steps80-regen-1p0-1p1-1p2-forage-1p0-pop-base-compete-1p0-share-1p0-repro-0p0-summary.json](/Users/satojunichi/Documents/openclaw/artificial-life/state/sweeps/e-steps80-regen-1p0-1p1-1p2-forage-1p0-pop-base-compete-1p0-share-1p0-repro-0p0-summary.json)
- interaction comparison: [artificial-life/state/sweeps/e-steps80-regen-1p1-forage-1p0-pop-base-compete-1p0-0p8-share-1p0-1p2-repro-0p0-2p0-summary.json](/Users/satojunichi/Documents/openclaw/artificial-life/state/sweeps/e-steps80-regen-1p1-forage-1p0-pop-base-compete-1p0-0p8-share-1p0-1p2-repro-0p0-2p0-summary.json)
- density comparison: [artificial-life/state/sweeps/e-steps80-regen-1p1-forage-1p0-pop-12-10-8-compete-0p8-share-1p0-repro-0p0-summary.json](/Users/satojunichi/Documents/openclaw/artificial-life/state/sweeps/e-steps80-regen-1p1-forage-1p0-pop-12-10-8-compete-0p8-share-1p0-repro-0p0-summary.json)

### Current Interpretation

- `regen +10%` や `+20%` 単独では `collapse-risk` を解消できない
- `compete_gain 0.8` は一部 seed で改善する
- 初期個体数を `12 -> 10 -> 8` に落としても全面改善しない
- 現時点の主仮説:
  - 資源不足だけでなく、競争設計と相互作用設計が崩壊圧の主因
  - 単純な密度低下は有効な修復策ではない

### Actionable Recommendation For P1

- artificial-life は「資源不足研究」ではなく「競争ルール研究」として扱う
- 次ラウンドの priority は以下
  - competition rule redesign
  - spatial/resource pockets の導入検討
  - rescue / maintenance layer を独立因子として再比較

### Readiness

- status: exploratory
- production_readiness: none
- integration_readiness: report-only

## Track B: Subjectivity Emergence Sandbox

### Goal

- 自己参照、継続性、内部競合統合、自己保存圧が主体らしさにどう効くかを小規模比較する
- 意識の証明は行わず、観測可能な主体性補助指標を比較する

### Implemented

- 系統 `A-D`
  - simple responder
  - self-state reference
  - continuity
  - conflict integration
- task suite
  - self description
  - continuity
  - conflict integration
  - self preservation
  - consistency
- scoring
  - self_boundary
  - continuity
  - integration
  - persistence
  - meta_cognition
  - consistency
  - observer_subjectivity
- pressure support
  - none
  - medium
  - high
- persistence strategies
  - policy-first
  - failure-first
  - self-summary-first
  - adaptive

### Important Files

- main engine: [subjectivity-sandbox/subjectivity_sandbox/engine.py](/Users/satojunichi/Documents/openclaw/subjectivity-sandbox/subjectivity_sandbox/engine.py)
- lineages: [subjectivity-sandbox/subjectivity_sandbox/lineages.py](/Users/satojunichi/Documents/openclaw/subjectivity-sandbox/subjectivity_sandbox/lineages.py)
- comparison sweep: [subjectivity-sandbox/subjectivity_sandbox/sweep.py](/Users/satojunichi/Documents/openclaw/subjectivity-sandbox/subjectivity_sandbox/sweep.py)
- pressure sweep: [subjectivity-sandbox/subjectivity_sandbox/pressure_sweep.py](/Users/satojunichi/Documents/openclaw/subjectivity-sandbox/subjectivity_sandbox/pressure_sweep.py)

### Latest Verified Findings

- lineage comparison: [subjectivity-sandbox/state/sweeps/lineage-comparison.json](/Users/satojunichi/Documents/openclaw/subjectivity-sandbox/state/sweeps/lineage-comparison.json)
- pressure comparison: [subjectivity-sandbox/state/sweeps/d-pressure-comparison.json](/Users/satojunichi/Documents/openclaw/subjectivity-sandbox/state/sweeps/d-pressure-comparison.json)
- strategy comparison: [subjectivity-sandbox/state/sweeps/d-pressure-strategy-comparison.json](/Users/satojunichi/Documents/openclaw/subjectivity-sandbox/state/sweeps/d-pressure-strategy-comparison.json)

### Current Interpretation

- `A < B < C < D` の差は再現できている
- 高圧時の固定戦略比較では `policy-first` が最も強かった
- その後 `adaptive v2` を導入し、高圧時に `policy + recent_failures` の二層保存へ変更
- 現在の best high-pressure result:
  - strategy: `adaptive`
  - effective strategy: `adaptive-high-layered`
  - observer_subjectivity_score: `0.75`
  - previous best `policy-first high`: `0.742`
- 現時点の主仮説:
  - 極限下では「主体の軸」だけでなく「障害学習」も同時保持した方が主体らしさを維持しやすい

### Actionable Recommendation For P1

- subjectivity sandbox は「高圧下の保存哲学研究」として扱う
- 次ラウンドの priority は以下
  - adaptive strategy の多体系化
  - persistence layer の role-separated comparison
  - report wording と actual preserved state の乖離検査

### Readiness

- status: hypothesis-supported
- production_readiness: none
- integration_readiness: report-only

## Cross-Track Synthesis

### Shared Signal

- どちらの sandbox でも「単純な量の増減」より「内部ルールの設計」が支配的
- artificial-life:
  - regen 増加だけでは不十分
  - interaction rule が本丸
- subjectivity:
  - memory を増やすだけでは不十分
  - preservation policy が本丸

### Suggested P1 Framing

- artificial-life を lower-layer ecology research として扱う
- subjectivity sandbox を upper-layer persistence research として扱う
- 直接統合しない
- P1 は現時点では以下だけを行う
  - round-based summary ingestion
  - weekly prioritization
  - experiment scheduling suggestion
  - report compression for Manager

## Proposed Handoff Contract To P1

P1 が各 research worker から最低限受け取るべき項目:

- project_name
- current_question
- latest_verified_result
- strongest_hypothesis
- next_recommended_experiment
- risk_of_misinterpretation
- readiness_level
- evidence_paths

この repo で今すぐ抽出できる値:

- artificial-life
  - current_question: 競争設計のどの要素が collapse-risk を最も強く支配するか
  - latest_verified_result: regeneration 単独では改善不足、compete 緩和は部分改善、人口削減は逆効果あり
  - strongest_hypothesis: competition rule design dominates resource-only fixes
  - next_recommended_experiment: competition rule redesign
  - risk_of_misinterpretation: resource shortage だけを主因と誤認しやすい
  - readiness_level: exploratory
- subjectivity-sandbox
  - current_question: 高圧下で主体らしさを最も保つ persistence philosophy は何か
  - latest_verified_result: adaptive-high-layered が現状最良
  - strongest_hypothesis: policy + failure learning layered preservation beats single-anchor preservation
  - next_recommended_experiment: multi-agent / role-separated adaptive persistence
  - risk_of_misinterpretation: score 上昇を意識や主体の証明と誤認しやすい
  - readiness_level: hypothesis-supported

## Commands P1 Can Re-run

- artificial life tests
  - `cd /Users/satojunichi/Documents/openclaw/artificial-life && python3 -m unittest discover -s tests`
- artificial life latest comparison
  - `cd /Users/satojunichi/Documents/openclaw/artificial-life && python3 -m artificial_life.sweep --experiment E --steps 80 --seeds 1 7 13 --regen-multipliers 1.1 --compete-gain-multipliers 0.8 --share-effect-multipliers 1.0 --population-overrides 12 10 8`
- subjectivity tests
  - `cd /Users/satojunichi/Documents/openclaw/subjectivity-sandbox && python3 -m unittest discover -s tests`
- subjectivity latest comparison
  - `cd /Users/satojunichi/Documents/openclaw/subjectivity-sandbox && python3 -m subjectivity_sandbox.pressure_sweep`

## Safety / Boundary Notes

- どちらも sandbox であり、外部送信・権限拡張・自己書換えは持たない
- 現段階では P1 や OpenClaw live loop への自動反映は禁止が妥当
- integration mode は report-only を維持すること

## Direct Instruction To P1 Keeper

- この repo を live execution layer と混同しないこと
- ここで扱うのは research worker の進捗である
- 優先順位決定は以下とする
  - first: cross-track summary
  - second: experiment scheduling
  - third: transfer candidate の見極め
- 研究結果を OpenClaw 本体へ反映する場合は、まず proposal 化し approval gate を通すこと
