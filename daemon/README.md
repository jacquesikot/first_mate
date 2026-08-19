# First Mate daemon

Python package for the `fm` daemon (PRD §7). Phase 1 (orchestrator core)
is implemented on top of the Phase-0-proven execution layer.

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
  relay.py         handoff acquisition (resume-the-walled-session)
  orchestrator.py  the deterministic per-task loop (wall relay, park mode,
                   failure ladder, validation gates)
  server.py        FastAPI daemon: REST + WebSocket, Manager (concurrency
                   cap, boot reconciliation)
  cli.py           `fm` CLI (serve/task/run/status/attach/answer/pause/
                   abandon/remember/ask/_event)
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

## Using it (Phase 1)

Scoping conversations arrive in Phase 2; today a task starts from a
hand-written contract JSON:

```jsonc
{
  "goal": "add rate limiting to the API",
  "repo": "/abs/path/to/repo",
  "steps": [
    {"id": "implement", "prompt": "…what to do…", "criteria": ["tests"],
     "allowed_tools": ["Read", "Edit", "Write", "Bash(npm test:*)"]}
  ],
  "criteria": [
    {"id": "tests", "command": "npm test", "timeout": 600}
  ]
}
```

```sh
uv run fm serve                 # daemon on 127.0.0.1:8787
uv run fm task add contract.json --run
uv run fm status                # tasks + open questions
uv run fm attach <task-id>      # drop into the live tmux window
uv run fm answer <qid> <choice> # unblock a parked task
```

State lives under `~/.firstmate/` (`FM_HOME` overrides), all of it
`cat`/`jq`-debuggable; `firstmate.db` is only an index and is rebuilt
from the files on every daemon start.

Config: `~/.firstmate/config.json` — `port`, `max_workers` (global worker
slot cap), `worker_model`, `wall_tokens` (relay trigger), `max_generations`,
`worker_timeout_s`, `poll_seconds`.

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
