"""Supervisor — diagnosing and repairing a gate that will never open.

The motivating case is real (2026-08-20): a `verify` gate waited for a
cubic review row whose commit_id equalled HEAD. Cubic reviewed the parent,
had nothing to say about the child, and filed no new review — so the check
went green while the row never appeared, and the gate could not open.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from firstmate import supervisor
from firstmate.models import (
    Contract, Criterion, Gate, LoopBack, StepSpec, StepState,
)
from firstmate.orchestrator import TaskRunner
from firstmate.store import Store

from test_orchestrator import build, fake_generations


def contract_with_gate(command="test -f never", description="the reviewer"):
    return Contract(
        goal="fix the review findings and get the PR green",
        repo="/tmp/repo",
        steps=[
            StepSpec(id="fix", prompt="fix things", criteria=[]),
            StepSpec(id="verify", prompt="check the review", criteria=["clean"],
                     when=Gate(command=command, description=description,
                               interval=60, ceiling=3600),
                     on_failure=LoopBack(goto="fix", max_iterations=5)),
        ],
        criteria=[Criterion(id="clean", command="gh pr checks 493")],
    )


def reply(**kw):
    body = {"verdict": "gate_wrong", "findings": "f", "reasoning": "r",
            "new_command": "gh pr checks 493", "drop_gate": False,
            "confidence": "high"}
    body.update(kw)
    return json.dumps(body)


# --------------------------------------------------------------- parsing


def test_parses_a_gate_wrong_verdict():
    d = supervisor.parse_reply(reply(
        findings="cubic reviewed d3ed0339, HEAD is 6b96ad09, checks all SUCCESS"))
    assert not d.errors
    assert d.verdict == "gate_wrong"
    assert d.repairs_gate
    assert "6b96ad09" in d.findings


def test_still_waiting_is_not_a_repair():
    """Waiting is free; a premature 'fix' is worse than waiting."""
    d = supervisor.parse_reply(reply(verdict="still_waiting", new_command=""))
    assert not d.errors
    assert not d.repairs_gate


def test_cannot_tell_is_not_a_repair():
    d = supervisor.parse_reply(reply(verdict="cannot_tell", new_command=""))
    assert not d.repairs_gate


def test_rejects_a_trivially_true_replacement():
    """A probe that always passes removes the precondition instead of
    correcting it — the gate would become a lie."""
    for cmd in ("true", "exit 0", ":", "/bin/true", "  true  "):
        d = supervisor.parse_reply(reply(new_command=cmd))
        assert d.errors, cmd
        assert not d.repairs_gate


def test_rejects_gate_wrong_with_no_replacement():
    d = supervisor.parse_reply(reply(new_command=""))
    assert d.errors and not d.repairs_gate


def test_rejects_junk_and_unknown_verdicts():
    assert supervisor.parse_reply("I cannot help with that").errors
    assert supervisor.parse_reply(reply(verdict="banana")).errors
    # Fenced JSON is tolerated.
    assert not supervisor.parse_reply("```json\n" + reply() + "\n```").errors


def test_drop_gate_is_a_valid_repair():
    d = supervisor.parse_reply(reply(drop_gate=True, new_command=""))
    assert not d.errors and d.repairs_gate


# ------------------------------------------------------------- the repair


def test_repair_only_touches_the_gate():
    """The safety boundary: a gate is First Mate's instrument, criteria are
    the operator's definition of done. A confused supervisor must not be
    able to redefine 'done'."""
    c = contract_with_gate()
    before = json.dumps({
        "steps": [{k: v for k, v in s.to_dict().items() if k != "when"}
                  for s in c.steps],
        "criteria": [x.to_dict() for x in c.criteria],
        "scope_in": c.scope_in, "scope_out": c.scope_out,
        "tripwires": c.tripwires, "goal": c.goal, "repo": c.repo,
    }, sort_keys=True)

    d = supervisor.parse_reply(reply(new_command="gh pr checks 493 --json state"))
    gate = supervisor.apply_gate_repair(c, "verify", d)

    assert gate is not None
    assert gate.command == "gh pr checks 493 --json state"
    after = json.dumps({
        "steps": [{k: v for k, v in s.to_dict().items() if k != "when"}
                  for s in c.steps],
        "criteria": [x.to_dict() for x in c.criteria],
        "scope_in": c.scope_in, "scope_out": c.scope_out,
        "tripwires": c.tripwires, "goal": c.goal, "repo": c.repo,
    }, sort_keys=True)
    assert after == before, "only the gate may change"
    # The operator-facing framing and timing envelope survive.
    assert gate.description == "the reviewer"
    assert gate.interval == 60 and gate.ceiling == 3600


def test_repair_can_drop_the_gate_entirely():
    c = contract_with_gate()
    d = supervisor.parse_reply(reply(drop_gate=True, new_command=""))
    assert supervisor.apply_gate_repair(c, "verify", d) is None
    assert c.steps[1].when is None
    # The step and its criteria are untouched.
    assert c.steps[1].criteria == ["clean"]


def test_repair_refuses_when_the_diagnosis_does_not_prescribe_one():
    c = contract_with_gate()
    d = supervisor.parse_reply(reply(verdict="still_waiting", new_command=""))
    with pytest.raises(ValueError):
        supervisor.apply_gate_repair(c, "verify", d)


def test_repair_refuses_an_ungated_or_unknown_step():
    c = contract_with_gate()
    d = supervisor.parse_reply(reply())
    with pytest.raises(ValueError):
        supervisor.apply_gate_repair(c, "fix", d)      # no gate
    with pytest.raises(ValueError):
        supervisor.apply_gate_repair(c, "nope", d)     # no such step


def test_prompt_carries_the_facts_needed_to_diagnose():
    c = contract_with_gate(
        command="test \"$(gh api .../reviews --jq '...')\" != 0",
        description="cubic to review the current head")
    p = supervisor.build_prompt(
        c, "verify", c.steps[1].when, probes=24, elapsed_s=1400,
        last_exit=1, last_output="")
    assert "cubic to review the current head" in p
    assert "gh api" in p
    assert "24 times" in p and "23 minutes" in p
    # It must see what the step will be judged on, without being invited to
    # change it.
    assert "gh pr checks 493" in p
    assert "NOT evaluating or changing them" in p
    # And it must be steered toward terminal check state over record existence.
    assert "terminal check state" in p


def test_investigate_tools_are_read_only():
    joined = " ".join(supervisor.INVESTIGATE_TOOLS)
    for forbidden in ("Edit", "Write", "git push", "git commit", "gh pr merge"):
        assert forbidden not in joined, forbidden


# ------------------------------------------------- end to end in the loop


def gate_scenario(tmp_path, probe_cmd, monkeypatch, diagnosis_reply,
                  config=None, ceiling=3600):
    """A task parked on a gate, with the supervisor's LLM call stubbed."""
    steps = [
        StepSpec(id="verify", prompt="p", criteria=["c"],
                 when=Gate(command=probe_cmd, description="the reviewer",
                           interval=1, ceiling=ceiling)),
    ]
    cfg = {"supervise_after_s": 0, "max_gate_supervisions": 3}
    cfg.update(config or {})
    store, runner, repo = build(
        tmp_path, steps, [Criterion(id="c", command="true")], config=cfg)
    calls = fake_generations(runner, ["exited"] * 4)

    seen = {"prompts": 0}

    def fake_investigate(worktree, contract, step_id, gate, probes,
                         elapsed_s, last_exit, last_output, model,
                         timeout=600):
        seen["prompts"] += 1
        return supervisor.parse_reply(
            diagnosis_reply if isinstance(diagnosis_reply, str)
            else diagnosis_reply(seen["prompts"]))

    monkeypatch.setattr(supervisor, "investigate", fake_investigate)
    return store, runner, calls, seen


