"""Daemon API — hermetic (autostart=False: no workers, tmux, or claude)."""

from pathlib import Path

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
    assert data["tasks"] == [] and data["questions"] == []
    assert data["config"]["max_workers"]


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


def test_diff_endpoints(client, tmp_path):
    from firstmate.exec import gitops

    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    # No worktree yet → empty, not an error (dashboard is refresh-safe).
    assert client.get(f"/tasks/{tid}/diff").json()["files"] == []

    wt = tmp_path / "wt"
    gitops.init_repo(wt)
    (wt / "tracked.txt").write_text("one\n")
    import subprocess
    subprocess.run(["git", "-C", str(wt), "add", "."], check=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "base"], check=True)
    (wt / "tracked.txt").write_text("one\ntwo\n")
    (wt / "new.txt").write_text("a\nb\nc\n")
    task = client.store.load_task(tid)
    task.worktree = str(wt)
    client.store.save_task(task)

    data = client.get(f"/tasks/{tid}/diff").json()
    byname = {f["path"]: f for f in data["files"]}
    assert byname["tracked.txt"]["added"] == 1
    assert byname["new.txt"]["untracked"] is True and byname["new.txt"]["added"] == 3
    assert data["added"] == 1  # tracked lines only (matches diff tripwires)

    d = client.get(f"/tasks/{tid}/diff/file", params={"path": "tracked.txt"}).json()
    assert "+two" in d["diff"]
    d = client.get(f"/tasks/{tid}/diff/file", params={"path": "new.txt"}).json()
    assert "+a" in d["diff"]


