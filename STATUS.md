# First Mate — Project Status

> Living hand-off log. Every session reads this first and updates it before ending.
> Protocol: see `CLAUDE.md`. Spec: see `PRD.md`.

**Current phase:** Phase 0 — relay spike. **Not started.** No application code exists yet; the repo contains only the spec, the UI prototype, and this memory system.

---

## Next up (start here)

1. **Repo setup** (not yet done):
   - `git init` + initial commit (repo is not yet a git repository).
   - Decide and scaffold the project layout (suggested: `daemon/` Python package, `dashboard/` later in Phase 3, `skills/` for the scoping skill in Phase 2).
2. **Phase 0 — relay spike** (PRD §8, this is the go/no-go for the whole design):
   - Headless spawner: spawn `claude -p` + a skill in a git worktree, capture session ID and structured JSON output.
   - Forced context-wall relay: pre-compaction hook blocks auto-compaction and signals the orchestrator; handoff brief written; session killed; generation N+1 spawned with contract + handoff injected via session-start hook.
   - **Exit criterion:** a task deliberately overflowed at a tiny context limit completes correctly across ≥3 generations.
   - First step of the spike: verify current Claude Code headless flags, `--output-format json` shape, and hook event names against live docs — do not trust the PRD's snapshot (PRD §11).

## In progress

- Nothing. Clean starting point.

## Done

- **2026-08-19** — PRD written and agreed (`PRD.md` v1.0, status: architecture agreed, ready for implementation).
- **2026-08-19** — Dashboard UI prototype built (`prototype/dashboard.html`, mock data, clickable; design decisions documented in `README.md`).
- **2026-08-19** — Multi-session memory system set up (`CLAUDE.md` + this file, session protocol defined).

## Decision log

Decisions made outside the PRD, dated, with rationale. PRD §7 constraints are settled and not repeated here.

- **2026-08-19** — Project memory = `CLAUDE.md` (stable context, auto-loaded by Claude Code) + `STATUS.md` (mutable state). Split so the auto-loaded file stays small and stable while the hand-off log churns freely.

## Open questions

Carried from PRD §10 — raise with Jacques when they become blocking; otherwise record an assumption above and proceed:

1. Scoping-in-browser mechanics (deferred by terminal-first Phase 2).
2. Multi-repo tasks — v1 may declare single-repo only.
3. Cost controls (per-task budget) — v1 or Phase 5?
4. Dashboard auth — localhost-only assumed for v1.

## Session log

One dated entry per working session: who/what/outcome, newest first.

- **2026-08-19** — Session 1: created the multi-session memory system (`CLAUDE.md`, `STATUS.md`). No application code written. Next session should start at "Next up" item 1.
