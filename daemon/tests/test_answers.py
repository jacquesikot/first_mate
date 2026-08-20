"""Answering questions: fingerprint reuse, refusals, structured actions,
and re-planning.

These cover the "stop bothering me about the same thing" and "my answer
should actually change something" behaviours (decision log 2026-08-20).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from firstmate.models import (
    Contract, Criterion, Question, StepSpec, question_fingerprint,
)
from firstmate.server import create_app, needs_replan
from firstmate.store import Store


@pytest.fixture()
def client(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = Store(tmp_path / "home")
    app = create_app(store, autostart=False)
    with TestClient(app) as c:
        c.repo = str(repo)
        c.store = store
        yield c


def make_task(client, steps=None, criteria=None):
    contract = {
        "goal": "g", "repo": client.repo,
        "scope_in": ["src/**"],
        "steps": steps or [{"id": "s1", "prompt": "do the thing",
                            "criteria": ["c1"]}],
        "criteria": criteria or [{"id": "c1", "command": "true"}],
    }
    r = client.post("/tasks", json={"contract": contract})
    assert r.status_code == 200, r.text
    return r.json()["task"]["id"]


def ask(client, tid, **kw):
    body = {"task_id": tid, "step_id": "s1", "type": "scope_change",
            "question": "may I?", "options": ["allow", "deny"]}
    body.update(kw)
    return client.post("/internal/ask", json=body)


# ------------------------------------------------------- fingerprinting


def test_fingerprint_keys_on_the_situation_not_the_wording():
    """Three generations phrase the same block three ways; it's one decision."""
    ev = {"paths": ["ENG-652-PLAN.md"]}
    a = question_fingerprint("scope_change", ev, "May I write the plan file?")
    b = question_fingerprint("scope_change", ev, "The guard blocks ENG-652-PLAN.md — allow?")
    assert a == b
    # A different path is a different decision.
    assert a != question_fingerprint("scope_change", {"paths": ["OTHER.md"]}, "x")
    # With no structured evidence, normalized prose is the fallback — this
    # is what catches the escalation ladder re-asking verbatim.
    assert (question_fingerprint("decision", {}, "Same   question?")
            == question_fingerprint("decision", None, "same question?"))
    assert (question_fingerprint("decision", {}, "one thing?")
            != question_fingerprint("decision", {}, "another thing?"))


def test_equivalent_question_reuses_the_prior_answer(client):
    """The ENG-652 loop: the operator answered once, so the next generation
    must be told the answer rather than re-asking them."""
    tid = make_task(client)
    r1 = ask(client, tid, question="May I write ENG-652-PLAN.md?",
             evidence={"paths": ["ENG-652-PLAN.md"]})
    q1 = r1.json()
    assert q1["status"] == "parked"
    client.post(f"/questions/{q1['id']}/answer",
                json={"answer": "no, just write to the linear issue"})

    # A later generation hits the same block, wording it differently.
    r2 = ask(client, tid, question="The scope guard blocks the plan artifact "
                                   "at the repo root. Allow it?",
             evidence={"paths": ["ENG-652-PLAN.md"]})
    body = r2.json()
    assert body["status"] == "answered", "must not park the task again"
    assert body["answer"] == "no, just write to the linear issue"
    assert "continue working" in body["message"].lower()

    # The operator's open-question list stays clean.
    open_qs = client.get("/questions?status=open").json()["questions"]
    assert open_qs == []
    events = [e["event"] for e in client.get(f"/tasks/{tid}").json()["events"]]
    assert "question_auto_answered" in events, "the reuse is recorded, not silent"


def test_a_genuinely_new_question_still_parks(client):
    tid = make_task(client)
    r1 = ask(client, tid, evidence={"paths": ["A.md"]})
    client.post(f"/questions/{r1.json()['id']}/answer", json={"answer": "allow"})
    r2 = ask(client, tid, evidence={"paths": ["B.md"]})
    assert r2.json()["status"] == "parked"


# ------------------------------------------------------------- refusals


