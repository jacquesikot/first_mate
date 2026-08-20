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


def test_fs_refs_ranks_starting_points(client, tmp_path):
    """The picker's data: what can I start from, and how fresh is it."""
    from firstmate.exec import gitops

    repo = tmp_path / "refsrepo"
    gitops.init_repo(repo)
    (repo / "f.txt").write_text("x\n")
    gitops._git(repo, "add", "f.txt")
    gitops._git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-qm", "one")
    gitops._git(repo, "branch", "feature/side")

    # No remote: fetch is skipped, not an error, and the recommendation
    # falls back to the checked-out branch.
    r = client.get("/fs/refs", params={"repo": str(repo)})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["fetched"] is False and d["fetch_error"] is None
    assert d["default_branch"] is None
    assert d["current_branch"] == d["recommended"]
    assert d["dirty"] is False
    names = [x["name"] for x in d["refs"]]
    assert set(names) == {d["current_branch"], "feature/side"}
    # the checked-out branch is ranked first and labelled
    assert d["refs"][0]["role"] == "current"
    assert all(x["remote"] is False for x in d["refs"])

    # A dirty tree is reported, so the UI can say why "current branch" may
    # not be what the operator expects.
    (repo / "f.txt").write_text("dirty\n")
    assert client.get("/fs/refs", params={"repo": str(repo)}).json()["dirty"] is True

    # First Mate's own task branches are outputs, not starting points, so
    # they don't accumulate in the picker.
    gitops._git(repo, "branch", "fm/some-old-task")
    names = [x["name"] for x in
             client.get("/fs/refs", params={"repo": str(repo)}).json()["refs"]]
    assert "fm/some-old-task" not in names
    assert "feature/side" in names

    r = client.get("/fs/refs", params={"repo": str(tmp_path / "nope")})
    assert r.status_code == 400


def test_fs_refs_prefers_remote_default(client, tmp_path, monkeypatch):
    """With a remote, `origin/<default>` is the recommendation — the
    "I usually pull latest from origin/main" case."""
    from firstmate.exec import gitops

    origin = tmp_path / "origin"
    gitops.init_repo(origin)
    (origin / "f.txt").write_text("x\n")
    gitops._git(origin, "add", "f.txt")
    gitops._git(origin, "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-qm", "one")
    clone = tmp_path / "clone"
    gitops._git(tmp_path, "clone", "-q", str(origin), str(clone))

    d = client.get("/fs/refs", params={"repo": str(clone)}).json()
    assert d["fetched"] is True, d["fetch_error"]
    assert d["default_branch"] == d["recommended"]
    assert d["default_branch"].startswith("origin/")
    assert d["refs"][0]["role"] == "default"
    # ...and starting a task with no explicit base uses exactly that.
    from firstmate import scoping_api
    monkeypatch.setattr(scoping_api, "run_turn_subprocess",
                        lambda chat, text: ("proposal", "sid-1"))
    body = client.post("/scoping", json={"goal": "g", "repo": str(clone)}).json()
    assert body["task"]["base"] == d["default_branch"]
    assert body["chat"]["base"] == d["default_branch"]
    # the worktree starts at the remote tip, not at the local checkout
    assert body["task"]["base_sha"] == gitops.resolve_ref(clone, d["default_branch"])


