# First Mate — Product Requirements Document

**Owner:** Jacques Ikot
**Status:** Architecture agreed; ready for implementation
**Version:** 1.0

---

## 1. Vision

First Mate is an autonomous coding-workflow orchestrator that runs locally. I give it a task in a short scoping conversation; it decomposes the task, runs my existing Claude Code skills (`plan`, `implement`, `review`, and others) across headless sessions in isolated git worktrees, validates its own work, manages each session's context window — relaying to fresh sessions before context runs out — and accumulates durable project memory so it improves with use.

It is autonomous where autonomy is cheap and interruptive where my judgment is genuinely needed: it pulls me in for scope changes, ambiguities, approvals, and repeated failures — through a web dashboard when I'm at my desk and Slack when I'm not.

**The defining loop:** scope interactively → run autonomously → interrupt me only at genuine forks → learn from every answer.

## 2. Problem

Multi-stage work (plan → implement → review, research pipelines, transcription flows) currently requires me to be the orchestrator: opening terminal tabs, invoking skills in sequence, watching for stalls, noticing when a session nears its context limit, carrying context between sessions by hand, and re-teaching every session the same project-specific lessons. Tab-management tooling treats the symptom. The disease is that *I* am the control loop.

## 3. Jobs to be done

| # | Job | Today | With First Mate |
|---|-----|-------|-----------------|
| J1 | **Scope a task with clear completion criteria** | In my head, implicitly | Interactive scoping session produces a machine-checkable task contract |
| J2 | **Execute multi-step skill flows end to end** | I invoke each skill manually, in sequence, per tab | Orchestrator runs steps headlessly in worktrees, sequentially or in parallel |
| J3 | **Manage context limits across sessions** | I notice too late; auto-compaction loses the thread | Context relay: intercept before compaction, write handoff, respawn fresh session with continuity |
| J4 | **Prevent and negotiate scope creep** | Discovered in review, after the damage | Scope guard blocks out-of-contract edits mechanically; agent must ask |
| J5 | **Pull me in when needed, wherever I am** | I have to be watching | Question queue → dashboard inbox + Slack ping; parked tasks resume on my answer |
| J6 | **Validate work before calling it done** | I check manually | Per-criterion validation gates (tests, Playwright, diff constraints) enforced by the orchestrator |
| J7 | **Get better over time** | Every session starts from zero | Durable per-project memory, written by me and by the system, injected into every session |
| J8 | **Observe everything from one place** | Tab archaeology | Web dashboard: tasks, steps, sessions, live output, diffs, context meters, question inbox |
| J9 | **Drop into any live session** | Find the right tab | One CLI command (`fm attach`) from the dashboard's instruction, straight into the tmux window |

## 4. High-level architecture

```
                        ┌──────────────────────────────┐
   You (desk)  ────────►│   Web dashboard (localhost)  │
                        └────────────┬─────────────────┘
                                     │ HTTP + WebSocket
   You (phone) ───► Slack ◄──────────┤
                    connector        │
                        ┌────────────▼─────────────────┐
                        │      fm daemon (local)       │
                        │  ┌────────────────────────┐  │
                        │  │  Orchestrator loop     │  │  deterministic code;
                        │  │  (per active task)     │  │  LLM only at decision
                        │  └───┬────────────┬───────┘  │  points
                        │  ┌───▼───┐   ┌────▼───────┐  │
                        │  │ State │   │ Question   │  │
                        │  │ store │   │ queue      │  │
                        │  └───────┘   └────────────┘  │
                        │  ┌────────────────────────┐  │
                        │  │ Project memory         │  │
                        │  └────────────────────────┘  │
                        └───────┬──────────────────────┘
                                │ spawns / kills / captures
                 ┌──────────────▼───────────────────────┐
                 │   tmux session "firstmate"           │
                 │  ┌──────────┐ ┌──────────┐ ┌──────┐  │
                 │  │ worker 1 │ │ worker 2 │ │  …   │  │  claude -p + skill,
                 │  │ worktree │ │ worktree │ │      │  │  one window each
                 │  └────┬─────┘ └────┬─────┘ └──────┘  │
                 └───────┼────────────┼─────────────────┘
                         │ hooks (events up)
                         ▼
                 fm callback commands → daemon
```

