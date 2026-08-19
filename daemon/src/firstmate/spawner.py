"""Headless worker spawner.

Spawns `claude -p` in a tmux window inside a worktree, with a pinned
session ID (so context tracking targets the right transcript from the
first token), a First-Mate-owned settings file for hook wiring, an
explicit tool allowlist, and JSON output captured to a file.

Verified against Claude Code 2.1.227: --session-id <uuid> exists and
pins the session ID at spawn; --settings accepts a file path;
--permission-mode dontAsk denies anything not allowlisted instead of
stalling on a prompt (headless sessions must never wait on a human).
"""

from __future__ import annotations

import json
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .exec import tmux


@dataclass
class WorkerSpec:
    prompt: str
    cwd: Path
    name: str
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    resume: str | None = None  # session ID to resume instead of fresh spawn
    settings_file: Path | None = None
    allowed_tools: list[str] = field(default_factory=list)
    permission_mode: str = "dontAsk"
    append_system_prompt: str | None = None
    model: str | None = None
    output_file: Path | None = None  # defaults to <cwd>/.fm/<name>.json
    env: dict[str, str] = field(default_factory=dict)

    def resolved_output_file(self) -> Path:
        return self.output_file or (self.cwd / ".fm" / f"{self.name}.json")


def build_command(spec: WorkerSpec) -> list[str]:
    cmd = ["claude", "-p", spec.prompt, "--output-format", "json"]
    if spec.resume:
        cmd += ["--resume", spec.resume]
    else:
        cmd += ["--session-id", spec.session_id]
    if spec.settings_file:
        cmd += ["--settings", str(spec.settings_file)]
    if spec.allowed_tools:
        cmd += ["--allowedTools", ",".join(spec.allowed_tools)]
    if spec.permission_mode:
        cmd += ["--permission-mode", spec.permission_mode]
    if spec.append_system_prompt:
        cmd += ["--append-system-prompt", spec.append_system_prompt]
    if spec.model:
        cmd += ["--model", spec.model]
    return cmd


def spawn(spec: WorkerSpec) -> tmux.Window:
    """Launch the worker; returns immediately with its tmux window."""
    out = spec.resolved_output_file()
    out.parent.mkdir(parents=True, exist_ok=True)
    err = out.with_suffix(".err.log")
    env_prefix = "".join(
        f"{k}={shlex.quote(v)} " for k, v in spec.env.items()
    )
    shell_cmd = (
        f"{env_prefix}{shlex.join(build_command(spec))} "
        f"> {shlex.quote(str(out))} 2> {shlex.quote(str(err))}"
    )
    return tmux.new_window(spec.name, ["sh", "-c", shell_cmd], cwd=str(spec.cwd))


@dataclass(frozen=True)
class WorkerResult:
    exit_status: int | None
    output: dict | None  # parsed --output-format json payload
    raw: str


def wait(
    window: tmux.Window,
    spec: WorkerSpec,
    poll_seconds: float = 2.0,
    timeout_seconds: float = 1800.0,
    on_poll: Callable[[], bool] | None = None,
) -> WorkerResult | None:
    """Wait for the worker to exit. Returns None if `on_poll` aborted the
    wait (caller decides what to do with the still-running worker)."""
    deadline = time.monotonic() + timeout_seconds
    while not tmux.pane_dead(window):
        if time.monotonic() > deadline:
            raise TimeoutError(f"worker {spec.name} exceeded {timeout_seconds}s")
        if on_poll is not None and on_poll():
            return None
        time.sleep(poll_seconds)
    return collect(window, spec)


def collect(window: tmux.Window, spec: WorkerSpec) -> WorkerResult:
    raw = ""
    out = spec.resolved_output_file()
    if out.exists():
        raw = out.read_text()
    parsed: dict | None = None
    try:
        parsed = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError:
        parsed = None
    return WorkerResult(
        exit_status=tmux.pane_exit_status(window), output=parsed, raw=raw
    )


def interrupt_and_wait(
    window: tmux.Window, spec: WorkerSpec, grace_seconds: float = 20.0
) -> WorkerResult:
    """Stop a running worker (SIGINT via tmux), wait briefly, collect."""
    tmux.send_interrupt(window)
    deadline = time.monotonic() + grace_seconds
    while not tmux.pane_dead(window) and time.monotonic() < deadline:
        time.sleep(0.5)
    if not tmux.pane_dead(window):
        tmux.send_interrupt(window)  # second C-c if it ignored the first
        time.sleep(2.0)
    return collect(window, spec)