def test_stalled_gate_is_repaired_and_the_job_proceeds(tmp_path, monkeypatch):
    """The whole point: First Mate notices the gate is the problem, fixes
    it, and finishes the job without asking the operator."""
    # A realistic corrected probe: wait on the terminal check state (which
    # here is already satisfied) instead of a review row that never appears.
    flag = tmp_path / "pr-checks-green"
    fixed = f"test -f {flag}"
    flag.write_text("SUCCESS\n")
    store, runner, calls, seen = gate_scenario(
        tmp_path, "test -f cubic-review-for-head", monkeypatch,
        reply(new_command=fixed))

    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    task = store.load_task("t1")
    assert task.status == "done", "the job completed on its own"
    assert calls == ["verify"], "the step ran once the gate was fixed"
    assert seen["prompts"] == 1

    # The contract now carries the repaired probe...
    contract = store.load_contract("t1")
    assert contract.steps[0].when.command == fixed
    # ...with the description and envelope preserved.
    assert contract.steps[0].when.description == "the reviewer"
    assert any(a.get("by") == "supervisor" for a in contract.amendments)

    # ...the operator was told, but not stopped.
    qs = store.list_questions(task_id="t1")
    repair = [q for q in qs if q.evidence.get("old_command")]
    assert len(repair) == 1
    assert repair[0].type == "fyi" and repair[0].status == "noted"
    assert not [q for q in qs if q.status == "open"]
    assert repair[0].evidence["old_command"] == "test -f cubic-review-for-head"
    assert repair[0].evidence["new_command"] == fixed

    events = [e["event"] for e in store.events_tail("t1", 300)]
    assert "gate_supervising" in events and "gate_repaired" in events
    # The reasoning is on disk for the operator to audit.
    assert (store.step_dir("t1", "verify") / "gate-diagnosis-1.md").exists()


