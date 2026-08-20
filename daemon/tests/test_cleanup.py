"""Cleanup — reclaiming disk without destroying work.

The safety bar is the whole point of this module: a worktree is where the
work physically happened, and uncommitted or unpushed changes exist
nowhere else. Observed live (2026-08-20): one abandoned task's worktree
held 8 modified source files that an unconditional clean would have lost.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from firstmate import cleanup
from firstmate.server import create_app
from firstmate.store import Store


def run(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True)


def repo_with_remote(tmp_path):
    """An origin plus a clone, so 'pushed' is a real distinction."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True,
                   capture_output=True)
    run(repo, "config", "user.email", "t@t")
    run(repo, "config", "user.name", "t")
    (repo / "README").write_text("x\n")
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "init")
    run(repo, "push", "-q", "origin", "main")
    return repo


def make_task(store, repo, status="done", branch="fm/t1"):
    from firstmate.models import Contract, StepSpec, Task

    contract = Contract(goal="g", repo=str(repo),
                        steps=[StepSpec(id="s", prompt="p")])
    task = Task(id="t1", repo=str(repo), branch=branch, status=status,
                goal="g")
    from firstmate.models import StepState
    task.steps = [StepState(id="s")]
    store.save_contract(task.id, contract)
    store.save_task(task)
    return task


def add_worktree(repo, branch, base="main"):
    from firstmate.exec import gitops
    return gitops.create_worktree(repo, branch, base)


# ---------------------------------------------------------------- sizing


def test_finds_dep_dirs_nested_in_a_monorepo(tmp_path):
    """A root-only scan reports 0B for a worktree that is 450MB of
    dependencies — monorepos put node_modules under frontend/."""
    wt = tmp_path / "wt"
    (wt / "frontend" / "node_modules" / "pkg").mkdir(parents=True)
    (wt / "frontend" / "node_modules" / "pkg" / "f").write_text("x" * 100)
    (wt / "packages" / "api" / ".venv").mkdir(parents=True)
    (wt / "packages" / "api" / ".venv" / "f").write_text("y" * 50)
    (wt / "src").mkdir()
    (wt / "src" / "keep.ts").write_text("code")

    found = {str(p.relative_to(wt)) for p in cleanup.find_dep_dirs(wt)}
    assert "frontend/node_modules" in found
    assert "packages/api/.venv" in found
    assert cleanup.dir_size(wt) > 0
    assert sum(cleanup.dir_size(p) for p in cleanup.find_dep_dirs(wt)) == 150


def test_dep_scan_does_not_recurse_into_a_match_or_into_fm_state(tmp_path):
    wt = tmp_path / "wt"
    inner = wt / "node_modules" / "a" / "node_modules"
    inner.mkdir(parents=True)
    (wt / ".fm" / "artifacts" / "node_modules").mkdir(parents=True)
    found = [str(p.relative_to(wt)) for p in cleanup.find_dep_dirs(wt)]
    assert found == ["node_modules"], found


def test_human_readable_sizes():
    assert cleanup.human(0) == "0B"
    assert cleanup.human(512) == "512B"
    assert cleanup.human(2048) == "2.0KB"
    assert cleanup.human(5 * 1024 * 1024) == "5.0MB"
    assert cleanup.human(1800 * 1024 * 1024) == "1.8GB"


# ------------------------------------------------------------ safety bar


def test_clean_worktree_with_pushed_commits_is_safe(tmp_path):
    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo)
    wt = add_worktree(repo, task.branch)
    task.worktree = str(wt)
    task.base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    store.save_task(task)

    cand = cleanup.inspect(task)
    assert cand is not None
    assert cand.safe, cand.blockers


def test_uncommitted_files_block_the_clean(tmp_path):
    """The live case: 8 modified source files in an abandoned task."""
    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo)
    wt = add_worktree(repo, task.branch)
    task.worktree = str(wt)
    store.save_task(task)
    (wt / "README").write_text("edited but never committed\n")

    cand = cleanup.inspect(task)
    assert not cand.safe
    assert any("uncommitted" in b for b in cand.blockers)
    # And remove() refuses without force.
    ok, msg = cleanup.remove(store, task)
    assert not ok and "uncommitted" in msg
    assert Path(task.worktree).is_dir(), "the worktree must still be there"


def test_unpushed_commits_block_the_clean(tmp_path):
    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo)
    wt = add_worktree(repo, task.branch)
    task.worktree = str(wt)
    store.save_task(task)
    (wt / "new.txt").write_text("work\n")
    run(wt, "add", "-A")
    run(wt, "commit", "-qm", "work that exists only here")

    cand = cleanup.inspect(task)
    assert not cand.safe
    assert any("unpushed" in b for b in cand.blockers)
    ok, msg = cleanup.remove(store, task)
    assert not ok and "unpushed" in msg
    assert Path(wt).is_dir()


