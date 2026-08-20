"""The memory loop — extraction, promotion, compaction (PRD §6.6).

Project memory is the only place a worker's hard-won knowledge outlives
the task that produced it, so the failure modes worth testing are the ones
that make it *worse* than empty: filler crowding out real facts, the same
fact written four times by a convergence loop, a decision promoted without
the operator's say-so, and a compaction pass that quietly eats the file.

Hermetic: every LLM call is stubbed. What's under test is the gating, the
parsing, and the guardrails — the parts that decide whether a call happens
at all and whether its answer is allowed to touch the operator's data.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from firstmate import learning
from firstmate.models import (
    Contract, Criterion, Question, StepSpec, StepState, Task,
    question_fingerprint,
)
from firstmate.store import Store

from test_orchestrator import build, fake_generations


# --------------------------------------------------------- struggle gate


def test_a_clean_first_try_step_is_not_worth_a_call():
    """The gate that keeps memory readable. A step that passed on attempt
    one taught nothing, and paying a call per step to be told "write
    tests" is exactly how a memory file becomes noise."""
    assert learning.step_struggled(StepState(id="s")) is False


@pytest.mark.parametrize("field,value", [
    ("attempt", 2),            # failed validation, retried
    ("generation", 2),         # hit the context wall, handed off
    ("iteration", 1),          # a convergence loop fired
    ("criteria_diagnoses", 1),  # the supervisor had to judge it
])
def test_every_kind_of_wall_counts_as_a_struggle(field, value):
    st = StepState(id="s")
    setattr(st, field, value)
    assert learning.step_struggled(st) is True
    # And the prompt is told which wall it was — a retry and a context
    # wall teach different lessons.
    assert learning.struggle_summary(st) != "completed without difficulty"


# ------------------------------------------------------ parsing a reply


def test_null_fact_is_a_complete_answer_not_a_failure():
    """The expected outcome most of the time. It must not read as an
    error, or the logs fill with failures for the system working."""
    res = learning.parse_extraction(
        '{"fact": null, "reason": "nothing durable here"}')
    assert res.ok and res.fact is None
    assert res.reason == "nothing durable here"


def test_a_real_project_fact_survives():
    res = learning.parse_extraction(json.dumps({
        "fact": "Tests run with `pnpm -C dashboard vitest run`; plain "
                "`pnpm test` is not wired up in this repo.",
        "reason": "cost two attempts to discover",
    }))
    assert res.ok and res.fact and "pnpm -C dashboard" in res.fact


def test_generic_advice_never_reaches_memory():
    """Belt-and-braces behind the prompt. If the model ignores the
    instruction to refuse, the platitude still doesn't get written."""
    for filler in ["Always write tests before you refactor the module.",
                   "Be sure to read the error message carefully first.",
                   "It is important to commit small, focused changes."]:
        res = learning.parse_extraction(json.dumps({"fact": filler}))
        assert res.fact is None, filler
        assert "generic" in res.reason


def test_a_one_word_fact_and_an_essay_are_both_rejected():
    assert learning.parse_extraction('{"fact": "pnpm"}').fact is None
    assert learning.parse_extraction(
        json.dumps({"fact": "x " * 400})).fact is None


def test_fenced_json_and_junk_are_handled():
    res = learning.parse_extraction(
        '```json\n{"fact": null, "reason": "none"}\n```')
    assert res.ok and res.fact is None
    bad = learning.parse_extraction("I could not determine a fact, sorry.")
    assert not bad.ok and bad.errors


# ---------------------------------------------------------- deduplication


def test_a_fact_already_in_memory_is_not_written_twice():
    """Memory is append-only, so a convergence loop hitting the same wall
    on four rounds would otherwise leave four near-identical lines."""
    memory = ("# Project memory: repo\n\n"
              "- 2026-08-01T00:00:00+00:00 — Tests run with "
              "`pnpm -C dashboard vitest run`; plain `pnpm test` is not "
              "wired up in this repo.\n")
    # Same fact, different wording — which is what two extraction calls
    # about one fact actually produce.
    assert learning.already_known(
        memory, "In this repo tests run via `pnpm -C dashboard vitest run` "
                "because plain `pnpm test` is not wired up.")
    # An unrelated fact is not suppressed.
    assert not learning.already_known(
        memory, "Migrations must ship as their own PR, merged before the "
                "code PR that depends on them.")


# ------------------------------------------------------------- promotion


