"""Orchestrator — the waiting primitive and convergence loops.

Hermetic: no tmux, no claude, no git. Worker generations are faked by
stubbing `_run_generation`, so what's under test is the deterministic
control flow — gate probing, loop rewinds, and the brakes on both.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from firstmate.models import Contract, Criterion, Gate, LoopBack, StepSpec, Task
from firstmate.orchestrator import TaskRunner
from firstmate.store import Store


def git_repo(path):
    """A real one-commit repo — the runner creates a worktree from it."""
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True,
                                    capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (path / "README").write_text("x\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    return path


def build(tmp_path, steps, criteria, *, config=None):
    """A task + contract on disk, with a worktree the runner can cd into."""
    repo = git_repo(tmp_path / "repo")
    store = Store(tmp_path / "home")
    contract = Contract(goal="g", repo=str(repo), steps=steps, criteria=criteria)
    task = Task(id="t1", repo=str(repo), branch="fm/t1", status="ready", goal="g")
    task.steps = []
    from firstmate.models import StepState
    for sp in steps:
        task.steps.append(StepState(id=sp.id))
    store.save_task(task)
    store.save_contract(task.id, contract)
    cfg = dict(store.config())
    cfg.update(config or {})
    runner = TaskRunner(store, cfg, task.id,
                        slots=asyncio.Semaphore(2), daemon_url="http://x")
    return store, runner, repo


def fake_generations(runner, outcomes):
    """Stub worker generations; each call pops the next outcome."""
    seq = list(outcomes)
    calls = []

    async def _run_generation(task, contract, spec, st, handoff):
        calls.append(spec.id)
        from firstmate.models import SessionRecord
        st.sessions.append(SessionRecord(
            session_id=f"s{len(calls)}", generation=st.generation,
            attempt=st.attempt, started_at="now", ended_at="now",
            outcome="exited"))
        return (seq.pop(0) if seq else "exited"), None

    runner._run_generation = _run_generation  # type: ignore[assignment]
    return calls


# ------------------------------------------------------------------ gates


def test_gate_holds_the_step_until_the_probe_passes(tmp_path):
    """The whole point of the waiting primitive: no worker is spawned
    while the gate is red, and the wait costs no worker slot."""
    flag = tmp_path / "ready"
    steps = [StepSpec(id="wait", prompt="p", criteria=["c"],
                      when=Gate(command=f"test -f {flag}", interval=1,
                                ceiling=300, description="the flag to land"))]
    store, runner, wt = build(tmp_path, steps, [Criterion(id="c", command="true")])
    calls = fake_generations(runner, ["exited"])

    async def scenario():
        loop = asyncio.create_task(runner.run())
        # Let it probe a few times with the gate red.
        for _ in range(40):
            await asyncio.sleep(0.05)
            if store.load_task("t1").status == "waiting":
                break
        assert store.load_task("t1").status == "waiting"
        assert calls == [], "no worker may run while the gate is red"
        assert runner.slots._value == 2, "waiting must not hold a worker slot"
        flag.write_text("go")
        await asyncio.wait_for(loop, timeout=10)

    asyncio.run(scenario())
    task = store.load_task("t1")
    assert task.status == "done"
    assert calls == ["wait"], "worker ran exactly once, after the gate opened"
    st = task.step_state("wait")
    assert st.gate is not None and st.gate.probes >= 1
    events = [e["event"] for e in store.events_tail("t1")]
    assert "gate_waiting" in events and "gate_passed" in events


def test_gate_ceiling_escalates_with_useful_options(tmp_path):
    steps = [StepSpec(id="wait", prompt="p", criteria=["c"],
                      when=Gate(command="false", interval=1, ceiling=0,
                                description="something that never lands"))]
    store, runner, wt = build(tmp_path, steps, [Criterion(id="c", command="true")])
    calls = fake_generations(runner, ["exited"])

    asyncio.run(asyncio.wait_for(runner.run(), timeout=10))

    task = store.load_task("t1")
    assert task.status == "blocked"
    assert calls == []
    qs = store.list_questions(task_id="t1", status="open")
    assert len(qs) == 1
    assert "something that never lands" in qs[0].question
    # The operator gets a real choice, not just retry/abandon.
    assert "keep waiting" in qs[0].options
    assert "run the step anyway" in qs[0].options


def test_gate_elapsed_survives_a_daemon_restart(tmp_path):
    """A restart must not hand a stalled wait a brand-new ceiling."""
    from firstmate.models import GateState, StepState
    st = StepState(id="wait")
    st.gate = GateState(first_probe_at="2020-01-01T00:00:00+00:00")
    assert TaskRunner._gate_elapsed(st) > 3600, "elapsed is measured from disk"
    assert TaskRunner._gate_elapsed(StepState(id="x")) == 0.0


# ------------------------------------------------------------ loop edges


def test_criterion_failure_loops_back_instead_of_asking(tmp_path):
    """The Cubic case: the review step's criterion fails because the
    reviewer found something new, so the task rewinds to the fix step and
    converges on its own rather than escalating an unsatisfiable check."""
    counter = tmp_path / "rounds"
    counter.write_text("0")
    steps = [
        StepSpec(id="fix", prompt="fix things", criteria=[]),
        StepSpec(id="review", prompt="check the review", criteria=["clean"],
                 on_failure=LoopBack(goto="fix", max_iterations=5)),
    ]
    # Green only after the fix step has run twice, so passing requires a
    # genuine loop-back round rather than just the retry ladder.
    crit = Criterion(id="clean",
                     command=f'test "$(cat {counter})" -ge 2')
    store, runner, wt = build(tmp_path, steps, [crit],
                              config={"supervise_criteria": False})
    calls = fake_generations(runner, ["exited"] * 12)
    inner = runner._run_generation

    async def counting(task, contract, spec, st, handoff):
        if spec.id == "fix":
            counter.write_text(str(int(counter.read_text()) + 1))
        return await inner(task, contract, spec, st, handoff)

    runner._run_generation = counting

    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    task = store.load_task("t1")
    assert task.status == "done", "the loop converged without the operator"
    assert not store.list_questions(task_id="t1", status="open")
    # fix ran again each round; review re-ran after each fix.
    assert calls.count("fix") >= 2, calls
    assert calls.count("review") >= 2, calls
    events = [e for e in store.events_tail("t1") if e["event"] == "loop_back"]
    assert events, "the rewind is recorded for the operator to see"
    assert events[0]["data"]["goto"] == "fix"


def test_loop_stops_when_it_makes_no_progress(tmp_path):
    """A loop that fails identically twice is spinning — stop and ask,
    rather than burning rounds until max_iterations."""
    steps = [
        StepSpec(id="fix", prompt="p", criteria=[]),
        StepSpec(id="review", prompt="p", criteria=["clean"],
                 on_failure=LoopBack(goto="fix", max_iterations=20)),
    ]
    store, runner, wt = build(
        tmp_path, steps, [Criterion(id="clean", command="echo same; exit 1")],
        # Isolate the control flow: the criterion-judging LLM call is
        # covered in test_supervisor.py.
        config={"supervise_criteria": False})
    fake_generations(runner, ["exited"] * 40)

    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    task = store.load_task("t1")
    assert task.status == "blocked"
    st = task.step_state("review")
    assert st.iteration <= 2, "must not spin to max_iterations"
    q = store.list_questions(task_id="t1", status="open")[0]
    assert "not making progress" in q.question
    assert "accept and continue" in q.options


def test_loop_respects_max_iterations(tmp_path):
    """Distinct failures each round still stop at the declared bound."""
    counter = tmp_path / "n"
    counter.write_text("0")
    steps = [
        StepSpec(id="fix", prompt="p", criteria=[]),
        StepSpec(id="review", prompt="p", criteria=["clean"],
                 on_failure=LoopBack(goto="fix", max_iterations=2)),
    ]
    # Different output every time, so the no-progress brake never fires.
    crit = Criterion(
        id="clean",
        command=f'n=$(cat {counter}); echo $((n+1)) > {counter}; echo "round $n"; exit 1')
    store, runner, wt = build(tmp_path, steps, [crit],
                              config={"supervise_criteria": False})
    fake_generations(runner, ["exited"] * 40)

    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    task = store.load_task("t1")
    assert task.status == "blocked"
    assert task.step_state("review").iteration == 2
    q = store.list_questions(task_id="t1", status="open")[0]
    assert "convergence rounds" in q.question


def test_loop_rewind_carries_context_and_resets_downstream_steps(tmp_path):
    """The step we rewind to must be told why, and intermediate steps must
    actually re-run (the fix has to be re-pushed to be re-reviewed)."""
    counter = tmp_path / "n"
    counter.write_text("0")
    steps = [
        StepSpec(id="fix", prompt="p", criteria=[]),
        StepSpec(id="push", prompt="p", criteria=[]),
        StepSpec(id="review", prompt="p", criteria=["clean"],
                 on_failure=LoopBack(goto="fix", max_iterations=5)),
    ]
    marker = tmp_path / "fixes"
    marker.write_text("0")
    # Passes only once the fix step has run twice — i.e. only after a real
    # loop-back round, never off the back of the retry ladder alone.
    crit = Criterion(
        id="clean",
        command=f'echo "finding $(cat {marker})"; test "$(cat {marker})" -ge 2')
    store, runner, wt = build(tmp_path, steps, [crit],
                              config={"supervise_criteria": False})
    calls = fake_generations(runner, ["exited"] * 20)

    # Count fix runs the way a real worker would: by touching the repo.
    inner = runner._run_generation

    async def counting(task, contract, spec, st, handoff):
        if spec.id == "fix":
            marker.write_text(str(int(marker.read_text()) + 1))
        return await inner(task, contract, spec, st, handoff)

    runner._run_generation = counting
    notes: list[str] = []

    async def capturing(task, contract, spec, st, handoff):
        if spec.id == "fix" and runner._loop_note_for == "fix" and runner._loop_note:
            notes.append(runner._loop_note)
        return await counting(task, contract, spec, st, handoff)

    runner._run_generation = capturing

    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    assert store.load_task("t1").status == "done"
    # push re-ran after the rewind, not just fix and review.
    assert calls.count("push") >= 2, calls
    # The rewind is recorded, and the note the fixing step gets carries
    # both the round number and the verbatim failing evidence.
    events = [e for e in store.events_tail("t1", 200) if e["event"] == "loop_back"]
    assert events and events[0]["data"]["iteration"] == 1
    assert notes, "the fixing step was handed loop context"
    assert "convergence round 1" in notes[0]
    assert "finding 1" in notes[0], "the failing evidence reaches the fixing step"


def test_step_without_loop_edge_still_escalates(tmp_path):
    """The existing failure ladder is unchanged for steps that declare no
    loop edge: two attempts, then ask."""
    steps = [StepSpec(id="s", prompt="p", criteria=["c"])]
    store, runner, wt = build(
        tmp_path, steps, [Criterion(id="c", command="exit 1")])
    fake_generations(runner, ["exited"] * 10)

    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    task = store.load_task("t1")
    assert task.status == "blocked"
    assert task.step_state("s").attempt == 2
    q = store.list_questions(task_id="t1", status="open")[0]
    assert "failed validation twice" in q.question
    assert q.options == ["retry", "accept and continue", "abandon"]


# --------------------------------------------------------- lockfile drift


def test_lockfile_drift_is_caught_at_the_step_boundary(tmp_path):
    """The hook lets a bare install rewrite a lockfile; this is the check
    that still catches one which actually diverged."""
    steps = [StepSpec(id="s", prompt="p", criteria=[])]
    store, runner, repo = build(tmp_path, steps, [])
    calls = fake_generations(runner, ["exited"])
    inner = runner._run_generation

    async def touching(task, contract, spec, st, handoff):
        # Stand in for what `bun install` does to a fresh worktree.
        (Path(task.worktree) / "bun.lock").write_text("lockfile\n")
        return await inner(task, contract, spec, st, handoff)

    runner._run_generation = touching

    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    task = store.load_task("t1")
    assert task.status == "blocked"
    q = store.list_questions(task_id="t1", status="open")[0]
    assert q.type == "approval"
    assert "bun.lock" in q.question
    assert (q.evidence or {}).get("tripwire") == "dependency_manifests"


def test_no_lockfile_change_means_no_question(tmp_path):
    steps = [StepSpec(id="s", prompt="p", criteria=[])]
    store, runner, repo = build(tmp_path, steps, [])
    fake_generations(runner, ["exited"])
    inner = runner._run_generation

    async def normal(task, contract, spec, st, handoff):
        (Path(task.worktree) / "src.ts").write_text("code\n")
        return await inner(task, contract, spec, st, handoff)

    runner._run_generation = normal
    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    assert store.load_task("t1").status == "done"
    assert not store.list_questions(task_id="t1", status="open")


def test_keep_waiting_resumes_the_gate_rather_than_skipping_it(tmp_path):
    """After a ceiling escalation, 'keep waiting' must put the task back on
    the gate — not walk past it into a step whose precondition is unmet."""
    flag = tmp_path / "ready"
    steps = [StepSpec(id="wait", prompt="p", criteria=["c"],
                      when=Gate(command=f"test -f {flag}", interval=1,
                                ceiling=0, description="the flag"))]
    store, runner, repo = build(tmp_path, steps, [Criterion(id="c", command="true")])
    calls = fake_generations(runner, ["exited"] * 4)

    # Round one: ceiling is already blown, so it escalates immediately.
    asyncio.run(asyncio.wait_for(runner.run(), timeout=10))
    assert store.load_task("t1").status == "blocked"
    assert calls == []

    # The operator says keep waiting; the gate allowance resets.
    q = store.list_questions(task_id="t1", status="open")[0]
    store.answer_question(q.id, "keep waiting", "test")
    from firstmate.server import apply_structured_answer
    task = store.load_task("t1")
    assert apply_structured_answer(store, task, q, "keep waiting")
    task.status = "running"
    store.save_task(task)

    # Round two: it must probe again, and only run once the gate opens.
    flag.write_text("go")
    runner2 = TaskRunner(store, dict(store.config()), "t1",
                         slots=asyncio.Semaphore(2), daemon_url="http://x")
    calls2 = fake_generations(runner2, ["exited"])
    asyncio.run(asyncio.wait_for(runner2.run(), timeout=15))

    task = store.load_task("t1")
    assert task.status == "done"
    assert calls2 == ["wait"], "the step ran only after the gate reopened"


# ------------------------------------- a worker that asked nobody anything


def void_asks(runner, sequence):
    """Stub the void-ask detector; each generation pops the next verdict."""
    seq = list(sequence)

    def _detect(task, spec, st):
        return seq.pop(0) if seq else None

    runner._worker_asked_the_void = _detect  # type: ignore[assignment]


def test_void_ask_reprompts_without_burning_an_attempt(tmp_path):
    """The real failure (STATUS 2026-08-20): a session ended its turn
    asking the operator, so it did no work — validating it would judge it
    on that and spend attempt 1. It must be re-prompted instead."""
    steps = [StepSpec(id="plan", prompt="p", criteria=["c"])]
    # The criterion only passes once the worker "writes" the receipt, which
    # the void-asking generation never does.
    receipt = tmp_path / "repo" / "receipt"
    store, runner, repo = build(
        tmp_path, steps, [Criterion(id="c", command=f"test -f {receipt}")])
    calls = fake_generations(runner, ["exited", "exited"])
    void_asks(runner, [" let me know", None])

    # The second generation does the work.
    original = runner._run_generation

    async def _run(task, contract, spec, st, handoff):
        out = await original(task, contract, spec, st, handoff)
        if len(calls) == 2:
            receipt.write_text("done")
        return out

    runner._run_generation = _run  # type: ignore[assignment]
    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    task = store.load_task("t1")
    st = task.step_state("plan")
    assert task.status == "done"
    assert len(calls) == 2, "the void-asking generation was re-prompted"
    # The decisive assertion: no attempt was consumed by the void-ask, so
    # the step still had its full retry budget for a real failure.
    assert st.attempt == 1, f"void-ask must not burn an attempt (got {st.attempt})"
    events = [e["event"] for e in store.events_tail("t1")]
    assert "worker_asked_the_void" in events
    # and it was not treated as a validation failure
    assert "step_retry" not in events


def test_void_ask_correction_reaches_the_next_session(tmp_path):
    steps = [StepSpec(id="plan", prompt="p", criteria=["c"])]
    store, runner, repo = build(tmp_path, steps,
                                [Criterion(id="c", command="true")])
    fake_generations(runner, ["exited", "exited"])
    void_asks(runner, ["let me know", None])
    notes = []
    original = runner._run_generation

    async def _run(task, contract, spec, st, handoff):
        notes.append(runner._void_note)
        return await original(task, contract, spec, st, handoff)

    runner._run_generation = _run  # type: ignore[assignment]
    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    assert notes[0] is None, "the first generation gets no correction"
    assert notes[1] and "NOBODY READ IT" in notes[1]
    assert "fm ask" in notes[1]


def test_void_ask_is_corrected_only_once_per_step(tmp_path):
    """If a worker keeps doing it, validation must get its say rather than
    the step looping on re-prompts forever."""
    steps = [StepSpec(id="plan", prompt="p", criteria=["c"])]
    store, runner, repo = build(
        tmp_path, steps, [Criterion(id="c", command="false")],
        config={"max_generations": 6, "supervise_criteria": False})
    calls = fake_generations(runner, ["exited"] * 6)
    void_asks(runner, ["let me know"] * 6)
    asyncio.run(asyncio.wait_for(runner.run(), timeout=30))

    events = [e["event"] for e in store.events_tail("t1")]
    # corrected once...
    assert events.count("worker_asked_the_void") == 1
    # ...then the failing criterion was actually reported
    assert "validation_run" in events
    task = store.load_task("t1")
    assert task.status in ("blocked", "failed")


def test_a_clean_exit_still_validates(tmp_path):
    """The detector must not interfere with an ordinary successful step."""
    steps = [StepSpec(id="plan", prompt="p", criteria=["c"])]
    store, runner, repo = build(tmp_path, steps,
                                [Criterion(id="c", command="true")])
    calls = fake_generations(runner, ["exited"])
    void_asks(runner, [None])
    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    assert store.load_task("t1").status == "done"
    assert len(calls) == 1
    events = [e["event"] for e in store.events_tail("t1")]
    assert "worker_asked_the_void" not in events


def test_a_crash_leaves_no_step_claiming_to_run(tmp_path):
    """A failed task with a `running` step reads as stuck in the dashboard —
    the operator sees "running" over state "failed" (STATUS 2026-08-20)."""
    steps = [StepSpec(id="plan", prompt="p", criteria=["c"])]
    store, runner, repo = build(tmp_path, steps,
                                [Criterion(id="c", command="true")])

    async def boom(task, contract, spec, st, handoff):
        st.status = "running"
        store.save_task(task)
        raise RuntimeError("tmux: command too long")

    runner._run_generation = boom  # type: ignore[assignment]

    async def scenario():
        with __import__("pytest").raises(RuntimeError):
            await runner.run()

    asyncio.run(scenario())
    task = store.load_task("t1")
    assert task.status == "failed"
    st = task.step_state("plan")
    assert st.status == "failed", f"step still claims {st.status!r}"
    assert "crashed" in (st.last_failure or "")
    assert "command too long" in (st.last_failure or "")
    events = [e["event"] for e in store.events_tail("t1")]
    assert "task_error" in events
