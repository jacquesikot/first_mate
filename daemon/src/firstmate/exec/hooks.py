"""Hook management — generation and merging of hook settings.

Workers get their hook wiring through a First-Mate-owned settings file
passed to `claude --settings <file>`, so user/project settings are never
touched for spawning. When we DO have to write into an existing settings
file (e.g. installing the scope guard in a worktree's .claude/), we merge
— never overwrite (CLAUDE.md working rule / acceptance criterion).

Hook event names verified against Claude Code 2.1.227 docs (2026-08-19):
SessionStart (matchers: startup|resume|clear|compact|fork), PreToolUse,
PostToolUse, Stop, SubagentStop, Notification, PreCompact, PostCompact,
SessionEnd. Exit code 2 blocks on blockable events (PreCompact included).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

BLOCKING_EXIT_CODE = 2  # exit 2 blocks the event, stderr goes to the agent


def hook_entry(command: str, timeout: int | None = None, matcher: str | None = None) -> dict:
    """One matcher-group entry for a hook event list."""
    hook: dict[str, Any] = {"type": "command", "command": command}
    if timeout is not None:
        hook["timeout"] = timeout
    entry: dict[str, Any] = {"hooks": [hook]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def build_hooks_settings(events: dict[str, list[dict]]) -> dict:
    """Wrap {event_name: [entries]} into a settings-file dict."""
    return {"hooks": events}


def merge_settings(base: dict, extra: dict) -> dict:
    """Deep-merge `extra` into `base` without dropping anything.

    Dicts merge recursively; hook-event lists concatenate; scalar
    conflicts resolve to `extra` (the newer intent) — but existing hook
    wiring is always preserved.
    """
    out = copy.deepcopy(base)
    for key, value in extra.items():
        if key not in out:
            out[key] = copy.deepcopy(value)
        elif isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = merge_settings(out[key], value)
        elif isinstance(out[key], list) and isinstance(value, list):
            out[key] = out[key] + [v for v in value if v not in out[key]]
        else:
            out[key] = copy.deepcopy(value)
    return out


def write_settings(path: Path, settings: dict) -> None:
    """Write settings at `path`, merging with any existing file."""
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except json.JSONDecodeError:
            # Never clobber a file we can't parse; fail loudly instead.
            raise ValueError(f"refusing to merge into unparseable settings: {path}")
    merged = merge_settings(existing, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2) + "\n")