def _answered(store, task_id, text, answer, qid):
    """An answered question on a task, as the store would hold it."""
    q = Question(id=qid, task_id=task_id, type="clarification", question=text,
                 answer=answer, status="answered", answered_by="operator",
                 answered_at="2026-08-20T00:00:00+00:00")
    q.fingerprint = question_fingerprint(q.type, q.evidence, q.question)
    store.save_question(q)
    return q


def _task(store, tid):
    t = Task(id=tid, repo="/tmp/repo", branch=f"fm/{tid}", goal="g")
    store.save_task(t)
    return t


def test_one_task_answering_once_is_not_a_project_fact(tmp_path):
    """A decision made on a single task is task-specific until proven
    otherwise. Promoting it on first sight would put guesses in memory."""
    store = Store(tmp_path / "home")
    _task(store, "t1")
    q = _answered(store, "t1", "Which package manager?", "pnpm", "q1")
    assert learning.recurring_answer(store, q) is None


def test_the_same_decision_on_a_second_task_earns_a_suggestion(tmp_path):
    """The actual signal: the operator has now said the same thing while
    working on two different tasks, so it was never task-specific."""
    store = Store(tmp_path / "home")
    _task(store, "t1")
    _task(store, "t2")
    _answered(store, "t1", "Which package manager?", "pnpm", "q1")
    q2 = _answered(store, "t2", "Which package manager?", "pnpm", "q2")
    promo = learning.recurring_answer(store, q2)
    assert promo is not None
    assert promo.occurrences == 2
    assert promo.task_ids == ["t1", "t2"]
    # The operator's own words, not a paraphrase — no LLM call here.
    assert "pnpm" in promo.fact and "Which package manager?" in promo.fact


def test_an_auto_answered_echo_cannot_promote_itself(tmp_path):
    """Within-task re-asks are auto-answered from the earlier answer. If
    those counted, one operator answer would manufacture its own
    corroboration and promote itself."""
    store = Store(tmp_path / "home")
    _task(store, "t1")
    _answered(store, "t1", "Which package manager?", "pnpm", "q1")
    echo = Question(id="q2", task_id="t1", type="clarification",
                    question="Which package manager?", answer="pnpm",
                    status="answered", answered_from="q1")
    echo.fingerprint = question_fingerprint(echo.type, {}, echo.question)
    store.save_question(echo)
    assert learning.recurring_answer(store, echo) is None


def test_many_answers_on_one_task_still_count_once(tmp_path):
    """One operator, one decision, one vote — however many times a task's
    generations managed to ask it."""
    store = Store(tmp_path / "home")
    _task(store, "t1")
    _answered(store, "t1", "Which package manager?", "pnpm", "q1")
    q2 = _answered(store, "t1", "Which package manager?", "pnpm", "q2")
    assert learning.recurring_answer(store, q2) is None


def test_fyi_notices_are_never_promoted(tmp_path):
    store = Store(tmp_path / "home")
    _task(store, "t1")
    for tid, qid in (("t1", "q1"), ("t1", "q2")):
        q = Question(id=qid, task_id=tid, type="fyi", question="disk is full",
                     answer="ok", status="answered")
        q.fingerprint = question_fingerprint("fyi", {}, q.question)
        store.save_question(q)
    assert learning.recurring_answer(store, q) is None


# ------------------------------------------------------------ compaction


def _memfile(n: int) -> str:
    lines = [f"- 2026-08-{i % 28 + 1:02d}T00:00:00+00:00 — fact number {i}"
             for i in range(n)]
    return "# Project memory: repo\n\n" + "\n".join(lines) + "\n"


def test_a_consolidating_rewrite_is_accepted():
    before = _memfile(10)
    after = _memfile(6)
    res = learning.parse_compaction(before, after)
    assert res.ok and res.text
    assert res.after_bytes < res.before_bytes


def test_a_rewrite_that_gutted_the_file_is_rejected(tmp_path):
    """The one path that can reduce the operator's notes is the one that
    has to be suspicious. A large memory file is a mild problem; a
    silently truncated one is their notes gone."""
    before = _memfile(20)
    res = learning.parse_compaction(before, _memfile(3))
    assert not res.ok
    assert "dropped too much" in res.errors[0]


def test_an_apology_instead_of_a_file_is_rejected():
    res = learning.parse_compaction(
        _memfile(10), "I'm sorry, I can't consolidate this file.")
    assert not res.ok


def test_an_empty_reply_is_rejected():
    assert not learning.parse_compaction(_memfile(5), "   ").ok


