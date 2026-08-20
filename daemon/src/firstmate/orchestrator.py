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
failure as context → a declared `on_failure` loop edge rewinds to an
earlier step (bounded, and stopped when a round makes no progress) →
otherwise escalates to a `decision` question with diff and failing check
attached. Never a third autonomous attempt at the same thing.

Waiting (decision log 2026-08-20): a step may declare a `when` gate — a
shell probe the daemon runs on an interval *before* spawning a worker. The
task sits in `waiting`, holding no worker slot and burning no tokens, so
First Mate can wait out something slow and external (an AI reviewer, CI, a
deploy) the way a human-attended session would. Gate progress persists, so
a daemon restart resumes the wait instead of restarting its ceiling.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import guard, relay, spawner, supervisor, validation, workerfiles
from .exec import context, gitops, tmux
from .models import (
    Contract, GateState, Question, SessionRecord, StepSpec, StepState, Task,
    new_id, now_iso, question_fingerprint,
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
- If `fm ask` replies that the question is ALREADY ANSWERED, that answer \
is binding and the operator has already been consulted — do NOT stop and \
do NOT re-ask. Carry on with it. Re-asking a settled question is the \
single most annoying thing a worker can do.
- Need somewhere to put your own working files (a draft, notes, a report \
you generated)? Write them under `.fm/artifacts/` — always allowed, no \
approval needed, never committed. Do not invent a file at the repo root \
for this, and do not ask for scope to be widened for it.
- Do NOT poll, sleep, or busy-wait for something slow and external (a \
review, CI, a deploy). First Mate waits for those itself via the step's \
gate, before your session even starts — so if the contract shows a gate on \
this step, the wait is already over. If you find yourself wanting to wait \
minutes for something, say so with `fm ask` instead of burning your \
context on it.
- A scope guard may BLOCK tool calls that leave the contract's scope or \
trip a tripwire (dependency changes, migrations, pushes). The block \
message tells you exactly how to raise the question — follow it, then \
stop. Do not try to work around a block.
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
        # Set when a loop edge fires: context for the step we rewind to.
        self._loop_note: str | None = None
        self._loop_note_for: str | None = None

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

        # Idempotent: the worktree normally already exists (created when the
        # task was scoped, at the operator's chosen starting point). The
        # pinned base_sha is what a task created another way branches from,
        # so a run days later starts where the operator decided, not at
        # whatever HEAD has become.
        worktree = gitops.create_worktree(
            Path(task.repo), task.branch, task.base_sha or "HEAD")
        if task.worktree != str(worktree):
            task.worktree = str(worktree)
        await self._set_status(task, "running")

        # Index-driven rather than a plain for-loop: a step's `on_failure`
        # edge can rewind the cursor to an earlier step, which is how
        # convergence loops (fix → push → re-review → fix) run without the
        # operator arbitrating every round.
        i = 0
        while i < len(contract.steps):
            spec = contract.steps[i]
            st = task.step_state(spec.id)
            if st.status == "done":
                i += 1
                continue
            if st.status in ("blocked", "waiting"):
                # Operator intervened (answered a question); the ladder
                # restarts with their answer in context.
                st.attempt = 1
                st.last_failure = None
            outcome = await self._run_step(task, contract, spec, st)
            if outcome is None:
                return
            # NB: bool is a subclass of int — test for the step-done case
            # first, or `True` would be read as "rewind to index 1".
            if outcome is not True:
                # Loop edge fired: rewind to the target step and re-run
                # everything from there, so the fix is re-pushed and
                # re-reviewed rather than just re-validated.
                i = int(outcome)
                continue
            i += 1

        # Task boundary: every contract criterion must hold (PRD §6.7).
        task.current_step = None
        await self._set_status(task, "validating")
        # A criterion the operator waived mid-run is not re-run here —
        # otherwise "accept and continue" marks the step done and then the
        # boundary asks the identical question all over again.
        boundary = [c for c in contract.criteria
                    if c.id not in (contract.waived_criteria or [])]
        results = await asyncio.to_thread(
            validation.run_criteria, worktree, boundary
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
                        spec: StepSpec, st: StepState) -> bool | int | None:
        """Run one step to a conclusion.

        Returns True when the step is done and the task should advance to
        the next one, an int when a loop edge fired (the step index to
        rewind to), and None when the task went to a resting state
        (blocked / paused / abandoned).
        """
        worktree = Path(task.worktree)

        # Gate first: no worker is spawned — and so no context is spent —
        # until the step's precondition actually holds.
        if spec.when is not None and not await self._await_gate(
                task, contract, spec, st):
            return None

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
                return None

            st.generation += 1
            outcome, question = await self._run_generation(
                task, contract, spec, st, handoff)

            if outcome == "abandoned":
                await self._set_status(task, "abandoned")
                return None
            if outcome == "paused":
                st.status = "pending"
                self.store.save_task(task)
                await self._set_status(task, "paused")
                return None
            if outcome == "parked":
                sess = st.sessions[-1]
                handoff = await self._acquire_handoff(
                    task, spec, st.generation, sess.session_id, worktree)
                st.status = "blocked"
                self.store.save_task(task)
                await self._set_status(task, "blocked")
                await self.emit("task_parked", step_id=spec.id,
                                question_id=question["id"] if question else None)
                return None
            if outcome in ("walled", "timeout"):
                sess = st.sessions[-1]
                handoff = await self._acquire_handoff(
                    task, spec, st.generation, sess.session_id, worktree)
                continue

            # outcome == "exited" → validate this step's criteria
            crits = [c for c in contract.resolve_criteria(spec.criteria)
                     if c.id not in (contract.waived_criteria or [])]
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
                if self._loop_note_for == spec.id:
                    self._loop_note = None
                    self._loop_note_for = None
                self.store.save_task(task)
                await self.emit("step_done", step_id=spec.id,
                                generations=st.generation)
                # Diff-shaped tripwires (PRD §6.4) at the step boundary —
                # the step's work stands, but the task pauses for approval
                # before any further steps build on an oversized diff.
                if await self._diff_tripwires(task, contract, spec.id, worktree) \
                        or await self._lockfile_drift(task, contract, spec.id,
                                                      worktree):
                    await self._set_status(task, "blocked")
                    return None
                return True

            summary = "; ".join(
                f"{r.id}: " + (r.error or f"exit {r.exit_status}") for r in failing
            )
            if st.attempt >= 2:
                # Before looping (or asking), find out whether more work
                # could fix this at all. A criterion that asserts something
                # unreachable will fail identically every round, so looping
                # would just burn the allowance and delay the operator
                # hearing about it.
                cdiag = await self._diagnose_criteria(
                    task, contract, spec, st, failing, worktree)
                if cdiag is not None and cdiag.blocks_loop:
                    await self._escalate(
                        task, spec.id,
                        f"Step '{spec.id}' cannot pass its criteria — "
                        f"{cdiag.criterion_id or 'a check'} asserts something "
                        f"that can never become true, so looping would not "
                        f"help. {cdiag.findings[:600]}",
                        failing, worktree,
                        options=["accept and continue", "abandon"],
                        extra_evidence={
                            "unsatisfiable": True,
                            "criterion_id": cdiag.criterion_id,
                            "supervisor_findings": cdiag.findings,
                            "supervisor_reasoning": cdiag.reasoning,
                            "supervisor_suggestion": cdiag.suggestion,
                            "confidence": cdiag.confidence,
                        },
                    )
                    st.status = "blocked"
                    self.store.save_task(task)
                    await self._set_status(task, "blocked")
                    return None
                # A declared loop edge is the contract saying "this failure
                # is an expected outcome — go round again" (e.g. a reviewer
                # found new issues). Take it before bothering the operator.
                target = await self._loop_back(
                    task, contract, spec, st, summary, failing, worktree)
                if target is not None:
                    return target
                # Never a third autonomous attempt.
                await self._escalate(
                    task, spec.id,
                    f"Step '{spec.id}' failed validation twice: {summary}",
                    failing, worktree,
                )
                st.status = "blocked"
                self.store.save_task(task)
                await self._set_status(task, "blocked")
                return None
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
        loop_note = None
        if self._loop_note and self._loop_note_for == spec.id:
            loop_note = self._loop_note
        inject = workerfiles.build_inject(
            contract, spec, gen, st.attempt,
            memory=memory, handoff=handoff, answered=answered,
            retry_note=self._retry_note if st.attempt > 1 else None,
            loop_note=loop_note,
        )
        workerfiles.write_inject(worktree, inject)
        self.store.save_step_artifact(task.id, spec.id, f"inject-gen{gen}.md", inject)
        binary = fm_bin()
        settings = workerfiles.write_worker_hooks(
            worktree, task.id, spec.id, self.daemon_url, binary,
            guard_config=guard.build_config(contract, self.config, worktree))

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

    # --------------------------------------------------------------- gates

    async def _await_gate(self, task: Task, contract: Contract,
                          spec: StepSpec, st: StepState) -> bool:
        """Wait for a step's `when` gate to pass. Returns True when it
        passed (the step may run), False when the task went to a resting
        state (ceiling escalation, pause, abandon).

        The whole point is that this costs nothing: no worker, no tmux
        window, no tokens, and no worker slot — the semaphore is untouched,
        so other tasks keep running while this one waits. Gate progress is
        persisted, so a daemon restart resumes the same wait rather than
        granting a fresh ceiling.
        """
        gate = spec.when
        assert gate is not None
        worktree = Path(task.worktree)
        loop = asyncio.get_running_loop()

        if st.gate is None:
            st.gate = GateState(first_probe_at=now_iso())
        st.status = "waiting"
        task.current_step = spec.id
        self.store.save_task(task)
        await self._set_status(task, "waiting")
        await self.emit("gate_waiting", step_id=spec.id,
                        command=gate.command,
                        description=gate.description,
                        interval=gate.interval, ceiling=gate.ceiling,
                        elapsed_s=self._gate_elapsed(st))

        while True:
            result = await asyncio.to_thread(validation.run_gate, worktree, gate)
            st.gate.probes += 1
            st.gate.last_probe_at = now_iso()
            st.gate.last_exit = result.exit_status
            st.gate.last_output = ((result.stdout or "") + (result.stderr or ""))[-2000:]
            self.store.save_task(task)
            await self.emit("gate_probe", step_id=spec.id,
                            passed=result.passed, probe=st.gate.probes,
                            exit_status=result.exit_status,
                            elapsed_s=self._gate_elapsed(st))
            if result.passed:
                st.status = "pending"
                self.store.save_task(task)
                await self.emit("gate_passed", step_id=spec.id,
                                probes=st.gate.probes,
                                elapsed_s=self._gate_elapsed(st))
                await self._set_status(task, "running")
                return True

            # A gate that has been red for a while may be waiting on
            # something that already happened in a shape it cannot see. Ask
            # the supervisor to look at the real world before burning the
            # rest of the ceiling — and long before bothering the operator.
            if self._should_supervise(st, gate):
                outcome = await self._supervise_gate(task, contract, spec, st,
                                                     gate, result)
                if outcome == "repaired":
                    gate = spec.when
                    if gate is None:
                        # Nothing left to wait for.
                        st.status = "pending"
                        self.store.save_task(task)
                        await self.emit("gate_dropped", step_id=spec.id)
                        await self._set_status(task, "running")
                        return True
                    continue  # re-probe immediately with the fixed gate
                if outcome == "blocked":
                    return False

            if self._gate_elapsed(st) >= gate.ceiling:
                what = gate.description or gate.command
                # Hand the operator whatever the supervisor learned, so the
                # question is "here is what I found" rather than "no idea".
                summary = (f"Still waiting on {what} after "
                           f"{int(self._gate_elapsed(st) // 60)} min "
                           f"(ceiling {gate.ceiling // 60} min, "
                           f"{st.gate.probes} probes)")
                if st.gate.diagnoses:
                    last = st.gate.diagnoses[-1]
                    summary += (f" — supervisor checked and reported: "
                                f"{last.get('verdict')}: "
                                f"{(last.get('findings') or '')[:400]}")
                await self._escalate(
                    task, spec.id, summary, [result], worktree,
                    options=["keep waiting", "run the step anyway", "abandon"],
                )
                st.status = "blocked"
                self.store.save_task(task)
                await self._set_status(task, "blocked")
                return False

            # Sleep in slices so a pause/abandon lands promptly.
            wake = loop.time() + gate.interval
            while loop.time() < wake:
                try:
                    item = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(min(1.0, max(0.05, wake - loop.time())))
                    continue
                if item.get("kind") == "control":
                    action = item.get("action", "paused")
                    st.status = "pending"
                    self.store.save_task(task)
                    await self._set_status(
                        task, "abandoned" if action == "abandoned" else "paused")
                    return False

    def _should_supervise(self, st: StepState, gate) -> bool:
        """Whether a stalled gate has earned a look from the supervisor.

        Cheap waits shouldn't pay for an LLM call, and a gate that is
        merely slow shouldn't be second-guessed on its second probe. The
        trigger is "stalled long enough to be suspicious, and not looked
        at recently", bounded by a repair cap.
        """
        if st.gate is None:
            return False
        if not self.config.get("supervise_gates", True):
            return False
        if st.gate.supervisions >= int(self.config.get("max_gate_supervisions", 3)):
            return False
        threshold = float(self.config.get("supervise_after_s", 300))
        # ...or a quarter of the ceiling, whichever is sooner, so a short
        # ceiling still gets supervised before it expires.
        threshold = min(threshold, max(60.0, gate.ceiling / 4))
        if self._gate_elapsed(st) < threshold:
            return False
        # Space attempts: at least this many probes since the last look.
        gap = max(3, int(threshold // max(gate.interval, 1)))
        return st.gate.probes - st.gate.supervised_at_probe >= gap

    async def _supervise_gate(self, task: Task, contract: Contract,
                              spec: StepSpec, st: StepState, gate,
                              result) -> str:
        """Have the supervisor investigate a stalled gate.

        Returns "repaired" when the gate was changed and should be
        re-probed, "blocked" when the task went to a resting state, and
        "continue" to keep waiting as before.

        A repair is only accepted if the NEW probe actually passes — a
        confident-sounding fix that still doesn't open the gate is not a
        fix, and applying it would silently move the goalposts.
        """
        assert st.gate is not None
        st.gate.supervisions += 1
        st.gate.supervised_at_probe = st.gate.probes
        self.store.save_task(task)
        worktree = Path(task.worktree)
        model = (self.config.get("supervisor_model")
                 or self.config.get("handoff_model")
                 or self.config["worker_model"])
        await self.emit("gate_supervising", step_id=spec.id,
                        attempt=st.gate.supervisions,
                        elapsed_s=self._gate_elapsed(st))

        diagnosis = await asyncio.to_thread(
            supervisor.investigate, worktree, contract, spec.id, gate,
            st.gate.probes, self._gate_elapsed(st), result.exit_status,
            st.gate.last_output, model)
        record = diagnosis.to_dict()
        record["at"] = now_iso()
        st.gate.diagnoses.append(record)
        self.store.save_task(task)
        self.store.save_step_artifact(
            task.id, spec.id,
            f"gate-diagnosis-{st.gate.supervisions}.md",
            self._render_diagnosis(gate, diagnosis))

        if not diagnosis.repairs_gate:
            # Either it's genuinely still waiting, or the supervisor
            # couldn't tell. Both mean: carry on waiting.
            await self.emit("gate_diagnosed", step_id=spec.id,
                            verdict=diagnosis.verdict,
                            confidence=diagnosis.confidence,
                            errors=diagnosis.errors or None,
                            findings=diagnosis.findings[:500])
            return "continue"

        old_command = gate.command
        try:
            new_gate = supervisor.apply_gate_repair(contract, spec.id, diagnosis)
        except ValueError as e:
            await self.emit("gate_repair_rejected", step_id=spec.id,
                            reason=str(e))
            return "continue"

        # Verify before trusting: the repaired probe must actually pass.
        if new_gate is not None:
            check = await asyncio.to_thread(
                validation.run_gate, worktree, new_gate)
            if not check.passed:
                # Put the original back; a probe that still fails is not a
                # diagnosis we can act on, and swapping it in would change
                # what we're waiting for on nothing but confidence.
                step = next(sp for sp in contract.steps if sp.id == spec.id)
                step.when = gate
                await self.emit("gate_repair_rejected", step_id=spec.id,
                                reason="the replacement probe also failed",
                                exit_status=check.exit_status)
                return "continue"

        st.gate.repairs += 1
        self.store.save_contract(task.id, contract)
        contract.amendments.append({
            "at": now_iso(), "question_id": None,
            "question": f"gate on step '{spec.id}' was not opening",
            "answer": ("dropped the gate" if new_gate is None
                       else f"repaired probe: {new_gate.command}"),
            "by": "supervisor",
            "summary": diagnosis.findings[:600],
        })
        self.store.save_contract(task.id, contract)
        self.store.save_task(task)

        # Tell the operator what happened, without stopping for them: this
        # is First Mate fixing its own instrument, not a decision about
        # their work.
        q = Question(
            id=new_id("q"),
            task_id=task.id,
            step_id=spec.id,
            type="fyi",
            urgency="normal",
            status="noted",
            question=(
                f"The gate on step '{spec.id}' was waiting for something it "
                f"could not observe, so I fixed the check and carried on. "
                f"{diagnosis.findings[:600]}"),
            evidence={
                "old_command": old_command,
                "new_command": new_gate.command if new_gate else None,
                "dropped": new_gate is None,
                "verdict": diagnosis.verdict,
                "confidence": diagnosis.confidence,
                "reasoning": diagnosis.reasoning[:1000],
            },
        )
        q.fingerprint = question_fingerprint(q.type, q.evidence, q.question)
        self.store.save_question(q)
        await self.emit("gate_repaired", step_id=spec.id,
                        question_id=q.id,
                        dropped=new_gate is None,
                        old_command=old_command,
                        new_command=new_gate.command if new_gate else None,
                        confidence=diagnosis.confidence,
                        findings=diagnosis.findings[:500])
        return "repaired"

    @staticmethod
    def _render_diagnosis(gate, diagnosis) -> str:
        return "\n".join([
            "# Gate diagnosis",
            "",
            f"**Verdict:** {diagnosis.verdict} (confidence: {diagnosis.confidence})",
            "",
            "## The gate as it stood",
            "",
            f"Waiting for: {gate.description or '(no description)'}",
            "",
            "```sh",
            gate.command,
            "```",
            "",
            "## Findings",
            "",
            diagnosis.findings or "(none)",
            "",
            "## Reasoning",
            "",
            diagnosis.reasoning or "(none)",
            "",
            "## Prescribed change",
            "",
            ("drop the gate entirely" if diagnosis.drop_gate and not diagnosis.new_command
             else f"```sh\n{diagnosis.new_command}\n```" if diagnosis.new_command
             else "(none)"),
            "",
            *(["## Errors", "", *(f"- {e}" for e in diagnosis.errors), ""]
              if diagnosis.errors else []),
        ]) + "\n"

    @staticmethod
    def _gate_elapsed(st: StepState) -> float:
        """Seconds spent waiting on this step's gate, across daemon restarts."""
        if st.gate is None or not st.gate.first_probe_at:
            return 0.0
        try:
            started = datetime.fromisoformat(st.gate.first_probe_at)
        except ValueError:
            return 0.0
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())

    # ------------------------------------------------ criterion judgement

    async def _diagnose_criteria(self, task: Task, contract: Contract,
                                 spec: StepSpec, st: StepState,
                                 failing: list[validation.CriterionResult],
                                 worktree: Path):
        """Ask the supervisor whether more work could ever satisfy these
        checks. Returns a CriterionDiagnosis, or None when not consulted.

        Deliberately advisory-only: the supervisor may report that a check
        is unsatisfiable and say what it thinks the check meant, but it
        cannot edit one. A criterion is the operator's statement of what
        they wanted, so acting on the diagnosis is their call — made
        through the escalation, where free text already routes to the
        re-planner.
        """
        if not self.config.get("supervise_criteria", True):
            return None
        if st.criteria_diagnoses >= int(
                self.config.get("max_criteria_supervisions", 2)):
            return None
        # Only where the answer can actually change what happens next:
        # a step with a loop edge is about to spend rounds on this, so it
        # is worth an LLM call to check the rounds aren't futile. Without
        # a loop edge the next stop is the operator either way, and the
        # escalation already carries the failing evidence.
        if spec.on_failure is None:
            return None
        # Give the loop a real chance first. A criterion is only worth
        # judging once a round has gone by and changed nothing: that is the
        # signal that re-doing the work cannot reach the check. Before
        # that, looping is cheap and might simply work — and _loop_back's
        # own no-progress brake would stop us here anyway, so this is the
        # same moment, reached with an explanation instead of a shrug.
        if st.iteration < 1:
            return None
        if self._failure_signature(failing) != st.last_failure_signature:
            return None  # still moving; let it keep going
        model = (self.config.get("supervisor_model")
                 or self.config.get("handoff_model")
                 or self.config["worker_model"])
        edge = spec.on_failure
        st.criteria_diagnoses += 1
        self.store.save_task(task)
        await self.emit("criteria_supervising", step_id=spec.id,
                        attempt=st.attempt, iteration=st.iteration,
                        failing=[r.id for r in failing])
        diag = await asyncio.to_thread(
            supervisor.investigate_criteria, worktree, contract, spec.id,
            failing, model, st.iteration,
            edge.max_iterations if edge else 0)
        self.store.save_step_artifact(
            task.id, spec.id,
            f"criteria-diagnosis-{st.criteria_diagnoses}.md",
            self._render_criterion_diagnosis(failing, diag))
        await self.emit("criteria_diagnosed", step_id=spec.id,
                        verdict=diag.verdict,
                        criterion_id=diag.criterion_id or None,
                        confidence=diag.confidence,
                        blocks_loop=diag.blocks_loop,
                        errors=diag.errors or None,
                        findings=diag.findings[:500])
        return diag

    @staticmethod
    def _render_criterion_diagnosis(failing, diag) -> str:
        return "\n".join([
            "# Criteria diagnosis",
            "",
            f"**Verdict:** {diag.verdict} (confidence: {diag.confidence})",
            f"**Criterion judged:** {diag.criterion_id or '(unspecified)'}",
            "",
            "## Failing checks",
            "",
            *(f"- `{r.id}`: exit {r.exit_status}\n  ```sh\n  {r.command}\n  ```"
              for r in failing),
            "",
            "## Findings",
            "",
            diag.findings or "(none)",
            "",
            "## Reasoning",
            "",
            diag.reasoning or "(none)",
            "",
            "## Suggested correction (for the operator to decide on)",
            "",
            diag.suggestion or "(none)",
            "",
            *(["## Errors", "", *(f"- {e}" for e in diag.errors), ""]
              if diag.errors else []),
        ]) + "\n"

    # ------------------------------------------------------- loop edges

    async def _loop_back(self, task: Task, contract: Contract, spec: StepSpec,
                         st: StepState, summary: str,
                         failing: list[validation.CriterionResult],
                         worktree: Path) -> int | None:
        """Take the step's `on_failure` edge if it has one and the loop is
        still making progress. Returns the step index to rewind to, or
        None to fall through to operator escalation.

        Two independent brakes, because an autonomous loop that cannot
        stop is worse than no loop at all:
          * max_iterations caps the number of rounds;
          * an identical failure signature two rounds running means the
            loop is spinning, so we stop and ask.
        """
        edge = spec.on_failure
        if edge is None:
            return None
        try:
            target = next(i for i, sp in enumerate(contract.steps)
                          if sp.id == edge.goto)
        except StopIteration:
            return None  # validated at contract time; be defensive anyway

        signature = self._failure_signature(failing)
        if st.iteration >= edge.max_iterations:
            await self._escalate(
                task, spec.id,
                f"Step '{spec.id}' still failing after {st.iteration} "
                f"convergence rounds through '{edge.goto}': {summary}",
                failing, worktree,
                options=["loop again", "accept and continue", "abandon"],
            )
            st.status = "blocked"
            self.store.save_task(task)
            await self._set_status(task, "blocked")
            return None
        if st.iteration >= 1 and signature == st.last_failure_signature:
            await self._escalate(
                task, spec.id,
                f"Convergence loop through '{edge.goto}' is not making "
                f"progress — round {st.iteration + 1} failed exactly the way "
                f"round {st.iteration} did: {summary}",
                failing, worktree,
                options=["loop again", "accept and continue", "abandon"],
            )
            st.status = "blocked"
            self.store.save_task(task)
            await self._set_status(task, "blocked")
            return None

        st.iteration += 1
        st.last_failure_signature = signature
        st.status = "pending"
        st.attempt = 1
        st.last_failure = summary
        st.gate = None  # the next round waits afresh (new push, new review)
        self._retry_note = None
        # Rewind every step from the target forward, so the fix is actually
        # re-done, re-pushed and re-reviewed rather than just re-validated.
        for sp in contract.steps[target:]:
            rst = task.step_state(sp.id)
            if sp.id != spec.id:
                rst.status = "pending"
                rst.attempt = 1
                rst.generation = 0
                rst.gate = None
        # What the next round of the target step needs to know.
        self._loop_note = (
            f"This is convergence round {st.iteration} of "
            f"{edge.max_iterations}. Step '{spec.id}' did not pass its "
            f"criteria, so the task looped back here to address it.\n\n"
            f"{summary}\n\n" + "\n\n".join(
                f"### {r.id}\n$ {r.command}\n{(r.stdout or '')[-3000:]}"
                f"\n{(r.stderr or '')[-3000:]}"
                for r in failing
            ) + "\n\nAddress what this check is reporting. It is the same "
            f"kind of work as before, on new input — do not redo work that "
            f"already landed, and do not repeat an approach that just failed."
        )
        self._loop_note_for = edge.goto
        self.store.save_task(task)
        await self.emit("loop_back", step_id=spec.id, goto=edge.goto,
                        iteration=st.iteration,
                        max_iterations=edge.max_iterations, failure=summary)
        return target

    @staticmethod
    def _failure_signature(failing: list[validation.CriterionResult]) -> str:
        """A stable fingerprint of a validation failure, used to detect a
        convergence loop that is not converging."""
        import hashlib

        h = hashlib.sha256()
        for r in sorted(failing, key=lambda r: r.id):
            h.update(r.id.encode())
            h.update(str(r.exit_status).encode())
            h.update((r.stdout or "").strip()[-2000:].encode())
            h.update((r.stderr or "").strip()[-2000:].encode())
        return h.hexdigest()[:16]

    # ------------------------------------------------------------- helpers

    async def _lockfile_drift(self, task: Task, contract: Contract,
                              step_id: str, worktree: Path) -> bool:
        """Catch a lockfile that genuinely diverged.

        The hook lets a bare `bun install` / `npm ci` rewrite a lockfile,
        because in a fresh worktree that is just populating node_modules and
        is not something the operator can meaningfully approve. The real
        guarantee — "no dependency actually changed" — is enforced here, once
        per step, by checking whether a lockfile is modified in the diff.
        Returns True if a question was raised.
        """
        merged = dict(guard.DEFAULT_TRIPWIRES)
        merged.update(self.config.get("tripwires") or {})
        merged.update(contract.tripwires or {})
        if not merged.get("dependency_manifests"):
            return False
        if any((q.evidence or {}).get("tripwire") == "dependency_manifests"
               for q in self.store.list_questions(task_id=task.id)):
            return False  # already settled for this task
        try:
            changed = await asyncio.to_thread(gitops.changed_files, worktree)
        except Exception:
            return False
        drifted = [f for f in changed
                   if Path(f).name in guard.LOCKFILE_NAMES
                   and not guard.matches_any(f, contract.tripwire_allow or [])]
        if not drifted:
            return False
        q = Question(
            id=new_id("q"),
            task_id=task.id,
            step_id=step_id,
            type="approval",
            urgency="blocking",
            question=(f"A lockfile changed during step '{step_id}': "
                      f"{', '.join(drifted)}. A plain install shouldn't alter "
                      f"one, so a dependency may actually have changed — keep it?"),
            options=["allow", "revert the lockfile", "abandon"],
            default="allow",
            evidence={"tripwire": "dependency_manifests", "paths": drifted},
        )
        q.fingerprint = question_fingerprint(q.type, q.evidence, q.question)
        self.store.save_question(q)
        await self.emit("question_asked", step_id=step_id, question_id=q.id,
                        question=q.question, type=q.type)
        return True

    async def _diff_tripwires(self, task: Task, contract: Contract,
                              step_id: str, worktree: Path) -> bool:
        """Check the diff-shaped tripwires (max_diff_lines,
        max_deleted_lines). Returns True if a question was raised — each
        tripwire is raised at most once per task; an 'allow' answer
        disables it via the contract amendment semantics."""
        merged = dict(guard.DEFAULT_TRIPWIRES)
        merged.update(self.config.get("tripwires") or {})
        merged.update(contract.tripwires or {})
        try:
            added, deleted = await asyncio.to_thread(gitops.diff_numstat, worktree)
        except Exception:
            return False
        already_raised = {
            (q.evidence or {}).get("tripwire")
            for q in self.store.list_questions(task_id=task.id)
        }
        for name, value in (("max_diff_lines", added + deleted),
                            ("max_deleted_lines", deleted)):
            limit = merged.get(name)
            if not limit or value <= int(limit) or name in already_raised:
                continue
            try:
                diff_stat = gitops.diff_stat(worktree)
            except Exception:
                diff_stat = "(diff unavailable)"
            q = Question(
                id=new_id("q"),
                task_id=task.id,
                step_id=step_id,
                type="approval",
                urgency="blocking",
                question=(f"Tripwire '{name}': the task's diff is at {value} lines "
                          f"(limit {limit}) after step '{step_id}' — proceed?"),
                options=["allow", "abandon"],
                default="abandon",
                evidence={"tripwire": name, "value": value, "limit": int(limit),
                          "diff_stat": diff_stat},
            )
            self.store.save_question(q)
            await self.emit("question_asked", step_id=step_id,
                            question_id=q.id, question=q.question, type=q.type)
            return True
        return False

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
                        worktree: Path,
                        options: list[str] | None = None,
                        extra_evidence: dict | None = None) -> None:
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
            # Free text is a first-class answer here: it goes to the
            # re-planning decision point, which edits the contract.
            options=list(options or ["retry", "accept and continue", "abandon"]),
            evidence={
                "diff_stat": diff_stat,
                "failing": [
                    {"id": r.id, "command": r.command, "exit_status": r.exit_status,
                     "error": r.error, "stdout_tail": (r.stdout or "")[-2000:],
                     "stderr_tail": (r.stderr or "")[-2000:]}
                    for r in failing
                ],
                **(extra_evidence or {}),
            },
        )
        q.fingerprint = question_fingerprint(q.type, q.evidence, q.question)
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
