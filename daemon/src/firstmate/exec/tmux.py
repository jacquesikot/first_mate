"""tmux control — the only module that talks to tmux.

One tmux session ("firstmate") owns all worker windows. Workers run as
one window each; the daemon creates, kills, and captures them here.
Terminal attach is the user-facing `tmux attach` command string we hand
out — First Mate never embeds or emulates a terminal (PRD §7).
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass

SESSION = "firstmate"


class TmuxError(RuntimeError):
    pass


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["tmux", *args], capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)!s} failed: {proc.stderr.strip()}")
    return proc


@dataclass(frozen=True)
class Window:
    """A worker's tmux window, addressed by stable window ID (e.g. '@42')."""

    window_id: str
    name: str

    @property
    def target(self) -> str:
        return f"{SESSION}:{self.window_id}"


def server_running() -> bool:
    return _tmux("has-session", "-t", SESSION, check=False).returncode == 0


def ensure_session() -> None:
    """Create the firstmate session (detached) if it doesn't exist."""
    if not server_running():
        # A placeholder window keeps the session alive with no workers.
        _tmux("new-session", "-d", "-s", SESSION, "-n", "daemon")


def new_window(name: str, command: list[str], cwd: str) -> Window:
    """Spawn `command` in a new window; returns the window handle.

    The window stays open after the command exits (remain-on-exit) so the
    exit status and final output remain capturable until we kill it.
    """
    ensure_session()
    out = _tmux(
        "new-window",
        "-d",
        "-t",
        SESSION,
        "-n",
        name,
        "-c",
        cwd,
        "-P",
        "-F",
        "#{window_id}",
        shlex.join(command),
    ).stdout.strip()
    window = Window(window_id=out, name=name)
    _tmux("set-option", "-t", window.target, "remain-on-exit", "on")
    return window


def window_alive(window: Window) -> bool:
    """True while the window exists (its command may have exited)."""
    proc = _tmux(
        "list-windows", "-t", SESSION, "-F", "#{window_id}", check=False
    )
    return window.window_id in proc.stdout.split()


def pane_dead(window: Window) -> bool:
    """True when the window's command has exited (pane in dead state)."""
    proc = _tmux(
        "list-panes", "-t", window.target, "-F", "#{pane_dead}", check=False
    )
    return proc.returncode != 0 or proc.stdout.strip() == "1"


def pane_exit_status(window: Window) -> int | None:
    proc = _tmux(
        "list-panes", "-t", window.target, "-F", "#{pane_dead_status}", check=False
    )
    text = proc.stdout.strip()
    return int(text) if text.isdigit() else None


def capture(window: Window, lines: int = 2000) -> str:
    """Read-only capture of the window's pane content (for observability,
    never for inferring state — hooks are the event mechanism)."""
    return _tmux(
        "capture-pane", "-p", "-t", window.target, "-S", f"-{lines}"
    ).stdout


def kill_window(window: Window) -> None:
    _tmux("kill-window", "-t", window.target, check=False)


def send_interrupt(window: Window) -> None:
    """Send SIGINT to the window's foreground process (graceful stop)."""
    _tmux("send-keys", "-t", window.target, "C-c", check=False)


def attach_command(window: Window) -> str:
    """The command a human runs to drop into this worker's window."""
    return f"tmux attach -t {SESSION} \\; select-window -t {window.window_id}"
