"""Skill state — durable progress for a long skill run (PRD §6.3).

A step that drives a multi-phase skill (reach-plan: audit → grill →
draft → write back) does not fit in one session's context. The context
relay already carries a *prose* handoff, but prose written by a
context-exhausted session degrades: a real reach-plan task re-ran its
entire repo audit in three successive generations because each handoff
said "re-verify, don't trust the prior audit" (STATUS 2026-08-20).

So the durable facts live in a file instead. The worker writes
`.fm/skill-state.json` as it goes; the orchestrator injects it verbatim
into the next generation. Prose handoff still happens, but it carries the
delta, not the whole world.

The file stays flat and readable — `cat`/`jq` must be enough to see where
a skill got to (acceptance criterion 10).
"""

from __future__ import annotations

import json
from pathlib import Path

STATE_FILE = ".fm/skill-state.json"

# A skill's own progress, not the contract's. Kept small on purpose: this
# is what a fresh session needs in order NOT to redo work.
_KEYS = ("skill", "phase", "phases_done", "findings", "decided",
         "outstanding", "artifacts", "notes", "rounds_asked")


def path_for(worktree: Path) -> Path:
    return worktree / STATE_FILE


def load(worktree: Path) -> dict | None:
    """Read the state a previous generation left behind. A malformed file
    is not fatal — a skill mid-run is more useful than a hard failure, so
    the error is surfaced as state rather than raised."""
    p = path_for(worktree)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {"_unreadable": f"{type(e).__name__}: {e}"}
    if not isinstance(data, dict):
        return {"_unreadable": "skill-state.json is not a JSON object"}
    return data


def merge(worktree: Path, patch: dict) -> dict:
    """Apply a worker's update.

    Lists are *unioned*, not replaced: a session that learns one more
    audit finding must not silently drop the nine a previous generation
    recorded. Scalars overwrite (phase advances), dicts merge key-wise
    (one decision settled at a time).
    """
    state = load(worktree) or {}
    state.pop("_unreadable", None)
    for key, value in patch.items():
        if value is None:
            continue
        prior = state.get(key)
        if isinstance(prior, list) and isinstance(value, list):
            merged = list(prior)
            for item in value:
                if item not in merged:
                    merged.append(item)
            state[key] = merged
        elif isinstance(prior, dict) and isinstance(value, dict):
            state[key] = {**prior, **value}
        else:
            state[key] = value
    save(worktree, state)
    return state


def save(worktree: Path, state: dict) -> Path:
    p = path_for(worktree)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, sort_keys=False) + "\n")
    return p


def seed(worktree: Path, skill: str) -> dict:
    """Start state for a step whose contract names a skill, so the very
    first session already has somewhere to record progress."""
    existing = load(worktree)
    if existing and existing.get("skill"):
        return existing
    return merge(worktree, {"skill": skill, "phase": "starting",
                            "phases_done": [], "outstanding": [],
                            "decided": {}, "findings": []})


def render(state: dict | None) -> str:
    """The injected form: readable prose over raw JSON, because this goes
    into a worker's context where every token competes with real work."""
    if not state:
        return ""
    if state.get("_unreadable"):
        return (f"`{STATE_FILE}` could not be read ({state['_unreadable']}). "
                f"Treat this step as having no recorded skill progress, and "
                f"start a fresh state file.")
    lines: list[str] = []
    skill = state.get("skill")
    phase = state.get("phase")
    if skill:
        lines.append(f"**Skill:** `{skill}`"
                     + (f" · **current phase:** {phase}" if phase else ""))
    elif phase:
        lines.append(f"**Current phase:** {phase}")
    done = state.get("phases_done") or []
    if done:
        lines.append(f"**Phases already complete:** {', '.join(map(str, done))} "
                     f"— do NOT redo these.")
    rounds = state.get("rounds_asked")
    if rounds:
        lines.append(f"**Question rounds already asked:** {rounds}")

    findings = state.get("findings") or []
    if findings:
        lines.append("")
        lines.append("**Established findings** (verified by an earlier "
                     "generation — trust these, re-reading them is wasted "
                     "context):")
        lines += [f"- {f}" for f in findings]

    decided = state.get("decided") or {}
    if decided:
        lines.append("")
        lines.append("**Already settled** (binding; do not re-open or re-ask):")
        lines += [f"- {k}: {v}" for k, v in decided.items()]

    outstanding = state.get("outstanding") or []
    if outstanding:
        lines.append("")
        lines.append("**Still outstanding** (this is your work queue):")
        lines += [f"- {o}" for o in outstanding]

    artifacts = state.get("artifacts") or []
    if artifacts:
        lines.append("")
        lines.append("**Artifacts written so far:** "
                     + ", ".join(f"`{a}`" for a in artifacts))

    notes = state.get("notes")
    if notes:
        lines.append("")
        lines.append(f"**Notes:** {notes}")

    extra = {k: v for k, v in state.items()
             if k not in _KEYS and not k.startswith("_")}
    if extra:
        lines.append("")
        lines.append("**Other recorded state:** "
                     + json.dumps(extra, sort_keys=True))
    return "\n".join(lines)
