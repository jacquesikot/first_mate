"""Domain model round-tripping and contract validation."""

from firstmate.models import (
    Contract, Question, Task, slugify, validate_contract,
)

CONTRACT = {
    "goal": "Add auth check",
    "repo": "/tmp/repo",
    "context": "JWT already wired",
    "steps": [
        {"id": "implement", "prompt": "do it", "criteria": ["tests"]},
        {"id": "review", "prompt": "review it", "allowed_tools": ["Read"]},
    ],
    "criteria": [{"id": "tests", "command": "pytest -q", "timeout": 120}],
}


def test_contract_roundtrip():
    c = Contract.from_dict(CONTRACT)
    assert c.steps[1].allowed_tools == ["Read"]
    assert c.criterion("tests").timeout == 120
    d = c.to_dict()
    again = Contract.from_dict(d)
    assert again.to_dict() == d


def test_contract_markdown_render():
    c = Contract.from_dict(CONTRACT)
    c.amendments.append({"at": "2026-08-19", "question": "Q?", "answer": "A."})
    md = c.render_markdown()
    for needle in ("Add auth check", "implement", "pytest -q", "JWT already wired",
                   "Q?", "A."):
        assert needle in md


def test_validate_contract_ok():
    assert validate_contract(CONTRACT) == []


def test_validate_contract_catches_problems():
    bad = {
        "goal": "",
        "repo": "",
        "steps": [
            {"id": "a", "prompt": ""},
            {"id": "a", "prompt": "x", "criteria": ["nope"]},
        ],
        "criteria": [{"id": "c1", "command": ""}, {"id": "c1", "command": "x", "kind": "playwright"}],
    }
    errors = " | ".join(validate_contract(bad))
    for needle in ("goal is required", "repo is required", "no prompt",
                   "duplicate step id", "unknown criterion 'nope'",
                   "machine-checkable", "duplicate criterion id",
                   "not supported in Phase 1"):
        assert needle in errors, f"missing: {needle}"


def test_task_and_question_roundtrip():
    t = Task(id="t1", repo="/r", branch="fm/t1",
             steps=[])
    assert Task.from_dict(t.to_dict()).to_dict() == t.to_dict()
    q = Question(id="q1", task_id="t1", type="decision", question="?",
                 options=["a", "b"])
    assert Question.from_dict(q.to_dict()).to_dict() == q.to_dict()


def test_slugify():
    assert slugify("Fix the (weird) AUTH bug!!") == "fix-the-weird-auth-bug"
    assert slugify("???") == "task"
