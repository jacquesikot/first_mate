# First Mate daemon

Python package for the `fm` daemon (PRD §7). Currently: the four
execution-module boundaries, the headless worker spawner, and the
Phase 0 relay spike.

```
src/firstmate/
  exec/
    tmux.py      tmux control — the only code that touches tmux
    gitops.py    git operations — the only code that shells out to git
    context.py   transcript discovery + token accounting (~/.claude/projects)
    hooks.py     hook-settings generation and merge-not-overwrite writing
  spawner.py     headless `claude -p` worker spawning in tmux windows
  spike/
    relay.py     Phase 0 relay spike (go/no-go for the design)
```

## Setup

Requires [uv](https://docs.astral.sh/uv/), tmux, jq, and a logged-in
Claude Code install.

```sh
cd daemon
uv sync            # Python 3.12+, no runtime deps yet
uv run --group dev pytest
```

## Phase 0 relay spike

Deliberately overflows a task at a tiny context wall and verifies the
relay (interrupt → handoff brief → fresh generation with injected
context) completes the task correctly across ≥3 generations:

```sh
uv run fm-spike --items 12 --wall 70000 --max-gens 10
```

Artifacts land under `~/.firstmate/spike/run-<timestamp>/` — worktree,
`.fm/handoff-gen*.md` briefs, `.fm/events.jsonl` hook events,
`.fm/report.json`. Workers run in tmux session `firstmate`
(`tmux attach -t firstmate` to watch live).

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