def test_compaction_archives_before_it_overwrites(tmp_path):
    """A lossy rewrite of the operator's own notes must always be
    recoverable — so the archive is written from the pre-compaction text,
    not from whatever ends up on disk afterwards."""
    store = Store(tmp_path / "home")
    store.remember("repo", "the original fact")
    original = store.memory_for_project("repo")
    archived = store.archive_memory("repo")
    store.write_memory("repo", "# Project memory: repo\n\n- consolidated\n")
    assert archived is not None and archived.read_text() == original
    assert "consolidated" in store.memory_for_project("repo")
    listing = store.list_memory_archive("repo")
    assert listing and listing[0]["project"] == "repo"


def test_the_archive_never_shows_up_as_a_project(tmp_path):
    """archive/ and suggestions/ live under memory/ — they must not read
    as projects with memory files of their own."""
    store = Store(tmp_path / "home")
    store.remember("repo", "a fact")
    store.archive_memory("repo")
    assert [p["project"] for p in store.list_memory()] == ["repo"]


# ---------------------------------------------- suggestions on the store


def test_a_dismissed_suggestion_never_comes_back(tmp_path):
    """"Don't remember that" is itself a decision. Re-offering it is the
    exact nagging that fingerprinting exists to stop."""
    store = Store(tmp_path / "home")
    sug = {"id": "sug1", "status": "pending", "project": "repo",
           "fingerprint": "abc123", "fact": "f", "created_at": "now"}
    store.save_suggestion(sug)
    store.resolve_suggestion("sug1", "dismissed")
    assert store.list_suggestions(status="pending") == []
    # Still found by fingerprint, which is what suppresses the re-offer.
    found = store.suggestion_for_fingerprint("abc123")
    assert found is not None and found["status"] == "dismissed"


def test_suggestions_survive_a_restart(tmp_path):
    """On disk, not in memory: a pending suggestion the daemon never got
    round to showing must still be there after a restart."""
    store = Store(tmp_path / "home")
    store.save_suggestion({"id": "sug1", "status": "pending",
                           "project": "repo", "fingerprint": "f1",
                           "fact": "a standing fact", "created_at": "now"})
    reopened = Store(tmp_path / "home")
    pending = reopened.list_suggestions(status="pending")
    assert [s["fact"] for s in pending] == ["a standing fact"]


# ------------------------------------------- extraction in the real loop


def _stub_extraction(monkeypatch, fact, reason="because"):
    calls = []

    def _fake(worktree, step_id, goal, prompt, struggle, evidence, model,
              timeout=180):
        calls.append({"step_id": step_id, "struggle": struggle,
                      "model": model, "goal": goal})
        return learning.Extraction(fact, reason)

    monkeypatch.setattr(learning, "request_extraction", _fake)
    return calls


def test_a_struggling_step_writes_what_it_learned(tmp_path, monkeypatch):
    """End to end through the real orchestrator loop: the step fails its
    criteria once, passes on the retry, and the lesson lands in the
    project's memory file where the next task will see it."""
    marker = tmp_path / "pass"
    steps = [StepSpec(id="s1", prompt="do the thing", criteria=["c"])]
    crit = [Criterion(id="c", command=f"test -f {marker}")]
    store, runner, repo = build(tmp_path, steps, crit)
    calls = _stub_extraction(
        monkeypatch,
        "Integration tests need DATABASE_URL pointing at the docker compose "
        "postgres, not the system one.")

    seq = {"n": 0}

    async def _gen(task, contract, spec, st, handoff):
        seq["n"] += 1
        if seq["n"] == 2:  # the retry makes the criterion pass
            marker.write_text("x")
        from firstmate.models import SessionRecord
        st.sessions.append(SessionRecord(
            session_id=f"s{seq['n']}", generation=st.generation,
            attempt=st.attempt, started_at="n", ended_at="n",
            outcome="exited"))
        return "exited", None

    runner._run_generation = _gen  # type: ignore[assignment]
    asyncio.run(runner.run())

    assert store.load_task("t1").status == "done"
    assert len(calls) == 1
    assert "failed its criteria and retried" in calls[0]["struggle"]
    memory = store.memory_for_project(Path(repo).name) or ""
    assert "DATABASE_URL" in memory
    # Provenance: which task and step taught it.
    assert "t1/s1" in memory
    events = [e["event"] for e in store.events_tail("t1", 200)]
    assert "learning_recorded" in events