def test_fm_state_does_not_count_as_the_operators_work(tmp_path):
    """`.fm/` is First Mate's own bookkeeping — it must not make a
    worktree look like it holds unsaved work."""
    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo)
    wt = add_worktree(repo, task.branch)
    task.worktree = str(wt)
    store.save_task(task)
    (wt / ".fm" / "artifacts").mkdir(parents=True)
    (wt / ".fm" / "artifacts" / "plan.md").write_text("draft")
    (wt / ".fm" / "inject.md").write_text("ctx")

    assert cleanup.inspect(task).safe


def test_a_live_task_is_never_a_candidate(tmp_path):
    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo, status="running")
    wt = add_worktree(repo, task.branch)
    task.worktree = str(wt)
    store.save_task(task)

    cand = cleanup.inspect(task)
    assert not cand.safe
    assert any("not finished" in b for b in cand.blockers)


def test_force_removes_and_clears_the_worktree_pointer(tmp_path):
    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo)
    wt = add_worktree(repo, task.branch)
    task.worktree = str(wt)
    store.save_task(task)
    (wt / "README").write_text("dirty\n")

    ok, msg = cleanup.remove(store, task, force=True)
    assert ok, msg
    assert not Path(wt).exists()
    assert store.load_task("t1").worktree == ""
    # The fm/* branch goes with it.
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list",
                               task.branch], capture_output=True,
                              text=True).stdout
    assert task.branch not in branches


def test_remove_only_ever_deletes_the_tasks_own_fm_branch(tmp_path):
    """A task checked out onto someone's real branch must not have that
    branch deleted along with its worktree."""
    repo = repo_with_remote(tmp_path)
    run(repo, "branch", "feat/someones-work")
    store = Store(tmp_path / "home")
    task = make_task(store, repo, branch="feat/someones-work")
    from firstmate.exec import gitops
    wt = gitops.worktree_path(repo, task.branch)
    run(repo, "worktree", "add", "-q", str(wt), task.branch)
    task.worktree = str(wt)
    store.save_task(task)

    ok, _ = cleanup.remove(store, task, force=True)
    assert ok
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list",
                               "feat/someones-work"], capture_output=True,
                              text=True).stdout
    assert "feat/someones-work" in branches, "a non-fm branch must survive"


# ----------------------------------------------------------- deps + smoke


def test_drop_deps_keeps_the_code_and_the_git_history(tmp_path):
    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo)
    wt = add_worktree(repo, task.branch)
    (wt / "frontend" / "node_modules" / "x").mkdir(parents=True)
    (wt / "frontend" / "node_modules" / "x" / "f").write_text("z" * 1000)
    (wt / "keep.ts").write_text("source that matters")

    freed, names = cleanup.drop_deps(wt)
    assert freed == 1000
    assert names == ["frontend/node_modules"]
    assert not (wt / "frontend" / "node_modules").exists()
    assert (wt / "keep.ts").read_text() == "source that matters"
    assert (wt / ".git").exists()
    # Still a working worktree afterwards.
    assert subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                          capture_output=True).returncode == 0


def test_prune_smoke_runs_respects_the_age_cutoff(tmp_path):
    import os
    import time

    home = tmp_path / "home"
    smoke = home / "smoke"
    smoke.mkdir(parents=True)
    old = smoke / "run-20200101-000000"
    new = smoke / "run-99991231-235959"
    for d in (old, new):
        d.mkdir()
        (d / "f").write_text("x" * 10)
    os.utime(old, (time.time() - 10 * 86400,) * 2)
    # Anything that isn't a run dir is left alone.
    (smoke / "notes.md").write_text("keep me")

    runs, freed = cleanup.prune_smoke_runs(home, keep_days=3.0)
    assert runs == 1 and freed == 10
    assert not old.exists()
    assert new.exists()
    assert (smoke / "notes.md").exists()


# ------------------------------------------------------------------- api


@pytest.fixture()
def client(tmp_path):
    store = Store(tmp_path / "home")
    app = create_app(store, autostart=False)
    with TestClient(app) as c:
        c.store = store
        c.tmp = tmp_path
        yield c


def test_cleanup_report_endpoint(client):
    repo = repo_with_remote(client.tmp)
    task = make_task(client.store, repo)
    wt = add_worktree(repo, task.branch)
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "f").write_text("q" * 500)
    task.worktree = str(wt)
    client.store.save_task(task)

    data = client.get("/cleanup").json()
    assert len(data["candidates"]) == 1
    c = data["candidates"][0]
    assert c["task_id"] == "t1" and c["safe"] is True
    assert c["dep_bytes"] == 500
    assert data["dep_bytes"] == 500