**Components:**

1. **fm daemon** (`fm serve`) — a single long-running local process. Hosts the orchestrator loops (one per active task), the HTTP/WebSocket API, the question queue, the state store, and the Slack connector. Nothing runs in the cloud; the browser talks to `localhost`.
2. **Execution layer** — a tmux window per worker session, a git worktree per task, Claude Code invoked headlessly (`claude -p`) with the relevant skill in the prompt, structured JSON output, per-worktree hook files.
3. **Event flow is one-directional and event-driven:** workers report upward via Claude Code hooks calling `fm` callback commands (status changes, context walls, questions, scope-guard trips); the daemon never scrapes terminal output to infer state. The dashboard and Slack receive pushes over WebSocket/API from the daemon.
4. **Three clients, one API.** Web dashboard (primary), Slack (remote/notifications/answers), CLI (scripting, scoping entry point, and terminal attach). All operate on the same daemon API so state is always consistent.

## 5. Core concepts

- **Task** — the unit of work I hand over. Has a contract, a step plan, a worktree (usually one), and a lifecycle: `scoping → ready → running → blocked → validating → done/failed/abandoned`.
- **Contract** — the scoping session's output: goal, in-scope/out-of-scope paths, machine-checkable completion criteria, validation commands, step plan, known context. The single source of truth for what "done" means. Amendable mid-run (via answered questions), never implicit.
- **Step** — one skill execution within a task (`plan`, `implement` phase, `review`, or user-defined). Has attempts, generations (see relay), and a status.
- **Worker session** — one headless Claude Code invocation in a tmux window inside the task's worktree. Disposable by design; continuity lives in files, not transcripts.
- **Generation** — the Nth session serving the same step. Generation increments when the context relay fires.
- **Question** — a structured request for my input, with type (`clarification`, `scope_change`, `decision`, `approval`, `fyi`), urgency, options, evidence, and a default. Lives in a queue; surfaces in dashboard and Slack.
- **Memory** — two tiers. *Working memory*: the task's state files, dies with the task. *Durable memory*: per-project markdown of accumulated lessons, injected into every session, appended by the system and by me.

## 6. Functional requirements by subsystem

### 6.1 Scoping (interactive entry point)

- `fm task "<goal>"` (or "new task" in the dashboard) opens an interactive scoping conversation backed by a dedicated scoping skill.
- The scoping skill reads project memory and relevant repo/issue context first, then *proposes* scope, steps, and completion criteria for me to push back on — it does not open with a questionnaire.
- It must refuse to finalize until every completion criterion is machine-checkable (a command, a test, a diff constraint). "Make X better" is rejected with a request for a concrete check.
- Output: `contract.md` + initial `state.json` for the task. The scoping session then ends; nothing downstream is interactive by default.
- Contracts are amendable: answered questions append to the contract; I can also edit it directly between steps.

### 6.2 Orchestration engine

