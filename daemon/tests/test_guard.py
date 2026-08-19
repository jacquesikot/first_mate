"""Scope guard — glob matching, path checks, bash heuristics, tripwires."""

from firstmate.guard import (
    build_config, check_bash, check_path, evaluate, glob_to_regex, matches_any,
)
from firstmate.models import Contract, validate_contract


def cfg(tmp_path, **kw):
    base = {
        "worktree": str(tmp_path),
        "scope_in": ["src/**", "tests/**"],
        "scope_out": ["src/legacy/**"],
        "tripwires": {},
        "tripwire_allow": [],
    }
    base.update(kw)
    return base


# --------------------------------------------------------------- globs


def test_glob_matching():
    assert matches_any("src/a/b.py", ["src/**"])
    assert not matches_any("srcx/a.py", ["src/**"])
    assert matches_any("anything/at/all.txt", ["**"])
    assert matches_any("README.md", ["*.md"])
    assert not matches_any("docs/README.md", ["*.md"])
    assert matches_any("app/migrations/0001_init.py", ["**/migrations/**"])
    assert matches_any("migrations/0001_init.py", ["**/migrations/**"])
    assert glob_to_regex("a?c") == "a[^/]c"


# ---------------------------------------------------------- file tools


def test_edit_in_scope_allowed(tmp_path):
    d = evaluate(cfg(tmp_path), "Edit", {"file_path": str(tmp_path / "src/app.py")})
    assert d.allowed


def test_edit_out_of_scope_blocked_with_ask_hint(tmp_path):
    d = evaluate(cfg(tmp_path), "Edit", {"file_path": str(tmp_path / "docs/x.md")})
    assert not d.allowed and d.code == "out_of_scope"
    assert "fm ask --type scope_change" in d.message
    assert '"docs/x.md"' in d.message  # evidence carries the path


def test_scope_out_excludes(tmp_path):
    d = check_path(cfg(tmp_path), str(tmp_path / "src/legacy/old.py"))
    assert not d.allowed and d.code == "out_of_scope"


def test_relative_paths_resolve_against_worktree(tmp_path):
    assert check_path(cfg(tmp_path), "src/app.py").allowed
    assert not check_path(cfg(tmp_path), "docs/x.md").allowed


def test_outside_worktree_blocked(tmp_path):
    d = check_path(cfg(tmp_path), "/etc/hosts")
    assert not d.allowed and d.code == "outside_worktree"
    # Non-temp worktree (paths are never touched on disk): ../ escapes block.
    c = cfg("/Users/nobody/proj")
    d = check_path(c, "../sibling/file.py")
    assert not d.allowed and d.code == "outside_worktree"


def test_dotdot_inside_worktree_ok(tmp_path):
    assert check_path(cfg(tmp_path), "src/sub/../app.py").allowed


def test_fm_dir_protected(tmp_path):
    d = check_path(cfg(tmp_path, scope_in=["**"]), ".fm/guard.json")
    assert not d.allowed and d.code == "fm_owned"


def test_temp_paths_outside_worktree_allowed(tmp_path):
    assert check_path(cfg(tmp_path), "/tmp/scratch.txt").allowed
    assert check_path(cfg(tmp_path), "/dev/null").allowed


def test_worktree_under_tmp_still_enforced():
    # Regression: the scratch exemption must not bypass scope for
    # worktrees that live under a temp prefix themselves.
    c = {"worktree": "/tmp/fm-wt", "scope_in": ["src/**"], "scope_out": [],
         "tripwires": {}, "tripwire_allow": []}
    assert check_path(c, "/tmp/fm-wt/src/app.py").allowed
    d = check_path(c, "/tmp/fm-wt/docs/x.md")
    assert not d.allowed and d.code == "out_of_scope"


def test_unknown_tool_passes(tmp_path):
    assert evaluate(cfg(tmp_path), "Glob", {"pattern": "**"}).allowed


# ------------------------------------------------------- path tripwires


def test_dependency_manifest_tripwire(tmp_path):
    c = cfg(tmp_path, scope_in=["**"])
    d = check_path(c, "package.json")
    assert not d.allowed and d.tripwire == "dependency_manifests"
    assert "fm ask --type approval" in d.message


def test_tripwire_disabled_by_contract(tmp_path):
    c = cfg(tmp_path, scope_in=["**"], tripwires={"dependency_manifests": False})
    assert check_path(c, "package.json").allowed


