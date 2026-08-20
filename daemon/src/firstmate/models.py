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
    "waiting", "validating", "done", "failed", "abandoned",
}
TERMINAL_TASK_STATUSES = {"done", "failed", "abandoned"}
STEP_STATUSES = {"pending", "waiting", "running", "blocked", "done", "failed"}
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
class Gate:
    """A precondition a step waits on before a worker is ever spawned
    (PRD §6.2, waiting primitive).

    The daemon runs `command` in the worktree on an interval; the step
    stays parked — holding no worker slot and burning no tokens — until it
    exits 0. This is how First Mate sits and waits on something slow and
    external (a CI run, an AI reviewer, a deploy) without a live session
    spending context on sleep loops.

    `ceiling` bounds the total wait; hitting it escalates to the operator
    rather than silently proceeding.
    """

    command: str
    kind: str = "shell"
    cwd: str = "."
    interval: int = 60        # seconds between probes
    ceiling: int = 3600       # give up (and escalate) after this long
    timeout: int = 120        # per-probe timeout
    description: str = ""     # human-readable "what we're waiting for"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Gate":
        return _from(cls, d)


@dataclass
class LoopBack:
    """A loop edge: what to do when this step's criteria fail (PRD §6.2).

    Convergence loops (fix → push → await review → fix again) are the
    normal shape of review-driven work. Declaring the edge in the contract
    lets the orchestrator iterate deterministically instead of escalating
    an unsatisfiable criterion to the operator over and over.

    Bounded two ways: `max_iterations` caps the rounds, and the
    orchestrator additionally stops when a round makes no progress (same
    failing evidence as last time), so a loop can never spin forever.
    """

    goto: str                 # step id to rewind to
    max_iterations: int = 5

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LoopBack":
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
    # Wait for this before spawning a worker (no slot, no tokens).
    when: Gate | None = None
    # Where to rewind when this step's criteria fail.
    on_failure: LoopBack | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StepSpec":
        step = _from(cls, d)
        if isinstance(step.when, dict):
            step.when = Gate.from_dict(step.when)
        if isinstance(step.on_failure, dict):
            step.on_failure = LoopBack.from_dict(step.on_failure)
        return step


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
    # Criteria the operator explicitly waived mid-run ("accept and
    # continue"). Kept as data rather than deleted, so the contract still
    # records what "done" was meant to mean and the waiver is auditable.
    waived_criteria: list[str] = field(default_factory=list)

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
            if s.when:
                what = s.when.description or s.when.command
                lines.append(
                    f"   _Gate: First Mate waits for {what} before this step "
                    f"runs — the wait already happened, so do NOT poll or "
                    f"sleep for it yourself._"
                )
            if s.on_failure:
                lines.append(
                    f"   _On criteria failure: First Mate loops back to step "
                    f"'{s.on_failure.goto}' (up to "
                    f"{s.on_failure.max_iterations} rounds) — new findings "
                    f"are expected and handled by that loop, not by you._"
                )
            lines.append(f"   {s.prompt}")
        lines += ["", "## Completion criteria (machine-checkable)", ""]
        for c in self.criteria:
            if c.id in self.waived_criteria:
                lines.append(
                    f"- `{c.id}`: WAIVED by the operator mid-run "
                    f"(was: `{c.command}`)")
                continue
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
        gate = s.get("when")
        if gate is not None:
            if not isinstance(gate, dict):
                errors.append(f"step '{sid}': 'when' must be an object")
            else:
                if not str(gate.get("command", "")).strip():
                    errors.append(
                        f"step '{sid}': 'when' needs a command — a gate must be "
                        f"machine-checkable"
                    )
                if gate.get("kind", "shell") != "shell":
                    errors.append(
                        f"step '{sid}': gate kind "
                        f"'{gate.get('kind')}' not supported (shell only)"
                    )
                for key in ("interval", "ceiling", "timeout"):
                    val = gate.get(key)
                    if val is not None and (not isinstance(val, (int, float))
                                            or isinstance(val, bool) or val <= 0):
                        errors.append(
                            f"step '{sid}': gate '{key}' must be a positive number")
    # Loop edges are validated after every step id is known (forward and
    # backward gotos are both legal).
    for s in steps:
        sid = str(s.get("id", "")).strip()
        lb = s.get("on_failure")
        if lb is None:
            continue
        if not isinstance(lb, dict):
            errors.append(f"step '{sid}': 'on_failure' must be an object")
            continue
        goto = str(lb.get("goto", "")).strip()
        if not goto:
            errors.append(f"step '{sid}': 'on_failure' needs a 'goto' step id")
        elif goto not in step_ids:
            errors.append(
                f"step '{sid}': on_failure.goto references unknown step '{goto}'")
        if not s.get("criteria"):
            errors.append(
                f"step '{sid}': on_failure needs at least one criterion to "
                f"fail on — otherwise the loop can never trigger"
            )
        mi = lb.get("max_iterations")
        if mi is not None and (not isinstance(mi, int) or isinstance(mi, bool)
                               or mi < 1):
            errors.append(
                f"step '{sid}': on_failure.max_iterations must be an integer >= 1")
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
class GateState:
    """Persisted progress of a step's `when` gate, so a wait survives a
    daemon restart instead of starting its ceiling over."""

    first_probe_at: str = ""
    last_probe_at: str = ""
    probes: int = 0
    last_exit: int | None = None
    last_output: str = ""
    # Supervisor bookkeeping: how many times it has looked at this stalled
    # gate, and what it concluded each time (kept so the ceiling escalation
    # can tell the operator what was already checked).
    supervisions: int = 0
    repairs: int = 0
    diagnoses: list[dict] = field(default_factory=list)
    # Probe count at the last supervision, so the trigger spaces attempts
    # out instead of firing every probe once the threshold is crossed.
    supervised_at_probe: int = 0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GateState":
        return _from(cls, d)