def test_refusal_edits_the_step_prompt(client):
    """"I said no twice and it kept asking": a refusal has to change the
    instruction that sent the worker at the blocked action."""
    tid = make_task(client)
    r = ask(client, tid, question="May I write ENG-652-PLAN.md at the repo root?",
            evidence={"paths": ["ENG-652-PLAN.md"]})
    client.post(f"/questions/{r.json()['id']}/answer",
                json={"answer": "no, just write to the linear issue, update the plan"})

    contract = client.get(f"/tasks/{tid}").json()["contract"]
    prompt = contract["steps"][0]["prompt"]
    assert "OPERATOR CORRECTION" in prompt
    assert "just write to the linear issue" in prompt
    assert "do not re-ask" in prompt.lower()
    # A refusal must NOT widen scope.
    assert "ENG-652-PLAN.md" not in contract["scope_in"]


def test_allow_still_widens_scope(client):
    tid = make_task(client)
    r = ask(client, tid, evidence={"paths": ["notes.md"]})
    client.post(f"/questions/{r.json()['id']}/answer", json={"answer": "allow"})
    contract = client.get(f"/tasks/{tid}").json()["contract"]
    assert "notes.md" in contract["scope_in"]
    assert "OPERATOR CORRECTION" not in contract["steps"][0]["prompt"]


# ----------------------------------------------------- structured actions


def test_structured_answers_do_not_trigger_a_replan():
    q = Question(id="q", task_id="t", type="decision", question="?",
                 options=["retry", "abandon"])
    assert needs_replan(q, "retry") is False
    assert needs_replan(q, "abandon") is False
    assert needs_replan(q, "Abandon") is False
    assert needs_replan(q, "accept and continue") is False
    # Prose is what re-planning is for.
    assert needs_replan(q, "I am sure I have seen cubic review feedback") is True


def test_accept_and_continue_marks_the_step_done(client):
    tid = make_task(client)
    q = Question(id="q-acc", task_id=tid, step_id="s1", type="decision",
                 urgency="blocking", question="failed twice — proceed?",
                 options=["retry", "accept and continue", "abandon"])
    client.store.save_question(q)
    task = client.store.load_task(tid)
    task.status = "blocked"
    client.store.save_task(task)

    r = client.post("/questions/q-acc/answer",
                    json={"answer": "accept and continue"})
    assert r.status_code == 200, r.text
    task = client.store.load_task(tid)
    assert task.step_state("s1").status == "done"


def test_run_the_step_anyway_drops_the_gate(client):
    tid = make_task(client, steps=[{
        "id": "s1", "prompt": "p", "criteria": ["c1"],
        "when": {"command": "false", "description": "the review"},
    }])
    q = Question(id="q-gate", task_id=tid, step_id="s1", type="decision",
                 urgency="blocking", question="still waiting — proceed?",
                 options=["keep waiting", "run the step anyway", "abandon"])
    client.store.save_question(q)
    task = client.store.load_task(tid)
    task.status = "blocked"
    client.store.save_task(task)

    client.post("/questions/q-gate/answer",
                json={"answer": "run the step anyway"})
    contract = client.store.load_contract(tid)
    assert contract.steps[0].when is None, "the gate was dropped for this step"


def test_keep_waiting_resets_the_wait_allowance(client):
    tid = make_task(client)
    task = client.store.load_task(tid)
    task.status = "blocked"
    from firstmate.models import GateState
    st = task.step_state("s1")
    st.gate = GateState(first_probe_at="2020-01-01T00:00:00+00:00", probes=99)
    st.iteration = 3
    client.store.save_task(task)
    q = Question(id="q-kw", task_id=tid, step_id="s1", type="decision",
                 urgency="blocking", question="still waiting?",
                 options=["keep waiting", "abandon"])
    client.store.save_question(q)

    client.post("/questions/q-kw/answer", json={"answer": "keep waiting"})
    st = client.store.load_task(tid).step_state("s1")
    assert st.gate is None, "the ceiling starts over so the wait can continue"
    assert st.iteration == 0


# --------------------------------------------------------------- replan