def test_scoping_creates_worktree_at_chosen_base(client, tmp_path, monkeypatch):
    """Every task declares its starting point up front; the worktree exists
    from the moment scoping starts so the conversation reads that clean
    checkout, not the operator's (possibly mid-work) tree."""
    from firstmate import scoping_api
    from firstmate.exec import gitops

    repo = tmp_path / "baserepo"
    gitops.init_repo(repo)
    (repo / "a.txt").write_text("first\n")
    gitops._git(repo, "add", "a.txt")
    gitops._git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-qm", "first")
    first = gitops.head_commit(repo)
    # A second commit, plus a branch pointing at the first, so "start from
    # the older branch" is distinguishable from "start from HEAD".
    gitops._git(repo, "branch", "older", first)
    (repo / "a.txt").write_text("second\n")
    gitops._git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-aqm", "second")
    # ...and uncommitted mess in the operator's checkout, which must not
    # leak into the task's worktree.
    (repo / "messy.txt").write_text("work in progress\n")

    seen = {}

    def runner(chat, text):
        seen["cwd"] = chat.workdir
        return "proposal", "sid-1"

    monkeypatch.setattr(scoping_api, "run_turn_subprocess", runner)

    body = client.post("/scoping", json={"goal": "from older",
                                        "repo": str(repo),
                                        "base": "older"}).json()
    task, chat = body["task"], body["chat"]
    assert task["base"] == "older"
    assert task["base_sha"] == first
    wt = Path(task["worktree"])
    assert wt.is_dir(), "worktree should exist as soon as scoping starts"
    # The worktree holds the chosen starting point...
    assert (wt / "a.txt").read_text() == "first\n"
    # ...and none of the operator's uncommitted mess.
    assert not (wt / "messy.txt").exists()
    # The conversation reads the worktree, not the repo.
    _await_chat(client, chat["id"], {"awaiting_operator"})
    assert seen["cwd"] == str(wt)
    assert client.get(f"/tasks/{task['id']}").json()["scoping"]["base"] == "older"


def test_scoping_rejects_unresolvable_base(client, tmp_path):
    from firstmate.exec import gitops

    repo = tmp_path / "badbase"
    gitops.init_repo(repo)
    r = client.post("/scoping", json={"goal": "g", "repo": str(repo),
                                      "base": "no-such-branch"})
    assert r.status_code == 400
    assert "no-such-branch" in r.text
    # nothing half-created
    assert client.get("/tasks").json()["tasks"] == []


def test_scoping_abandon_discards_untouched_worktree(client, tmp_path, monkeypatch):
    """An abandoned conversation shouldn't litter a worktree and a branch."""
    from firstmate import scoping_api
    from firstmate.exec import gitops

    repo = tmp_path / "cleanup"
    gitops.init_repo(repo)
    monkeypatch.setattr(scoping_api, "run_turn_subprocess",
                        lambda chat, text: ("proposal", "sid-1"))
    body = client.post("/scoping", json={"goal": "drop me",
                                         "repo": str(repo)}).json()
    tid, cid = body["task"]["id"], body["chat"]["id"]
    wt = Path(body["task"]["worktree"])
    branch = body["task"]["branch"]
    assert wt.is_dir()
    _await_chat(client, cid, {"awaiting_operator"})

    client.post(f"/scoping/{cid}/abandon")
    assert not wt.exists(), "clean worktree should be removed"
    assert branch not in gitops._git(repo, "branch", "--format=%(refname:short)").stdout.split()
    assert client.get(f"/tasks/{tid}").json()["task"]["status"] == "abandoned"


def test_scoping_abandon_keeps_worktree_with_work_in_it(client, tmp_path, monkeypatch):
    from firstmate import scoping_api
    from firstmate.exec import gitops

    repo = tmp_path / "keepwork"
    gitops.init_repo(repo)
    monkeypatch.setattr(scoping_api, "run_turn_subprocess",
                        lambda chat, text: ("proposal", "sid-1"))
    body = client.post("/scoping", json={"goal": "keep me",
                                         "repo": str(repo)}).json()
    tid = body["task"]["id"]
    wt = Path(body["task"]["worktree"])
    _await_chat(client, body["chat"]["id"], {"awaiting_operator"})
    (wt / "someones-work.txt").write_text("do not delete me\n")

    client.post(f"/tasks/{tid}/abandon")
    assert wt.is_dir(), "a worktree holding work must survive an abandon"
    assert (wt / "someones-work.txt").exists()


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


# ---- multi-question rounds (a skill's grilling round; STATUS 2026-08-20) ----


def round_body(tid: str) -> dict:
    return {
        "task_id": tid, "step_id": "s1", "type": "decision",
        "question": "Verified the sidebar; 2 forks need settling.",
        "questions": [
            {"id": "q1", "question": "Keep the existing context tab?",
             "options": [{"label": "keep both", "recommended": True},
                         {"label": "replace"}]},
            {"id": "q2", "question": "How to differentiate?",
             "options": [{"label": "sub-tabs"}, {"label": "collapsible"}]},
        ],
    }


