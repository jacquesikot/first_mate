"""Skill state — durable progress across a context wall (STATUS 2026-08-20).

The failure this guards against: a reach-plan task re-ran its entire repo
audit in three successive generations, because the only thing carried
across the wall was prose written by a dying session.
"""

import json

from firstmate import skillstate
from firstmate.cli import main


def test_seed_then_load(tmp_path):
    skillstate.seed(tmp_path, "reach-plan")
    state = skillstate.load(tmp_path)
    assert state["skill"] == "reach-plan"
    assert state["phase"] == "starting"
    # the file is plain and jq-readable (acceptance criterion 10)
    raw = json.loads((tmp_path / ".fm" / "skill-state.json").read_text())
    assert raw["skill"] == "reach-plan"


def test_seed_does_not_clobber_existing_progress(tmp_path):
    skillstate.seed(tmp_path, "reach-plan")
    skillstate.merge(tmp_path, {"phase": "2-grilling",
                                "findings": ["ContextTab renders X"]})
    skillstate.seed(tmp_path, "reach-plan")  # next generation starts
    state = skillstate.load(tmp_path)
    assert state["phase"] == "2-grilling"
    assert state["findings"] == ["ContextTab renders X"]


def test_missing_state_is_none(tmp_path):
    assert skillstate.load(tmp_path) is None
    assert skillstate.render(None) == ""


def test_lists_union_rather_than_replace(tmp_path):
    """The regression that mattered: a later session recording one new
    finding must not drop the nine an earlier one established."""
    skillstate.merge(tmp_path, {"findings": ["a", "b"]})
    skillstate.merge(tmp_path, {"findings": ["c"]})
    assert skillstate.load(tmp_path)["findings"] == ["a", "b", "c"]
    # and a repeat does not duplicate
    skillstate.merge(tmp_path, {"findings": ["b"]})
    assert skillstate.load(tmp_path)["findings"] == ["a", "b", "c"]


def test_dicts_merge_key_wise(tmp_path):
    skillstate.merge(tmp_path, {"decided": {"tab": "keep both"}})
    skillstate.merge(tmp_path, {"decided": {"layout": "narrow blocks"}})
    assert skillstate.load(tmp_path)["decided"] == {
        "tab": "keep both", "layout": "narrow blocks"}


def test_scalars_overwrite(tmp_path):
    skillstate.merge(tmp_path, {"phase": "1-audit"})
    skillstate.merge(tmp_path, {"phase": "2-grilling"})
    assert skillstate.load(tmp_path)["phase"] == "2-grilling"


def test_none_values_are_ignored(tmp_path):
    skillstate.merge(tmp_path, {"phase": "1-audit"})
    skillstate.merge(tmp_path, {"phase": None, "notes": "x"})
    state = skillstate.load(tmp_path)
    assert state["phase"] == "1-audit" and state["notes"] == "x"


def test_unreadable_state_is_reported_not_raised(tmp_path):
    p = skillstate.path_for(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not json")
    state = skillstate.load(tmp_path)
    assert "_unreadable" in state
    # and it renders as an instruction rather than crashing the relay
    assert "could not be read" in skillstate.render(state)


def test_non_object_state_is_reported(tmp_path):
    p = skillstate.path_for(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[1, 2]")
    assert "_unreadable" in skillstate.load(tmp_path)


def test_render_tells_the_successor_what_not_to_redo(tmp_path):
    skillstate.merge(tmp_path, {
        "skill": "reach-plan",
        "phase": "2-grilling",
        "phases_done": ["1-audit"],
        "findings": ["ContextTab renders GenerationContextApi"],
        "decided": {"tab": "keep both"},
        "outstanding": ["ask about phasing", "ask about testing"],
        "artifacts": [".fm/artifacts/round1.json"],
        "rounds_asked": 1,
    })
    out = skillstate.render(skillstate.load(tmp_path))
    assert "reach-plan" in out and "2-grilling" in out
    assert "do NOT redo" in out and "1-audit" in out
    assert "ContextTab renders GenerationContextApi" in out
    assert "trust these" in out
    assert "do not re-open or re-ask" in out and "keep both" in out
    assert "ask about phasing" in out
    assert ".fm/artifacts/round1.json" in out
    assert "rounds already asked" in out.lower()


def test_render_keeps_unknown_keys(tmp_path):
    """A skill may record its own shape; nothing should be silently lost."""
    skillstate.merge(tmp_path, {"skill": "s", "issue": "ENG-654"})
    assert "ENG-654" in skillstate.render(skillstate.load(tmp_path))


# ---------------------------------------------------------- the CLI surface


def test_cli_records_and_shows(tmp_path, capsys):
    argv = ["skill", "--worktree", str(tmp_path), "--skill", "reach-plan",
            "--phase", "1-audit",
            "--finding", "sidebar is w-88 (352px)",
            "--outstanding", "ask about phasing",
            "--decided", "tab=keep both"]
    assert main(argv) == 0
    state = skillstate.load(tmp_path)
    assert state["skill"] == "reach-plan"
    assert state["findings"] == ["sidebar is w-88 (352px)"]
    assert state["decided"] == {"tab": "keep both"}

    capsys.readouterr()
    assert main(["skill", "show", "--worktree", str(tmp_path)]) == 0
    assert "reach-plan" in capsys.readouterr().out


def test_cli_show_with_no_state(tmp_path, capsys):
    assert main(["skill", "show", "--worktree", str(tmp_path)]) == 0
    assert "no skill state" in capsys.readouterr().out


def test_cli_resolve_removes_an_outstanding_item(tmp_path):
    main(["skill", "--worktree", str(tmp_path),
          "--outstanding", "ask about phasing",
          "--outstanding", "ask about testing"])
    main(["skill", "--worktree", str(tmp_path),
          "--resolve", "ask about phasing"])
    assert skillstate.load(tmp_path)["outstanding"] == ["ask about testing"]


def test_cli_rejects_malformed_decided(tmp_path, capsys):
    code = main(["skill", "--worktree", str(tmp_path), "--decided", "nokey"])
    assert code == 1
    assert "key=value" in capsys.readouterr().err


def test_cli_rejects_an_empty_update(tmp_path, capsys):
    assert main(["skill", "--worktree", str(tmp_path)]) == 1
    assert "nothing to record" in capsys.readouterr().err


def test_cli_uses_fm_worktree_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FM_WORKTREE", str(tmp_path))
    assert main(["skill", "--phase", "3-draft"]) == 0
    assert skillstate.load(tmp_path)["phase"] == "3-draft"
