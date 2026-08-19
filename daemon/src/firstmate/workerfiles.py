"""Per-worker files — hook scripts, settings, and context injection.

Workers report up via Claude Code hooks calling `fm _event`, which POSTs
to the daemon (PRD §4; the spike's append-to-jsonl hooks are superseded).
The hook wiring lives in a First-Mate-owned settings file passed with
`--settings`, never in the user's or repo's settings files. Values (task
id, daemon url, fm binary) are baked into the scripts at generation time
so hooks don't depend on inherited environment.

Injection: SessionStart's additionalContext delivers .fm/inject.md into
the new session (spike-verified, 7/7 sessions).
"""

from __future__ import annotations

import json
import shlex
import textwrap
from pathlib import Path

from . import guard
from .exec import hooks
from .models import Contract, Question, StepSpec


def write_inject(worktree: Path, text: str) -> Path:
    fm_dir = worktree / ".fm"
    fm_dir.mkdir(parents=True, exist_ok=True)
    path = fm_dir / "inject.md"
    path.write_text(text)
    return path


def write_worker_hooks(worktree: Path, task_id: str, step_id: str,
                       daemon_url: str, fm_bin: str,
                       guard_config: dict | None = None) -> Path:
    """Write hook scripts + the settings file wiring them; returns the
    settings path (for `claude --settings`). When `guard_config` is given,
    a PreToolUse scope guard (PRD §6.4) is installed alongside the event
    hooks."""
    fm_dir = worktree / ".fm"
    hooks_dir = fm_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    fallback = fm_dir / "events-fallback.jsonl"
    inject = fm_dir / "inject.md"
    guard_json = fm_dir / "guard.json"

    def event_cmd(name: str) -> str:
        return (
            f'printf \'%s\' "$INPUT" | {shlex.quote(fm_bin)} _event {name} '
            f"--task {shlex.quote(task_id)} --step {shlex.quote(step_id)} "
            f"--url {shlex.quote(daemon_url)} "
            f"--fallback {shlex.quote(str(fallback))} || true"
        )

    scripts = {
        "session_start.sh": textwrap.dedent(
            f"""\
            #!/bin/sh
            INPUT=$(cat)
            {event_cmd("SessionStart")}
            if [ -f {shlex.quote(str(inject))} ]; then
              jq -n --rawfile ctx {shlex.quote(str(inject))} \\
                '{{hookSpecificOutput: {{hookEventName: "SessionStart", additionalContext: $ctx}}}}'
            fi
            exit 0
            """
        ),
        "pre_compact.sh": textwrap.dedent(
            f"""\
            #!/bin/sh
            INPUT=$(cat)
            {event_cmd("PreCompact")}
            echo "First Mate: compaction blocked; the orchestrator relay owns context handoff." >&2
            exit 2
            """
        ),
        "stop.sh": textwrap.dedent(
            f"""\
            #!/bin/sh
            INPUT=$(cat)
            {event_cmd("Stop")}
            exit 0
            """
        ),
    }
    if guard_config is not None:
        guard_json.write_text(json.dumps(guard_config, indent=2) + "\n")
        scripts["pre_tool_use.sh"] = textwrap.dedent(
            f"""\
            #!/bin/sh
            INPUT=$(cat)
            printf '%s' "$INPUT" | {shlex.quote(fm_bin)} _guard \\
              --config {shlex.quote(str(guard_json))} \\
              --task {shlex.quote(task_id)} --step {shlex.quote(step_id)} \\
              --url {shlex.quote(daemon_url)} \\
              --fallback {shlex.quote(str(fallback))}
            exit $?
            """
        )

    for name, body in scripts.items():
        p = hooks_dir / name
        p.write_text(body)
        p.chmod(0o755)

    events = {
        "SessionStart": [hooks.hook_entry(str(hooks_dir / "session_start.sh"), timeout=10)],
        "PreCompact": [hooks.hook_entry(str(hooks_dir / "pre_compact.sh"), timeout=10)],
        "Stop": [hooks.hook_entry(str(hooks_dir / "stop.sh"), timeout=10)],
    }
    if guard_config is not None:
        events["PreToolUse"] = [
            hooks.hook_entry(str(hooks_dir / "pre_tool_use.sh"), timeout=15,
                             matcher=guard.GUARDED_TOOLS_MATCHER)
        ]
    settings = hooks.build_hooks_settings(events)
    settings_file = fm_dir / "settings.json"
    # FM-owned file: regenerate rather than merge so stale wiring from a
    # previous step/daemon-url never lingers.
    if settings_file.exists():
        settings_file.unlink()
    hooks.write_settings(settings_file, settings)
    return settings_file


def build_inject(
    contract: Contract,
    step: StepSpec,
    generation: int,
    attempt: int,
    memory: str | None = None,
    handoff: str | None = None,
    answered: list[Question] | None = None,
    retry_note: str | None = None,
) -> str:
    parts = [
        f"# First Mate — injected context (step '{step.id}', "
        f"generation {generation}, attempt {attempt})",
        "",
        "## Contract",
        "",
        contract.render_markdown().rstrip(),
    ]
    if memory:
        parts += ["", "## Project memory (accumulated lessons — apply them)", "", memory.rstrip()]
    if answered:
        parts += ["", "## Operator answers (binding decisions)", ""]
        for q in answered:
            parts.append(f"- Q ({q.type}): {q.question}")
            parts.append(f"  A: {q.answer}")
    if handoff:
        parts += ["", "## Handoff from the previous session generation", "", handoff.rstrip()]
    if retry_note:
        parts += [
            "", "## Previous attempt failed validation", "", retry_note.rstrip(), "",
            "Diagnose and fix the failure; do not repeat the same approach blindly.",
        ]
    return "\n".join(parts) + "\n"