def test_round_parks_once_and_keeps_options_per_question(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    r = client.post("/internal/ask", json=round_body(tid))
    assert r.status_code == 200 and r.json()["status"] == "parked"
    qid = r.json()["id"]
    # ONE park for the whole round, not one per question.
    open_qs = client.get("/questions", params={"status": "open"}).json()["questions"]
    assert [q["id"] for q in open_qs] == [qid]
    q = open_qs[0]
    # No top-level option: that is what rendered as the phantom
    # "See inline options per question" button.
    assert q["options"] == []
    assert [s["id"] for s in q["questions"]] == ["q1", "q2"]
    assert q["questions"][0]["options"][0]["recommended"] is True
    assert q["questions"][0]["options"][1]["recommended"] is False


def test_round_requires_an_answer_for_every_question(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    qid = client.post("/internal/ask", json=round_body(tid)).json()["id"]
    r = client.post(f"/questions/{qid}/answer", json={"answers": {"q1": "keep both"}})
    assert r.status_code == 400 and "q2" in str(r.json())
    # still open — a partial answer must not resolve the round
    assert client.get("/questions", params={"status": "open"}).json()["questions"]


def test_round_answered_per_question(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    qid = client.post("/internal/ask", json=round_body(tid)).json()["id"]
    r = client.post(f"/questions/{qid}/answer", json={
        "answers": {"q1": "keep both", "q2": "sub-tabs"}, "by": "test"})
    assert r.status_code == 200
    q = r.json()["question"]
    assert q["status"] == "answered"
    assert q["questions"][0]["answer"] == "keep both"
    assert q["questions"][1]["answer"] == "sub-tabs"
    # the rolled-up answer names each decision, so the amendment is auditable
    assert "q1:" in q["answer"] and "sub-tabs" in q["answer"]
    detail = client.get(f"/tasks/{tid}").json()
    assert "keep both" in detail["contract"]["amendments"][0]["answer"]


def test_round_accepts_one_free_text_reply_for_all(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    qid = client.post("/internal/ask", json=round_body(tid)).json()["id"]
    r = client.post(f"/questions/{qid}/answer",
                    json={"answer": "do the simplest thing everywhere"})
    assert r.status_code == 200
    q = r.json()["question"]
    assert all(s["answer"] == "do the simplest thing everywhere"
               for s in q["questions"])


def test_round_rejects_unknown_subquestion_id(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    qid = client.post("/internal/ask", json=round_body(tid)).json()["id"]
    r = client.post(f"/questions/{qid}/answer", json={
        "answers": {"q1": "a", "q2": "b", "q9": "typo"}})
    assert r.status_code == 400 and "q9" in str(r.json())


def test_round_rejects_empty_subquestion_text(client):
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    r = client.post("/internal/ask", json={
        "task_id": tid, "type": "decision", "question": "preamble",
        "questions": [{"id": "q1", "question": "  "}]})
    assert r.status_code == 400


def test_plain_question_still_answers_flatly(client):
    """The old shape must keep working — questions already on disk use it."""
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    qid = client.post("/internal/ask", json={
        "task_id": tid, "type": "decision", "question": "Which color?",
        "options": ["red", "blue"]}).json()["id"]
    r = client.post(f"/questions/{qid}/answer", json={"answer": "red"})
    assert r.status_code == 200 and r.json()["question"]["answer"] == "red"
    assert r.json()["question"]["questions"] == []


# ---- worker artifacts: the deliverable when it isn't code (STATUS 2026-08-20) ----


def artifact_task(client) -> str:
    tid = client.post("/tasks", json={"contract": contract(client.repo)}).json()["task"]["id"]
    task = client.store.load_task(tid)
    task.worktree = client.repo
    client.store.save_task(task)
    return tid


def test_artifacts_empty_when_none_written(client):
    tid = artifact_task(client)
    r = client.get(f"/tasks/{tid}/artifacts")
    assert r.status_code == 200 and r.json()["files"] == []


def test_artifacts_lists_newest_first_with_sizes(client):
    import os
    import time

    tid = artifact_task(client)
    root = Path(client.repo) / ".fm" / "artifacts"
    root.mkdir(parents=True)
    (root / "round1.json").write_text('{"a": 1}')
    (root / "plan-draft.md").write_text("# Plan\n\nBody.\n")
    # make the draft newer regardless of filesystem timestamp granularity
    now = time.time()
    os.utime(root / "round1.json", (now - 60, now - 60))
    os.utime(root / "plan-draft.md", (now, now))

    files = client.get(f"/tasks/{tid}/artifacts").json()["files"]
    assert [f["path"] for f in files] == ["plan-draft.md", "round1.json"]
    assert files[0]["bytes"] == len("# Plan\n\nBody.\n")
    assert files[0]["text"] is True
    assert files[0]["modified"]


def test_artifacts_reads_a_file_back(client):
    tid = artifact_task(client)
    root = Path(client.repo) / ".fm" / "artifacts"
    root.mkdir(parents=True)
    (root / "plan-draft.md").write_text("## Implementation Plan\n")
    r = client.get(f"/tasks/{tid}/artifacts/file",
                   params={"path": "plan-draft.md"})
    assert r.status_code == 200
    assert r.json()["text"] == "## Implementation Plan\n"
    assert r.json()["truncated"] is False


def test_artifacts_finds_nested_files(client):
    tid = artifact_task(client)
    nested = Path(client.repo) / ".fm" / "artifacts" / "reports"
    nested.mkdir(parents=True)
    (nested / "audit.md").write_text("x")
    files = client.get(f"/tasks/{tid}/artifacts").json()["files"]
    assert [f["path"] for f in files] == ["reports/audit.md"]
    assert client.get(f"/tasks/{tid}/artifacts/file",
                      params={"path": "reports/audit.md"}).status_code == 200


def test_artifacts_refuses_to_escape_the_scratch_dir(client):
    """`path` comes from the browser — it must not reach First Mate's own
    orchestration state, let alone the rest of the disk."""
    tid = artifact_task(client)
    (Path(client.repo) / ".fm" / "artifacts").mkdir(parents=True)
    (Path(client.repo) / ".fm" / "settings.json").write_text("{}")
    for bad in ["../settings.json", "../../../../etc/passwd",
                "reports/../../settings.json"]:
        r = client.get(f"/tasks/{tid}/artifacts/file", params={"path": bad})
        assert r.status_code == 400, bad


def test_artifacts_missing_file_is_404(client):
    tid = artifact_task(client)
    (Path(client.repo) / ".fm" / "artifacts").mkdir(parents=True)
    assert client.get(f"/tasks/{tid}/artifacts/file",
                      params={"path": "nope.md"}).status_code == 404


def test_artifacts_declines_to_inline_something_huge(client):
    from firstmate import server

    tid = artifact_task(client)
    root = Path(client.repo) / ".fm" / "artifacts"
    root.mkdir(parents=True)
    (root / "big.log").write_text("x" * (server.ARTIFACT_MAX_BYTES + 1))
    body = client.get(f"/tasks/{tid}/artifacts/file",
                      params={"path": "big.log"}).json()
    assert body["truncated"] is True and body["text"] is None
    assert "too large" in body["reason"]


def test_artifacts_stay_out_of_the_diff(client):
    """They are not repo changes and must not read as pending commits."""
    import subprocess

    tid = artifact_task(client)
    repo = Path(client.repo)
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "README").write_text("x\n")
    run("add", "-A")
    run("commit", "-qm", "init")
    root = repo / ".fm" / "artifacts"
    root.mkdir(parents=True)
    (root / "plan-draft.md").write_text("# Plan\n")
    # a real repo change, so we know the diff isn't just empty
    (repo / "src.py").write_text("print(1)\n")

    body = client.get(f"/tasks/{tid}/diff").json()
    paths = [f["path"] for f in body["files"]]
    assert "src.py" in paths, "a genuine change must still show"
    assert not any(p.startswith(".fm") for p in paths), paths
