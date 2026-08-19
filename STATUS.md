# First Mate — Project Status

> Living hand-off log. Every session reads this first and updates it before ending.
> Protocol: see `CLAUDE.md`. Spec: see `PRD.md`.

**Current phase:** Phase 1 — orchestrator core: **COMPLETE** (2026-08-19; 44 unit tests pass, end-to-end smoke passed twice with real workers). Next: Phase 2 — scoping + scope guard.

---

## Next up (start here)

**Phase 2 — scoping + scope guard** (PRD §8, §6.1, §6.4):

1. **Scoping skill + contract generation (terminal chat first).** `fm task "<goal>"` currently prints a Phase-2 placeholder (`cli.py:cmd_task`). Build a scoping conversation (interactive `claude` session with a scoping skill/prompt) that reads project memory + repo context, proposes scope/steps/criteria, refuses non-machine-checkable criteria, and emits contract JSON that `POST /tasks` already validates (`models.validate_contract`).
2. **Scope-guard hook (PreToolUse).** Contract already carries `scope_in`/`scope_out` globs (unused so far). Compile them into a PreToolUse hook in the worker settings (`workerfiles.write_worker_hooks`) that blocks Edit/Write/Bash writes outside scope (exit 2) with an in-band message telling the agent to raise a `scope_change` question. `exec/hooks.py` has merge-not-overwrite writing if a repo settings file must be touched.
3. **Tripwires** (each → a question, not a surprise): dependency-manifest changes, migrations, pushes, deletion/diff-size thresholds (PRD §6.4). Configurable per project.
4. **Contract amendment via answers** already works mechanically (answers append to `contract.amendments` and are injected); scoping should make amendment semantics richer if needed.
5. Nice-to-have carried from Phase 1: `block-with-timeout` ask mode (park-only today), question batching at step boundaries (PRD §6.5), `--max-budget-usd` per worker (flag verified to exist).

## In progress

- Nothing half-finished. Phase 1 closed clean.

## Done

- **2026-08-19** — **Phase 1 orchestrator core complete.** New modules in `daemon/src/firstmate/`: `models.py` (task/contract/step/question dataclasses + `validate_contract` machine-checkability gate), `store.py` (files under `~/.firstmate/` are source of truth, SQLite is a rebuildable index reindexed on boot), `validation.py` (shell criteria + evidence), `workerfiles.py` (per-worker hook scripts → `fm _event` POSTs to daemon; SessionStart injection), `relay.py` (handoff via resume, lifted from spike), `orchestrator.py` (deterministic per-task loop: wall relay, park mode, failure ladder — never a 3rd attempt, escalation question carries diff + failing checks), `server.py` (FastAPI REST + WebSocket, Manager with global worker-slot semaphore + boot reconciliation of orphaned sessions), `cli.py` (`fm serve/task add/run/status/attach/answer/pause/abandon/remember/ask/_event`), `smoke.py` (e2e). 44 unit tests pass (`uv run --group dev pytest`, hermetic). **Smoke passed twice** (`uv run fm-smoke`): worker execution → validation → `fm ask` park (worker torn down with handoff) → answer → respawn with answer injected → task-boundary validation → `done`. First run also organically exercised the failure ladder (worker wrote to repo root instead of worktree; retry with failure context fixed it) — worker prompt now pins cwd=worktree explicitly.
- **2026-08-19** — **Phase 0 relay spike passed (GO).** `daemon/src/firstmate/spike/relay.py`; run with `cd daemon && uv run fm-spike`. A task deliberately overflowed at a 70k-token wall completed correctly across **7 generations** (criterion: ≥3): per-gen progress 0→2→4→6→8→10→12 items with no continuity loss; SessionStart `additionalContext` injection landed in 7/7 sessions (canary-verified); handoff briefs were specific and correct. Cost ≈ $0.45/generation (sonnet workers).
- **2026-08-19** — **Claude Code facts verified against live CLI (2.1.227) + docs**, superseding PRD §11 snapshot — full list in `daemon/README.md`. Highlights: `--session-id` pins session IDs at spawn; JSON output shape confirmed; PreCompact exit-2 blocks compaction; transcripts at `~/.claude/projects/<munged-cwd>/<sid>.jsonl`; interrupting a worker mid-tool-call then `--resume`-ing for a handoff brief works.
- **2026-08-19** — **`daemon/` package scaffolded** (uv, Python 3.12): four execution-module boundaries per PRD §7 — `exec/tmux.py`, `exec/gitops.py`, `exec/context.py`, `exec/hooks.py` — plus `spawner.py` (headless `claude -p` in tmux windows).
- **2026-08-19** — PRD written and agreed (`PRD.md` v1.0).
- **2026-08-19** — Dashboard UI prototype built (`prototype/dashboard.html`, design decisions in `README.md`).
- **2026-08-19** — Multi-session memory system set up (`CLAUDE.md` + this file).
- **2026-08-19** — Git initialized; public repo: https://github.com/jacquesikot/first_mate.

## Decision log

Decisions made outside the PRD, dated, with rationale. PRD §7 constraints are settled and not repeated here.