def test_a_clean_step_costs_no_call_at_all(tmp_path, monkeypatch):
    steps = [StepSpec(id="s1", prompt="p", criteria=["c"])]
    store, runner, repo = build(tmp_path, steps,
                                [Criterion(id="c", command="true")])
    calls = _stub_extraction(monkeypatch, "a fact that should never be asked for")
    fake_generations(runner, ["exited"])
    asyncio.run(runner.run())
    assert store.load_task("t1").status == "done"
    assert calls == []
    assert store.memory_for_project(Path(repo).name) is None


def test_nothing_learned_is_recorded_as_having_looked(tmp_path, monkeypatch):
    """"We looked and there was nothing" must be distinguishable from
    "we never looked" — otherwise a silently broken extraction path is
    invisible."""
    marker = tmp_path / "pass"
    steps = [StepSpec(id="s1", prompt="p", criteria=["c"])]
    store, runner, repo = build(tmp_path, steps,
                                [Criterion(id="c", command=f"test -f {marker}")])
    _stub_extraction(monkeypatch, None, reason="nothing project-specific")

    seq = {"n": 0}

    async def _gen(task, contract, spec, st, handoff):
        seq["n"] += 1
        if seq["n"] == 2:
            marker.write_text("x")
        from firstmate.models import SessionRecord
        st.sessions.append(SessionRecord(
            session_id="s", generation=st.generation, attempt=st.attempt,
            started_at="n", ended_at="n", outcome="exited"))
        return "exited", None

    runner._run_generation = _gen  # type: ignore[assignment]
    asyncio.run(runner.run())
    assert store.memory_for_project(Path(repo).name) is None
    assert "learning_none" in [e["event"] for e in store.events_tail("t1", 200)]


def test_a_broken_extraction_never_fails_a_finished_step(tmp_path, monkeypatch):
    """The step is already done and validated. A learning that can't be
    extracted is a missed opportunity, not an error — and certainly not a
    reason to fail a task that succeeded."""
    marker = tmp_path / "pass"
    steps = [StepSpec(id="s1", prompt="p", criteria=["c"])]
    store, runner, repo = build(tmp_path, steps,
                                [Criterion(id="c", command=f"test -f {marker}")])

    def _boom(*a, **k):
        raise RuntimeError("claude is not on PATH")

    monkeypatch.setattr(learning, "request_extraction", _boom)

    seq = {"n": 0}

    async def _gen(task, contract, spec, st, handoff):
        seq["n"] += 1
        if seq["n"] == 2:
            marker.write_text("x")
        from firstmate.models import SessionRecord
        st.sessions.append(SessionRecord(
            session_id="s", generation=st.generation, attempt=st.attempt,
            started_at="n", ended_at="n", outcome="exited"))
        return "exited", None

    runner._run_generation = _gen  # type: ignore[assignment]
    asyncio.run(runner.run())
    assert store.load_task("t1").status == "done"
    assert "learning_skipped" in [e["event"] for e in store.events_tail("t1", 200)]


def test_extraction_can_be_switched_off(tmp_path, monkeypatch):
    marker = tmp_path / "pass"
    steps = [StepSpec(id="s1", prompt="p", criteria=["c"])]
    store, runner, repo = build(tmp_path, steps,
                                [Criterion(id="c", command=f"test -f {marker}")],
                                config={"learn_from_steps": False})
    calls = _stub_extraction(monkeypatch, "a fact")

    seq = {"n": 0}

    async def _gen(task, contract, spec, st, handoff):
        seq["n"] += 1
        if seq["n"] == 2:
            marker.write_text("x")
        from firstmate.models import SessionRecord
        st.sessions.append(SessionRecord(
            session_id="s", generation=st.generation, attempt=st.attempt,
            started_at="n", ended_at="n", outcome="exited"))
        return "exited", None

    runner._run_generation = _gen  # type: ignore[assignment]
    asyncio.run(runner.run())
    assert calls == []


