"""Scoping — prompt/command building, contract checking, session flow."""

import json
from pathlib import Path

from firstmate import scoping


def test_build_prompt_contains_the_essentials(tmp_path):
    prompt = scoping.build_prompt(
        "add rate limiting", tmp_path, tmp_path / "contract.json",
        memory="- always run make lint",
    )
    for needle in ("add rate limiting", str(tmp_path), "machine-checkable",
                   "fm contract check", "always run make lint",
                   json.dumps(str(tmp_path))):
        assert needle in prompt, f"missing: {needle}"


def test_build_prompt_without_memory(tmp_path):
    prompt = scoping.build_prompt("goal", tmp_path, tmp_path / "c.json", memory=None)
    assert "(no project memory yet)" in prompt


def test_build_command_allowlist(tmp_path):
    cmd = scoping.build_command("PROMPT", tmp_path, model="opus")
    assert cmd[0] == "claude" and cmd[1] == "PROMPT"
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert f"Write(/{tmp_path}/**)" in allowed  # //abs/path permission syntax
    assert "Bash(fm contract check:*)" in allowed
    assert "Edit" not in allowed  # scoping never edits the repo
    assert cmd[-2:] == ["--model", "opus"]
    assert "--model" not in scoping.build_command("P", tmp_path)


def test_check_contract_file(tmp_path):
    missing = scoping.check_contract_file(tmp_path / "nope.json")
    assert any("no such file" in e for e in missing)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert any("not valid JSON" in e for e in scoping.check_contract_file(bad))
    vague = tmp_path / "vague.json"
    vague.write_text(json.dumps({
        "goal": "g", "repo": str(tmp_path),
        "steps": [{"id": "s1", "prompt": "p", "criteria": ["c1"]}],
        "criteria": [{"id": "c1", "command": ""}],
    }))
    assert any("machine-checkable" in e for e in scoping.check_contract_file(vague))
    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "goal": "g", "repo": str(tmp_path),
        "steps": [{"id": "s1", "prompt": "p", "criteria": ["c1"]}],
        "criteria": [{"id": "c1", "command": "true"}],
    }))
    assert scoping.check_contract_file(good) == []
    ghost = tmp_path / "ghost.json"
    ghost.write_text(json.dumps({
        "goal": "g", "repo": "/no/such/dir",
        "steps": [{"id": "s1", "prompt": "p"}], "criteria": [],
    }))
    assert any("repo path does not exist" in e for e in scoping.check_contract_file(ghost))


def test_run_scoping_collects_written_contract(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    (home / "memory").mkdir(parents=True)
    (home / "memory" / "repo.md").write_text("- lesson\n")

    def fake_claude(cmd, cwd=None):
        # The "session" writes a valid contract to the path in the prompt.
        prompt = cmd[1]
        path = next(line.split(": ", 1)[1] for line in prompt.splitlines()
                    if line.startswith("1. Write the contract as JSON to exactly this path: "))
        Path(path).write_text(json.dumps({
            "goal": "g", "repo": str(repo),
            "steps": [{"id": "s1", "prompt": "p", "criteria": ["c1"]}],
            "criteria": [{"id": "c1", "command": "true"}],
        }))
        assert "lesson" in prompt  # memory was injected

    monkeypatch.setattr(scoping.subprocess, "run", fake_claude)
    result = scoping.run_scoping("my goal", repo, home)
    assert result.errors == []
    assert result.contract["goal"] == "g"
    assert (result.contract_path.parent / "prompt.md").exists()


def test_run_scoping_without_contract_reports(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(scoping.subprocess, "run", lambda cmd, cwd=None: None)
    result = scoping.run_scoping("goal", repo, tmp_path / "home")
    assert result.contract is None
    assert any("without writing a contract" in e for e in result.errors)
