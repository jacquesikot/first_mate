"""Orchestrator — one deterministic loop per active task (PRD §6.2).

The loop is plain code: pick next step → spawn worker → wait on events →
handle outcome (done / context wall / question / failure) → validate →
advance. LLM calls happen only at named decision points — here, handoff
summarization (relay.request_handoff). Continuity lives in state files;
workers are disposable.

Wall trigger is orchestrator-side transcript polling + SIGINT (decision
log 2026-08-19); the PreCompact exit-2 hook stays installed in every
worker as a backstop so compaction can never silently fire.

Failure ladder (PRD §6.2): validation failure → one retry with the
failure as context → second failure escalates to a `decision` question
with diff and failing check attached. Never a third autonomous attempt.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from . import relay, spawner, validation, workerfiles
from .exec import context, gitops, tmux
from .models import (
    Contract, Question, SessionRecord, StepSpec, StepState, Task,
    new_id, now_iso,
)
from .store import Store

# Every worker may raise questions; nothing else is granted by default.
DEFAULT_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write"]
ASK_TOOL = "Bash(fm ask:*)"

PARK_GRACE_SECONDS = 90.0  # worker is told to stop after fm ask; then SIGINT

WORKER_PROMPT = """\
You are a First Mate worker on task '{task_id}', step '{step_id}' \
(generation {generation}, attempt {attempt}).

Your working context — the task contract, project memory, operator \
answers, and a handoff brief from the previous session generation (if \
any) — was injected at session start. If you don't see it, read \
.fm/inject.md.

Execute this step now:
{step_block}

Rules:
- Work in your CURRENT WORKING DIRECTORY — it is the task's dedicated git \
worktree. Never write into the original repository path mentioned in the \
contract; all paths are relative to your cwd.
- Do only this step's work, within the contract's scope. Other steps run \
in their own sessions.
- If you hit a decision you genuinely cannot make (ambiguity, scope \
change, approval needed, a failure you cannot diagnose), run:
    fm ask --type <clarification|scope_change|decision|approval> \
--question "..." [--option "A"] [--option "B"] [--default "A"]
  then STOP IMMEDIATELY and end the session. The orchestrator parks the \
task and resumes with the operator's answer.
- For trivial assumptions, do NOT stop; record and continue:
    fm ask --type fyi --question "assumed X because Y"
