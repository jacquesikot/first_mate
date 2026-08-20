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


def test_numstat_filters_orchestration_noise(tmp_path):
    """`.fm/` is First Mate's own per-worktree state and caches are droppings;
    neither belongs in the operator's diff or in diff-tripwire totals."""
    from firstmate.exec import gitops

    repo = tmp_path / "noisy"
    gitops.init_repo(repo)
    (repo / "real.py").write_text("x = 1\n")
    (repo / ".fm").mkdir()
    (repo / ".fm" / "guard.json").write_text("{}\n" * 20)
    (repo / ".fm" / "hooks").mkdir()
    (repo / ".fm" / "hooks" / "stop.sh").write_text("#!/bin/sh\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "real.cpython-312.pyc").write_bytes(b"\x00" * 40)
    (repo / "stale.pyc").write_bytes(b"\x00")

    paths = {f["path"] for f in gitops.numstat_files(repo)}
    assert paths == {"real.py"}

    # ...and a tracked .fm file doesn't inflate the tripwire totals either.
    gitops._git(repo, "add", "-f", ".fm/guard.json", "real.py")
    gitops._git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-qm", "add")
    (repo / ".fm" / "guard.json").write_text("{}\n" * 400)
    (repo / "real.py").write_text("x = 1\ny = 2\n")
    added, deleted = gitops.diff_numstat(repo)
    assert added == 1 and deleted == 0
    assert {f["path"] for f in gitops.numstat_files(repo)} == {"real.py"}


def test_is_review_noise():
    from firstmate.exec import gitops

    for noisy in [".fm/guard.json", ".fm/hooks/stop.sh", "a/__pycache__/b.pyc",
                  "x.pyc", "node_modules/pkg/index.js", ".pytest_cache/v/x"]:
        assert gitops.is_review_noise(noisy), noisy
    for clean in ["src/app.py", "README.md", "fm_helper.py", "a/fmx/b.py",
                  "docs/.fmrc"]:
        assert not gitops.is_review_noise(clean), clean


def _commit(repo, msg, **files):
    from firstmate.exec import gitops
    for name, text in files.items():
        (repo / name.replace("__", ".")).write_text(text)
    gitops._git(repo, "add", "-A")
    gitops._git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-qm", msg)
    return gitops.head_commit(repo)


def test_starting_point_helpers(tmp_path):
    """What the New-task picker needs: which refs exist, how fresh, and how
    they relate to their upstream."""
    from firstmate.exec import gitops

    origin = tmp_path / "origin"
    gitops.init_repo(origin)
    _commit(origin, "one", f__txt="1\n")
    clone = tmp_path / "clone"
    gitops._git(tmp_path, "clone", "-q", str(origin), str(clone))

    assert gitops.has_remote(clone) is True
    assert gitops.has_remote(origin) is False
    assert gitops.default_branch(clone) in ("origin/main", "origin/master")
    assert gitops.default_branch(origin) is None
    assert gitops.current_branch(clone) in ("main", "master")
    assert gitops.is_dirty(clone) is False

    # The remote moves on; the clone doesn't know until it fetches.
    newest = _commit(origin, "two", f__txt="2\n")
    stale = {r["name"]: r for r in gitops.list_refs(clone)}
    remote_default = gitops.default_branch(clone)
    assert stale[remote_default]["sha"] != newest[:10]
    assert gitops.fetch(clone) is None
    fresh = {r["name"]: r for r in gitops.list_refs(clone)}
    assert fresh[remote_default]["sha"] == newest[:10]

    # ...and the local branch is now reported behind it.
    local = gitops.current_branch(clone)
    assert fresh[local]["behind"] == 1
    assert fresh[local]["upstream"] == remote_default
    assert fresh[local]["remote"] is False
    assert fresh[remote_default]["remote"] is True
    # origin/HEAD is an alias, never offered as a choice
    assert not any(name.endswith("/HEAD") for name in fresh)

    (clone / "f.txt").write_text("dirty\n")
    assert gitops.is_dirty(clone) is True

    assert gitops.resolve_ref(clone, remote_default) == newest
    assert gitops.resolve_ref(clone, "nope") is None
    # fetch against a repo with no remote is reported, not raised
    assert gitops.fetch(origin) == "no remote configured"


def test_worktree_starts_at_given_base(tmp_path):
    """A task's worktree is cut from the chosen point, so the operator's
    uncommitted work never leaks into it."""
    from firstmate.exec import gitops

    repo = tmp_path / "wt"
    gitops.init_repo(repo)
    first = _commit(repo, "one", f__txt="one\n")
    _commit(repo, "two", f__txt="two\n")
    (repo / "f.txt").write_text("uncommitted mess\n")
    (repo / "untracked.txt").write_text("also mess\n")

    older = gitops.create_worktree(repo, "fm/from-first", first)
    assert (older / "f.txt").read_text() == "one\n"
    assert not (older / "untracked.txt").exists()
    assert gitops.head_commit(older) == first
    assert gitops.changed_files(older) == []

    # Idempotent: a second call reuses it rather than re-cutting.
    assert gitops.create_worktree(repo, "fm/from-first", "HEAD") == older
    assert gitops.head_commit(older) == first

    gitops.remove_worktree(repo, "fm/from-first")
    assert not older.exists()
    gitops.delete_branch(repo, "fm/from-first", force=True)
    branches = gitops._git(repo, "branch", "--format=%(refname:short)").stdout.split()
    assert "fm/from-first" not in branches
    # deleting a branch that isn't there is a quiet no-op
    gitops.delete_branch(repo, "fm/never-existed")


# ---- the prompt must not travel through tmux's command line ----
# A real reach-plan task died at generation 6 with `tmux: command too
# long` after three grilling rounds folded operator answers into the step
# prompt — losing an already-approved plan (STATUS 2026-08-20).


def shell_command_for(spec) -> str:
    """What `spawn` would hand to `sh -c`, without touching tmux."""
    import shlex

    placeholder = "\x00PROMPT\x00"
    joined = shlex.join(spawner.build_command(spec, prompt=placeholder))
    pfile = spawner.prompt_file(spec)
    return joined.replace(shlex.quote(placeholder),
                          f'"$(cat {shlex.quote(str(pfile))})"')


def test_command_length_does_not_grow_with_the_prompt(tmp_path: Path):
    sid = "11111111-2222-3333-4444-555555555555"  # pin: only prompt may vary
    short = spawner.WorkerSpec(prompt="do the thing", cwd=tmp_path, name="w",
                               session_id=sid)
    huge = spawner.WorkerSpec(prompt="x" * 60_000, cwd=tmp_path, name="w",
                              session_id=sid)
    a, b = shell_command_for(short), shell_command_for(huge)
    assert a == b, "the command must not vary with prompt content at all"
    # tmux 3.7b refuses somewhere between 16K and 17K.
    assert len(b) < 4000, len(b)


def test_prompt_is_referenced_by_file_not_inlined(tmp_path: Path):
    spec = spawner.WorkerSpec(prompt="SECRET-MARKER-TEXT", cwd=tmp_path, name="w")
    cmd = shell_command_for(spec)
    assert "SECRET-MARKER-TEXT" not in cmd
    assert '"$(cat ' in cmd
    assert str(spawner.prompt_file(spec)) in cmd


def test_spawn_writes_the_prompt_file(tmp_path: Path, monkeypatch):
    """The file must exist before the window starts, or `cat` gets nothing."""
    seen = {}

    def fake_new_window(name, argv, cwd=None):
        # By now the prompt file must already be on disk.
        seen["exists"] = spawner.prompt_file(spec).exists()
        seen["text"] = spawner.prompt_file(spec).read_text()
        seen["argv"] = argv
        return spawner.tmux.Window("@1", name)

    monkeypatch.setattr(spawner.tmux, "new_window", fake_new_window)
    spec = spawner.WorkerSpec(prompt="a very long prompt " * 2000,
                              cwd=tmp_path, name="w")
    spawner.spawn(spec)
    assert seen["exists"] is True
    assert seen["text"] == spec.prompt
    assert len(seen["argv"][2]) < 4000


def test_quoting_survives_a_prompt_full_of_shell_metacharacters(tmp_path: Path):
    """The prompt now contains operator answers verbatim — quotes,
    backticks and $() included. None of it may be interpreted."""
    nasty = """He said "don't" & `whoami`; $(echo pwned) | rm -rf / #done"""
    spec = spawner.WorkerSpec(prompt=nasty, cwd=tmp_path, name="w")
    cmd = shell_command_for(spec)
    for frag in ("whoami", "rm -rf", "echo pwned"):
        assert frag not in cmd, f"{frag} leaked into the command line"
    spawner.spawn.__doc__  # documented rationale