def test_one_task_cannot_flood_memory(tmp_path, monkeypatch):
    """A pathological task that struggles on every step of a long
    contract must not turn the memory file into its own changelog."""
    marker = tmp_path / "pass"
    n_steps = 4
    steps = [StepSpec(id=f"s{i}", prompt="p", criteria=["c"])
             for i in range(n_steps)]
    store, runner, repo = build(
        tmp_path, steps, [Criterion(id="c", command=f"test -f {marker}")],
        config={"max_learnings_per_task": 2})

    # Genuinely unrelated facts — otherwise the near-duplicate check
    # suppresses them and this stops testing the per-task cap.
    facts = iter([
        "Integration tests need DATABASE_URL pointing at the compose postgres.",
        "The staging deploy requires an approval in the GitHub environment.",
        "Playwright specs only pass with the dev server already listening.",
        "Alembic heads must be merged before any migration is generated.",
    ])

    def _fake(*a, **k):
        return learning.Extraction(next(facts), "because")

    monkeypatch.setattr(learning, "request_extraction", _fake)

    # Every step fails once then passes: marker is removed after each
    # step's validation so the next step also has to retry.
    state = {"attempts": 0}

    async def _gen(task, contract, spec, st, handoff):
        state["attempts"] += 1
        if st.attempt >= 2:
            marker.write_text("x")
        elif marker.exists():
            marker.unlink()
        from firstmate.models import SessionRecord
        st.sessions.append(SessionRecord(
            session_id="s", generation=st.generation, attempt=st.attempt,
            started_at="n", ended_at="n", outcome="exited"))
        return "exited", None

    runner._run_generation = _gen  # type: ignore[assignment]
    asyncio.run(runner.run())
    memory = store.memory_for_project(Path(repo).name) or ""
    written = [l for l in memory.splitlines() if l.startswith("- ")]
    assert len(written) == 2, memory


# ------------------------------------------------- through the daemon API

@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from firstmate.server import create_app

    repo = tmp_path / "repo"
    repo.mkdir()
    store = Store(tmp_path / "home")
    app = create_app(store, autostart=False)
    with TestClient(app) as c:
        c.repo = str(repo)
        c.store = store
        yield c


def _api_contract(repo: str, goal: str) -> dict:
    return {
        "goal": goal,
        "repo": repo,
        "steps": [{"id": "s1", "prompt": "p", "criteria": ["c1"]}],
        "criteria": [{"id": "c1", "command": "true"}],
    }


def _ask_and_answer(client, tid, question, answer):
    qid = client.post("/internal/ask", json={
        "task_id": tid, "step_id": "s1", "type": "clarification",
        "question": question,
    }).json()["id"]
    return client.post(f"/questions/{qid}/answer",
                       json={"answer": answer, "by": "operator"})


def test_answering_the_same_thing_on_two_tasks_offers_a_promotion(client):
    """The cross-task half of fingerprinting: within a task the re-ask is
    auto-answered, but across tasks it means the answer was never
    task-specific and should become a standing fact."""
    t1 = client.post("/tasks", json={
        "contract": _api_contract(client.repo, "first task")}).json()["task"]["id"]
    t2 = client.post("/tasks", json={
        "contract": _api_contract(client.repo, "second task")}).json()["task"]["id"]

    r1 = _ask_and_answer(client, t1, "Which package manager does this use?",
                         "pnpm, always")
    assert r1.status_code == 200
    assert "suggestion" not in r1.json()  # one task is not a pattern

    r2 = _ask_and_answer(client, t2, "Which package manager does this use?",
                         "pnpm, always")
    sug = r2.json().get("suggestion")
    assert sug is not None, r2.json()
    assert sug["occurrences"] == 2 and sug["status"] == "pending"
    assert "pnpm" in sug["fact"]

    listed = client.get("/memory-suggestions").json()["suggestions"]
    assert [s["id"] for s in listed] == [sug["id"]]
    # And it is nowhere near memory yet — that needs the operator.
    assert client.get(f"/memory/{Path(client.repo).name}").status_code == 404


def test_accepting_a_suggestion_writes_memory_once(client):
    t1 = client.post("/tasks", json={
        "contract": _api_contract(client.repo, "first")}).json()["task"]["id"]
    t2 = client.post("/tasks", json={
        "contract": _api_contract(client.repo, "second")}).json()["task"]["id"]
    _ask_and_answer(client, t1, "Where do migrations live?", "db/migrations")
    sug = _ask_and_answer(client, t2, "Where do migrations live?",
                          "db/migrations").json()["suggestion"]

    r = client.post(f"/memory-suggestions/{sug['id']}/accept", json={})
    assert r.status_code == 200
    assert "db/migrations" in r.json()["text"]
    # Second accept is refused rather than duplicating the line.
    again = client.post(f"/memory-suggestions/{sug['id']}/accept", json={})
    assert again.status_code == 409
    text = client.get(f"/memory/{Path(client.repo).name}").json()["text"]
    assert text.count("db/migrations") == 1
    assert client.get("/memory-suggestions").json()["suggestions"] == []