def test_a_repair_that_does_not_actually_open_the_gate_is_rejected(
        tmp_path, monkeypatch):
    """A confident diagnosis whose new probe still fails is not a fix.
    Applying it would move the goalposts on nothing but confidence."""
    store, runner, calls, seen = gate_scenario(
        tmp_path, "false", monkeypatch,
        reply(new_command="false"),  # still red
        config={"max_gate_supervisions": 1}, ceiling=8)

    asyncio.run(asyncio.wait_for(runner.run(), timeout=25))

    task = store.load_task("t1")
    # It kept waiting, then hit the ceiling and asked — never ran the step.
    assert task.status == "blocked"
    assert calls == []
    contract = store.load_contract("t1")
    assert contract.steps[0].when.command == "false", "original gate restored"
    events = [e["event"] for e in store.events_tail("t1", 300)]
    assert "gate_repair_rejected" in events
    assert "gate_repaired" not in events


def test_still_waiting_verdict_leaves_the_gate_alone(tmp_path, monkeypatch):
    flag = tmp_path / "ready"
    store, runner, calls, seen = gate_scenario(
        tmp_path, f"test -f {flag}", monkeypatch,
        reply(verdict="still_waiting", new_command=""))

    async def scenario():
        loop = asyncio.create_task(runner.run())
        for _ in range(60):
            await asyncio.sleep(0.05)
            if seen["prompts"] >= 1:
                break
        assert store.load_task("t1").status == "waiting"
        assert store.load_contract("t1").steps[0].when.command.startswith("test -f")
        flag.write_text("go")  # the world catches up on its own
        await asyncio.wait_for(loop, timeout=15)

    asyncio.run(scenario())
    assert store.load_task("t1").status == "done"
    assert calls == ["verify"]
    # No gate-repair FYI: nothing was changed, so there is nothing to
    # report. (A finished task always gets a cleanup notice; that's not this.)
    assert not [q for q in store.list_questions(task_id="t1")
                if "gate" in q.question.lower()]


