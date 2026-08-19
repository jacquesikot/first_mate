"""CLI — the hook-facing `fm _guard` and `fm contract check`."""

import io
import json
import sys

from firstmate.cli import main

UNREACHABLE = "http://127.0.0.1:1"  # connection refused instantly


def guard_config(tmp_path) -> str:
    path = tmp_path / "guard.json"
    path.write_text(json.dumps({
        "worktree": str(tmp_path),
        "scope_in": ["src/**"],
        "scope_out": [],
        "tripwire_allow": [],
        "tripwires": {},
    }))
    return str(path)


def run_guard(tmp_path, monkeypatch, payload: dict, fallback: str | None = None) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    argv = ["_guard", "--config", guard_config(tmp_path),
            "--task", "t1", "--step", "s1", "--url", UNREACHABLE]
    if fallback:
        argv += ["--fallback", fallback]
    return main(argv)


def test_guard_allows_in_scope(tmp_path, monkeypatch):
    code = run_guard(tmp_path, monkeypatch, {
        "tool_name": "Edit", "tool_input": {"file_path": "src/app.py"}})
    assert code == 0


def test_guard_blocks_out_of_scope(tmp_path, monkeypatch, capsys):
    fallback = str(tmp_path / "fallback.jsonl")
    code = run_guard(tmp_path, monkeypatch, {
        "tool_name": "Write", "tool_input": {"file_path": "docs/x.md"}},
        fallback=fallback)
    assert code == 2
    err = capsys.readouterr().err
    assert "scope guard: BLOCKED" in err and "fm ask" in err
    events = [json.loads(l) for l in open(fallback)]
    assert events[0]["event"] == "GuardBlock"
    assert events[0]["payload"]["code"] == "out_of_scope"


def test_guard_without_config_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert main(["_guard", "--config", str(tmp_path / "missing.json"),
                 "--task", "t1", "--url", UNREACHABLE]) == 0


def test_guard_garbage_stdin_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    argv = ["_guard", "--config", guard_config(tmp_path),
            "--task", "t1", "--url", UNREACHABLE]
    assert main(argv) == 0  # no tool_name → nothing to judge


def test_contract_check_command(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "goal": "g", "repo": str(tmp_path),
        "steps": [{"id": "s1", "prompt": "p", "criteria": ["c1"]}],
        "criteria": [{"id": "c1", "command": "true"}],
    }))
    assert main(["contract", "check", str(good)]) == 0
    assert "contract OK" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"goal": "", "repo": "", "steps": []}))
    assert main(["contract", "check", str(bad)]) == 1
    assert "goal is required" in capsys.readouterr().out
