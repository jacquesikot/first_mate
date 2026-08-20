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

# When the step is driving a skill, the durable facts belong in
# .fm/skill-state.json, not in this prose — a brief written by a
# context-exhausted session is exactly the thing that told three
# successive generations to "re-verify, don't trust the prior audit" and
# cost a real task its entire audit three times over (STATUS 2026-08-20).
SKILL_HANDOFF_PROMPT = """\
STOP working on the task. You are out of context budget and are being
replaced by a fresh session (a relay).

You are part-way through the '{skill}' skill. Your replacement will be
given `.fm/skill-state.json` verbatim, so that file — not this brief — is
how durable progress travels. FIRST, bring it up to date with anything
you established that is not in it yet:

  fm skill --phase "<the phase you are in>" \\
           --phase-done "<any phase you finished>" \\
           --finding "<a verified fact worth not re-deriving>" \\
           --decided "<key>=<what was settled>" \\
           --outstanding "<something still to do>" \\
           --resolve "<an outstanding item now done, verbatim>"

Record findings your replacement would otherwise have to re-derive from
scratch (audit results, file facts, answers already given). Be specific
and self-contained: your replacement cannot see this conversation.

THEN write a short handoff brief covering only what does NOT belong in
that file, as plain text with exactly these sections:

DONE: what you did in THIS session (a few lines; the durable facts are in
the state file, do not repeat them).
REMAINING: the exact next action.
GOTCHAS: mistakes to avoid, and anything about how the skill itself must
be run that the state file does not capture.

Answer with the brief only.
"""


def request_handoff(worktree: Path, session_id: str, model: str,
                    timeout: int = 300, skill: str | None = None) -> str:
    """Ask the dying session for a handoff brief.

    With `skill`, the session is first told to flush its durable progress
    into .fm/skill-state.json — so it needs write tools, unlike the
    prose-only path.
    """
    prompt = (SKILL_HANDOFF_PROMPT.format(skill=skill) if skill
              else HANDOFF_PROMPT)
    cmd = [
        "claude", "-p", prompt,
        "--resume", session_id,
        "--output-format", "json",
        "--model", model,
    ]
    if skill:
        # Only what it takes to update the state file — no repo edits.
        cmd += ["--allowedTools", "Bash(fm skill:*),Read"]
    else:
        cmd += ["--tools", ""]
    cmd += [
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
