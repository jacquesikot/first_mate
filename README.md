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
└── prototype/
    ├── dashboard.html          clickable UI prototype (open in a browser)
    └── screenshots/
        ├── 01-task-board.png
        ├── 02-task-detail.png
        ├── 03-inbox.png
        ├── 04-memory.png
        └── 05-new-task.png
```

## PRD.md

The full specification: vision, jobs to be done, architecture, per-subsystem requirements, technology direction, delivery phases, and acceptance criteria. This is the handoff document — an implementing agent should be able to build from it without further context.

**Status:** agreed. Phase 0 (the context-relay spike) is the recommended starting point and the go/no-go for the whole design.

## prototype/dashboard.html

A working, clickable prototype of the web dashboard — no build step, no dependencies. Open it directly in a browser.

It runs on mock data and demonstrates the intended interaction model:

- **Tasks** — all tasks grouped by lifecycle state, with live context meters and attention markers
- **Task detail** — the generation rail (the visual rendering of context-relay handoffs), contract criteria with validation evidence, changed files and diffs, live session output
- **Inbox** — the question queue; answering a blocking question resumes its parked task
- **Memory** — per-project durable memory, plus promotion suggestions for recurring answers
- **New task** — the interactive scoping conversation that produces a task contract

Try: open a task from the board, move through its tabs, then answer the scope-change question in the inbox and watch the blocked task return to running.

**Status:** base version, under iteration. Design decisions established so far:

- Near-monochrome dark surface with a single amber accent, reserved for attention only — the needs-you counter, blocking questions, active selection, and the live session.
- Monospace for anything the system measured (IDs, percentages, paths, timings); sans for anything a human wrote (goals, questions, contracts, handoff briefs). The typographic split carries the distinction without labels.
- Status carried by glyphs (`●` running, `◐` blocked, `○` ready, `✓` done, `✗` failed) rather than coloured words.
- The generation rail is the signature element: each step renders its sessions as segments with context fill and handoff markers, making the relay mechanism directly observable.

**Open for iteration:** board layout (flat grouped list vs. columns), density, the detail sidebar's card selection, file-by-file diff navigation, and whether a task history/timeline view is needed.

## Not included

The CLI surface is specified in the PRD (§6.10) but not designed — the dashboard and Slack are the primary interfaces.
