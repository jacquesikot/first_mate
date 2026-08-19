"""Unit tests for the execution modules' pure logic (no tmux, no claude)."""

import json
from pathlib import Path

import pytest

from firstmate.exec import context, gitops, hooks
from firstmate import spawner


# ------------------------------------------------------------- hooks

def test_merge_settings_concatenates_hook_lists_and_keeps_existing():
    existing = {
        "permissions": {"allow": ["Read"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing.sh"}]}]},
    }
    ours = {
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "fm.sh"}]}],
            "PreCompact": [{"hooks": [{"type": "command", "command": "wall.sh"}]}],
        }
    }
    merged = hooks.merge_settings(existing, ours)
    assert len(merged["hooks"]["Stop"]) == 2
    assert merged["hooks"]["Stop"][0]["hooks"][0]["command"] == "existing.sh"
    assert "PreCompact" in merged["hooks"]
    assert merged["permissions"] == {"allow": ["Read"]}


def test_merge_settings_dedupes_identical_entries():
    entry = {"hooks": {"Stop": [hooks.hook_entry("same.sh")]}}
    merged = hooks.merge_settings(entry, entry)
    assert len(merged["hooks"]["Stop"]) == 1


def test_write_settings_refuses_unparseable_file(tmp_path: Path):
    target = tmp_path / "settings.json"
    target.write_text("{not json")
    with pytest.raises(ValueError):
        hooks.write_settings(target, {"hooks": {}})
    assert target.read_text() == "{not json"  # untouched


def test_write_settings_merges_on_disk(tmp_path: Path):
    target = tmp_path / "settings.json"
    hooks.write_settings(target, {"hooks": {"Stop": [hooks.hook_entry("a.sh")]}})
    hooks.write_settings(target, {"hooks": {"Stop": [hooks.hook_entry("b.sh")]}})
    on_disk = json.loads(target.read_text())
    cmds = [e["hooks"][0]["command"] for e in on_disk["hooks"]["Stop"]]
    assert cmds == ["a.sh", "b.sh"]


# ----------------------------------------------------------- context

def test_project_dir_munging():
    p = context.project_dir(Path("/Users/x/code/first_mate"))
    assert p.name == "-Users-x-code-first-mate"


def test_read_context_takes_latest_assistant_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECTS_DIR", tmp_path)
    cwd = Path("/repo/wt")
    tdir = context.project_dir(cwd)
    tdir.mkdir(parents=True)

    def entry(inp, cr, cc, out):
        return json.dumps(
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": inp,
                        "cache_read_input_tokens": cr,
                        "cache_creation_input_tokens": cc,
                        "output_tokens": out,
                    }
                },
            }
        )

    lines = [
        json.dumps({"type": "user"}),
        entry(10, 0, 1000, 50),
        "not json at all",
        entry(5, 1000, 200, 80),
    ]
    (tdir / "sid.jsonl").write_text("\n".join(lines) + "\n")
    reading = context.read_context(cwd, "sid")
    assert reading is not None
    assert reading.tokens == 5 + 1000 + 200 + 80
    assert reading.assistant_turns == 2
    assert reading.band == "neutral"


def test_read_context_missing_transcript_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "PROJECTS_DIR", tmp_path)
    assert context.read_context(Path("/nowhere"), "nope") is None


def test_context_bands():
    r = context.ContextReading("s", 130_000, 200_000, 1)
    assert r.band == "elevated"
    assert context.ContextReading("s", 175_000, 200_000, 1).band == "warning"


# ------------------------------------------------------------ gitops

def test_worktree_path_layout(tmp_path: Path):
    repo = tmp_path / "myrepo"
    assert gitops.worktree_path(repo, "fm/task-1") == (
        tmp_path / "myrepo-worktrees" / "fm-task-1"
    )


def test_worktree_lifecycle(tmp_path: Path):
    repo = tmp_path / "r"
    gitops.init_repo(repo)
    wt = gitops.create_worktree(repo, "spike")
    assert wt.exists()
    assert wt in gitops.list_worktrees(repo)
    (wt / "new.txt").write_text("x")
    assert gitops.changed_files(wt) == ["new.txt"]
    gitops.remove_worktree(repo, "spike", force=True)
    assert not wt.exists()


# ----------------------------------------------------------- spawner

def test_build_command_fresh_vs_resume(tmp_path: Path):
    spec = spawner.WorkerSpec(prompt="p", cwd=tmp_path, name="w", model="sonnet")
    cmd = spawner.build_command(spec)
    assert cmd[:3] == ["claude", "-p", "p"]
    assert "--session-id" in cmd and "--resume" not in cmd

    spec_r = spawner.WorkerSpec(prompt="p", cwd=tmp_path, name="w", resume="abc")
    cmd_r = spawner.build_command(spec_r)
    assert "--resume" in cmd_r and "--session-id" not in cmd_r


def test_diff_numstat(tmp_path: Path):
    repo = tmp_path / "repo"
    gitops.init_repo(repo)
    (repo / "a.txt").write_text("one\ntwo\nthree\n")
    gitops._git(repo, "add", "a.txt")
    gitops._git(repo, "commit", "-q", "-m", "add a")
    assert gitops.diff_numstat(repo) == (0, 0)
    (repo / "a.txt").write_text("one\nTWO\n")  # 1 added, 2 deleted
    (repo / "b.txt").write_text("x\ny\n")
    gitops._git(repo, "add", "b.txt")  # staged new file counts: 2 added
    added, deleted = gitops.diff_numstat(repo)
    assert (added, deleted) == (3, 2)