- **2026-08-19** — **Files are the source of truth; SQLite is a rebuildable index.** PRD §7 says "SQLite + plain files"; resolving the split: every task/contract/question/event lives as JSON/markdown/jsonl under `~/.firstmate/` (criterion 10), and `firstmate.db` is reindexed from files on every `Store()` construction. Deleting the db or hand-editing a file is always safe.
- **2026-08-19** — **Phase 1 task creation = `fm task add <contract.json>`.** Scoping conversations are Phase 2; until then contracts are hand-written JSON, validated by the same machine-checkability gate the scoping skill will feed (`POST /tasks` rejects criteria without commands).
- **2026-08-19** — **`paused` added to the task lifecycle.** The PRD lists `fm pause` but no paused state; treated as a resting state between `ready` and `running`, resumable with `fm run`.
- **2026-08-19** — **Park mode mechanics:** any non-`fyi` `fm ask` parks. The daemon's reply tells the worker to stop; the orchestrator gives it 90s to exit on its own, then interrupts; a handoff brief is acquired from the dying session before teardown; the answer is injected (with the handoff) into the respawn. `fyi` records without parking. On operator answer, the step's attempt counter resets to 1 (human intervened → ladder restarts).
- **2026-08-19** — **Escalation answers:** the failure-ladder escalation question carries options `retry`/`abandon` plus free text; answering `abandon` abandons the task, anything else restarts the step with the answer injected as a binding amendment.
- **2026-08-19** — **Worker timeout is treated like a context wall** (interrupt → handoff → next generation), bounded by `max_generations`; a hung worker therefore relays rather than failing opaquely.
- **2026-08-19** — **Hook callbacks are `fm _event <name> --task ... --url ...` POSTing to the daemon**; values baked into generated scripts (no env inheritance assumptions); on unreachable daemon they append to `<worktree>/.fm/events-fallback.jsonl` and never fail the worker (hook exit 0 always).
- **2026-08-19** — **Daemon stack: FastAPI + uvicorn**, bound to 127.0.0.1 only. Worker `fm ask` allowlisted as `Bash(fm ask:*)` only — workers cannot operate the daemon.
- **2026-08-19** — **Default worker tool allowlist** = Read/Glob/Grep/Edit/Write + `Bash(fm ask:*)`; contracts widen per step (e.g. `Bash(npm test:*)`).
- **2026-08-19** — **Worker prompts pin cwd = worktree.** Smoke run 1 showed a worker "helpfully" writing into the original repo path it saw in the contract; the prompt now forbids that explicitly. (Phase 2's scope guard will also enforce it mechanically.)
- **2026-08-19** — **Wall trigger is orchestrator-side transcript polling + SIGINT, not PreCompact-blocking.** Auto-compaction's window can't go below 100k tokens; our tracker allows arbitrary thresholds. The PreCompact exit-2 hook stays installed as a backstop. Default wall: 150k tokens (config).
- **2026-08-19** — **Handoff acquisition = resume-the-walled-session** (`claude -p --resume <sid> --tools ""`); injection into gen N+1 via SessionStart `additionalContext`.
- **2026-08-19** — **Workers get hook wiring via a First-Mate-owned settings file passed with `--settings`** (regenerated fresh each generation), never by writing into user/repo settings files.
- **2026-08-19** — **Worker permission posture: `--permission-mode dontAsk` + explicit `--allowedTools`.**
- **2026-08-19** — Worker model pinned per invocation (config `worker_model`, per-step override in contract).
- **2026-08-19** — Headless worker baseline context is ~38k tokens — wall thresholds are relative to that, not zero.
- **2026-08-19** — Project memory = `CLAUDE.md` (stable) + `STATUS.md` (mutable state), so the auto-loaded file stays small.

## Open questions

Carried from PRD §10 — raise with Jacques when they become blocking; otherwise record an assumption above and proceed:

1. Scoping-in-browser mechanics (deferred by terminal-first Phase 2).
2. Multi-repo tasks — v1 may declare single-repo only.
3. Cost controls (per-task budget) — v1 or Phase 5? (`--max-budget-usd` exists on `claude -p`; cheap to wire per-worker.)
4. Dashboard auth — localhost-only assumed for v1 (daemon already binds 127.0.0.1 only).

## Session log

One dated entry per working session: who/what/outcome, newest first.

- **2026-08-19** — Session 3 (Claude): built Phase 1 orchestrator core end to end — models/store (files + rebuildable SQLite index), shell validation with evidence, worker hook files (`fm _event` → daemon), orchestrator loop (relay, park mode, failure ladder, reconciliation), FastAPI daemon (REST + WS + worker-slot cap), full `fm` CLI, 33 new unit tests (44 total, all passing), `fm-smoke` e2e. Smoke passed twice with real sonnet workers (second run clean after pinning cwd=worktree in the worker prompt); park→answer→resume and the failure ladder both exercised for real. Phase 1 exit criteria met. Next session: Phase 2 item 1 (scoping skill) or item 2 (scope-guard hook) — they're independent.
- **2026-08-19** — Session 2 (Claude): verified Claude Code facts against live CLI/docs; scaffolded `daemon/` (four exec modules, spawner, 11 tests); built and **passed the Phase 0 relay spike** — 12/12 correct across 7 generations, exit criterion met, design is GO. Artifacts under `~/.firstmate/spike/run-20260819-130152/`.
- **2026-08-19** — Session 1: created the multi-session memory system (`CLAUDE.md`, `STATUS.md`); initialized git and pushed the public repo. No application code written.
