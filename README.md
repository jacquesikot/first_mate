# First Mate

An autonomous coding-workflow orchestrator that runs locally. Scope a task in a short conversation; it decomposes the work, runs Claude Code skills across headless sessions in isolated git worktrees, validates its own output, relays context to fresh sessions before limits are hit, and pulls you in — via web dashboard or Slack — only at genuine decision points.

## Contents

```
first_mate/
├── README.md                   this file
├── PRD.md                      the specification — start here
├── CLAUDE.md                   agent context + session protocol (auto-loaded by Claude Code)
├── STATUS.md                   living project status / hand-off log — agents read first, update last
├── daemon/                     the fm daemon (Python) — orchestrator, API, CLI; see daemon/README.md
├── dashboard/                  the web dashboard (React/TypeScript SPA, Phase 3)
└── prototype/
    ├── First Mate System Prototype.html   clickable UI prototype (open in a browser)
    └── support.js                         runtime the prototype HTML loads
```

## PRD.md

The full specification: vision, jobs to be done, architecture, per-subsystem requirements, technology direction, delivery phases, and acceptance criteria. This is the handoff document — an implementing agent should be able to build from it without further context.

**Status:** agreed. Phase 0 (the context-relay spike) is the recommended starting point and the go/no-go for the whole design.

## dashboard/

The real web dashboard (Phase 3): a thin React/TypeScript client over the
daemon API, visually translated from the prototype below. Build once with
`cd dashboard && pnpm install && pnpm build`; the daemon then serves it at
`http://127.0.0.1:<port>/ui/` (`fm serve --open`). For UI development,
`pnpm dev` proxies API + WebSocket calls to a locally running daemon.
`pnpm test` runs the frontend unit tests (vitest).

Views: **Now** (needs-you queue, live sessions with streamed output, open
scoping sessions, settled list), **Tasks** (board grouped by lifecycle),
**Task detail** (the scoping conversation while the task is being scoped;
then the step timeline with the generation rail and handoff briefs, criteria
with validation evidence, contract render/edit between steps, changed files
+ diff viewer, live output, question history), **Inbox**, **Memory**
(view/edit/append), **New task** (repo picker + goal).

**Starting a task is task-first:** pick a repo, say what you want, and
First Mate creates the session immediately (status `scoping`) and hands you
its task view. The scoping conversation happens inside that session — it
shows up in the queue from the first keystroke, survives a reload or a
daemon restart, and approving its contract turns that same task into a
running one. Assistant text (scoping replies, questions, handoff briefs)
renders as markdown. Pasting a pre-written contract JSON remains a secondary
path on the New task view.

## prototype/First Mate System Prototype.html

A working, clickable prototype of the web dashboard — no build step; open it directly in a browser (it loads `support.js` from the same directory).

It runs on mock data and demonstrates the intended interaction model:

- **Tasks** — all tasks grouped by lifecycle state, with live context meters and attention markers
- **Task detail** — the generation rail (the visual rendering of context-relay handoffs), contract criteria with validation evidence, changed files and diffs, live session output
- **Inbox** — the question queue; answering a blocking question resumes its parked task
- **Memory** — per-project durable memory, plus promotion suggestions for recurring answers
- **New task** — the interactive scoping conversation that produces a task contract

Try: open a task from the board, move through its tabs, then answer the scope-change question in the inbox and watch the blocked task return to running.

**Status:** improved UI/UX iteration (2026-08-19, supersedes the original `dashboard.html`, which remains in git history). This file is the design reference for Phase 3. Design decisions established so far:

- Near-monochrome dark surface with a single amber accent, reserved for attention only — the needs-you counter, blocking questions, active selection, and the live session.
- Monospace for anything the system measured (IDs, percentages, paths, timings); sans for anything a human wrote (goals, questions, contracts, handoff briefs). The typographic split carries the distinction without labels.
- Status carried by glyphs (`●` running, `◐` blocked, `○` ready, `✓` done, `✗` failed) rather than coloured words.
- The generation rail is the signature element: each step renders its sessions as segments with context fill and handoff markers, making the relay mechanism directly observable.

**Open for iteration:** board layout (flat grouped list vs. columns), density, the detail sidebar's card selection, file-by-file diff navigation, and whether a task history/timeline view is needed.

## Not included

The CLI surface is specified in the PRD (§6.10) but not designed — the dashboard and Slack are the primary interfaces.
