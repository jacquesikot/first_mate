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
import subprocess
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


def _exclude_fm_dir(worktree: Path) -> None:
    """Keep `.fm/` out of git without touching the repo's own .gitignore.

    A worktree's private excludes live in its git dir, so this never edits
    a tracked file and never shows up in the operator's diff.
    """
    try:
        # `git status` reads info/exclude from the COMMON git dir, not the
        # per-worktree one — `git rev-parse --git-common-dir` is what points
        # at it (for a linked worktree that's the main repo's .git).
        proc = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True)
        if proc.returncode != 0 or not proc.stdout.strip():
            return
        common = Path(proc.stdout.strip())
        gitdir = common if common.is_absolute() else (worktree / common)
        if not gitdir.exists():
            return
        info = gitdir / "info"
        info.mkdir(parents=True, exist_ok=True)
        exclude = info / "exclude"
        current = exclude.read_text() if exclude.exists() else ""
        if ".fm/" not in current:
            exclude.write_text(current.rstrip("\n") + "\n.fm/\n" if current else ".fm/\n")
    except OSError:
        return  # best effort; gitops already filters .fm/ from diffs


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
    # The worker's own scratch space: always writable, never committed, so
    # drafting a plan or a report never costs the operator an approval.
    (fm_dir / guard.SCRATCH_DIR.split("/", 1)[1]).mkdir(exist_ok=True)
    _exclude_fm_dir(worktree)
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
    loop_note: str | None = None,
    skill_state: str | None = None,
    void_note: str | None = None,
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
            if q.is_round():
                for sq in q.questions:
                    parts.append(f"  - {sq.question}")
                    parts.append(f"    A: {sq.answer}")
            else:
                parts.append(f"  A: {q.answer}")
    # Skill state comes BEFORE the prose handoff: it is the durable record,
    # and the handoff is the (less reliable) narrative around it.
    if skill_state:
        parts += [
            "", "## Skill progress so far (durable state — this is authoritative)",
            "", skill_state.rstrip(), "",
            "This was recorded by earlier sessions of this same step and "
            "survives context walls. Do NOT re-verify established findings "
            "or re-ask settled decisions; pick up from the outstanding list. "
            "Keep it current as you work with `fm skill` "
            "(--phase / --phase-done / --finding / --decided / "
            "--outstanding / --resolve).",
        ]
    if handoff:
        parts += ["", "## Handoff from the previous session generation", "", handoff.rstrip()]
    if loop_note:
        parts += [
            "", "## Why you are running again (convergence loop)", "",
            loop_note.rstrip(),
        ]
    if retry_note:
        parts += [
            "", "## Previous attempt failed validation", "", retry_note.rstrip(), "",
            "Diagnose and fix the failure; do not repeat the same approach blindly.",
        ]
    # Loud, and last, because the previous session's mistake was to stop
    # working — this one must not repeat it.
    if void_note:
        parts += [
            "", "## READ THIS FIRST — your predecessor stopped for nothing",
            "", void_note.rstrip(),
        ]
    return "\n".join(parts) + "\n"