- When the step is complete, just stop. Do not write summaries.
"""


def fm_bin() -> str:
    """The fm executable workers and hooks should call — prefer the one
    next to the running interpreter (same venv as the daemon)."""
    cand = Path(sys.executable).with_name("fm")
    if cand.exists():
        return str(cand)
    return shutil.which("fm") or "fm"


class TaskRunner:
    """Runs one task to its next resting state (done, blocked, paused,
    failed, abandoned). Exits when parked; the manager restarts it when
    an answer arrives, so blocked tasks consume no worker slots."""

    def __init__(self, store: Store, config: dict, task_id: str,
                 slots: asyncio.Semaphore, daemon_url: str,
                 broadcast=None):
        self.store = store
        self.config = config
        self.task_id = task_id
        self.slots = slots
        self.daemon_url = daemon_url
        self.broadcast = broadcast  # async fn(event dict) | None
        self.queue: asyncio.Queue[dict] = asyncio.Queue()
        self._retry_note: str | None = None

    # Called by the daemon (fm ask / pause / abandon) while we run.
    def deliver(self, item: dict) -> None:
        self.queue.put_nowait(item)

    async def emit(self, event: str, step_id: str | None = None, **data) -> None:
        evt = self.store.append_event(self.task_id, event, step_id=step_id,
                                      data=data or None)
        if self.broadcast:
            await self.broadcast(evt)

    # ----------------------------------------------------------- main loop

    async def run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            # Daemon shutting down: leave state as-is; boot reconciliation
            # marks orphaned sessions and resumes (acceptance criterion 8).
            raise
        except Exception as e:
            task = self.store.load_task(self.task_id)
            if task is not None:
                task.status = "failed"
                self.store.save_task(task)
            await self.emit("task_error", error=repr(e))
            raise

    async def _run(self) -> None:
        task = self.store.load_task(self.task_id)
        contract = self.store.load_contract(self.task_id)
        if task is None or contract is None:
            raise RuntimeError(f"task {self.task_id} has no state on disk")
        if task.status in {"done", "failed", "abandoned", "paused"}:
            return
        open_blocking = [
            q for q in self.store.list_questions(task_id=task.id, status="open")
            if q.type != "fyi"
        ]
        if open_blocking:
            await self._set_status(task, "blocked")
            return

        worktree = gitops.create_worktree(Path(task.repo), task.branch)
        if task.worktree != str(worktree):
            task.worktree = str(worktree)
        await self._set_status(task, "running")

        for spec in contract.steps:
            st = task.step_state(spec.id)
            if st.status == "done":
                continue
            if st.status == "blocked":
                # Operator intervened (answered a question); the ladder
                # restarts with their answer in context.
                st.attempt = 1
                st.last_failure = None
            if not await self._run_step(task, contract, spec, st):
                return

        # Task boundary: every contract criterion must hold (PRD §6.7).
        task.current_step = None
        await self._set_status(task, "validating")
        results = await asyncio.to_thread(
            validation.run_criteria, worktree, contract.criteria
        )
        path = self.store.save_validation(task.id, None, 0, results)
        failing = [r for r in results if not r.passed]
        await self.emit("task_validation", passed=not failing,
                        failing=[r.id for r in failing], evidence=str(path))
        if failing:
            await self._escalate(
                task, None,
                "Task-boundary validation failed: "
                + "; ".join(f"{r.id} (exit {r.exit_status})" for r in failing),
                failing, worktree,
            )
            await self._set_status(task, "blocked")
            return
        await self._set_status(task, "done")
        await self.emit("task_done")

    # ---------------------------------------------------------------- step

    async def _run_step(self, task: Task, contract: Contract,
                        spec: StepSpec, st: StepState) -> bool:
        """Returns True when the step reached done and the task should
        advance; False when the task went to a resting state."""
        worktree = Path(task.worktree)
        st.status = "running"
        task.current_step = spec.id
        self.store.save_task(task)
        await self.emit("step_started", step_id=spec.id,
                        attempt=st.attempt, generation=st.generation)

        latest = self.store.latest_handoff(task.id, spec.id)
        handoff = latest[1] if latest else None
        # If the daemon died mid-generation, that session never wrote a
        # handoff — its transcript is still resumable after the fact.
        last = st.sessions[-1] if st.sessions else None
        if last and last.outcome == "orphaned" and (latest is None or latest[0] < last.generation):
            handoff = await self._acquire_handoff(
                task, spec, last.generation, last.session_id, worktree)

        while True:
            if st.generation >= int(self.config["max_generations"]):
                await self._escalate(
                    task, spec.id,
                    f"Step '{spec.id}' hit the generation limit "
                    f"({self.config['max_generations']}) without completing",
                    [], worktree,
                )
                st.status = "blocked"
                self.store.save_task(task)
                await self._set_status(task, "blocked")
                return False

            st.generation += 1
            outcome, question = await self._run_generation(
                task, contract, spec, st, handoff)

            if outcome == "abandoned":
                await self._set_status(task, "abandoned")
                return False
            if outcome == "paused":
                st.status = "pending"
                self.store.save_task(task)
                await self._set_status(task, "paused")
                return False
            if outcome == "parked":
                sess = st.sessions[-1]
                handoff = await self._acquire_handoff(
                    task, spec, st.generation, sess.session_id, worktree)
                st.status = "blocked"
                self.store.save_task(task)
                await self._set_status(task, "blocked")
                await self.emit("task_parked", step_id=spec.id,
                                question_id=question["id"] if question else None)
                return False
            if outcome in ("walled", "timeout"):
                sess = st.sessions[-1]
                handoff = await self._acquire_handoff(
                    task, spec, st.generation, sess.session_id, worktree)
                continue

            # outcome == "exited" → validate this step's criteria
            crits = contract.resolve_criteria(spec.criteria)
            results = await asyncio.to_thread(validation.run_criteria, worktree, crits)
            path = self.store.save_validation(task.id, spec.id, st.attempt, results)
            failing = [r for r in results if not r.passed]
            await self.emit("validation_run", step_id=spec.id, attempt=st.attempt,
                            passed=not failing, failing=[r.id for r in failing],
                            evidence=str(path))
            if not failing:
                st.status = "done"
                st.last_failure = None
                self._retry_note = None
                self.store.save_task(task)
                await self.emit("step_done", step_id=spec.id,
                                generations=st.generation)
                return True

            summary = "; ".join(
                f"{r.id}: " + (r.error or f"exit {r.exit_status}") for r in failing
            )
            if st.attempt >= 2:
                # Never a third autonomous attempt.
                await self._escalate(
                    task, spec.id,
                    f"Step '{spec.id}' failed validation twice: {summary}",
                    failing, worktree,
                )
                st.status = "blocked"
                self.store.save_task(task)
                await self._set_status(task, "blocked")
                return False
            st.attempt = 2
            st.last_failure = summary
            self._retry_note = summary + "\n\n" + "\n\n".join(
                f"### {r.id}\n$ {r.command}\n{(r.stdout or '')[-2000:]}\n{(r.stderr or '')[-2000:]}"
                for r in failing
            )
            self.store.save_task(task)
            await self.emit("step_retry", step_id=spec.id, failure=summary)
            handoff = None  # fresh attempt starts from the failure, not the old plan

    # ---------------------------------------------------------- generation

    async def _run_generation(self, task: Task, contract: Contract,
                              spec: StepSpec, st: StepState,
                              handoff: str | None) -> tuple[str, dict | None]:
        worktree = Path(task.worktree)
        gen = st.generation
        answered = [
            q for q in self.store.list_questions(task_id=task.id, status="answered")
            if q.type != "fyi"
        ][-10:]
        memory = self.store.memory_for_project(Path(task.repo).name)
        inject = workerfiles.build_inject(
            contract, spec, gen, st.attempt,
            memory=memory, handoff=handoff, answered=answered,
            retry_note=self._retry_note if st.attempt > 1 else None,
        )
        workerfiles.write_inject(worktree, inject)
        self.store.save_step_artifact(task.id, spec.id, f"inject-gen{gen}.md", inject)
        binary = fm_bin()
        settings = workerfiles.write_worker_hooks(
            worktree, task.id, spec.id, self.daemon_url, binary)

        allowed = list(spec.allowed_tools or DEFAULT_ALLOWED_TOOLS)
        if ASK_TOOL not in allowed:
            allowed.append(ASK_TOOL)
        step_block = (f"{spec.title}\n{spec.prompt}" if spec.title else spec.prompt)
        if spec.skill:
            step_block = f"Use your '{spec.skill}' skill/command for this step.\n{step_block}"
        wspec = spawner.WorkerSpec(
            prompt=WORKER_PROMPT.format(
                task_id=task.id, step_id=spec.id,
                generation=gen, attempt=st.attempt, step_block=step_block,
            ),
            cwd=worktree,
            name=f"{task.id}-{spec.id}-g{gen}",
            settings_file=settings,
            allowed_tools=allowed,
            model=spec.model or self.config["worker_model"],
            env={
                "FM_TASK_ID": task.id,
                "FM_STEP_ID": spec.id,
                "FM_DAEMON_URL": self.daemon_url,
                "PATH": f"{Path(binary).parent}:{os.environ.get('PATH', '')}",
            },
        )
        record = SessionRecord(
            session_id=wspec.session_id, generation=gen,
            attempt=st.attempt, started_at=now_iso(),
        )

        wall = int(self.config["wall_tokens"])
        poll = float(self.config["poll_seconds"])
        timeout = float(self.config["worker_timeout_s"])
        loop = asyncio.get_running_loop()

        async with self.slots:
            window = spawner.spawn(wspec)
            record.window_id = window.window_id
            st.sessions.append(record)
            self.store.save_task(task)
            await self.emit(
                "worker_spawned", step_id=spec.id,
                session_id=wspec.session_id, generation=gen, attempt=st.attempt,
                attach=tmux.attach_command(window),
            )

            outcome = "exited"
            parked_q: dict | None = None
            peak = 0
            deadline = loop.time() + timeout
            grace: float | None = None
            while True:
                while True:
                    try:
                        item = self.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    kind = item.get("kind")
                    if kind == "ask" and parked_q is None:
                        parked_q = item.get("question")
                        grace = loop.time() + PARK_GRACE_SECONDS
                    elif kind == "control":
                        outcome = item.get("action", "paused")
                if outcome in ("paused", "abandoned"):
                    break
                if tmux.pane_dead(window):
                    outcome = "parked" if parked_q else "exited"
                    break
                reading = context.read_context(worktree, wspec.session_id)
                if reading:
                    peak = max(peak, reading.tokens)
                    if reading.tokens >= wall:
                        outcome = "walled"
                        await self.emit("context_wall", step_id=spec.id,
                                        session_id=wspec.session_id, tokens=reading.tokens)
                        break
                if parked_q is not None and grace is not None and loop.time() > grace:
                    outcome = "parked"
                    break
                if loop.time() > deadline:
                    outcome = "timeout"
                    break
                await asyncio.sleep(poll)

            if not tmux.pane_dead(window):
                await asyncio.to_thread(spawner.interrupt_and_wait, window, wspec)
            tmux.kill_window(window)

        record.ended_at = now_iso()
        record.outcome = outcome
        record.peak_tokens = peak
        self.store.save_task(task)
        await self.emit("worker_ended", step_id=spec.id,
                        session_id=wspec.session_id, outcome=outcome,
                        generation=gen, peak_tokens=peak)
        return outcome, parked_q

    # ------------------------------------------------------------- helpers

    async def _acquire_handoff(self, task: Task, spec: StepSpec, generation: int,
                               session_id: str, worktree: Path) -> str:
        model = (self.config.get("handoff_model")
                 or spec.model or self.config["worker_model"])
        text = await asyncio.to_thread(
            relay.request_handoff, worktree, session_id, model)
        self.store.save_step_artifact(
            task.id, spec.id, f"handoff-gen{generation}.md", text)
        await self.emit("handoff_written", step_id=spec.id, generation=generation)
        return text

    async def _escalate(self, task: Task, step_id: str | None, summary: str,
                        failing: list[validation.CriterionResult],
                        worktree: Path) -> None:
        """The failure ladder's top rung: a decision question with the diff
        and the failing checks attached (acceptance criterion 9)."""
        try:
            diff_stat = gitops.diff_stat(worktree)
        except Exception:
            diff_stat = "(diff unavailable)"
        q = Question(
            id=new_id("q"),
            task_id=task.id,
            step_id=step_id,
            type="decision",
            urgency="blocking",
            question=f"{summary} — how should we proceed?",
            options=["retry", "abandon"],
            evidence={
                "diff_stat": diff_stat,
                "failing": [
                    {"id": r.id, "command": r.command, "exit_status": r.exit_status,
                     "error": r.error, "stdout_tail": (r.stdout or "")[-2000:],
                     "stderr_tail": (r.stderr or "")[-2000:]}
                    for r in failing
                ],
            },
        )
        self.store.save_question(q)
        await self.emit("question_asked", step_id=step_id,
                        question_id=q.id, question=q.question, type=q.type)

    async def _set_status(self, task: Task, status: str) -> None:
        if task.status != status:
            task.status = status
            self.store.save_task(task)
            await self.emit("task_status", status=status)
        else:
            self.store.save_task(task)