- One deterministic loop per active task: pick next step → spawn worker → wait on events → handle outcome (done / context wall / question / failure) → validate → advance.
- LLM calls occur only at fixed decision points — task decomposition, handoff summarization, learning extraction, and optional judgment calls (e.g. "does this diff satisfy criterion 3?") — each as a fresh, stateless, schema-constrained headless invocation. The loop itself is code.
- Multiple tasks run concurrently; a global concurrency cap (configurable) limits simultaneous worker sessions to respect rate limits and machine load. Blocked tasks consume no worker slots.
- Steps may declare dependencies; independent steps within a task may parallelize (post-v1 acceptable; sequential is fine for v1).
- Failure ladder: validation failure → one retry with the failure as context → a declared loop edge (below) rewinds and iterates → otherwise escalates to a `decision` question with diff and failing check attached. Never a third autonomous attempt at the same thing.
- Every step's worker gets a tool allowlist appropriate to its skill (review = read-only; implement = scoped writes). Defaults per skill, overridable in the contract.
- **Waiting (gates).** A step may declare a `when` gate: a machine-checkable shell probe that must pass before a worker is spawned. While it is red the task sits in `waiting` — no session, no tokens, no worker slot — and the daemon re-probes on the gate's interval. Gate progress is persisted, so a daemon restart resumes the same wait rather than granting a fresh ceiling; exceeding `ceiling` escalates. This is what lets First Mate sit out something slow and external (an AI reviewer, CI, a deploy) instead of spending a session's context on a sleep loop. Step prompts must never poll or sleep for minutes; that is what gates replace.
- **Supervision.** A gate can be *wrong* — waiting on a record that only exists in some cases, a SHA that has moved, a field the provider never populates. When a gate stalls, a supervisor decision point investigates the real external state with read-only tools (`gh`, `git`, file reads) and returns one of `gate_wrong` / `still_waiting` / `cannot_tell`. On `gate_wrong` it rewrites the probe — or drops the gate when there is genuinely nothing left to wait for — and the repair is accepted **only if the new probe actually passes**, so a confident-but-wrong diagnosis cannot move the goalposts. Bounded attempts; on giving up, the ceiling escalation carries what it learned instead of a bare "still waiting". The operator is told after the fact with a non-blocking FYI, never stopped: **a gate is First Mate's own measuring instrument, and fixing a broken instrument is a runtime decision.** Enforced mechanically — the supervisor's only reachable field is `steps[i].when`; it can never touch a completion criterion, which is the operator's definition of done.
- **Unsatisfiable criteria.** A criterion can also assert something unreachable (a record that will never be created, an identifier that has moved, a resource now closed). Looping against one fails identically every round, so after a round that changes nothing the supervisor judges whether more work could ever satisfy the check: `unsatisfiable` / `needs_more_work` / `cannot_tell`. On a confident `unsatisfiable` the task escalates **immediately** with the findings and a suggested correction, instead of burning its remaining rounds. The vocabulary deliberately has no "here is a better criterion, apply it" — the supervisor may diagnose and recommend, never edit; a criterion is the operator's statement of what they wanted, and altering it is their decision, taken through the escalation (whose free text already routes to the re-planner). Low-confidence "impossible" does not stop the loop: looping is cheap, stopping wrongly is not.
- **Iterating (loop edges).** A step may declare `on_failure: {goto, max_iterations}`. When its criteria fail, the orchestrator rewinds to the named step and re-runs everything from there — so a fix is genuinely re-made, re-pushed and re-verified. Convergence work (fix → push → re-review → fix) is therefore expressed in the contract and runs autonomously. Two independent brakes: `max_iterations`, and a no-progress check that stops the loop when a round fails with the same evidence as the last one. The step it rewinds to is told why, with the failing evidence attached.

### 6.3 Context management (the relay)

