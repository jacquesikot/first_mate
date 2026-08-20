"""State store — SQLite + plain human-readable files under ~/.firstmate.

The JSON/markdown files are the source of truth (debuggable with cat and
jq — acceptance criterion 10); SQLite is a rebuildable index for queries
and is reindexed from the files on every Store construction, so deleting
firstmate.db or hand-editing a task file is always safe. The daemon is
the single writer; CLI fallbacks only read.

Layout:
  ~/.firstmate/                     (override with FM_HOME for tests/spikes)
    config.json
    daemon.json                     written by `fm serve` (url, pid)
    firstmate.db                    index; safe to delete
    memory/<project>.md             durable per-project memory
    memory/archive/<project>-<ts>.md  pre-compaction snapshots (never pruned)
    memory/suggestions/<sid>.json   pending "promote to memory?" suggestions
    tasks/<task-id>/
      task.json                     lifecycle + per-step runtime state
      contract.json / contract.md
      events.jsonl                  append-only task event log
      questions/<qid>.json
      steps/<step-id>/              inject/handoff per generation, validation evidence
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Contract, Question, StepState, Task, now_iso, slugify

DEFAULT_CONFIG = {
    "port": 8787,
    "max_workers": 3,
    "worker_model": "sonnet",
    "handoff_model": None,  # null → the step's worker model
    "scoping_model": None,  # null → the user's default claude model
    "replan_model": None,
    "supervisor_model": None,  # null → handoff_model, else the worker model
    # A stalled gate gets investigated by the supervisor rather than simply
    # burning its ceiling and interrupting the operator.
    "supervise_gates": True,
    "supervise_after_s": 300,
    "max_gate_supervisions": 3,
    # A step that has failed its criteria twice is judged once: can more
    # work fix this, or is the check asserting something unreachable?
    "supervise_criteria": True,
    "max_criteria_supervisions": 2,
    # Housekeeping (`fm clean --maintenance`). Worktree removal is never
    # automatic; these only touch regenerable or archivable things.
    "clean_deps_after_days": 3,
    "archive_tasks_after_days": 14,
    # Memory loop (PRD §6.6). Extraction only fires on a step that
    # struggled, so this is a cap on the *interesting* steps, not on all
    # of them. null model → the supervisor/handoff model, else the worker's.
    "learn_from_steps": True,
    "learning_model": None,
    "max_learnings_per_task": 5,
    # Compaction is offered (never automatic) once a memory file passes
    # this size — a file big enough to crowd the context it feeds.
    "memory_compact_bytes": 8000,
    "wall_tokens": 150_000,
    "max_generations": 8,
    "worker_timeout_s": 3600,
    "poll_seconds": 2.0,
    # Scope-guard tripwires (PRD §6.4): project-wide defaults, overridable
    # per task in the contract's "tripwires". False/0 disables one.
    "tripwires": {
        "dependency_manifests": True,
        "migrations": True,
        "git_push": True,
        "max_diff_lines": 3000,
        "max_deleted_lines": 500,
    },
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, status TEXT, repo TEXT, branch TEXT, goal TEXT,
    current_step TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY, task_id TEXT, step_id TEXT, type TEXT,
    urgency TEXT, status TEXT, asked_at TEXT, answered_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, task_id TEXT,
    step_id TEXT, event TEXT, json TEXT
);
"""


def fm_home() -> Path:
    return Path(os.environ.get("FM_HOME", str(Path.home() / ".firstmate")))


