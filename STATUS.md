# First Mate — Project Status

> Living hand-off log. Every session reads this first and updates it before ending.
> Protocol: see `CLAUDE.md`. Spec: see `PRD.md`.

**Current phase:** Phase 0 — relay spike: **COMPLETE, GO** (2026-08-19, exit criterion met: 12/12 items correct across 7 generations). Next: Phase 1 — orchestrator core.

---

## Next up (start here)

**Phase 1 — orchestrator core** (PRD §8, §6.2). Build on the existing `daemon/` package (execution modules + spawner are done and spike-proven):

1. **Task/contract/step model + state store** — SQLite + human-readable files under `~/.firstmate/` (acceptance criterion 10: debuggable with `cat`/`jq`). Task lifecycle: `scoping → ready → running → blocked → validating → done/failed/abandoned`.
2. **Orchestrator loop** (deterministic, one per active task): pick next step → spawn worker (reuse `firstmate/spawner.py`) → wait on events → handle outcome (done / context wall / question / failure) → validate → advance. The relay procedure is already proven in `firstmate/spike/relay.py` — lift it into the loop.
3. **Failure ladder**: validation failure → one retry with failure as context → second failure escalates to a `decision` question. Never a third attempt.
4. **Validation via shell commands** per contract criterion; store evidence.
5. **`fm ask` in park mode** + question queue (state only; dashboard/Slack later).
6. **CLI surface** (`fm serve/task/run/status/attach/answer/...`) + daemon skeleton (async web framework, REST + WebSocket).
7. Hook callbacks should become `fm _event ...` commands hitting the daemon (the spike used append-to-`.fm/events.jsonl` shell hooks — fine for spike, not for the daemon).

## In progress

- Nothing half-finished. Phase 0 closed clean; Phase 1 not started.

## Done

- **2026-08-19** — **Phase 0 relay spike passed (GO).** `daemon/src/firstmate/spike/relay.py`; run with `cd daemon && uv run fm-spike`. A task deliberately overflowed at a 70k-token wall completed correctly across **7 generations** (criterion: ≥3): per-gen progress 0→2→4→6→8→10→12 items with no continuity loss; SessionStart `additionalContext` injection landed in 7/7 sessions (canary-verified); handoff briefs were specific and correct (see `~/.firstmate/spike/run-20260819-130152/.../.fm/handoff-gen*.md`). Cost ≈ $0.45/generation (sonnet workers, client-side estimate).
- **2026-08-19** — **Claude Code facts verified against live CLI (2.1.227) + docs**, superseding PRD §11 snapshot — full list in `daemon/README.md`. Highlights: `--session-id` pins session IDs at spawn (docs omit it, CLI has it); JSON output shape confirmed; PreCompact exit-2 blocks compaction; transcripts at `~/.claude/projects/<munged-cwd>/<sid>.jsonl` with per-assistant-entry `message.usage` token fields; **interrupting a worker mid-tool-call and then `--resume`-ing it for a handoff brief works** (the riskiest unverified assumption — confirmed in all 7 generations).
- **2026-08-19** — **`daemon/` package scaffolded** (uv, Python 3.12): four execution-module boundaries per PRD §7 — `exec/tmux.py`, `exec/gitops.py`, `exec/context.py` (token accounting from transcripts), `exec/hooks.py` (merge-not-overwrite settings writing) — plus `spawner.py` (headless `claude -p` in tmux windows). 11 unit tests pass (`uv run --group dev pytest`).
- **2026-08-19** — PRD written and agreed (`PRD.md` v1.0).
- **2026-08-19** — Dashboard UI prototype built (`prototype/dashboard.html`, design decisions in `README.md`).
- **2026-08-19** — Multi-session memory system set up (`CLAUDE.md` + this file).
- **2026-08-19** — Git initialized; public repo: https://github.com/jacquesikot/first_mate.

## Decision log

Decisions made outside the PRD, dated, with rationale. PRD §7 constraints are settled and not repeated here.

- **2026-08-19** — **Wall trigger is orchestrator-side transcript polling + SIGINT, not PreCompact-blocking.** Auto-compaction's window can't go below 100k tokens, and our own tracker allows arbitrary thresholds and earlier, cheaper relays. The PreCompact exit-2 hook stays installed in every worker as a backstop so compaction can never silently fire. (PRD §6.3's "hard trigger" is thus the backstop; the tracker is primary.)
- **2026-08-19** — **Handoff acquisition = resume-the-walled-session.** After interrupting a worker (even mid-tool-call), `claude -p --resume <sid> --tools ""` asking for a DONE/REMAINING/GOTCHAS brief works reliably and reuses the dying session's full context. Injection into gen N+1 is via SessionStart hook `additionalContext` (verified working headlessly).
- **2026-08-19** — **Workers get hook wiring via a First-Mate-owned settings file passed with `--settings`**, never by writing into the user's or repo's settings files. `exec/hooks.py` still has merge-not-overwrite writing for when we must touch an existing file (scope guard, Phase 2).
- **2026-08-19** — **Worker permission posture: `--permission-mode dontAsk` + explicit `--allowedTools`.** Denies anything off-list instead of stalling on a prompt; denials are recorded in the output JSON (`permission_denials`) and workers adapt in-band. Observed working as mechanical enforcement in the spike.
- **2026-08-19** — Worker model pinned per invocation (spike used `--model sonnet`); orchestrator decision-point calls also pin their model. Keeps cost/behavior deterministic per step.
- **2026-08-19** — Headless worker baseline context is ~38k tokens (system prompt + user config) — wall thresholds must be set relative to that, not to zero.
- **2026-08-19** — Project memory = `CLAUDE.md` (stable) + `STATUS.md` (mutable state), so the auto-loaded file stays small.

## Open questions

Carried from PRD §10 — raise with Jacques when they become blocking; otherwise record an assumption above and proceed:

1. Scoping-in-browser mechanics (deferred by terminal-first Phase 2).
2. Multi-repo tasks — v1 may declare single-repo only.
3. Cost controls (per-task budget) — v1 or Phase 5? (Note: `--max-budget-usd` flag exists on `claude -p`; cheap to wire per-worker.)
4. Dashboard auth — localhost-only assumed for v1.

## Session log

One dated entry per working session: who/what/outcome, newest first.

- **2026-08-19** — Session 2 (Claude): verified Claude Code facts against live CLI/docs; scaffolded `daemon/` (four exec modules, spawner, 11 tests); built and **passed the Phase 0 relay spike** — 12/12 correct across 7 generations, exit criterion met, design is GO. Artifacts under `~/.firstmate/spike/run-20260819-130152/`. Next session: Phase 1 item 1 (task/contract/step model + state store).
- **2026-08-19** — Session 1: created the multi-session memory system (`CLAUDE.md`, `STATUS.md`); initialized git and pushed the public repo. No application code written.