def test_the_operator_can_reword_before_it_is_remembered(client):
    """The phrasing was assembled mechanically from their answer — it's
    their file, so they get the last word on the wording."""
    t1 = client.post("/tasks", json={
        "contract": _api_contract(client.repo, "first")}).json()["task"]["id"]
    t2 = client.post("/tasks", json={
        "contract": _api_contract(client.repo, "second")}).json()["task"]["id"]
    _ask_and_answer(client, t1, "Deploy how?", "make deploy")
    sug = _ask_and_answer(client, t2, "Deploy how?",
                          "make deploy").json()["suggestion"]
    r = client.post(f"/memory-suggestions/{sug['id']}/accept",
                    json={"fact": "Deploys go through `make deploy`, never "
                                  "the cloud console."})
    assert "never the cloud console" in r.json()["text"]


def test_dismissing_a_suggestion_stops_it_recurring(client):
    t1 = client.post("/tasks", json={
        "contract": _api_contract(client.repo, "first")}).json()["task"]["id"]
    t2 = client.post("/tasks", json={
        "contract": _api_contract(client.repo, "second")}).json()["task"]["id"]
    t3 = client.post("/tasks", json={
        "contract": _api_contract(client.repo, "third")}).json()["task"]["id"]
    _ask_and_answer(client, t1, "Which port?", "8787")
    sug = _ask_and_answer(client, t2, "Which port?",
                          "8787").json()["suggestion"]
    client.post(f"/memory-suggestions/{sug['id']}/dismiss")
    # A third task answering the same way must not re-offer it.
    r3 = _ask_and_answer(client, t3, "Which port?", "8787")
    assert "suggestion" not in r3.json()
    assert client.get("/memory-suggestions").json()["suggestions"] == []


def test_compaction_offers_itself_only_when_the_file_is_large(client):
    project = Path(client.repo).name
    client.post(f"/memory/{project}", json={"fact": "one small fact"})
    listing = client.get("/memory").json()
    assert listing["projects"][0]["compact_due"] is False
    assert listing["compact_bytes"] > 0
    # Push it over the threshold.
    big = "# Project memory: x\n\n" + "".join(
        f"- 2026-08-01T00:00:00+00:00 — fact {i} padding padding padding\n"
        for i in range(300))
    client.put(f"/memory/{project}", json={"text": big})
    assert client.get("/memory").json()["projects"][0]["compact_due"] is True


def test_compaction_archives_and_rewrites(client, monkeypatch):
    project = Path(client.repo).name
    before = ("# Project memory: x\n\n"
              + "".join(f"- 2026-08-01T00:00:00+00:00 — fact {i}\n"
                        for i in range(10)))
    client.put(f"/memory/{project}", json={"text": before})

    def _fake(cwd, memory, model, timeout=300):
        return learning.parse_compaction(
            memory, "# Project memory: x\n\n"
                    + "".join(f"- 2026-08-01T00:00:00+00:00 — merged {i}\n"
                              for i in range(6)))

    monkeypatch.setattr(learning, "request_compaction", _fake)
    r = client.post(f"/memory/{project}/compact")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "merged 0" in body["text"] and body["after_bytes"] < body["before_bytes"]
    archived = Path(body["archived"])
    assert archived.exists() and "fact 9" in archived.read_text()
    assert client.get("/memory-archive").json()["archive"][0]["project"] == project


def test_a_bad_compaction_leaves_the_file_alone(client, monkeypatch):
    """The rejection has to be visible at the API, not just inside the
    parser — the operator's file must be exactly as it was."""
    project = Path(client.repo).name
    before = ("# Project memory: x\n\n"
              + "".join(f"- 2026-08-01T00:00:00+00:00 — fact {i}\n"
                        for i in range(20)))
    client.put(f"/memory/{project}", json={"text": before})
    monkeypatch.setattr(
        learning, "request_compaction",
        lambda *a, **k: learning.parse_compaction(before, "- one line\n"))
    r = client.post(f"/memory/{project}/compact")
    assert r.status_code == 422
    assert client.get(f"/memory/{project}").json()["text"] == before
    assert client.get("/memory-archive").json()["archive"] == []


def test_suggestions_is_not_read_as_a_project_name(client):
    """Route ordering: a literal path must win over /memory/{project}."""
    r = client.get("/memory-suggestions")
    assert r.status_code == 200 and "suggestions" in r.json()
