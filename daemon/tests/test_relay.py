"""Context relay — handoff acquisition, including the skill-aware path.

The skill path exists because prose alone lost a real task its audit three
times over (STATUS 2026-08-20): the dying session must flush durable state
into .fm/skill-state.json before it writes any narrative.
"""

import json
import subprocess

from firstmate import relay


class FakeProc:
    returncode = 0
    stderr = ""

    def __init__(self, result: str):
        self.stdout = json.dumps({"result": result})


def capture(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return FakeProc("DONE: x\nREMAINING: y\nGOTCHAS: z")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_plain_handoff_uses_no_tools(tmp_path, monkeypatch):
    calls = capture(monkeypatch)
    text = relay.request_handoff(tmp_path, "sid-1", "sonnet")
    assert "DONE: x" in text
    cmd = calls[0]
    # prose only: the session must not be able to touch anything
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""
    assert "--allowedTools" not in cmd
    assert "--resume" in cmd and "sid-1" in cmd
    assert "STOP working on the task" in cmd[2]
    assert "skill" not in cmd[2].lower()


def test_skill_handoff_can_write_state_and_says_so(tmp_path, monkeypatch):
    calls = capture(monkeypatch)
    relay.request_handoff(tmp_path, "sid-2", "sonnet", skill="reach-plan")
    cmd = calls[0]
    prompt = cmd[2]
    # it is told which skill it is in, and to flush state FIRST
    assert "reach-plan" in prompt
    assert "skill-state.json" in prompt
    assert "fm skill" in prompt
    assert prompt.index("FIRST") < prompt.index("THEN")
    # and it is granted exactly the tools that needs — no repo edits
    assert "--allowedTools" in cmd
    granted = cmd[cmd.index("--allowedTools") + 1]
    assert granted == "Bash(fm skill:*),Read"
    assert "Edit" not in granted and "Write" not in granted
    assert "--tools" not in cmd


def test_skill_handoff_tells_it_not_to_repeat_durable_facts(tmp_path, monkeypatch):
    calls = capture(monkeypatch)
    relay.request_handoff(tmp_path, "sid-3", "sonnet", skill="reach-plan")
    prompt = calls[0][2]
    assert "do not repeat them" in prompt
    assert "cannot see this conversation" in prompt


def test_handoff_failure_is_returned_not_raised(tmp_path, monkeypatch):
    class Bad(FakeProc):
        returncode = 1
        stderr = "boom"

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: Bad(""))
    text = relay.request_handoff(tmp_path, "sid", "sonnet")
    assert "handoff request failed" in text


def test_handoff_timeout_is_returned_not_raised(tmp_path, monkeypatch):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(subprocess, "run", boom)
    assert "timed out" in relay.request_handoff(tmp_path, "sid", "sonnet")


def test_handoff_non_json_is_returned_not_raised(tmp_path, monkeypatch):
    class Junk:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: Junk())
    assert "non-JSON" in relay.request_handoff(tmp_path, "sid", "sonnet")