def test_supervision_is_bounded_and_the_escalation_carries_findings(
        tmp_path, monkeypatch):
    """When the supervisor can't crack it, the operator gets what it
    learned rather than a bare 'still waiting'."""
    store, runner, calls, seen = gate_scenario(
        tmp_path, "false", monkeypatch,
        reply(verdict="cannot_tell", new_command="",
              findings="checks are green but I could not match the review"),
        config={"max_gate_supervisions": 2, "supervise_after_s": 0}, ceiling=12)

    asyncio.run(asyncio.wait_for(runner.run(), timeout=25))

    task = store.load_task("t1")
    assert task.status == "blocked"
    st = task.step_state("verify")
    assert st.gate.supervisions <= 2, "bounded, not once per probe"
    q = [q for q in store.list_questions(task_id="t1") if q.status == "open"][0]
    assert "supervisor checked and reported" in q.question
    assert "could not match the review" in q.question
    assert "run the step anyway" in q.options


def test_supervision_can_be_switched_off(tmp_path, monkeypatch):
    store, runner, calls, seen = gate_scenario(
        tmp_path, "false", monkeypatch, reply(),
        config={"supervise_gates": False, "supervise_after_s": 0}, ceiling=4)

    asyncio.run(asyncio.wait_for(runner.run(), timeout=25))

    assert seen["prompts"] == 0
    assert store.load_task("t1").status == "blocked"


def test_should_supervise_waits_before_firing():
    """A gate that is merely slow must not be second-guessed immediately."""
    runner = TaskRunner.__new__(TaskRunner)
    runner.config = {"supervise_gates": True, "supervise_after_s": 300,
                     "max_gate_supervisions": 3}
    gate = Gate(command="x", interval=60, ceiling=3600)
    st = StepState(id="s")
    from firstmate.models import GateState

    # No gate state yet, or barely started: no.
    assert not runner._should_supervise(st, gate)
    st.gate = GateState(first_probe_at="2020-01-01T00:00:00+00:00", probes=99)
    # Long-stalled and never looked at: yes.
    assert runner._should_supervise(st, gate)
    # Just looked at: no (spacing).
    st.gate.supervised_at_probe = 99
    assert not runner._should_supervise(st, gate)
    # Cap reached: no.
    st.gate.supervised_at_probe = 0
    st.gate.supervisions = 3
    assert not runner._should_supervise(st, gate)


def test_short_ceiling_is_supervised_before_it_expires():
    """A 4-minute gate must not sail past a 5-minute supervision threshold
    and expire un-investigated."""
    runner = TaskRunner.__new__(TaskRunner)
    runner.config = {"supervise_gates": True, "supervise_after_s": 300,
                     "max_gate_supervisions": 3}
    from firstmate.models import GateState
    st = StepState(id="s")
    st.gate = GateState(first_probe_at="2020-01-01T00:00:00+00:00", probes=99)
    assert runner._should_supervise(st, Gate(command="x", interval=10,
                                             ceiling=240))


# ------------------------------------------ unsatisfiable criteria


def creply(**kw):
    body = {"verdict": "unsatisfiable", "criterion_id": "cubic_clean",
            "findings": "PR 493 is MERGED; no review row can be created for a "
                        "closed PR, so this check can never pass",
            "reasoning": "r", "suggestion": "assert the check run instead",
            "confidence": "high"}
    body.update(kw)
    return json.dumps(body)


def test_unsatisfiable_needs_evidence():
    """A claim that a check can NEVER pass has to be evidenced — otherwise
    it is just a way to give up."""
    d = supervisor.parse_criterion_reply(creply(findings=""))
    assert d.errors and not d.blocks_loop


