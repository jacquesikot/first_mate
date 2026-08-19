"""Context tracking — transcript discovery and token accounting.

Reads Claude Code session transcripts under ~/.claude/projects/ to derive
the current context size of a live session. The transcript format is
internal to Claude Code and can change between versions (their docs say
so explicitly), so this module is deliberately defensive: unknown lines
are skipped, and absence of usage data reads as "unknown", never a crash.

Verified against Claude Code 2.1.227 (2026-08-19): assistant entries carry
`message.usage` with input_tokens / cache_read_input_tokens /
cache_creation_input_tokens / output_tokens. Current context occupancy is
the sum of those on the *latest* assistant entry (input side counts the
whole conversation via cache fields).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

# Claude Code's default context window; make configurable when models vary.
DEFAULT_CONTEXT_LIMIT = 200_000

PROJECTS_DIR = Path.home() / ".claude" / "projects"


def project_dir(cwd: Path) -> Path:
    """Map a working directory to its transcript directory.

    Claude Code munges the absolute path by replacing every character
    that isn't [A-Za-z0-9-] with '-' (verified: /Users/x/code/first_mate
    -> -Users-x-code-first-mate).
    """
    munged = re.sub(r"[^A-Za-z0-9-]", "-", str(cwd.resolve()))
    return PROJECTS_DIR / munged


def transcript_path(cwd: Path, session_id: str) -> Path:
    return project_dir(cwd) / f"{session_id}.jsonl"


@dataclass(frozen=True)
class ContextReading:
    """A point-in-time measurement of one session's context occupancy."""

    session_id: str
    tokens: int
    limit: int
    assistant_turns: int

    @property
    def percent(self) -> float:
        return 100.0 * self.tokens / self.limit if self.limit else 0.0

    @property
    def band(self) -> str:
        # Dashboard thresholds per PRD §6.3.
        if self.percent >= 85:
            return "warning"
        if self.percent >= 60:
            return "elevated"
        return "neutral"


def _usage_of(entry: dict) -> dict | None:
    message = entry.get("message")
    if isinstance(message, dict):
        usage = message.get("usage")
        if isinstance(usage, dict):
            return usage
    return None


def read_context(
    cwd: Path, session_id: str, limit: int = DEFAULT_CONTEXT_LIMIT
) -> ContextReading | None:
    """Latest context occupancy for a session, or None if no transcript
    or no usage data yet."""
    path = transcript_path(cwd, session_id)
    if not path.exists():
        return None
    tokens: int | None = None
    turns = 0
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # possibly a partially-written last line
            if entry.get("type") != "assistant":
                continue
            usage = _usage_of(entry)
            if usage is None:
                continue
            turns += 1
            tokens = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
                + int(usage.get("output_tokens") or 0)
            )
    if tokens is None:
        return None
    return ContextReading(
        session_id=session_id, tokens=tokens, limit=limit, assistant_turns=turns
    )