def test_tripwire_allow_exempts_path(tmp_path):
    c = cfg(tmp_path, scope_in=["**"], tripwire_allow=["package.json"])
    assert check_path(c, "package.json").allowed


def test_migration_tripwire(tmp_path):
    c = cfg(tmp_path, scope_in=["**"])
    d = check_path(c, "app/migrations/0002_add_col.py")
    assert not d.allowed and d.tripwire == "migrations"


# ---------------------------------------------------------------- bash


def test_git_push_tripwire(tmp_path):
    d = check_bash(cfg(tmp_path), "git push origin main")
    assert not d.allowed and d.tripwire == "git_push"
    c = cfg(tmp_path, tripwires={"git_push": False})
    assert check_bash(c, "git push origin main").allowed


def test_git_push_detected_in_compound_command(tmp_path):
    d = check_bash(cfg(tmp_path), "git add -A && git commit -m x && git push")
    assert not d.allowed and d.tripwire == "git_push"


def test_git_push_raw_fallback_on_unparseable(tmp_path):
    d = check_bash(cfg(tmp_path), 'git push "unclosed')
    assert not d.allowed and d.tripwire == "git_push"


def test_bash_redirect_out_of_scope_blocked(tmp_path):
    d = check_bash(cfg(tmp_path), "echo hi > docs/notes.md")
    assert not d.allowed and d.code == "out_of_scope"
    assert check_bash(cfg(tmp_path), "echo hi > src/notes.txt").allowed
    assert check_bash(cfg(tmp_path), "echo hi > /tmp/notes.txt").allowed


def test_bash_read_redirect_not_a_write(tmp_path):
    assert check_bash(cfg(tmp_path), "wc -l < docs/notes.md").allowed


def test_bash_write_commands_checked(tmp_path):
    assert not check_bash(cfg(tmp_path), "rm docs/old.md").allowed
    assert not check_bash(cfg(tmp_path), "mv src/a.py docs/a.py").allowed
    assert not check_bash(cfg("/Users/nobody/proj"), "cp src/a.py ../elsewhere.py").allowed
    assert not check_bash(cfg(tmp_path), "sed -i '' s/a/b/ docs/x.md").allowed
    assert check_bash(cfg(tmp_path), "mkdir -p src/newdir").allowed
    assert check_bash(cfg(tmp_path), "grep -r foo docs/").allowed  # reads are fine


def test_bash_dependency_installs(tmp_path):
    c = cfg(tmp_path)
    assert not check_bash(c, "npm install lodash").allowed
    assert not check_bash(c, "pnpm add -D vitest").allowed
    assert not check_bash(c, "uv add httpx").allowed
    assert not check_bash(c, "pip install requests").allowed
    assert not check_bash(c, "go get example.com/pkg").allowed
    assert check_bash(c, "npm install").allowed  # lockfile restore
    assert check_bash(c, "npm ci").allowed
    assert check_bash(c, "pip install -r requirements.txt").allowed
    assert check_bash(c, "npm test").allowed


# ------------------------------------------------------------- config


def test_build_config_merges_layers(tmp_path):
    contract = Contract.from_dict({
        "goal": "g", "repo": "/tmp/r",
        "scope_in": ["src/**"],
        "tripwires": {"git_push": False},
        "tripwire_allow": ["package.json"],
        "steps": [{"id": "s1", "prompt": "p"}],
    })
    conf = build_config(contract, {"tripwires": {"max_diff_lines": 100}}, tmp_path)
    assert conf["worktree"] == str(tmp_path)
    assert conf["scope_in"] == ["src/**"]
    assert conf["tripwires"]["git_push"] is False        # contract override
    assert conf["tripwires"]["max_diff_lines"] == 100    # project override
    assert conf["tripwires"]["migrations"] is True       # default
    assert conf["tripwire_allow"] == ["package.json"]


def test_validate_contract_tripwire_and_scope_fields():
    base = {
        "goal": "g", "repo": "/tmp/r",
        "steps": [{"id": "s1", "prompt": "p"}],
    }
    assert validate_contract({**base, "tripwires": {"git_push": False}}) == []
    errs = validate_contract({**base, "tripwires": {"gitpush": False}})
    assert any("unknown tripwire" in e for e in errs)
    errs = validate_contract({**base, "scope_in": ["ok", 3]})
    assert any("scope_in" in e for e in errs)
    errs = validate_contract({**base, "tripwires": "nope"})
    assert any("tripwires must be an object" in e for e in errs)
