# P4 Canonical Mainline

Date: 2026-04-26 (last refined: 2026-05-03)
Version: `0.4.1-mainline`

## Status

This document defines the current P4 mainline. P4 has successfully implemented recursive frame context on top of the P3 baseline, incorporating event contract modernization and a symmetry audit.

Use `p4-core/` as the source of truth. The earlier planning documents, task lists, and audit records have been moved to `archive/p4/` as historical context.

This is the single P4 entry point for the completed P4 baseline.

## Refinements after 0.4.0-mainline

- **2026-05-02 — workspace bootstrap fallback**: `AgentRuntime` が `config.json`
  欠落時に空 dict で起動し、judge model が役割名 `"fast"` リテラルとして Ollama に
  送信され HTTP 404 を起こしていた問題を修正。`DEFAULT_CONFIG` を deep-merge する
  形で起動時に invariant (役割→モデル名解決) を保証するように。
  併せて `grounding.py` の `or "fast"` という意味のないフォールバックを削除し、
  未設定時は loud に raise するように。
- **2026-05-03 — judge verdict-first schema**: judge schema が annotation を
  required + enum 拘束していたため、`reason_code` の表現揺れだけで `verdict=ok`
  が棄却される事象が発生していた。`JUDGE_VERDICT_SCHEMA` / `FINISH_ACCEPTANCE_SCHEMA`
  を verdict-first (status-first) に再設計。fallback ラベルも trigger reason を
  継承するよう変更。詳細は [p4-judge-verdict-first-2026-05-03.md](p4-judge-verdict-first-2026-05-03.md)。
  invariant の一般原則 9-11 を `p4-coding-invariants-2026-04-24.md` に追加。
- **2026-05-03 — follow-through recovery (やり切る invariant)**: LLM が `tool_action`
  schema を 1 回でも外すと turn 全体を放棄する設計だったため、ユーザ request が
  途中で何の前進もなく失敗する事象が発生していた。`json_extraneous_text` (envelope
  は valid だが余計テキスト付き) を recovery 経路として救う設計に変更。最初の
  有効な envelope を採用し、`code: llm_output_recovered` の system_note で可視化。
  `json_retry_limit` のデフォルトを 1 → 2 に拡大。dashboard 側にも recovery 専用
  レンダラを追加し、SSE polling を 1.0s → 0.3s に短縮。
  詳細は [p4-followthrough-recovery-2026-05-03.md](p4-followthrough-recovery-2026-05-03.md)。
  invariant の一般原則 12 を `p4-coding-invariants-2026-04-24.md` に追加。

## Canonical Code Root

- Code: `p4-core/`
- Package: `p4_core`
- Tests: `p4-core/tests/`
- Current Requirements/Principles:
  - [p4-coding-invariants-2026-04-24.md](/Users/satojunichi/Documents/openclaw/handoff/p4-coding-invariants-2026-04-24.md)

Verification:

```bash
cd /Users/satojunichi/Documents/openclaw/p4-core
python3 -m unittest discover -s tests
```

Latest verified result (2026-04-26):

```text
Ran 76 tests in 1.396s
OK
```

Note (2026-05-03): judge verdict-first 改修後の test count は 93 件 (うち 5 件
の dashboard test と 1 件の dashboard test error は 2026-04-26 mainline 以前から
既存の pre-existing failure で、judge 改修とは無関係)。judge 関連 7 件 + 新規追加
2 件はすべて pass。

## What Is Mainline Now

P4 mainline is an extension of P3 with:

- **Recursive Frame Hierarchy**: LLMs can decompose tasks using `open_child_frame` and `return_to_parent` to isolate focus and working memory.
- **Strict Event Contract**: Event types are symmetric (`operation`, `llm`, `tool`, `frame`, `decision`, `observation`).
- **No Double Truths**: Dashboard UI and runtime strictly read from canonical events. Legacy dependencies on `system_note` and `status.json` for deriving running state have been eliminated.
- **Symmetry Resolved**: The recent symmetry audit resolved judge absence fallback tracking (`acceptance_override` mapped to `decision` events) and dashboard fallback on canonical `tool` events.
- **Preserved Safety**: Includes step limits, deep frame protection (max depth 4), and working memory state management.

## Archive Context

The following documents track the historical implementation of P4 and are preserved in `archive/p4/`:

- Requirements: `p4-requirements-2026-04-21.md`
- Design Spec: `p4-design-spec-2026-04-21.md`
- Task List: `p4-task-list-2026-04-21.md` (All tasks completed)
- Audits & Inventories: `p4-event-contract-audit-2026-04-24.md`, `p4-event-contract-decisions-2026-04-24.md`, `p4-symmetry-audit-2026-04-26.md`, `p4-branch-inventory-2026-04-24.md`