def test_replan_parses_and_validates_a_reply():
    from firstmate import replan

    current = Contract(
        goal="g", repo="/tmp",
        steps=[StepSpec(id="fix", prompt="p", criteria=["c"])],
        criteria=[Criterion(id="c", command="true")])
    proposed = current.to_dict()
    proposed["steps"].append({"id": "review", "prompt": "check",
                              "criteria": ["c"],
                              "on_failure": {"goto": "fix",
                                             "max_iterations": 3}})
    reply = json.dumps({"contract": proposed, "summary": "added a review loop"})
    result = replan.parse_reply(current, reply)
    assert result.ok, result.errors
    assert result.summary == "added a review loop"
    assert result.contract.steps[1].on_failure.goto == "fix"
    assert replan.diff_contracts(current, result.contract).strip()


def test_replan_cannot_change_the_goal_or_repo():
    """A failed step is not a mandate to redefine the task."""
    from firstmate import replan

    current = Contract(goal="the real goal", repo="/tmp",
                       steps=[StepSpec(id="s", prompt="p", criteria=["c"])],
                       criteria=[Criterion(id="c", command="true")])
    proposed = current.to_dict()
    proposed["goal"] = "something else entirely"
    proposed["repo"] = "/etc"
    result = replan.parse_reply(
        current, json.dumps({"contract": proposed, "summary": "s"}))
    assert result.ok
    assert result.contract.goal == "the real goal"
    assert result.contract.repo == "/tmp"


def test_replan_rejects_an_unrunnable_contract():
    from firstmate import replan

    current = Contract(goal="g", repo="/tmp",
                       steps=[StepSpec(id="s", prompt="p", criteria=["c"])],
                       criteria=[Criterion(id="c", command="true")])
    proposed = current.to_dict()
    proposed["criteria"] = [{"id": "c", "command": ""}]  # not machine-checkable
    result = replan.parse_reply(
        current, json.dumps({"contract": proposed, "summary": "s"}))
    assert not result.ok
    assert any("machine-checkable" in e for e in result.errors)


def test_replan_handles_junk_replies():
    from firstmate import replan

    current = Contract(goal="g", repo="/tmp",
                       steps=[StepSpec(id="s", prompt="p", criteria=["c"])],
                       criteria=[Criterion(id="c", command="true")])
    assert not replan.parse_reply(current, "I'm afraid I can't do that").ok
    assert not replan.parse_reply(current, '{"summary": "no contract"}').ok
    # Fenced JSON is tolerated.
    ok = replan.parse_reply(
        current,
        "```json\n" + json.dumps({"contract": current.to_dict(),
                                  "summary": "unchanged"}) + "\n```")
    assert ok.ok