def test_output_endpoint_no_live_session(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    data = client.get(f"/tasks/{tid}/output").json()
    assert data["live"] is False and data["output"] is None


def test_contract_edit_between_steps(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    edited = contract(client.repo)
    edited["goal"] = "edited goal"
    edited["steps"].append({"id": "s2", "prompt": "extra", "criteria": []})
    r = client.put(f"/tasks/{tid}/contract", json={"contract": edited})
    assert r.status_code == 200, r.text
    detail = client.get(f"/tasks/{tid}").json()
    assert detail["contract"]["goal"] == "edited goal"
    assert [s["id"] for s in detail["task"]["steps"]] == ["s1", "s2"]
    assert any(e["event"] == "contract_edited" for e in detail["events"])
    # invalid edits are rejected by the same machine-checkability gate
    bad = contract(client.repo)
    bad["criteria"][0]["command"] = ""
    assert client.put(f"/tasks/{tid}/contract", json={"contract": bad}).status_code == 400
    # terminal tasks are immutable
    client.post(f"/tasks/{tid}/abandon")
    assert client.put(f"/tasks/{tid}/contract",
                      json={"contract": edited}).status_code == 409


def test_memory_endpoints(client):
    assert client.get("/memory").json()["projects"] == []
    r = client.post("/memory/proj", json={"fact": "the tests live in tests/"})
    assert r.status_code == 200
    assert "the tests live in tests/" in r.json()["text"]
    listing = client.get("/memory").json()["projects"]
    assert listing[0]["project"] == "proj" and listing[0]["entries"] == 1
    r = client.get("/memory/proj")
    assert "Project memory: proj" in r.json()["text"]
    r = client.put("/memory/proj", json={"text": "# rewritten\n\n- kept\n"})
    assert r.json()["text"].startswith("# rewritten")
    assert client.get("/memory/nope").status_code == 404
    assert client.post("/memory/..sneaky", json={"fact": "x"}).status_code == 400
    assert client.put("/memory/proj", json={"text": "  "}).status_code == 400


def test_root_without_dashboard_build(client, monkeypatch):
    # The client fixture app was created without FM_DASHBOARD_DIST; if the
    # repo has a real dashboard build the mount exists — accept either.
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (200, 307)


def test_fs_browse_and_repos(client, tmp_path):
    from firstmate.exec import gitops

    repo = tmp_path / "browse" / "myrepo"
    gitops.init_repo(repo)
    (tmp_path / "browse" / "plain").mkdir()
    data = client.get("/fs/browse", params={"path": str(tmp_path / "browse")}).json()
    byname = {d["name"]: d for d in data["dirs"]}
    assert byname["myrepo"]["is_repo"] is True
    assert byname["plain"]["is_repo"] is False
    assert data["parent"] == str(tmp_path)
    assert client.get("/fs/browse", params={"path": "/no/such/dir"}).status_code == 400

    client.post("/tasks", json={"contract": contract(client.repo)})
    repos = client.get("/fs/repos").json()["repos"]
    assert any(r["path"] == client.repo and r["source"] == "recent" for r in repos)


def _await_chat(client, chat_id, want, tries=80):
    """Scoping turns run off the request path now; poll for the outcome."""
    import time
    for _ in range(tries):
        chat = client.get(f"/scoping/{chat_id}").json()["chat"]
        if chat["status"] in want:
            return chat
        time.sleep(0.02)
    raise AssertionError(f"chat stuck in {chat['status']}, wanted {want}")


def test_scoping_chat_flow(client, tmp_path, monkeypatch):
    import json as _json

    from firstmate import scoping_api
    from firstmate.exec import gitops

    repo = tmp_path / "scoperepo"
    gitops.init_repo(repo)

    turns = []

    def fake_runner(chat, text):
        turns.append(text)
        if len(turns) == 1:
            return "Here is my proposed scope — push back.", "sid-1"
        # Second turn: the "assistant" writes a valid contract.
        chat.contract_path.write_text(_json.dumps({
            "goal": chat.goal, "repo": chat.repo,
            "steps": [{"id": "s1", "prompt": "p", "criteria": ["c1"]}],
            "criteria": [{"id": "c1", "command": "true"}],
        }))
        return "Contract written and checked — ready for approval.", "sid-2"

    monkeypatch.setattr(scoping_api, "run_turn_subprocess", fake_runner)

    # repo must be a git repo
    r = client.post("/scoping", json={"goal": "g", "repo": str(tmp_path)})
    assert r.status_code == 400

    r = client.post("/scoping", json={"goal": "scope me", "repo": str(repo)})
    assert r.status_code == 200, r.text
    body = r.json()
    # Task-first: the session exists immediately, in the queue, in `scoping`.
    tid = body["task"]["id"]
    assert body["task"]["status"] == "scoping"
    assert body["task"]["scoping_chat_id"] == body["chat"]["id"]
    assert [t["id"] for t in client.get("/status").json()["tasks"]] == [tid]
    # ...and it cannot be run until a contract is approved
    assert client.post(f"/tasks/{tid}/run").status_code == 409

    chat = _await_chat(client, body["chat"]["id"], {"awaiting_operator"})
    assert chat["session_id"] == "sid-1"
    assert chat["messages"][-1]["role"] == "firstmate"
    assert "proposed scope" in chat["messages"][-1]["text"]
    # first turn sends the scoping prompt, not the goal string alone
    assert "scoping assistant" in turns[0]

    # The task detail carries the conversation — it renders in the session.
    detail = client.get(f"/tasks/{tid}").json()
    assert detail["scoping"]["id"] == chat["id"]
    assert len(detail["scoping"]["messages"]) == 1

    r = client.post(f"/scoping/{chat['id']}/message",
                    json={"text": "looks right, finalize"})
    # The operator turn is recorded synchronously; the reply arrives later.
    assert r.json()["chat"]["messages"][-1]["role"] == "operator"
    chat = _await_chat(client, chat["id"], {"contract_ready"})
    assert chat["contract"]["goal"] == "scope me"
    assert chat["contract_errors"] == []
    # exactly one operator message — the pre-append must not double up
    assert sum(1 for m in chat["messages"] if m["role"] == "operator") == 1

    r = client.post(f"/scoping/{chat['id']}/approve", json={"run": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["started"] is False
    assert body["chat"]["status"] == "approved"
    # Approval adopts the contract into the SAME task — no second task.
    assert body["task"]["id"] == tid
    assert len(client.get("/tasks").json()["tasks"]) == 1
    detail = client.get(f"/tasks/{tid}").json()
    assert detail["task"]["status"] == "ready"
    assert detail["task"]["scoping_chat_id"] is None
    assert detail["scoping"] is None
    assert [st["id"] for st in detail["task"]["steps"]] == ["s1"]
    # double-approve rejected; messaging an approved chat rejected
    assert client.post(f"/scoping/{chat['id']}/approve", json={}).status_code == 409
    assert client.post(f"/scoping/{chat['id']}/message",
                       json={"text": "hi"}).status_code == 409
    # rehydration from disk after a "restart" (fresh app over same home)
    reloaded = scoping_api.load_chat(client.store.home, chat["id"])
    assert reloaded is not None and reloaded.status == "approved"


def test_scoping_abandon_abandons_its_task(client, tmp_path, monkeypatch):
    from firstmate import scoping_api
    from firstmate.exec import gitops

    repo = tmp_path / "abandonrepo"
    gitops.init_repo(repo)
    monkeypatch.setattr(scoping_api, "run_turn_subprocess",
                        lambda chat, text: ("proposal", "sid-1"))

    body = client.post("/scoping", json={"goal": "drop me",
                                         "repo": str(repo)}).json()
    tid, cid = body["task"]["id"], body["chat"]["id"]
    _await_chat(client, cid, {"awaiting_operator"})
    client.post(f"/scoping/{cid}/abandon")
    assert client.get(f"/tasks/{tid}").json()["task"]["status"] == "abandoned"

    # ...and the other direction: abandoning the task closes the chat.
    body = client.post("/scoping", json={"goal": "drop me too",
                                         "repo": str(repo)}).json()
    tid, cid = body["task"]["id"], body["chat"]["id"]
    _await_chat(client, cid, {"awaiting_operator"})
    client.post(f"/tasks/{tid}/abandon")
    assert client.get(f"/scoping/{cid}").json()["chat"]["status"] == "abandoned"


def test_scoping_abandon_during_turn_stays_abandoned(client, tmp_path, monkeypatch):
    """Turns run off the request path, so an operator can abandon while one is
    in flight. The landing turn must not resurrect the conversation."""
    from firstmate import scoping_api
    from firstmate.exec import gitops

    repo = tmp_path / "racerepo"
    gitops.init_repo(repo)

    def runner_that_abandons_midway(chat, text):
        # Simulate the operator hitting abandon while claude is thinking:
        # the endpoint writes the terminal status straight to disk.
        import json as _json
        path = Path(chat.dir) / "scoping.json"
        disk = _json.loads(path.read_text())
        disk["status"] = "abandoned"
        path.write_text(_json.dumps(disk))
        return "here is a proposal nobody asked for any more", "sid-1"

    monkeypatch.setattr(scoping_api, "run_turn_subprocess",
                        runner_that_abandons_midway)
    body = client.post("/scoping", json={"goal": "race me",
                                         "repo": str(repo)}).json()
    chat = _await_chat(client, body["chat"]["id"], {"abandoned"})
    assert chat["status"] == "abandoned"
    # the reply is still recorded — nothing is lost, it just doesn't reopen
    assert chat["messages"][-1]["role"] == "firstmate"


def test_scoping_never_hangs_in_thinking(client, tmp_path, monkeypatch):
    """The operator's message is recorded before the turn is spawned, so any
    path that drops the turn must hand the chat back rather than leave it
    `thinking` with an unanswered message."""
    from firstmate import scoping_api
    from firstmate.exec import gitops

    repo = tmp_path / "hangrepo"
    gitops.init_repo(repo)
    monkeypatch.setattr(scoping_api, "run_turn_subprocess",
                        lambda chat, text: ("proposal", "sid-1"))
    body = client.post("/scoping", json={"goal": "no hang",
                                         "repo": str(repo)}).json()
    cid = body["chat"]["id"]
    _await_chat(client, cid, {"awaiting_operator"})

    # A runner that blows up in a way `advance` does not handle (it catches
    # RuntimeError/TimeoutExpired/OSError) must still settle the chat.
    def exploding(chat, text):
        raise ValueError("something nobody planned for")

    monkeypatch.setattr(scoping_api, "run_turn_subprocess", exploding)
    client.post(f"/scoping/{cid}/message", json={"text": "go on"})
    chat = _await_chat(client, cid, {"failed", "awaiting_operator"})
    assert chat["status"] != "thinking"
    assert chat["messages"][-1]["role"] == "system"


def test_scoping_failed_turn(client, tmp_path, monkeypatch):
    from firstmate import scoping_api
    from firstmate.exec import gitops

    repo = tmp_path / "failrepo"
    gitops.init_repo(repo)

    def broken_runner(chat, text):
        raise RuntimeError("claude exploded")

    monkeypatch.setattr(scoping_api, "run_turn_subprocess", broken_runner)
    r = client.post("/scoping", json={"goal": "g", "repo": str(repo)})
    chat = _await_chat(client, r.json()["chat"]["id"], {"failed"})
    assert "turn failed" in chat["messages"][-1]["text"]
    # the task survives so the operator can retry or abandon deliberately
    assert client.get(f"/tasks/{r.json()['task']['id']}").json()[
        "task"]["status"] == "scoping"


def test_websocket_snapshot(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    with client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
        assert snap["kind"] == "snapshot"
        assert [t["id"] for t in snap["tasks"]] == [tid]