@dataclass
class StepState:
    id: str
    status: str = "pending"
    attempt: int = 1
    generation: int = 0
    last_failure: str | None = None
    sessions: list[SessionRecord] = field(default_factory=list)
    # Convergence-loop bookkeeping: how many times this step's on_failure
    # edge has fired, and the failure signature of the last round (used to
    # detect a loop that is making no progress).
    iteration: int = 0
    last_failure_signature: str = ""
    gate: GateState | None = None
    # How many times the supervisor has judged whether this step's criteria
    # are satisfiable at all (bounded — it is an LLM call per look).
    criteria_diagnoses: int = 0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StepState":
        st = _from(cls, d)
        st.sessions = [SessionRecord.from_dict(s) for s in d.get("sessions") or []]
        if isinstance(st.gate, dict):
            st.gate = GateState.from_dict(st.gate)
        return st


@dataclass
class Task:
    id: str
    repo: str
    branch: str
    status: str = "ready"
    worktree: str = ""
    goal: str = ""
    # The starting point this task branched from, chosen by the operator when
    # the task was created and pinned to a SHA so a later run can't silently
    # start somewhere else.
    base: str = ""
    base_sha: str = ""
    current_step: str | None = None
    # Set while status == "scoping": the conversation that will produce
    # this task's contract. The task exists from the first keystroke so
    # scoping happens inside a session, not in a New-task limbo.
    scoping_chat_id: str | None = None
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
    # Identity of the *situation* being asked about, so an equivalent
    # question later in the same task can reuse this answer instead of
    # interrupting the operator again (PRD §6.5, question fingerprinting).
    fingerprint: str = ""
    # Set when this question was auto-answered from an earlier equivalent
    # one: the id it inherited its answer from.
    answered_from: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Question":
        return _from(cls, d)


def question_fingerprint(qtype: str, evidence: dict | None,
                         question: str = "") -> str:
    """Identify the situation a question is about.

    Guard-raised questions are keyed on what the guard actually objected to
    (tripwire + paths) rather than the agent's prose, because the same block
    hit by three successive worker generations produces three differently
    worded questions about one identical decision — which is exactly the
    repetition that makes First Mate feel like it isn't listening.
    """
    import hashlib

    ev = evidence or {}
    tripwire = str(ev.get("tripwire") or "")
    paths = sorted(str(p) for p in (ev.get("paths") or []))
    if tripwire or paths:
        key = f"{qtype}|{tripwire}|{'|'.join(paths)}"
    else:
        # No structured evidence: fall back to the normalized prose, which
        # still catches a verbatim re-ask (e.g. the escalation ladder's own
        # "failed validation twice" question).
        key = f"{qtype}|" + " ".join(question.lower().split())
    return hashlib.sha256(key.encode()).hexdigest()[:16]