def test_free_text_answer_runs_a_replan_and_records_the_diff(client, monkeypatch):
    """The Cubic dead end: prose that used to be logged and ignored now
    edits the contract, and the operator can see exactly what changed."""
    from firstmate import replan as replan_mod

    tid = make_task(client, criteria=[{"id": "c1", "command": "false"}])

    def fake_request_replan(worktree, contract, situation, answer, model,
                            timeout=300):
        edited = contract.to_dict()
        edited["criteria"] = [{"id": "c1", "command": "echo fixed"}]
        return replan_mod.parse_reply(
            contract,
            json.dumps({"contract": edited,
                        "summary": "corrected the check the operator says is wrong"}))

    monkeypatch.setattr(replan_mod, "request_replan", fake_request_replan)

    q = Question(id="q-free", task_id=tid, step_id="s1", type="decision",
                 urgency="blocking",
                 question="Step 's1' failed validation twice: c1: exit 1 — "
                          "how should we proceed?",
                 options=["retry", "accept and continue", "abandon"],
                 evidence={"failing": [{"id": "c1", "command": "false"}]})
    client.store.save_question(q)
    task = client.store.load_task(tid)
    task.status = "blocked"
    client.store.save_task(task)

    r = client.post("/questions/q-free/answer",
                    json={"answer": "I am sure I have seen cubic review "
                                    "feedback on that PR — your check is wrong"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["replan"]["applied"] is True
    assert "corrected the check" in out["replan"]["summary"]

    contract = client.store.load_contract(tid)
    assert contract.criterion("c1").command == "echo fixed"
    # Auditable: the before-contract and the diff are both on disk.
    d = client.store.task_dir(tid)
    assert (d / "contract-before-q-free.json").exists()
    assert (d / "replan-q-free.diff").exists()
    assert contract.amendments[-1]["summary"].startswith("corrected the check")
    events = [e["event"] for e in client.get(f"/tasks/{tid}").json()["events"]]
    assert "replan_applied" in events


def test_replan_failure_is_recorded_and_does_not_corrupt_the_contract(
        client, monkeypatch):
    from firstmate import replan as replan_mod

    tid = make_task(client)
    before = client.store.load_contract(tid).to_dict()

    monkeypatch.setattr(
        replan_mod, "request_replan",
        lambda *a, **k: replan_mod.ReplanResult(None, "", ["model exploded"]))

    q = Question(id="q-bad", task_id=tid, step_id="s1", type="decision",
                 urgency="blocking", question="failed — proceed?",
                 options=["retry", "abandon"])
    client.store.save_question(q)
    task = client.store.load_task(tid)
    task.status = "blocked"
    client.store.save_task(task)

    r = client.post("/questions/q-bad/answer",
                    json={"answer": "please figure something else out"})
    assert r.json()["replan"]["applied"] is False
    after = client.store.load_contract(tid).to_dict()
    # Everything that decides what actually RUNS is untouched; only the
    # amendment log grew (the answer is still on the record).
    for key in ("goal", "repo", "steps", "criteria", "scope_in", "scope_out",
                "tripwires", "tripwire_allow"):
        assert after[key] == before[key], key
    assert (client.store.task_dir(tid) / "replan-failed.md").exists()


def test_accepting_a_failing_criterion_waives_it_for_the_whole_task(client):
    """"accept and continue" has to actually settle it. Marking the step
    done while leaving the criterion live means the task-boundary check
    re-runs it and asks the identical question again."""
    tid = make_task(client, criteria=[{"id": "c1", "command": "exit 1"}])
    q = Question(id="q-acc2", task_id=tid, step_id="s1", type="decision",
                 urgency="blocking",
                 question="Step 's1' cannot pass its criteria — c1 asserts "
                          "something that can never become true",
                 options=["accept and continue", "abandon"],
                 evidence={"unsatisfiable": True, "criterion_id": "c1",
                           "failing": [{"id": "c1", "command": "exit 1"}]})
    client.store.save_question(q)
    task = client.store.load_task(tid)
    task.status = "blocked"
    client.store.save_task(task)

    client.post("/questions/q-acc2/answer",
                json={"answer": "accept and continue"})

    contract = client.store.load_contract(tid)
    assert contract.waived_criteria == ["c1"]
    # Not deleted: the contract still records what "done" was meant to be,
    # and the waiver is visible to anyone reading it.
    assert contract.criterion("c1").command == "exit 1"
    assert "WAIVED by the operator" in contract.render_markdown()
    assert client.store.load_task(tid).step_state("s1").status == "done"


def test_a_waived_criterion_is_not_re_run_at_the_boundaries(tmp_path):
    """The waiver has to reach the validation calls, not just the file."""
    import asyncio

    from firstmate.models import Contract, Criterion, StepSpec, StepState, Task
    from firstmate.orchestrator import TaskRunner

    import sys
    sys.path.insert(0, "tests")
    from test_orchestrator import build, fake_generations

    steps = [StepSpec(id="s", prompt="p", criteria=["good", "bad"])]
    crits = [Criterion(id="good", command="true"),
             Criterion(id="bad", command="exit 1")]
    store, runner, repo = build(tmp_path, steps, crits)
    fake_generations(runner, ["exited"] * 4)

    contract = store.load_contract("t1")
    contract.waived_criteria = ["bad"]
    store.save_contract("t1", contract)

    asyncio.run(asyncio.wait_for(runner.run(), timeout=20))

    task = store.load_task("t1")
    assert task.status == "done", "the waived check must not block the task"
    assert not store.list_questions(task_id="t1", status="open")
