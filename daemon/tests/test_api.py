"""Daemon API — hermetic (autostart=False: no workers, tmux, or claude)."""

import pytest
from fastapi.testclient import TestClient

from firstmate.server import create_app
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


def contract(repo: str) -> dict:
    return {
        "goal": "api test task",
        "repo": repo,
        "steps": [{"id": "s1", "prompt": "p", "criteria": ["c1"]}],
        "criteria": [{"id": "c1", "command": "true"}],
    }


def test_health_and_status_empty(client):
    assert client.get("/health").json()["ok"] is True
    data = client.get("/status").json()
    assert data == {"tasks": [], "questions": []}


def test_create_task_and_get(client):
    r = client.post("/tasks", json={"contract": contract(client.repo)})
    assert r.status_code == 200, r.text
    tid = r.json()["task"]["id"]
    assert r.json()["started"] is False
    detail = client.get(f"/tasks/{tid}").json()
    assert detail["task"]["status"] == "ready"
    assert detail["contract"]["goal"] == "api test task"
    assert any(e["event"] == "task_created" for e in detail["events"])
    assert client.get("/tasks").json()["tasks"][0]["id"] == tid


def test_create_task_rejects_bad_contract(client):
    bad = {"goal": "x", "repo": client.repo,
           "steps": [{"id": "s", "prompt": "p", "criteria": ["missing"]}],
           "criteria": [{"id": "c", "command": ""}]}
    r = client.post("/tasks", json={"contract": bad})
    assert r.status_code == 400
    errors = str(r.json())
    assert "machine-checkable" in errors and "missing" in errors


def test_create_task_rejects_missing_repo(client):
    c = contract("/definitely/not/a/repo")
    r = client.post("/tasks", json={"contract": c})
    assert r.status_code == 400
    assert "does not exist" in str(r.json())


def test_ask_park_and_answer_flow(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    r = client.post("/internal/ask", json={
        "task_id": tid, "step_id": "s1", "type": "decision",
        "question": "Which color?", "options": ["red", "blue"], "default": "red",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "parked" and "STOP" in body["message"]
    qid = body["id"]
    open_qs = client.get("/questions", params={"status": "open"}).json()["questions"]
    assert [q["id"] for q in open_qs] == [qid]

    r = client.post(f"/questions/{qid}/answer", json={"answer": "red", "by": "test"})
    assert r.status_code == 200
    assert r.json()["question"]["answer"] == "red"
    # amendment landed in the contract
    detail = client.get(f"/tasks/{tid}").json()
    assert detail["contract"]["amendments"][0]["answer"] == "red"
    # first-write-wins
    r = client.post(f"/questions/{qid}/answer", json={"answer": "blue"})
    assert r.status_code == 409


def test_ask_fyi_does_not_park(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    r = client.post("/internal/ask", json={
        "task_id": tid, "type": "fyi", "question": "assumed X",
    })
    assert r.json()["status"] == "recorded"
    assert client.get("/questions", params={"status": "open"}).json()["questions"] == []


def test_internal_events(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    r = client.post("/internal/events", json={
        "event": "SessionStart", "task_id": tid, "step_id": "s1",
        "payload": {"session_id": "abc"},
    })
    assert r.json()["ok"] is True
    events = client.get(f"/tasks/{tid}").json()["events"]
    assert any(e["event"] == "hook.SessionStart" for e in events)
    # unknown task is not an error for hooks
    r = client.post("/internal/events", json={"event": "Stop", "task_id": "nope"})
    assert r.json()["ok"] is False


def test_lifecycle_endpoints(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    r = client.post(f"/tasks/{tid}/pause")
    assert r.json()["status"] == "paused"
    r = client.post(f"/tasks/{tid}/run")  # paused → ready (autostart off: not started)
    assert r.json()["status"] == "ready"
    r = client.post(f"/tasks/{tid}/abandon")
    assert r.json()["status"] == "abandoned"
    r = client.post(f"/tasks/{tid}/run")
    assert r.status_code == 409


def test_websocket_snapshot(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    with client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
        assert snap["kind"] == "snapshot"
        assert [t["id"] for t in snap["tasks"]] == [tid]
