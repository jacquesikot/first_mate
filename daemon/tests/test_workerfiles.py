"""Worker hook generation and context-injection building."""

import json

from firstmate.models import Contract, Question
from firstmate.workerfiles import build_inject, write_inject, write_worker_hooks

CONTRACT = Contract.from_dict({
    "goal": "wf test",
    "repo": "/tmp/repo",
    "steps": [{"id": "s1", "prompt": "do the thing", "criteria": ["c1"]}],
    "criteria": [{"id": "c1", "command": "true"}],
})


def test_write_worker_hooks_bakes_values(tmp_path):
    settings = write_worker_hooks(tmp_path, "task-1", "s1",
                                  "http://127.0.0.1:9999", "/opt/bin/fm")
    data = json.loads(settings.read_text())
    assert set(data["hooks"]) == {"SessionStart", "PreCompact", "Stop"}
    start = (tmp_path / ".fm" / "hooks" / "session_start.sh").read_text()
    assert "/opt/bin/fm" in start
    assert "--task task-1" in start
    assert "--url http://127.0.0.1:9999" in start
    assert "additionalContext" in start
    compact = (tmp_path / ".fm" / "hooks" / "pre_compact.sh").read_text()
    assert "exit 2" in compact


def test_write_worker_hooks_regenerates_without_duplicates(tmp_path):
    write_worker_hooks(tmp_path, "task-1", "s1", "http://a", "/bin/fm")
    settings = write_worker_hooks(tmp_path, "task-1", "s2", "http://b", "/bin/fm")
    data = json.loads(settings.read_text())
    assert len(data["hooks"]["SessionStart"]) == 1
    assert "--step s2" in (tmp_path / ".fm" / "hooks" / "stop.sh").read_text()


def test_build_inject_sections():
    q = Question(id="q1", task_id="t", type="decision", question="Which color?",
                 status="answered", answer="red")
    text = build_inject(
        CONTRACT, CONTRACT.steps[0], generation=3, attempt=2,
        memory="- lesson one", handoff="DONE: half of it",
        answered=[q], retry_note="c1: exit 1",
    )
    for needle in ("generation 3, attempt 2", "wf test", "Project memory",
                   "lesson one", "Which color?", "A: red",
                   "DONE: half of it", "failed validation", "c1: exit 1"):
        assert needle in text, f"missing: {needle}"


def test_build_inject_minimal_omits_empty_sections():
    text = build_inject(CONTRACT, CONTRACT.steps[0], generation=1, attempt=1)
    assert "Project memory" not in text
    assert "Handoff" not in text
    assert "Operator answers" not in text


def test_write_inject(tmp_path):
    path = write_inject(tmp_path, "hello\n")
    assert path.read_text() == "hello\n"
    assert path == tmp_path / ".fm" / "inject.md"


def test_write_worker_hooks_with_scope_guard(tmp_path):
    guard_config = {"worktree": str(tmp_path), "scope_in": ["src/**"],
                    "scope_out": [], "tripwire_allow": [], "tripwires": {}}
    settings = write_worker_hooks(tmp_path, "task-1", "s1", "http://127.0.0.1:9",
                                  "/opt/bin/fm", guard_config=guard_config)
    data = json.loads(settings.read_text())
    assert "PreToolUse" in data["hooks"]
    entry = data["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "Edit|Write|MultiEdit|NotebookEdit|Bash"
    script = (tmp_path / ".fm" / "hooks" / "pre_tool_use.sh").read_text()
    assert "_guard" in script and "guard.json" in script and "exit $?" in script
    stored = json.loads((tmp_path / ".fm" / "guard.json").read_text())
    assert stored["scope_in"] == ["src/**"]


def test_write_worker_hooks_without_guard_omits_pretooluse(tmp_path):
    settings = write_worker_hooks(tmp_path, "t", "s", "http://x", "/bin/fm")
    data = json.loads(settings.read_text())
    assert "PreToolUse" not in data["hooks"]


def test_scratch_dir_is_created_and_hidden_from_git(tmp_path):
    """The worker's scratch space must exist before it's needed, and must
    never surface in the operator's diff or a commit."""
    import subprocess

    from firstmate.exec import gitops

    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True,
                                    capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (repo / "README").write_text("x\n")
    run("add", "-A")
    run("commit", "-qm", "init")

    write_worker_hooks(
        repo, "t1", "s1", "http://x", "fm",
        guard_config={"worktree": str(repo), "scope_in": ["**"]})

    assert (repo / ".fm" / "artifacts").is_dir()
    (repo / ".fm" / "artifacts" / "draft.md").write_text("notes")
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                            capture_output=True, text=True).stdout
    assert status.strip() == "", f"scratch space leaked into git: {status!r}"
    assert gitops.changed_files(repo) == []


# ---- skill state and rounds in the injected context (STATUS 2026-08-20) ----


def test_build_inject_carries_skill_state_as_authoritative():
    from firstmate import skillstate

    state = skillstate.render({
        "skill": "reach-plan", "phase": "2-grilling",
        "phases_done": ["1-audit"],
        "findings": ["ContextTab renders GenerationContextApi"],
        "outstanding": ["ask about phasing"],
    })
    text = build_inject(CONTRACT, CONTRACT.steps[0], generation=4, attempt=1,
                        handoff="DONE: asked round 1", skill_state=state)
    assert "Skill progress so far" in text
    assert "authoritative" in text
    assert "ContextTab renders GenerationContextApi" in text
    # the successor is told not to redo the expensive part
    assert "Do NOT re-verify" in text
    assert "fm skill" in text
    # state precedes the prose handoff: it is the reliable half
    assert text.index("Skill progress so far") < text.index("Handoff from the")


def test_build_inject_omits_skill_state_when_absent():
    text = build_inject(CONTRACT, CONTRACT.steps[0], generation=1, attempt=1)
    assert "Skill progress" not in text


def test_build_inject_renders_a_round_answer_per_question():
    """A round's answers must arrive as separate binding decisions, not as
    one run-together string."""
    from firstmate.models import SubQuestion

    q = Question(
        id="q1", task_id="t", type="decision", question="2 forks to settle",
        status="answered", answer="rolled up",
        questions=[
            SubQuestion(id="q1", question="Keep the tab?", answer="keep both"),
            SubQuestion(id="q2", question="How to differentiate?",
                        answer="sub-tabs"),
        ],
    )
    text = build_inject(CONTRACT, CONTRACT.steps[0], generation=2, attempt=1,
                        answered=[q])
    assert "Keep the tab?" in text and "A: keep both" in text
    assert "How to differentiate?" in text and "A: sub-tabs" in text
