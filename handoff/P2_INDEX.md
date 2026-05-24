# P2 Index

最終更新: 2026-04-21

このファイルは `P2` 関連メモの正本インデックスである。
以後、`P2` の判断はまずこの索引を参照し、各メモはここでの位置づけを持つ。

## Final Mainline Artifact

このファイルが P2 の最終成果物の入口である。

- code root: [p2-core](/Users/satojunichi/Documents/openclaw/p2-core)
- package: `p2_core`
- current canonical implementation: session/event kernel with recursive frames
- source and tests: [p2-core/p2_core](/Users/satojunichi/Documents/openclaw/p2-core/p2_core), [p2-core/tests](/Users/satojunichi/Documents/openclaw/p2-core/tests)
- canonical seed/runtime baseline: [p2-core/seed](/Users/satojunichi/Documents/openclaw/p2-core/seed), [p2-core/runtime/versions/v0001](/Users/satojunichi/Documents/openclaw/p2-core/runtime/versions/v0001)

古い調査メモはこの索引から辿る。索引にない P2 メモを新しい正本として扱わない。
実装判断は `Canonical` の順序を優先し、`Supporting` は理由づけとして使う。

## 使い方

- `canonical`
  - 現時点の正本。設計判断や実装方針はまずこれに従う。
- `supporting`
  - 正本を補助する診断・調査・実装メモ。
- `historical`
  - 当時の判断として残すが、現時点の正本ではない。
- `conflict`
  - 後続の判断と衝突している。履歴としては残すが、今の意思決定には直接使わない。

## Canonical

### 1. 設計の主語と中心命題

- [p2-meta-design-clarification-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p2/p2-meta-design-clarification-2026-04-11.md)
  - 役割: `P2` 自身と `P2` を実装する側を分ける
  - 現在の扱い: 正本
  - 重要点:
    - `P2` は自分を改良する AI エージェント
    - 実装者の判断と `P2` 自身の思考を混同しない

### 2. 失敗原因の正本診断

- [p2-why-repeats-failures-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p2/p2-why-repeats-failures-2026-04-11.md)
  - 役割: 失敗反復の主因整理
  - 現在の扱い: 正本
  - 重要点:
    - raw な行為/結果の連続性が弱かった
    - phase pipeline が session loop より前面に出ていた

### 3. 現在の kernel 方針

- [p2-session-kernel-implementation-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p2/p2-session-kernel-implementation-2026-04-11.md)
  - 役割: 現在の実装済み kernel の説明
  - 現在の扱い: 正本
  - 重要点:
    - `session_action_loop_v1`
    - `action -> result -> 次の action`
    - `open_child_frame / return_to_parent`

### 4. 再帰フレーム運用

- [p2-recursive-frame-rules-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p2/p2-recursive-frame-rules-2026-04-11.md)
  - 役割: 子フレームへ降りる条件、戻る条件、小タスク作成ルール
  - 現在の扱い: 正本
  - 重要点:
    - 戻る基準は成功ではなく、親が次を決めるのに十分な結果が出たか
    - 小タスクは「狭い」「完了条件が明確」「因果が1本」

## Supporting

### 設計判断の補助

- [p2-context-vs-tool-loop-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p2/p2-context-vs-tool-loop-2026-04-11.md)
  - 役割: コンテキスト粗さと tool loop 不在の関係整理
  - 現在の扱い: 補助

- [p2-context-failure-ack-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p2/p2-context-failure-ack-2026-04-11.md)
  - 役割: 実装側の失敗認識
  - 現在の扱い: 補助

- [p2-what-to-do-next-meta-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p2/p2-what-to-do-next-meta-2026-04-11.md)
  - 役割: session/event loop への切り替え理由
  - 現在の扱い: 補助

- [p2-context-carryover-design-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p2/p2-context-carryover-design-2026-04-11.md)
  - 役割: OSS を基準にした P2 の文脈継承設計
  - 現在の扱い: 補助

### 調査・分析

- [p2-stability-log-investigation-2026-04-09.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p2/p2-stability-log-investigation-2026-04-09.md)
  - 役割: 長時間 run の分析
  - 現在の扱い: 補助

- [claw-code-purpose-achievement-analysis-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/general/claw-code-purpose-achievement-analysis-2026-04-11.md)
  - 役割: `claw-code` 実装調査
  - 現在の扱い: 補助

- [open-source-agent-purpose-execution-comparison-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/general/open-source-agent-purpose-execution-comparison-2026-04-11.md)
  - 役割: OSS agent 横断比較
  - 現在の扱い: 補助

## Historical / Article Drafts

- [note-self-improvement-ai-hierarchy-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/general/note-self-improvement-ai-hierarchy-2026-04-11.md)
  - 役割: note 向け叙述
  - 現在の扱い: 履歴 / 外部向け文章

- [note-self-improvement-ai-self-awareness-2026-04-09.md](/Users/satojunichi/Documents/openclaw/handoff/archive/general/note-self-improvement-ai-self-awareness-2026-04-09.md)
  - 役割: note 向け叙述
  - 現在の扱い: 履歴 / 外部向け文章

## Conflict

- [p2-minimal-kernel-cut-2026-04-11.md](/Users/satojunichi/Documents/openclaw/handoff/archive/p2/p2-minimal-kernel-cut-2026-04-11.md)
  - 役割: 仕切り直し時点の最小 kernel 案
  - 現在の扱い: 一部衝突あり
  - 衝突点:
    - `4.1 再帰階層システムを一旦外す` は、現在の合意
      - `再帰フレームは P2 の基本実行モデル`
      と衝突する
  - 使い方:
    - session/event kernel を軽くする方向の参考としては使う
    - 再帰を外す判断の根拠としては使わない

## 現時点の運用ルール

1. 新しい `P2` メモを追加する前に、この索引のどこに入るかを決める
2. 既存メモと主張が重なるなら、新規メモではなく既存メモ更新を優先する
3. 主張が衝突する場合は、新メモを増やす前にこの索引へ `conflict` を追記する
4. 実装判断では `canonical` を優先し、`supporting` は理由づけとして使う

## 今回確認した重複・衝突

- `p2-context-vs-tool-loop-2026-04-11.md`
  - `p2-why-repeats-failures-2026-04-11.md` と論点がかなり近い
  - ただし前者は「コンテキストと tool loop の関係整理」、後者は「失敗反復の総合診断」で役割を分けられる

- `p2-context-failure-ack-2026-04-11.md`
  - 上記 2 本と一部重複する
  - ただしこれは「実装側の誤り認識」を明示した記録として残す

- `p2-minimal-kernel-cut-2026-04-11.md`
  - 再帰フレームの扱いが現在の合意と衝突するため、正本から外す