- Continuous tracking: per-session context percentage derived from Claude Code session transcripts, shown on the dashboard as an early-warning meter (thresholds: neutral <60%, elevated 60–85%, warning ≥85%).
- Hard trigger: a pre-compaction hook in every worktree blocks Claude Code's automatic compaction and signals the daemon instead.
- Relay procedure: daemon requests a structured handoff brief from the running session (what's done, what remains, gotchas, key file locations), writes it into the step's state, kills the session, spawns generation N+1, and injects contract + handoff + memory into the new session at startup via the session-start hook.
- Transcript identity: each session's ID is captured at session start and pinned to the step, so context tracking and resume operations always target the right transcript, including when multiple sessions share a worktree.
- The relay must be observable: dashboard shows generation count per step and the handoff text.

### 6.4 Scope enforcement

- The contract's in-scope path globs compile to a pre-tool-use guard hook installed in the worktree: edits/writes outside scope are mechanically blocked before execution, with an in-band message telling the agent to raise a `scope_change` question instead.
- Additional tripwires, each producing a question rather than a surprise: dependency manifest changes, new/modified migrations, pushes to remotes, deletions above a threshold, total diff size above a threshold. All configurable per project.
- **Scratch space.** Workers may always write under `.fm/artifacts/` — no approval, no scope entry, excluded from git via the worktree's private excludes so it can never reach a diff or a commit. A worker's own bookkeeping (a draft it wrote, notes, a generated report) is a runtime concern, not a change to the operator's deliverable, and must never cost an interruption. Out-of-scope blocks name this directory as the alternative.
- **Manifests vs. lockfiles.** Editing a file that *declares* dependencies (`package.json`, `pyproject.toml`, …) trips the tripwire. A *derived* lockfile does not: a bare `bun install` / `npm ci` in a fresh worktree rewrites one purely as a side effect of populating `node_modules`, which is not something the operator can meaningfully approve. The guarantee "no dependency actually changed" is enforced instead at the step boundary, where a lockfile appearing in the diff raises the question once.

### 6.5 Question queue & human-in-the-loop

- Workers raise questions via an `fm ask` command (allowlisted for them), with type, question, options, default, evidence, and urgency.
- Two modes: **park** (default — question queued, session hands off and is torn down, task marked `blocked`, orchestrator moves on to other work; answer triggers respawn with the answer in context) and **block-with-timeout** (session stays alive polling for quick approvals; falls back to park on timeout).
- Batching rule: non-blocking questions accumulate and surface together at step boundaries; only `blocking` urgency pages me immediately. Skill guidance instructs workers to proceed on defensible assumptions recorded as `fyi` rather than blocking on trivia.
- Answers are recorded with attribution and timestamp, appended to the contract, and fingerprinted: a question recurring across tasks prompts a one-time "promote to project memory?" suggestion.
- **An answer must always change something mechanically**, or the next worker generation walks into the identical wall and asks again. Three paths, in order: (a) an `allow` widens exactly what the question's evidence names; (b) a refusal or redirect is appended to the offending step's prompt as a binding correction, so the instruction that caused the block no longer stands; (c) anything else the operator says in their own words goes to a **re-planning** decision point (§6.2's LLM call list) which rewrites steps/criteria/scope to express it. A re-plan may not touch `goal` or `repo`, must pass the same `validate_contract` gate as a freshly scoped contract, may not make a criterion trivially true, and persists the before-contract plus a unified diff as task artifacts so the operator can audit exactly what their words did.
- **Fingerprinting also suppresses re-asks within a task.** Guard-raised questions are keyed on the situation (tripwire + paths), not the agent's prose, because one block hit by three successive generations yields three differently worded questions about one identical decision. An equivalent question is auto-answered from the earlier one and the worker is told to continue rather than stop; the reuse is recorded as an event, never silent.
- Every question is answerable from all three clients identically: dashboard inbox, Slack message action/reply, `fm answer <id> <choice>`.

### 6.6 Memory

- Durable memory: one markdown file per project, append-only with dated entries, injected into every worker session at start and into every scoping session.
- Written three ways: (a) explicitly by me — `fm remember "<fact>"` or a dashboard/Slack equivalent; (b) by the system after successful steps via a narrowly-prompted learning extraction ("what project-specific fact would have saved time — no generic advice"); (c) by promotion of recurring question answers.
- Periodic compaction: when a memory file exceeds a size threshold, an LLM pass deduplicates and consolidates, preserving dated provenance. Never silently deletes; keeps an archive.
- Memory is inspectable and editable from the dashboard (it's my data, in plain markdown).

### 6.7 Validation

- Each completion criterion in the contract maps to a validation method: shell command (tests, linters, build), Playwright check (worker-driven — the implement/verify skill has Playwright MCP access and must emit a structured pass/fail marker with evidence), diff constraint (paths, size), or judgment call (schema-constrained LLM evaluation, used sparingly).
- The orchestrator runs/collects all criterion checks at step and task boundaries; a task cannot reach `done` with unmet criteria.
- Validation evidence (test output, screenshots, diff stats) is stored with the task and visible in the dashboard.

### 6.8 Web dashboard

Served by the daemon at `localhost:<port>`; opens automatically with `fm serve --open`. Visual direction: near-monochrome dark theme with a single amber accent reserved for attention (needs-input alerts, active selection), status carried by glyphs (`●` running, `◔` waiting on a gate, `◐` blocked, `✓` done, `✗` failed), fixed-column metrics, filenames prominent with directories de-emphasised, desaturated diff colors.

Views:
- **Task board** — all tasks by lifecycle state; each card shows current step, generation, context meter of the live session, and pending-question count. Global header carries the attention counter ("2 need input").
- **Task detail** — the contract (rendered, editable between steps), step timeline with attempts/generations/handoffs, validation status per criterion with evidence, live session output (streamed pane capture over WebSocket, read-only), changed files and diff viewer for the task's worktree, and the task's question history.
- **Inbox** — the question queue across all tasks, ranked by urgency then age; answering is a click (option buttons) or short text; supports amending the contract inline as part of an answer.
- **Memory** — per-project memory files, viewable and editable; pending "promote to memory?" suggestions.
- **New task** — launches the scoping conversation in the browser (chat panel backed by the daemon driving an interactive scoping session).
- **Attach affordance** — each live session shows its `fm attach <id>` command with one-click copy. v1 explicitly does *not* embed an interactive terminal in the browser; live output is read-only streaming. (Embedded xterm.js terminal is a stated post-v1 candidate.)

Non-functional: dashboard is a thin client — no state of its own; refresh-safe; everything it shows comes from the daemon API; usable at desktop widths, readable at tablet widths.

### 6.9 Slack connector

- A Slack app (socket-mode preferred, so no public endpoint or tunnel is required) connected to the daemon.
- Outbound: blocking questions (with option buttons), task completions/failures, escalations. Throttled — one active notification thread per task; follow-ups thread under it.
- Inbound: button clicks and threaded replies resolve to `fm answer`; `remember: <fact>` in a reply appends to project memory; a small command set (`status`, `pause <task>`, `abandon <task>`) — deliberately minimal, Slack is for unblocking, not operating.
- Delivery/answer consistency: an answer given in Slack reflects in the dashboard inbox within a second, and vice versa; double-answers resolve first-write-wins with a notice.

### 6.10 CLI surface

The daemon API's scripting face and the terminal-native operations:

```
fm serve [--open]            start daemon + dashboard
fm task "<goal>"             start a scoping conversation (terminal chat)
fm run <task>                start/resume an approved task
fm status                    tasks, steps, questions at a glance
fm attach <session|task>     jump into the live tmux window
fm answer <qid> <choice>     answer a question
fm remember "<fact>" [-p]    append to project memory
fm pause|abandon <task>      lifecycle controls
fm ask …                     (worker-facing) raise a question
fm _event|_wall|…            (hook-facing) internal callbacks
```

## 7. Technology direction (high level — implementer's latitude below this)

- **Daemon:** Python with an async web framework serving REST + WebSocket. A single process; state in SQLite + plain files under `~/.firstmate/` (tasks, contracts, questions, memory as human-readable markdown/JSON — debuggability is a feature).
- **Execution modules:** four clean boundaries — tmux control (window create/kill/capture/attach; nothing else touches tmux), git operations (worktree lifecycle at `<repo-parent>/<repo>-worktrees/<branch>`, changed files, diffs; nothing else shells out to git), context tracking (transcript discovery and token accounting from `~/.claude/projects/`), hook management (generation and merging of per-worktree hook settings).
- **Workers:** Claude Code headless (`claude -p`) with `--output-format json` (and schema-constrained output where the orchestrator branches on the result), per-skill tool allowlists, sessions resumable by captured session ID. Skills invoked by including the skill/command in the prompt.
- **Hooks:** per-worktree settings files wiring session-start (context injection), pre-compaction (relay trigger, blocking), stop/notification (status), pre-tool-use (scope guard) to `fm` callbacks. Hook wiring must merge with, not overwrite, existing settings.
- **Web UI:** any mainstream SPA stack the implementer prefers (owner's stack is TypeScript/React); talks only to the daemon API; no direct file or tmux access from the browser.
- **Slack:** official SDK, socket mode.
- **Constraints that are settled (do not re-litigate):** tmux owns all terminals — First Mate never embeds or emulates a terminal; orchestrator control flow is deterministic code with LLM calls only at named decision points; continuity lives in state files, never in long-lived conversations; hooks are the event mechanism, not output scraping.

## 8. Delivery phases

**Phase 0 — Relay spike (de-risk first).** Headless spawner (spawn `claude -p` + skill in a worktree, capture session ID and structured output) plus a forced context-wall relay: handoff written, session replaced, continuity verified end to end. *Exit criterion: a task deliberately overflowed at a tiny context limit completes correctly across ≥3 generations.* If this doesn't work, nothing else matters.

**Phase 1 — Orchestrator core.** Task/contract/step model, sequential step execution, failure ladder, validation via shell commands, `fm ask` in park mode, CLI surface, daemon skeleton with API.

**Phase 2 — Scoping + scope guard.** Scoping skill and contract generation (terminal chat first), scope-guard and tripwire hooks, contract amendment via answers.

**Phase 3 — Web dashboard.** Task board, task detail with live output streaming and diff viewer, inbox, memory view, scoping in the browser.

**Phase 4 — Slack + memory loop.** Slack connector both directions, learning extraction, promotion suggestions, memory compaction.

**Phase 5 — Polish.** Parallel steps, per-session cost accounting, embedded terminal exploration, multi-machine ambitions — all explicitly out of v1.

## 9. Acceptance criteria (system-level)

1. From `fm task` to `done`: a real multi-step task (plan → implement → review) on a real repo completes with zero manual terminal intervention, producing a merge-ready branch that satisfies every contract criterion.
2. The relay fires on a genuinely long task and the final output shows no loss of continuity attributable to the handoff (spot-checkable via generation handoff briefs).
3. An out-of-scope edit attempt is blocked mechanically and surfaces as a `scope_change` question within seconds, answerable from Slack, with the task resuming correctly after the answer.
4. A parked task consumes no worker slot; other tasks proceed; answering respawns within one orchestrator cycle.
5. A fact taught via `fm remember` demonstrably alters a later session's behavior (it appears in injected context and the session acts on it).
6. Dashboard reflects daemon state within 1s (WebSocket push), including live output, context meters, and question arrival; Slack and dashboard answers are mutually consistent.
7. Validation evidence is stored and reviewable for every completed task; no task reaches `done` with a failing criterion.
8. Killing the daemon mid-run and restarting it resumes tasks from state files without duplicating work or orphaning tmux windows (reconciliation on boot).
9. The failure ladder never makes a third autonomous attempt; the escalation question contains the diff and the failing check.
10. All state under `~/.firstmate/` is human-readable enough to debug with `cat` and `jq`.

## 10. Open questions for the implementer to raise early

1. Scoping-in-browser mechanics: drive an interactive Claude Code session from the daemon, or implement scoping as a daemon-mediated API conversation? (Terminal-first in Phase 2 defers this.)
2. Multi-repo tasks (a change spanning two repos): one task with two worktrees, or linked tasks? V1 may declare single-repo only.
3. Cost controls: per-task token/dollar budget with a pause-and-ask when exceeded — v1 or Phase 5?
4. Auth posture for the dashboard: localhost-only is the v1 assumption; confirm no LAN exposure requirement (Slack covers remote).

## 11. Prior art and references the implementer should read

claude-squad (tmux + worktree orchestration mechanics), Anthropic's writing on orchestrator–worker multi-agent patterns, and the Claude Code documentation for headless mode and hooks — the latter two are load-bearing for this design, and the implementer must verify current flags, output formats, and hook event names at build time rather than trusting this document's snapshot of them.