def test_cleanup_endpoint_refuses_unsafe_then_honours_force(client):
    repo = repo_with_remote(client.tmp)
    task = make_task(client.store, repo)
    wt = add_worktree(repo, task.branch)
    task.worktree = str(wt)
    client.store.save_task(task)
    (wt / "README").write_text("dirty\n")

    r = client.post("/tasks/t1/cleanup", json={"mode": "worktree"})
    assert r.status_code == 409
    assert "uncommitted" in str(r.json())
    assert Path(wt).is_dir()

    r = client.post("/tasks/t1/cleanup",
                    json={"mode": "worktree", "force": True})
    assert r.status_code == 200, r.text
    assert not Path(wt).exists()


def test_cleanup_endpoint_deps_mode_keeps_the_worktree(client):
    repo = repo_with_remote(client.tmp)
    task = make_task(client.store, repo)
    wt = add_worktree(repo, task.branch)
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "f").write_text("z" * 200)
    (wt / "README").write_text("dirty but that's fine for deps\n")
    task.worktree = str(wt)
    client.store.save_task(task)

    r = client.post("/tasks/t1/cleanup", json={"mode": "deps"})
    assert r.status_code == 200, r.text
    assert r.json()["freed"] == 200
    assert Path(wt).is_dir(), "deps mode must never remove the worktree"
    assert (wt / "README").exists()


def test_cleanup_endpoint_rejects_an_unknown_mode(client):
    repo = repo_with_remote(client.tmp)
    task = make_task(client.store, repo)
    wt = add_worktree(repo, task.branch)
    task.worktree = str(wt)
    client.store.save_task(task)
    r = client.post("/tasks/t1/cleanup", json={"mode": "everything"})
    assert r.status_code == 400


# ---------------------------------------------------- idle maintenance


def test_maintenance_drops_deps_from_idle_worktrees_only(tmp_path):
    """Rebuildable directories go once a worktree has clearly been left
    alone. The code stays, and the worktree is never removed."""
    import os
    import time

    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo)
    wt = add_worktree(repo, task.branch)
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "f").write_text("x" * 400)
    (wt / "keep.ts").write_text("source")
    task.worktree = str(wt)
    store.save_task(task)

    # Fresh: left alone.
    rep = cleanup.maintenance(store, deps_after_days=3.0)
    assert rep["deps"] == []
    assert (wt / "node_modules").exists()

    # Aged past the threshold: deps go, code and worktree stay.
    os.utime(wt, (time.time() - 5 * 86400,) * 2)
    rep = cleanup.maintenance(store, deps_after_days=3.0)
    assert [d["task_id"] for d in rep["deps"]] == ["t1"]
    assert rep["freed"] == 400
    assert not (wt / "node_modules").exists()
    assert (wt / "keep.ts").read_text() == "source"
    assert Path(wt).is_dir(), "maintenance must never remove a worktree"


def test_maintenance_dry_run_changes_nothing(tmp_path):
    import os
    import time

    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo)
    wt = add_worktree(repo, task.branch)
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "f").write_text("y" * 100)
    task.worktree = str(wt)
    store.save_task(task)
    os.utime(wt, (time.time() - 9 * 86400,) * 2)

    rep = cleanup.maintenance(store, deps_after_days=3.0, dry_run=True)
    assert rep["freed"] == 100
    assert (wt / "node_modules").exists(), "dry run must not delete"


def test_archive_keeps_the_state_it_moves(tmp_path):
    """Task state is the audit trail — archiving compresses it, never
    discards it."""
    import tarfile

    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo)
    store.append_event("t1", "task_done")
    d = store.task_dir("t1")
    assert (d / "task.json").exists()

    made = cleanup.archive_task_state(store, "t1")
    assert made is not None and made.exists()
    assert not d.exists(), "the original directory is moved, not duplicated"
    with tarfile.open(made) as tf:
        names = tf.getnames()
    assert any(n.endswith("task.json") for n in names)
    assert any(n.endswith("contract.json") for n in names)


def test_maintenance_never_archives_a_task_that_still_has_a_worktree(tmp_path):
    """If the worktree is still on disk the task is still in play, whatever
    its age."""
    import os
    import time

    repo = repo_with_remote(tmp_path)
    store = Store(tmp_path / "home")
    task = make_task(store, repo)
    wt = add_worktree(repo, task.branch)
    task.worktree = str(wt)
    store.save_task(task)
    os.utime(store.task_dir("t1"), (time.time() - 90 * 86400,) * 2)

    rep = cleanup.maintenance(store, archive_after_days=14.0)
    assert rep["archived"] == []
    assert store.task_dir("t1").is_dir()