def test_low_confidence_unsatisfiable_does_not_stop_the_loop():
    """Looping is cheap; stopping wrongly is not. A hedged 'impossible' is
    not a good enough reason to stop trying."""
    d = supervisor.parse_criterion_reply(creply(confidence="low"))
    assert not d.errors
    assert not d.blocks_loop
    assert supervisor.parse_criterion_reply(creply(confidence="medium")).blocks_loop


def test_needs_more_work_keeps_the_loop_going():
    d = supervisor.parse_criterion_reply(creply(verdict="needs_more_work"))
    assert not d.errors and not d.blocks_loop


def test_criterion_reply_rejects_junk_and_unknown_verdicts():
    assert supervisor.parse_criterion_reply("nope").errors
    assert supervisor.parse_criterion_reply(creply(verdict="broken")).errors


def test_criterion_prompt_forbids_editing_the_criterion():
    """The supervisor may say a check is unsatisfiable and say what it
    thinks it meant — it may not change it. That stays the operator's."""
    c = contract_with_gate()

    class R:
        id, command, exit_status = "cubic_clean", "gh api ... | grep -q x", 1
        error, stdout, stderr = None, "", ""

    p = supervisor.build_criterion_prompt(c, "verify", [R()], iteration=1,
                                          max_iterations=5)
    assert "cubic_clean" in p and "gh api" in p
    assert "round 2 of 5" in p
    assert "only they may alter it" in p
    assert "NOT applying that change" in p
    # And it must be steered against crying wolf.
    assert "impossible" in p and "NOT merely" in p


def test_there_is_no_verdict_that_edits_a_criterion():
    """Structural guarantee: the vocabulary itself offers no way to say
    'here is a better criterion, apply it'."""
    assert supervisor.CRITERION_VERDICTS == {
        "unsatisfiable", "needs_more_work", "cannot_tell"}
    assert not hasattr(supervisor.CriterionDiagnosis(), "new_command")
    # apply_gate_repair is the only mutator, and it only writes `when`.
    assert not hasattr(supervisor, "apply_criterion_repair")


def test_unsatisfiable_criterion_escalates_instead_of_looping(
        tmp_path, monkeypatch):
    """The real case: the PR merged, so the criterion's premise is dead.
    Looping five times would just delay telling the operator."""
    from firstmate.models import Criterion as C

    steps = [
        StepSpec(id="fix", prompt="p", criteria=[]),
        StepSpec(id="verify", prompt="p", criteria=["cubic_clean"],
                 on_failure=LoopBack(goto="fix", max_iterations=5)),
    ]
    store, runner, repo = build(
        tmp_path, steps, [C(id="cubic_clean", command="echo same; exit 1")],
        config={"supervise_criteria": True})
    calls = fake_generations(runner, ["exited"] * 40)

    seen = {"n": 0}

    def fake(worktree, contract, step_id, failures, model, iteration=0,
             max_iterations=0, timeout=600):
        seen["n"] += 1
        return supervisor.parse_criterion_reply(creply())

    monkeypatch.setattr(supervisor, "investigate_criteria", fake)

    asyncio.run(asyncio.wait_for(runner.run(), timeout=25))

    task = store.load_task("t1")
    assert task.status == "blocked"
    st = task.step_state("verify")
    # One loop round happened (giving the work a fair chance), then it
    # stopped rather than burning all five.
    assert st.iteration == 1, st.iteration
    assert seen["n"] == 1

    q = store.list_questions(task_id="t1", status="open")[0]
    assert "cannot pass its criteria" in q.question
    assert "cubic_clean" in q.question
    assert "MERGED" in q.question
    # The operator gets the supervisor's reasoning and its suggestion,
    # and is NOT offered a pointless "loop again".
    assert q.evidence["unsatisfiable"] is True
    assert q.evidence["criterion_id"] == "cubic_clean"
    assert "check run" in q.evidence["supervisor_suggestion"]
    assert "loop again" not in q.options
    assert "accept and continue" in q.options

    # The criterion itself is untouched — only the operator may change it.
    assert store.load_contract("t1").criterion("cubic_clean").command \
        == "echo same; exit 1"
    events = [e["event"] for e in store.events_tail("t1", 300)]
    assert "criteria_supervising" in events and "criteria_diagnosed" in events
    assert (store.step_dir("t1", "verify")
            / "criteria-diagnosis-1.md").exists()


