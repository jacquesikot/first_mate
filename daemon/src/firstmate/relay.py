"""Context relay — handoff acquisition (PRD §6.3).

Handoff acquisition = resume-the-walled-session (decision log 2026-08-19,
spike-proven across 7 generations): after interrupting a worker, even
mid-tool-call, `claude -p --resume <sid> --tools ""` reliably produces a
DONE/REMAINING/GOTCHAS brief that reuses the dying session's full context.
This is one of the named LLM decision points (handoff summarization).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HANDOFF_PROMPT = """\
STOP working on the task. You are out of context budget and are being
replaced by a fresh session (a relay). Write a handoff brief for your
replacement, as plain text with exactly these sections:

DONE: what has been completed (be specific: files, items, decisions).
REMAINING: what is left to do, starting with the exact next action.
GOTCHAS: anything the contract requires that a fresh session might miss,
plus any mistakes to avoid.

Do not use any tools. Answer with the brief only.
"""


def request_handoff(worktree: Path, session_id: str, model: str,
                    timeout: int = 300) -> str:
    cmd = [
        "claude", "-p", HANDOFF_PROMPT,
        "--resume", session_id,
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--permission-mode", "dontAsk",
    ]
    try:
        proc = subprocess.run(
            cmd, cwd=worktree, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return f"(handoff request timed out after {timeout}s)"
    if proc.returncode != 0:
        return f"(handoff request failed: exit {proc.returncode}: {proc.stderr[-500:]})"
    try:
        return json.loads(proc.stdout).get("result", "").strip()
    except json.JSONDecodeError:
        return f"(handoff request returned non-JSON: {proc.stdout[-500:]})"
