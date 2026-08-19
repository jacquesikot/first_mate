"""Core domain model — tasks, contracts, steps, questions (PRD §5).

Plain dataclasses with dict round-tripping. The store persists these as
human-readable JSON under ~/.firstmate (acceptance criterion 10) and
mirrors a queryable index into SQLite. No ORM; the daemon is the single
writer.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

TASK_STATUSES = {
    "scoping", "ready", "running", "blocked", "paused",
    "validating", "done", "failed", "abandoned",
}
TERMINAL_TASK_STATUSES = {"done", "failed", "abandoned"}
STEP_STATUSES = {"pending", "running", "blocked", "done", "failed"}
QUESTION_TYPES = {"clarification", "scope_change", "decision", "approval", "fyi"}
URGENCIES = {"blocking", "normal"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def slugify(text: str, max_len: int = 28) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "task"


def _from(cls, d: dict | None):
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in (d or {}).items() if k in names})


# ---------------------------------------------------------------- contract


@dataclass
class Criterion:
    """One machine-checkable completion criterion. Phase 1: shell only —
    the command runs in the worktree and passes iff it exits 0."""

    id: str
    command: str
    kind: str = "shell"
    cwd: str = "."
    timeout: int = 600

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Criterion":
        return _from(cls, d)


@dataclass
class StepSpec:
    """One skill execution within a task, as declared by the contract."""

    id: str
    prompt: str
    title: str = ""
    skill: str | None = None
    model: str | None = None
    allowed_tools: list[str] = field(default_factory=list)  # empty → defaults
    criteria: list[str] = field(default_factory=list)  # criterion ids

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StepSpec":
        return _from(cls, d)


@dataclass
class Contract:
    """The scoping session's output — the single source of truth for what
    "done" means. Amendable via answered questions, never implicit."""

    goal: str
    repo: str
    steps: list[StepSpec] = field(default_factory=list)
    criteria: list[Criterion] = field(default_factory=list)
    scope_in: list[str] = field(default_factory=lambda: ["**"])
    scope_out: list[str] = field(default_factory=list)
    # Tripwire overrides for this task (e.g. {"git_push": false} after an
    # operator approval); merged over the project defaults in config.json.
    tripwires: dict = field(default_factory=dict)
    # Path globs exempted from path tripwires by operator approvals.
    tripwire_allow: list[str] = field(default_factory=list)
    context: str = ""
    amendments: list[dict] = field(default_factory=list)

    def criterion(self, cid: str) -> Criterion:
        for c in self.criteria:
            if c.id == cid:
                return c
        raise KeyError(f"unknown criterion: {cid}")

    def resolve_criteria(self, ids: list[str]) -> list[Criterion]:
        return [self.criterion(cid) for cid in ids]

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Contract":
        contract = _from(cls, d)
        contract.steps = [StepSpec.from_dict(s) for s in d.get("steps") or []]
        contract.criteria = [Criterion.from_dict(c) for c in d.get("criteria") or []]
        return contract

    def render_markdown(self) -> str:
        lines = ["# Task contract", "", f"**Goal:** {self.goal}", "", f"**Repo:** {self.repo}"]
        if self.context:
            lines += ["", "## Known context", "", self.context]
        lines += [
            "", "## Scope", "",
            f"- In: {', '.join(self.scope_in)}",
            f"- Out: {', '.join(self.scope_out) or '(none)'}",
        ]
        if self.tripwires:
            lines.append(
                "- Tripwire overrides: "
                + ", ".join(f"{k}={v}" for k, v in sorted(self.tripwires.items()))
            )
        if self.tripwire_allow:
            lines.append(f"- Tripwire-exempt paths: {', '.join(self.tripwire_allow)}")
        lines += ["", "## Steps", ""]
        for i, s in enumerate(self.steps, 1):
            crit = f" (criteria: {', '.join(s.criteria)})" if s.criteria else ""
            title = f" — {s.title}" if s.title else ""
            lines.append(f"{i}. **{s.id}**{title}{crit}")
            lines.append(f"   {s.prompt}")
        lines += ["", "## Completion criteria (machine-checkable)", ""]
        for c in self.criteria:
            lines.append(f"- `{c.id}`: `{c.command}` (cwd: {c.cwd}, timeout: {c.timeout}s)")
        if self.amendments:
            lines += ["", "## Amendments (answered questions — binding)", ""]
            for a in self.amendments:
                lines.append(
                    f"- [{a.get('at', '')}] Q: {a.get('question', '')} → A: {a.get('answer', '')}"
                )
        return "\n".join(lines) + "\n"


def validate_contract(data: dict) -> list[str]:
    """Machine-checkability gate (PRD §6.1): every criterion must carry a
    concrete command; every step must reference known criteria."""
    if not isinstance(data, dict):
        return ["contract must be a JSON object"]
    errors: list[str] = []
    if not str(data.get("goal", "")).strip():
        errors.append("goal is required")
    if not str(data.get("repo", "")).strip():
        errors.append("repo is required")
    steps = data.get("steps") or []
    if not steps:
        errors.append("at least one step is required")
    for key in ("scope_in", "scope_out", "tripwire_allow"):
        val = data.get(key)
        if val is not None and (
            not isinstance(val, list) or any(not isinstance(g, str) or not g.strip() for g in val)
        ):
            errors.append(f"{key} must be a list of glob strings")
    tripwires = data.get("tripwires")
    if tripwires is not None:
        if not isinstance(tripwires, dict):
            errors.append("tripwires must be an object")
        else:
            from .guard import KNOWN_TRIPWIRES

            for k, v in tripwires.items():
                if k not in KNOWN_TRIPWIRES:
                    errors.append(
                        f"unknown tripwire '{k}' (known: {', '.join(sorted(KNOWN_TRIPWIRES))})"
                    )
                elif not isinstance(v, (bool, int)):
                    errors.append(f"tripwire '{k}' must be a boolean or number")
    crit_ids: set[str] = set()
    for c in data.get("criteria") or []:
        cid = str(c.get("id", "")).strip()
        if not cid:
            errors.append("criterion missing id")
            continue
        if cid in crit_ids:
            errors.append(f"duplicate criterion id: {cid}")
        crit_ids.add(cid)
        if not str(c.get("command", "")).strip():
            errors.append(
                f"criterion '{cid}' has no command — every criterion must be machine-checkable"
            )
        kind = c.get("kind", "shell")
        if kind != "shell":
            errors.append(f"criterion '{cid}': kind '{kind}' not supported in Phase 1 (shell only)")
    step_ids: set[str] = set()
    for s in steps:
        sid = str(s.get("id", "")).strip()
        if not sid:
            errors.append("step missing id")
            continue
        if sid in step_ids:
            errors.append(f"duplicate step id: {sid}")
        step_ids.add(sid)
        if not str(s.get("prompt", "")).strip():
            errors.append(f"step '{sid}' has no prompt")
        for cid in s.get("criteria") or []:
            if cid not in crit_ids:
                errors.append(f"step '{sid}' references unknown criterion '{cid}'")
    return errors


# ------------------------------------------------------------ runtime state


@dataclass
class SessionRecord:
    """One worker session (a generation of a step). Disposable by design;
    the record is what survives."""

    session_id: str
    generation: int
    attempt: int
    window_id: str | None = None
    started_at: str = ""
    ended_at: str | None = None
    # exited | walled | parked | paused | abandoned | timeout | orphaned
    outcome: str | None = None
    peak_tokens: int = 0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SessionRecord":
        return _from(cls, d)


@dataclass
class StepState:
    id: str
    status: str = "pending"
    attempt: int = 1
    generation: int = 0
    last_failure: str | None = None
    sessions: list[SessionRecord] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StepState":
        st = _from(cls, d)
        st.sessions = [SessionRecord.from_dict(s) for s in d.get("sessions") or []]
        return st


@dataclass
class Task:
    id: str
    repo: str
    branch: str
    status: str = "ready"
    worktree: str = ""
    goal: str = ""
    current_step: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    steps: list[StepState] = field(default_factory=list)

    def step_state(self, sid: str) -> StepState:
        for st in self.steps:
            if st.id == sid:
                return st
        raise KeyError(f"unknown step: {sid}")

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        task = _from(cls, d)
        task.steps = [StepState.from_dict(s) for s in d.get("steps") or []]
        return task


@dataclass
class Question:
    """A structured request for the owner's input (PRD §6.5)."""

    id: str
    task_id: str
    type: str
    question: str
    step_id: str | None = None
    urgency: str = "normal"
    options: list[str] = field(default_factory=list)
    default: str | None = None
    evidence: dict = field(default_factory=dict)
    status: str = "open"  # open | answered | noted (fyi)
    answer: str | None = None
    answered_by: str | None = None
    asked_at: str = field(default_factory=now_iso)
    answered_at: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Question":
        return _from(cls, d)
