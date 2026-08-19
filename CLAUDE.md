# First Mate — Agent Context

First Mate is a local autonomous coding-workflow orchestrator: scope a task in a short conversation, then a daemon runs Claude Code skills headlessly across tmux windows and git worktrees, relays context to fresh sessions before limits hit, enforces scope mechanically via hooks, validates against a machine-checkable contract, and interrupts the owner (dashboard/Slack) only at genuine decision points.

This repo is being built across **multiple independent Claude sessions**. This file is the stable context; the living state lives in `STATUS.md`.

## Session protocol (mandatory)

1. **On start:** read this file, then read `STATUS.md` end to end. It tells you the current phase, what's done, what's in progress, and exactly what to pick up next. Then read the PRD sections relevant to your work.
2. **Before implementing anything:** `PRD.md` is the specification and the single source of truth for requirements. Do not invent scope beyond it. If the PRD and STATUS.md conflict, STATUS.md's decision log wins (it's newer) — but flag the conflict in STATUS.md.
3. **Before ending your session:** update `STATUS.md`:
   - Move finished items to **Done** (with date + one-line summary of how/where).
   - Describe **In progress** work precisely enough that a fresh session can continue without you: files touched, approach taken, what's left, any half-finished edges.
   - Update **Next up** so the next session knows what to start.
   - Record any decision you made that isn't in the PRD in the **Decision log** (dated, with rationale).
   - Append a dated entry to the **Session log**.
4. Never leave knowledge only in your conversation. If it matters, it goes in STATUS.md, code comments, or a doc.

## Key documents

- `PRD.md` — full spec: vision, architecture, per-subsystem requirements (§6), tech direction (§7), delivery phases (§8), acceptance criteria (§9). Read §7 and §8 before writing any code.
- `STATUS.md` — living project state and hand-off log. Read first, update last.
- `README.md` — repo overview and prototype guide.
- `prototype/dashboard.html` — clickable UI prototype (mock data). It is the design reference for Phase 3; its visual decisions (documented in README) are agreed.

## Settled constraints — do not re-litigate (PRD §7)

- tmux owns all terminals; First Mate never embeds or emulates a terminal.
- Orchestrator control flow is **deterministic code**; LLM calls only at named decision points (decomposition, handoff summarization, learning extraction, judgment-call validation).
- Continuity lives in **state files**, never in long-lived conversations.
- **Hooks are the event mechanism** — workers report up via Claude Code hooks calling `fm` callback commands. Never scrape terminal output to infer state.
- Daemon: Python, async web framework, REST + WebSocket, single process. State in SQLite + plain human-readable files under `~/.firstmate/`.
- Dashboard: SPA (owner's stack is TypeScript/React), thin client, talks only to the daemon API.
- Slack: official SDK, socket mode.
- Four clean execution-module boundaries: tmux control, git operations, context tracking, hook management. Nothing else touches tmux or shells out to git.

## Build order (PRD §8)

Phase 0 (relay spike — go/no-go for the whole design) → 1 (orchestrator core) → 2 (scoping + scope guard) → 3 (web dashboard) → 4 (Slack + memory loop) → 5 (polish). Current phase: see STATUS.md.

## Working rules

- Verify current Claude Code flags, JSON output formats, and hook event names against live docs/`claude --help` at build time — the PRD's snapshot of them is not to be trusted (PRD §11).
- Everything under `~/.firstmate/` must stay debuggable with `cat` and `jq` (acceptance criterion 10).
- Hook wiring must merge with existing settings files, never overwrite them.
- Prefer small, verifiable increments; each phase has an explicit exit criterion — check it before declaring the phase done.
- Owner: Jacques Ikot. Ask (don't assume) on anything the PRD lists as an open question (§10) if it blocks you; otherwise record your working assumption in the decision log and proceed.
