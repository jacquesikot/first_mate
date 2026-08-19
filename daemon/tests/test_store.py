"""State store — files as source of truth, SQLite as rebuildable index."""

import json

from firstmate.models import Contract, Question, new_id
from firstmate.store import Store
from firstmate.validation import CriterionResult

CONTRACT = Contract.from_dict({
    "goal": "store test task",
    "repo": "/tmp/repo",
    "steps": [{"id": "s1", "prompt": "p", "criteria": ["c1"]}],
    "criteria": [{"id": "c1", "command": "true"}],
})


def test_create_task_writes_files_and_index(tmp_path):
    store = Store(tmp_path)
    task = store.create_task(CONTRACT)
    d = store.task_dir(task.id)
    assert (d / "task.json").exists()
    assert (d / "contract.json").exists()
    assert "store test task" in (d / "contract.md").read_text()
    assert (d / "events.jsonl").exists()
    rows = store.list_tasks()
    assert [r["id"] for r in rows] == [task.id]
    loaded = store.load_task(task.id)
    assert loaded.branch == f"fm/{task.id}"
    assert loaded.steps[0].status == "pending"


def test_questions_answer_and_amendment(tmp_path):
    store = Store(tmp_path)
    task = store.create_task(CONTRACT)
    q = Question(id=new_id("q"), task_id=task.id, step_id="s1",
                 type="decision", question="Which way?", options=["a", "b"])
    store.save_question(q)
    assert [x.id for x in store.list_questions(status="open")] == [q.id]
    answered = store.answer_question(q.id, "a", "test")
    assert answered.status == "answered" and answered.answer == "a"
    contract = store.load_contract(task.id)
    assert contract.amendments and contract.amendments[0]["answer"] == "a"
    # first-write-wins: second answer raises
    try:
        store.answer_question(q.id, "b", "test")
        assert False, "expected ValueError"
    except ValueError:
        pass
    assert store.list_questions(status="open") == []


def test_events_and_tail(tmp_path):
    store = Store(tmp_path)
    task = store.create_task(CONTRACT)
    for i in range(5):
        store.append_event(task.id, f"e{i}", step_id="s1", data={"i": i})
    tail = store.events_tail(task.id, n=3)
    assert [e["event"] for e in tail] == ["e2", "e3", "e4"]


def test_reindex_rebuilds_from_files(tmp_path):
    store = Store(tmp_path)
    task = store.create_task(CONTRACT)
    q = Question(id=new_id("q"), task_id=task.id, type="fyi", question="note")
    q.status = "noted"
    store.save_question(q)
    store.db.close()
    (tmp_path / "firstmate.db").unlink()
    fresh = Store(tmp_path)  # reindexes in __init__
    assert [r["id"] for r in fresh.list_tasks()] == [task.id]
    assert [x.id for x in fresh.list_questions(task_id=task.id)] == [q.id]
    assert fresh.load_question(q.id).question == "note"


def test_step_artifacts_and_latest_handoff(tmp_path):
    store = Store(tmp_path)
    task = store.create_task(CONTRACT)
    assert store.latest_handoff(task.id, "s1") is None
    store.save_step_artifact(task.id, "s1", "handoff-gen2.md", "second")
    store.save_step_artifact(task.id, "s1", "handoff-gen10.md", "tenth")
    gen, text = store.latest_handoff(task.id, "s1")
    assert gen == 10 and text.strip() == "tenth"


def test_save_validation_evidence(tmp_path):
    store = Store(tmp_path)
    task = store.create_task(CONTRACT)
    results = [CriterionResult(id="c1", passed=False, command="false",
                               exit_status=1, stderr="boom")]
    path = store.save_validation(task.id, "s1", 1, results)
    payload = json.loads(path.read_text())
    assert payload["results"][0]["stderr"] == "boom"
    task_level = store.save_validation(task.id, None, 0, results)
    assert task_level.name == "validation.json"


def test_memory(tmp_path):
    store = Store(tmp_path)
    assert store.memory_for_project("proj") is None
    store.remember("proj", "the API client retries twice")
    text = store.memory_for_project("proj")
    assert "retries twice" in text


def test_config_defaults_written(tmp_path):
    store = Store(tmp_path)
    cfg = store.config()
    assert cfg["max_workers"] == 3
    assert (tmp_path / "config.json").exists()
    # user override wins
    (tmp_path / "config.json").write_text(json.dumps({"max_workers": 7}))
    assert store.config()["max_workers"] == 7
    assert store.config()["port"] == 8787  # defaults still filled in
