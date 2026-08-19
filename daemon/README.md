# First Mate daemon

Python package for the `fm` daemon (PRD §7). Phases 1 (orchestrator core),
2 (scoping + scope guard), and 3 (web dashboard serving + live streaming)
are implemented on top of the Phase-0-proven execution layer.

The daemon serves the built dashboard SPA at `/ui` (see `../dashboard/` —
build it with `pnpm install && pnpm build` there; override the dist
location with `FM_DASHBOARD_DIST`). `fm serve --open` opens it. Live pane
output + context meters are pushed over `/ws` as `{"kind":"live",...}`
frames by a capture loop that only runs while a browser is connected.

```
src/firstmate/
  exec/
    tmux.py        tmux control — the only code that touches tmux
    gitops.py      git operations — the only code that shells out to git
    context.py     transcript discovery + token accounting (~/.claude/projects)
    hooks.py       hook-settings generation and merge-not-overwrite writing
  spawner.py       headless `claude -p` worker spawning in tmux windows
  models.py        tasks, contracts, steps, questions (dataclasses)
  store.py         state store: JSON/markdown files (source of truth) +
                   SQLite index (rebuildable) under ~/.firstmate
  validation.py    shell-command criteria + evidence capture
  workerfiles.py   per-worker hook scripts (`fm _event` → daemon) + injection
  guard.py         scope guard: glob scope checks, bash write/tripwire
                   heuristics, guard.json compilation (stdlib-only)
  scoping.py       interactive scoping conversation (fm task "<goal>")
  relay.py         handoff acquisition (resume-the-walled-session)
  orchestrator.py  the deterministic per-task loop (wall relay, park mode,
                   failure ladder, validation gates, diff tripwires)
  server.py        FastAPI daemon: REST + WebSocket, Manager (concurrency
                   cap, boot reconciliation)
  cli.py           `fm` CLI (serve/task/contract/run/status/attach/answer/
                   pause/abandon/remember/ask/_event/_guard)
  smoke.py         Phase 1 end-to-end smoke test (real workers)
  spike/relay.py   Phase 0 relay spike (kept as reference)
```

## Setup

Requires [uv](https://docs.astral.sh/uv/), tmux, jq, and a logged-in
Claude Code install.

```sh
cd daemon
uv sync
uv run --group dev pytest      # hermetic — no workers spawned
```

## Using it

```sh
uv run fm serve                 # daemon on 127.0.0.1:8787
cd /path/to/your/repo
uv run fm task "add rate limiting to the API"   # interactive scoping chat
uv run fm run <task-id>         # start the approved task
uv run fm status                # tasks + open questions
uv run fm attach <task-id>      # drop into the live tmux window
uv run fm answer <qid> <choice> # unblock a parked task
```

`fm task "<goal>"` opens an interactive Claude session primed with the
scoping prompt: it reads project memory + the repo, proposes scope/steps/
criteria, refuses vague criteria, self-checks with `fm contract check`,
and writes contract JSON that is submitted to the daemon when the session
ends. Hand-written contracts still work via `fm task add <contract.json>`:

```jsonc
{
  "goal": "add rate limiting to the API",
  "repo": "/abs/path/to/repo",
  "scope_in": ["src/api/**", "tests/api/**"],   // globs; default ["**"]
  "scope_out": [],
  "tripwires": {},               // per-task overrides, e.g. {"git_push": false}
  "steps": [
    {"id": "implement", "prompt": "…what to do…", "criteria": ["tests"],
     "allowed_tools": ["Read", "Edit", "Write", "Bash(npm test:*)"]}
  ],
  "criteria": [
    {"id": "tests", "command": "npm test", "timeout": 600}
  ]
}
```

### Scope guard + tripwires (Phase 2)

Every worker gets a PreToolUse hook (`fm _guard`) compiled from the
contract into `<worktree>/.fm/guard.json`. It mechanically blocks (exit 2,
in-band message): Edit/Write outside `scope_in`/inside `scope_out`, writes
outside the worktree, writes to `.fm/`, and tripwires — dependency-manifest
edits and installs (`npm install <pkg>`, `uv add`, …), migrations,
`git push`. Diff-shaped tripwires (`max_diff_lines`, `max_deleted_lines`)
are checked by the orchestrator at step boundaries. The block message tells
the worker exactly how to raise a `scope_change`/`approval` question; an
operator answer starting with "allow" mechanically widens `scope_in` /
`tripwire_allow` (path evidence) or disables that tripwire (non-path
evidence) via a contract amendment — the next generation's guard.json
picks it up.

State lives under `~/.firstmate/` (`FM_HOME` overrides), all of it
`cat`/`jq`-debuggable; `firstmate.db` is only an index and is rebuilt
from the files on every daemon start.

Config: `~/.firstmate/config.json` — `port`, `max_workers` (global worker
slot cap), `worker_model`, `scoping_model`, `wall_tokens` (relay trigger),
`max_generations`, `worker_timeout_s`, `poll_seconds`, `tripwires`
(project-wide defaults).

## End-to-end smoke test

Drives a real two-step task through worker execution, validation,
`fm ask` park mode, answer → resume, and task-boundary validation
(costs a few worker invocations):

```sh
uv run fm-smoke               # artifacts under ~/.firstmate/smoke/run-<ts>/
```

## Phase 0 relay spike (kept as reference)

```sh
uv run fm-spike --items 12 --wall 70000 --max-gens 10
```

Facts verified against Claude Code 2.1.227 (2026-08-19), superseding the
PRD's snapshot (PRD §11):

- `--session-id <uuid>` pins a session ID at spawn (docs omit it; the CLI has it).
- `--output-format json` returns `result`, `session_id`, `usage`,
  `total_cost_usd`, `num_turns`, `permission_denials`, `stop_reason`, ….
- SessionStart hook `hookSpecificOutput.additionalContext` **does** inject
  context into headless sessions (canary-verified).
- PreCompact hook exit code 2 blocks auto-compaction.
- Transcripts: `~/.claude/projects/<munged-cwd>/<session-id>.jsonl`;
  assistant entries carry `message.usage` token fields — current context
  occupancy = input + cache_read + cache_creation + output of the latest
  assistant entry.
- Headless permission posture for workers: `--permission-mode dontAsk`
  plus an explicit `--allowedTools` list (denies instead of stalling).
- Interrupting a worker mid-tool-call and `--resume`-ing it for a handoff
  brief works reliably (confirmed across 7 spike generations).
