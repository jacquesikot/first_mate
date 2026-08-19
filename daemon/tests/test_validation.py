"""Shell-criterion validation and evidence capture."""

from firstmate.models import Criterion
from firstmate.validation import run_criteria, run_criterion


def test_passing_criterion(tmp_path):
    r = run_criterion(tmp_path, Criterion(id="ok", command="echo hi"))
    assert r.passed and r.exit_status == 0 and "hi" in r.stdout


def test_failing_criterion_keeps_evidence(tmp_path):
    r = run_criterion(tmp_path, Criterion(id="no", command="echo bad >&2; exit 3"))
    assert not r.passed and r.exit_status == 3 and "bad" in r.stderr


def test_timeout(tmp_path):
    r = run_criterion(tmp_path, Criterion(id="slow", command="sleep 5", timeout=1))
    assert not r.passed and r.error and "timed out" in r.error


def test_cwd_is_relative_to_worktree(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "marker").write_text("x")
    r = run_criterion(tmp_path, Criterion(id="cwd", command="test -f marker", cwd="sub"))
    assert r.passed


def test_run_criteria_order(tmp_path):
    results = run_criteria(tmp_path, [
        Criterion(id="a", command="true"),
        Criterion(id="b", command="false"),
    ])
    assert [(r.id, r.passed) for r in results] == [("a", True), ("b", False)]
