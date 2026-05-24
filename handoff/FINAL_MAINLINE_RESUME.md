# Final Mainline Resume

Date: 2026-04-21
Commit: `abd3e42`
Branch: `main`

This file is the restart card for the current P1/P2/P3 final mainline.
Use it when resuming work after context loss, switching projects, or starting P4.

## Current Mainline Entry Points

- P1 canonical entry: [P1_MASTER.md](/Users/satojunichi/Documents/openclaw/handoff/P1_MASTER.md)
- P2 canonical entry: [P2_INDEX.md](/Users/satojunichi/Documents/openclaw/handoff/P2_INDEX.md)
- P3 canonical entry: [p3-canonical-mainline-2026-04-21.md](/Users/satojunichi/Documents/openclaw/handoff/p3-canonical-mainline-2026-04-21.md)
- P4 canonical entry: [p4-canonical-mainline-2026-04-26.md](/Users/satojunichi/Documents/openclaw/handoff/p4-canonical-mainline-2026-04-26.md)

## Code Roots

- P1 code: [p1-core](/Users/satojunichi/Documents/openclaw/p1-core)
- P2 code: [p2-core](/Users/satojunichi/Documents/openclaw/p2-core)
- P3 code: [p3-core](/Users/satojunichi/Documents/openclaw/p3-core)
- P4 code: [p4-core](/Users/satojunichi/Documents/openclaw/p4-core)

## Resume Order

1. Read this file.
2. Read the canonical entry for the project being resumed.
3. Check git status and avoid mixing unrelated dirty files.
4. Run the relevant test suite before changing behavior.
5. For P4, start from P3 mainline, not from old `/tmp` workspaces or dashboard logs.

## Verification Commands

```bash
cd /Users/satojunichi/Documents/openclaw/p1-core
python3 -m unittest discover -s tests
```

```bash
cd /Users/satojunichi/Documents/openclaw/p2-core
python3 -m unittest discover -s tests
```

```bash
cd /Users/satojunichi/Documents/openclaw/p3-core
python3 -m unittest discover -s tests
```

```bash
cd /Users/satojunichi/Documents/openclaw/p4-core
python3 -m unittest discover -s tests
```

## Latest Known Test State

- P2: `68 tests OK`
- P3: `35 tests OK`
- P4: `93 tests`, judge/runtime layer fully clean. 5 dashboard test failures + 1 dashboard error are pre-existing (rendering layer drift unrelated to runtime/judge invariants).
- P1: `76 tests`, with 5 known failures at the time of finalization

P4 refinements after 0.4.0-mainline:

- 2026-05-02: workspace bootstrap fallback (DEFAULT_CONFIG deep-merge)
- 2026-05-03: judge verdict-first schema (`p4-judge-verdict-first-2026-05-03.md`)
- 2026-05-03: follow-through recovery (やり切る invariant) (`p4-followthrough-recovery-2026-05-03.md`)

P1 is committed as a mainline artifact, but it is not test-clean. Treat P1 test repair as the first P1-specific continuation task.

Known P1 failure areas:

- dashboard history/state label expectation
- governance feedback low-risk freeze behavior
- growth loop rerun deferral and promotion behavior

## Remote / Push State

The final artifacts are committed locally on `main`.

Push is not complete because no valid remote is configured. `git remote -v` was empty, and `git push origin main` failed because `origin` does not exist.

To publish later:

```bash
cd /Users/satojunichi/Documents/openclaw
git remote add origin <repository-url>
git push -u origin main
```

## Do Not Treat As Mainline

- `p1-core/state/`, `p1-core/logs/`
- `p2-core/state/`, `p2-core/logs/`, `p2-core/runtime/candidates/`
- `p3-core/logs/`, `p3-core/dashboard.log`, `p3-core/debug.py`
- old `/tmp` P3 workspaces
- pycache files
- unrelated `social-agent`, `artificial-life`, or `subjectivity-sandbox` changes

## Practical Next Steps

For P1:

- repair the 5 failing tests before adding new behavior
- keep [P1_MASTER.md](/Users/satojunichi/Documents/openclaw/handoff/P1_MASTER.md) as the purpose and design authority

For P2:

- continue from the session/event kernel and recursive frame model
- keep [P2_INDEX.md](/Users/satojunichi/Documents/openclaw/handoff/P2_INDEX.md) as the document map

For P3/P4:

- use `p3-core/` at `0.3.0-mainline`
- make P4 inherit P3's isolated LLM workspace, event log, dashboard evidence, and Japanese commentator design
- implement formal artifact promotion before trusting generated code as project code