class Store:
    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else fm_home()
        (self.home / "tasks").mkdir(parents=True, exist_ok=True)
        (self.home / "memory").mkdir(parents=True, exist_ok=True)
        (self.home / "memory" / "archive").mkdir(parents=True, exist_ok=True)
        (self.home / "memory" / "suggestions").mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: FastAPI's test client and to_thread
        # helpers may touch the index from another thread; writes still
        # funnel through the single daemon process.
        self.db = sqlite3.connect(str(self.home / "firstmate.db"), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_SCHEMA)
        self.reindex()

    # ------------------------------------------------------------- paths

    def task_dir(self, task_id: str) -> Path:
        return self.home / "tasks" / task_id

    def step_dir(self, task_id: str, step_id: str) -> Path:
        return self.task_dir(task_id) / "steps" / step_id

    # ------------------------------------------------------------- config

    def config(self) -> dict:
        """Effective config: defaults, overridden by config.json.

        Keys added by a newer version are backfilled into the file so the
        operator can actually see and tune them — an option that only
        exists in Python is an option nobody knows they have. Existing
        values are never touched.
        """
        path = self.home / "config.json"
        cfg = dict(DEFAULT_CONFIG)
        if path.exists():
            stored = json.loads(path.read_text())
            cfg.update(stored)
            missing = [k for k in DEFAULT_CONFIG if k not in stored]
            if missing:
                # Preserve the operator's key order, append what's new.
                merged = dict(stored)
                for k in missing:
                    merged[k] = DEFAULT_CONFIG[k]
                try:
                    path.write_text(json.dumps(merged, indent=2) + "\n")
                except OSError:
                    pass  # read-only home: the effective config still holds
        else:
            path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
        return cfg

    # -------------------------------------------------------------- tasks

    def create_task(self, contract: Contract, status: str = "ready",
                    scoping_chat_id: str | None = None) -> Task:
        task_id = f"{slugify(contract.goal)}-{uuid.uuid4().hex[:4]}"
        task = Task(
            id=task_id,
            repo=contract.repo,
            branch=f"fm/{task_id}",
            status=status,
            goal=contract.goal,
            scoping_chat_id=scoping_chat_id,
            steps=[StepState(id=s.id) for s in contract.steps],
        )
        self.save_contract(task_id, contract)
        self.save_task(task)
        self.append_event(task_id, "task_created", data={"status": status})
        return task

    def adopt_contract(self, task: Task, contract: Contract) -> Task:
        """Replace a scoping task's placeholder contract with the real one
        (scoping approval). Step states are created fresh — a task in
        `scoping` has never run anything."""
        contract.repo = str(Path(contract.repo).expanduser().resolve())
        task.repo = contract.repo
        task.goal = contract.goal
        task.steps = [StepState(id=s.id) for s in contract.steps]
        task.current_step = None
        task.status = "ready"
        task.scoping_chat_id = None
        self.save_contract(task.id, contract)
        self.save_task(task)
        return task

    def save_task(self, task: Task) -> None:
        task.updated_at = now_iso()
        d = self.task_dir(task.id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "task.json").write_text(json.dumps(task.to_dict(), indent=2) + "\n")
        self.db.execute(
            "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?)",
            (task.id, task.status, task.repo, task.branch, task.goal,
             task.current_step, task.created_at, task.updated_at),
        )
        self.db.commit()

    def load_task(self, task_id: str) -> Task | None:
        path = self.task_dir(task_id) / "task.json"
        if not path.exists():
            return None
        return Task.from_dict(json.loads(path.read_text()))

    def list_tasks(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, status, repo, branch, goal, current_step, created_at, updated_at "
            "FROM tasks ORDER BY created_at"
        ).fetchall()
        keys = ["id", "status", "repo", "branch", "goal", "current_step", "created_at", "updated_at"]
        return [dict(zip(keys, r)) for r in rows]

    # ----------------------------------------------------------- contract

    def save_contract(self, task_id: str, contract: Contract) -> None:
        d = self.task_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "contract.json").write_text(json.dumps(contract.to_dict(), indent=2) + "\n")
        (d / "contract.md").write_text(contract.render_markdown())

    def load_contract(self, task_id: str) -> Contract | None:
        path = self.task_dir(task_id) / "contract.json"
        if not path.exists():
            return None
        return Contract.from_dict(json.loads(path.read_text()))

    # ------------------------------------------------------------- events

    def append_event(self, task_id: str, event: str, step_id: str | None = None,
                     data: dict | None = None) -> dict:
        evt = {"ts": now_iso(), "event": event, "task_id": task_id, "step_id": step_id}
        if data:
            evt["data"] = data
        d = self.task_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        with (d / "events.jsonl").open("a") as f:
            f.write(json.dumps(evt) + "\n")
        self.db.execute(
            "INSERT INTO events (ts, task_id, step_id, event, json) VALUES (?,?,?,?,?)",
            (evt["ts"], task_id, step_id, event, json.dumps(evt)),
        )
        self.db.commit()
        return evt

    def events_tail(self, task_id: str, n: int = 50) -> list[dict]:
        path = self.task_dir(task_id) / "events.jsonl"
        if not path.exists():
            return []
        lines = path.read_text().splitlines()
        return [json.loads(l) for l in lines[-n:] if l.strip()]

    # ---------------------------------------------------------- questions

    def save_question(self, q: Question) -> None:
        d = self.task_dir(q.task_id) / "questions"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{q.id}.json").write_text(json.dumps(q.to_dict(), indent=2) + "\n")
        self.db.execute(
            "INSERT OR REPLACE INTO questions VALUES (?,?,?,?,?,?,?,?)",
            (q.id, q.task_id, q.step_id, q.type, q.urgency, q.status,
             q.asked_at, q.answered_at),
        )
        self.db.commit()

    def load_question(self, qid: str) -> Question | None:
        row = self.db.execute("SELECT task_id FROM questions WHERE id=?", (qid,)).fetchone()
        candidates = [row[0]] if row else [t["id"] for t in self.list_tasks()]
        for task_id in candidates:
            path = self.task_dir(task_id) / "questions" / f"{qid}.json"
            if path.exists():
                return Question.from_dict(json.loads(path.read_text()))
        return None

    def list_questions(self, task_id: str | None = None,
                       status: str | None = None) -> list[Question]:
        sql, args = "SELECT id, task_id FROM questions", []
        clauses = []
        if task_id:
            clauses.append("task_id=?")
            args.append(task_id)
        if status:
            clauses.append("status=?")
            args.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY asked_at"
        out = []
        for qid, tid in self.db.execute(sql, args).fetchall():
            path = self.task_dir(tid) / "questions" / f"{qid}.json"
            if path.exists():
                out.append(Question.from_dict(json.loads(path.read_text())))
        return out

    def answer_question(self, qid: str, answer: str, by: str) -> Question:
        q = self.load_question(qid)
        if q is None:
            raise KeyError(f"unknown question: {qid}")
        if q.status == "answered":
            raise ValueError(f"question {qid} already answered: {q.answer!r}")
        q.status = "answered"
        q.answer = answer
        q.answered_by = by
        q.answered_at = now_iso()
        self.save_question(q)
        # Answers amend the contract (PRD §6.5) — never implicit scope.
        contract = self.load_contract(q.task_id)
        if contract is not None and q.type != "fyi":
            contract.amendments.append(
                {"at": q.answered_at, "question_id": q.id,
                 "question": q.question, "answer": answer, "by": by}
            )
            self._apply_scope_widening(contract, q, answer)
            self.save_contract(q.task_id, contract)
        return q

    @staticmethod
    def _apply_scope_widening(contract: Contract, q: Question, answer: str) -> None:
        """Mechanical amendment semantics for guard-raised questions.

        An 'allow' answer widens exactly what the evidence names — the guard
        picks the change up on the next generation's guard.json (PRD §6.4).

        Anything else is a refusal, and a refusal has to *change something*
        too. The step prompt is what sent the worker at the blocked action;
        if the answer only lands in the amendment log, the next generation
        reads the same instruction, hits the same block, and asks again. So a
        non-allow answer is appended to the step's prompt as a binding
        correction (decision log 2026-08-20).
        """
        if q.type not in ("scope_change", "approval"):
            return
        if not answer.strip().lower().startswith(("allow", "yes")):
            Store._apply_refusal(contract, q, answer)
            return
        paths = [str(p) for p in (q.evidence or {}).get("paths") or [] if str(p).strip()]
        tripwire = (q.evidence or {}).get("tripwire")
        for p in paths:
            if p not in contract.scope_in:
                contract.scope_in.append(p)
            if tripwire and p not in contract.tripwire_allow:
                contract.tripwire_allow.append(p)
        if tripwire and not paths:
            # Non-path tripwire (git_push, diff thresholds): approval
            # disables it for the rest of this task.
            contract.tripwires[str(tripwire)] = False

    @staticmethod
    def _apply_refusal(contract: Contract, q: Question, answer: str) -> None:
        """Fold a refused/redirected request into the step's own prompt, so
        the instruction that caused the block no longer stands."""
        step = next((sp for sp in contract.steps if sp.id == q.step_id), None)
        if step is None:
            return
        paths = [str(p) for p in (q.evidence or {}).get("paths") or [] if str(p).strip()]
        what = f" ({', '.join(paths)})" if paths else ""
        note = (
            f"\n\nOPERATOR CORRECTION (binding, supersedes anything above): "
            f"you asked — \"{q.question.strip()}\"{what} — and the answer was: "
            f"\"{answer.strip()}\". Do not attempt that action again, and do "
            f"not re-ask: follow this instruction instead and complete the "
            f"step without it."
        )
        if note.strip() not in step.prompt:
            step.prompt = step.prompt.rstrip() + note

    # ------------------------------------------------- step artifacts

    def save_task_artifact(self, task_id: str, filename: str, text: str) -> Path:
        """A task-level artifact (a contract diff, a re-plan record). Lives
        beside task.json so `cat`/`jq` still tell the whole story."""
        d = self.task_dir(task_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / filename
        path.write_text(text if text.endswith("\n") else text + "\n")
        return path

    def save_step_artifact(self, task_id: str, step_id: str, filename: str, text: str) -> Path:
        d = self.step_dir(task_id, step_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / filename
        path.write_text(text if text.endswith("\n") else text + "\n")
        return path

    def latest_handoff(self, task_id: str, step_id: str) -> tuple[int, str] | None:
        """Highest-generation handoff brief for a step, or None."""
        d = self.step_dir(task_id, step_id)
        best: tuple[int, Path] | None = None
        if d.exists():
            for p in d.glob("handoff-gen*.md"):
                m = re.match(r"handoff-gen(\d+)\.md$", p.name)
                if m:
                    gen = int(m.group(1))
                    if best is None or gen > best[0]:
                        best = (gen, p)
        if best is None:
            return None
        return best[0], best[1].read_text()

    def save_validation(self, task_id: str, step_id: str | None, attempt: int,
                        results: list) -> Path:
        """Persist criterion results (evidence) as jq-able JSON."""
        payload = {"at": now_iso(), "attempt": attempt,
                   "results": [r.to_dict() for r in results]}
        if step_id is None:
            d = self.task_dir(task_id)
            path = d / "validation.json"
        else:
            d = self.step_dir(task_id, step_id)
            path = d / f"validation-attempt{attempt}.json"
        d.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n")
        return path

    # ------------------------------------------------------------- memory

    def list_memory(self) -> list[dict]:
        """All per-project memory files with light metadata."""
        d = self.home / "memory"
        out = []
        for p in sorted(d.glob("*.md")):
            stat = p.stat()
            text = p.read_text()
            entries = sum(1 for l in text.splitlines() if l.startswith("- "))
            out.append({
                "project": p.stem,
                "bytes": stat.st_size,
                "entries": entries,
                "updated_at": datetime.fromtimestamp(
                    stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
            })
        return out

    def write_memory(self, project: str, text: str) -> Path:
        """Full-file replace — memory is the owner's data, editable from
        the dashboard (PRD §6.6)."""
        path = self.home / "memory" / f"{project}.md"
        path.write_text(text if text.endswith("\n") else text + "\n")
        return path

    def memory_for_project(self, project: str) -> str | None:
        path = self.home / "memory" / f"{project}.md"
        return path.read_text() if path.exists() else None

    def remember(self, project: str, fact: str) -> Path:
        path = self.home / "memory" / f"{project}.md"
        if not path.exists():
            path.write_text(f"# Project memory: {project}\n\n")
        with path.open("a") as f:
            f.write(f"- {now_iso()} — {fact}\n")
        return path

    def archive_memory(self, project: str) -> Path | None:
        """Snapshot the current memory file before anything rewrites it.

        Compaction is a lossy LLM pass over the operator's own notes, so
        the pre-compaction text is kept verbatim and forever — an archive
        that gets pruned is not an archive.
        """
        path = self.home / "memory" / f"{project}.md"
        if not path.exists():
            return None
        from .learning import archive_name

        dest = self.home / "memory" / "archive" / archive_name(project)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(path.read_text())
        return dest

    def list_memory_archive(self, project: str | None = None) -> list[dict]:
        d = self.home / "memory" / "archive"
        out = []
        for p in sorted(d.glob("*.md"), reverse=True):
            # <project>-<stamp>.md — the stamp never contains a hyphen.
            proj = p.stem.rsplit("-", 1)[0]
            if project and proj != project:
                continue
            out.append({"project": proj, "file": p.name,
                        "bytes": p.stat().st_size,
                        "path": str(p)})
        return out

    # ------------------------------------------- promotion suggestions

    def save_suggestion(self, sug: dict) -> Path:
        """A pending "promote to project memory?" suggestion (PRD §6.6).

        A separate file per suggestion, keyed by the question fingerprint
        so the same recurring decision can only ever be suggested once —
        including across daemon restarts, which is the whole point of it
        being on disk rather than in memory.
        """
        d = self.home / "memory" / "suggestions"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{sug['id']}.json"
        path.write_text(json.dumps(sug, indent=2) + "\n")
        return path

    def load_suggestion(self, sid: str) -> dict | None:
        path = self.home / "memory" / "suggestions" / f"{sid}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None

    def list_suggestions(self, status: str | None = None,
                         project: str | None = None) -> list[dict]:
        d = self.home / "memory" / "suggestions"
        out = []
        for p in sorted(d.glob("*.json")):
            try:
                sug = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            if status and sug.get("status") != status:
                continue
            if project and sug.get("project") != project:
                continue
            out.append(sug)
        out.sort(key=lambda s: s.get("created_at") or "")
        return out

    def suggestion_for_fingerprint(self, fingerprint: str) -> dict | None:
        """Any suggestion — pending, accepted or dismissed — for this
        situation. A dismissed suggestion must never come back: the
        operator saying "no, don't remember that" is itself a decision."""
        if not fingerprint:
            return None
        for sug in self.list_suggestions():
            if sug.get("fingerprint") == fingerprint:
                return sug
        return None

    def resolve_suggestion(self, sid: str, status: str,
                           by: str = "operator") -> dict | None:
        sug = self.load_suggestion(sid)
        if sug is None:
            return None
        sug["status"] = status
        sug["resolved_at"] = now_iso()
        sug["resolved_by"] = by
        self.save_suggestion(sug)
        return sug

    # -------------------------------------------------------------- index

    def reindex(self) -> None:
        """Rebuild the SQLite index from the files. Cheap at this scale;
        keeps hand-edits and db deletion safe."""
        self.db.execute("DELETE FROM tasks")
        self.db.execute("DELETE FROM questions")
        self.db.execute("DELETE FROM events")
        for tdir in sorted((self.home / "tasks").iterdir()) if (self.home / "tasks").exists() else []:
            tj = tdir / "task.json"
            if not tj.is_file():
                continue
            try:
                task = Task.from_dict(json.loads(tj.read_text()))
            except (json.JSONDecodeError, TypeError):
                continue
            self.db.execute(
                "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (task.id, task.status, task.repo, task.branch, task.goal,
                 task.current_step, task.created_at, task.updated_at),
            )
            qdir = tdir / "questions"
            if qdir.exists():
                for qf in sorted(qdir.glob("*.json")):
                    try:
                        q = Question.from_dict(json.loads(qf.read_text()))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    self.db.execute(
                        "INSERT OR REPLACE INTO questions VALUES (?,?,?,?,?,?,?,?)",
                        (q.id, q.task_id, q.step_id, q.type, q.urgency, q.status,
                         q.asked_at, q.answered_at),
                    )
            ev = tdir / "events.jsonl"
            if ev.exists():
                for line in ev.read_text().splitlines():
                    if not line.strip():
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.db.execute(
                        "INSERT INTO events (ts, task_id, step_id, event, json) VALUES (?,?,?,?,?)",
                        (evt.get("ts"), evt.get("task_id"), evt.get("step_id"),
                         evt.get("event"), line),
                    )
        self.db.commit()
