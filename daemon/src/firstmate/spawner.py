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
import re
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


def build_command(spec: WorkerSpec, prompt: str | None = None) -> list[str]:
    """The `claude` argv. `prompt` overrides the literal prompt text — the
    spawner passes a `$(cat …)` placeholder so a long prompt never travels
    through tmux's command line (see `spawn`)."""
    cmd = ["claude", "-p", prompt if prompt is not None else spec.prompt,
           "--output-format", "json"]
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


def prompt_file(spec: WorkerSpec) -> Path:
    return spec.resolved_output_file().with_suffix(".prompt.txt")


def spawn(spec: WorkerSpec) -> tmux.Window:
    """Launch the worker; returns immediately with its tmux window.

    The prompt goes to a FILE and the command references it as
    `"$(cat <file>)"`, expanded by the spawned `sh` — not by us. tmux caps
    the length of a command it will accept (~16KB on 3.7b), and a step
    prompt grows every time an operator answer is folded into it by
    `replan`. A real reach-plan task died at generation 6 with
    `tmux: command too long` after three grilling rounds, losing an
    approved plan (STATUS 2026-08-20). Keeping the prompt out of the
    command line makes the length of the prompt irrelevant to tmux.
    """
    out = spec.resolved_output_file()
    out.parent.mkdir(parents=True, exist_ok=True)
    err = out.with_suffix(".err.log")
    pfile = prompt_file(spec)
    pfile.write_text(spec.prompt)
    env_prefix = "".join(
        f"{k}={shlex.quote(v)} " for k, v in spec.env.items()
    )
    # shlex.join would quote the $(...) into a literal; splice it in after.
    placeholder = "\x00PROMPT\x00"
    joined = shlex.join(build_command(spec, prompt=placeholder))
    joined = joined.replace(
        shlex.quote(placeholder), f'"$(cat {shlex.quote(str(pfile))})"')
    shell_cmd = (
        f"{env_prefix}{joined} "
        f"> {shlex.quote(str(out))} 2> {shlex.quote(str(err))}"
    )
    return tmux.new_window(spec.name, ["sh", "-c", shell_cmd], cwd=str(spec.cwd))


@dataclass(frozen=True)
class WorkerResult:
    exit_status: int | None
    output: dict | None  # parsed --output-format json payload
    raw: str


# A worker that ends its turn asking the operator something has stopped
# for a reply that cannot arrive: there is no human in the session, so the
# text goes nowhere and the step is judged on work it never did. Seen for
# real on a reach-plan step, which played back its understanding and
# closed with "Let me know if I've mischaracterized anything before I dig
# in." — a clean exit as far as tmux is concerned, so the orchestrator
# validated an empty worktree and burned an attempt (STATUS 2026-08-20).
#
# Deliberately narrow: it only fires on the LAST sentence of the reply, so
# a report that merely discusses open questions is not caught.
_VOID_ASK_PATTERNS = (
    r"let me know\b",
    r"lmk\b",
    r"shall i\b",
    r"should i\b",
    r"(do|would) you (want|prefer|like)\b",
    r"please (confirm|advise|clarify|let me)\b",
    r"can you (confirm|clarify|tell me)\b",
    r"before i (dig|proceed|start|begin|continue|go)\b",
    r"sound(s)? (good|right|ok)\b",
    r"thoughts\?",
    r"is that (right|correct|what you)\b",
    r"(waiting|wait) for (your|the operator)\b",
    r"awaiting (your|confirmation|approval|a reply)\b",
    r"which (one )?(would|do) you\b",
    r"^(confirm|approve)\b",
)


def _final_sentences(text: str, count: int = 2) -> str:
    """The tail of a reply, where a check-in would sit."""
    body = (text or "").strip()
    if not body:
        return ""
    # Trailing list items and bold headers are still "the end" for our
    # purposes; split on sentence enders and newlines alike.
    parts = [p for p in re.split(r"(?<=[.!?])\s+|\n+", body) if p.strip()]
    return " ".join(parts[-count:]).strip()


def asked_the_void(text: str) -> str | None:
    """Return the matched phrase if this reply ends by asking a human who
    isn't there, else None."""
    tail = _final_sentences(text).lower()
    if not tail:
        return None
    for pat in _VOID_ASK_PATTERNS:
        m = re.search(pat, tail)
        if m:
            return m.group(0).strip()
    # A bare trailing question aimed at the operator ("...which surface do
    # you mean?") counts too, but only when it addresses them directly.
    if tail.endswith("?") and re.search(r"\byou(r)?\b", tail):
        return "a direct question to the operator"
    return None


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