def test_needs_more_work_lets_the_loop_converge(tmp_path, monkeypatch):
    """A sound-but-failing check must not be mistaken for an impossible
    one — the loop has to still be able to do its job."""
    from firstmate.models import Criterion as C

    marker = tmp_path / "rounds"
    marker.write_text("0")
    steps = [
        StepSpec(id="fix", prompt="p", criteria=[]),
        StepSpec(id="verify", prompt="p", criteria=["ok"],
                 on_failure=LoopBack(goto="fix", max_iterations=5)),
    ]
    store, runner, repo = build(
        tmp_path, steps, [C(id="ok", command=f'test "$(cat {marker})" -ge 2')],
        config={"supervise_criteria": True})
    calls = fake_generations(runner, ["exited"] * 40)
    inner = runner._run_generation

    async def counting(task, contract, spec, st, handoff):
        if spec.id == "fix":
            marker.write_text(str(int(marker.read_text()) + 1))
        return await inner(task, contract, spec, st, handoff)

    runner._run_generation = counting
    monkeypatch.setattr(
        supervisor, "investigate_criteria",
        lambda *a, **k: supervisor.parse_criterion_reply(
            creply(verdict="needs_more_work")))

    asyncio.run(asyncio.wait_for(runner.run(), timeout=25))

    assert store.load_task("t1").status == "done"
    assert not store.list_questions(task_id="t1", status="open")


def test_criteria_supervision_is_skipped_without_a_loop_edge(
        tmp_path, monkeypatch):
    """No loop edge means the next stop is the operator either way, so the
    LLM call would buy nothing."""
    from firstmate.models import Criterion as C

    steps = [StepSpec(id="s", prompt="p", criteria=["c"])]
    store, runner, repo = build(tmp_path, steps, [C(id="c", command="exit 1")],
                                config={"supervise_criteria": True})
    fake_generations(runner, ["exited"] * 10)
    seen = {"n": 0}

    def fake(*a, **k):
        seen["n"] += 1
        return supervisor.parse_criterion_reply(creply())

    monkeypatch.setattr(supervisor, "investigate_criteria", fake)

    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    assert seen["n"] == 0
    assert store.load_task("t1").status == "blocked"


def test_criteria_supervision_waits_for_a_non_progressing_round(
        tmp_path, monkeypatch):
    """While each round changes the failure, the work is evidently moving —
    don't spend an LLM call second-guessing it."""
    from firstmate.models import Criterion as C

    n = tmp_path / "n"
    n.write_text("0")
    steps = [
        StepSpec(id="fix", prompt="p", criteria=[]),
        StepSpec(id="verify", prompt="p", criteria=["c"],
                 on_failure=LoopBack(goto="fix", max_iterations=3)),
    ]
    # Different output each evaluation → never the same signature twice.
    store, runner, repo = build(
        tmp_path, steps,
        [C(id="c", command=f'v=$(cat {n}); echo $((v+1)) > {n}; echo "r $v"; exit 1')],
        config={"supervise_criteria": True})
    fake_generations(runner, ["exited"] * 40)
    seen = {"n": 0}

    def fake(*a, **k):
        seen["n"] += 1
        return supervisor.parse_criterion_reply(creply())

    monkeypatch.setattr(supervisor, "investigate_criteria", fake)

    asyncio.run(asyncio.wait_for(runner.run(), timeout=25))

    assert seen["n"] == 0, "a progressing loop is left alone"
    assert store.load_task("t1").status == "blocked"
    assert store.load_task("t1").step_state("verify").iteration == 3
