"""Detecting a worker that ended its turn asking a human who isn't there.

Seen for real on a reach-plan step (STATUS 2026-08-20): it played back its
understanding of the issue and closed with "Let me know if I've
mischaracterized anything before I dig in." tmux saw a clean exit, so the
orchestrator validated an empty worktree, failed the receipt criterion and
burned attempt 1 — for a session that had asked nobody anything.

The detector is deliberately narrow: it reads only the tail of the reply,
so a report that merely *discusses* open questions is not caught.
"""

import pytest

from firstmate.spawner import asked_the_void

# The actual text from the task that prompted this (trimmed).
REAL_GEN1_TAIL = (
    "Affected repo: `reach-app` only (frontend, likely no backend changes "
    "since the pre-scoping audit says the data layer already exists "
    "end-to-end) — I'll verify this rather than assume.\n\n"
    "I'll now move to Phase 1 and audit the actual code (ContentSidebar, "
    "ContextTab, RoadmapItemBrief, RankingAnalysisPanel, "
    "WritingContextPanel, and the editor/roadmap page mounting) with "
    "parallel Explore agents before grilling. Let me know if I've "
    "mischaracterized anything before I dig in."
)

# The same step's next generation, which behaved correctly: it used
# `fm ask` and said so. This must NOT fire.
REAL_GEN2_TAIL = (
    "Per the task rules, I'm stopping now and ending the session so the "
    "orchestrator can resume with the operator's answers. Progress has "
    "been recorded via `fm skill` so the next session can pick up "
    "directly at Phase 2 grilling without re-running the audit."
)


def test_fires_on_the_real_case():
    assert asked_the_void(REAL_GEN1_TAIL) == "let me know"


def test_silent_on_the_real_correct_case():
    assert asked_the_void(REAL_GEN2_TAIL) is None


@pytest.mark.parametrize("text", [
    "Let me know if I've mischaracterized anything before I dig in.",
    "I've drafted the plan. Thoughts?",
    "Should I use sub-tabs or collapsible sections?",
    "Shall I proceed with the narrow-rail approach?",
    "Waiting for your confirmation before proceeding.",
    "Awaiting your approval.",
    "Does this sound right?",
    "Which one would you prefer?",
    "Please confirm the scope before I continue.",
    "Can you clarify which sidebar you meant?",
    "Do you want me to include page_revamp docs?",
    "Before I proceed, I need to know the phasing.",
    "So: which surface do you actually mean?",
])
def test_fires_on_check_ins(text):
    assert asked_the_void(text) is not None, text


@pytest.mark.parametrize("text", [
    "Refactored the parser; all tests pass.",
    "The build is green. Deployed to staging.",
    "I raised the ambiguity with `fm ask` and stopped as instructed.",
    "Open questions about the layout remain, recorded as findings. "
    "Implementation is complete and validated.",
    "I could not determine whether you want X, so I assumed X and continued.",
    "Wrote the receipt to .fm/artifacts/eng-654-plan-receipt.json.",
    "Step complete: 4 files changed, 2 tests added.",
    # discusses questions, but does not end by asking one
    "The plan lists three open questions for later review. Done.",
    "",
    "   ",
])
def test_silent_on_ordinary_completions(text):
    assert asked_the_void(text) is None, text


def test_only_the_tail_counts():
    """A check-in phrase early in a long reply that ends with real work
    done is not a void-ask — the worker kept going."""
    text = ("Should I use sub-tabs here? I decided yes, on the grounds that "
            "the rail is too narrow for a merged stream.\n\n"
            "Implemented it, added tests, and everything passes.")
    assert asked_the_void(text) is None


def test_a_bare_question_needs_to_address_the_operator():
    # A rhetorical/technical question is not a request for input.
    assert asked_the_void("Why did the poll time out?") is None
    assert asked_the_void("Which layout do you want?") is not None


def test_none_and_nonstring_are_safe():
    assert asked_the_void("") is None
    assert asked_the_void(None) is None
