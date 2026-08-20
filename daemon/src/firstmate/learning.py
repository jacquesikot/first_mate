"""The memory loop — how First Mate stops relearning the same thing (PRD §6.6).

Project memory already existed as plumbing: one markdown file per project,
injected into every worker and every scoping session. What was missing is
everything that *writes* it apart from the operator typing `fm remember`.
Without that, every task starts as naive as the first one: the same wrong
test command, the same undocumented env var, the same "we use pnpm here"
discovered from scratch by a fresh worker that has no memory of the four
sessions that already hit it.

Three writers, in the order the PRD lists them:

  (b) **Learning extraction** — after a step that *struggled*, ask what
      project-specific fact would have saved the trouble. The gate matters
      more than the prompt: a step that passed on its first attempt taught
      nothing, and paying an LLM call per step to be told "write clear
      code" is how a memory file fills with advice nobody needs. So the
      call only fires when the step actually hit a wall (a retry, a
      generation handoff, a convergence loop, a supervisor repair), and
      the prompt's whole job is to refuse to answer when there's no
      durable fact — `{"fact": null}` is the expected reply most of the
      time and is not a failure.

  (c) **Promotion of recurring answers** — question fingerprinting already
      suppresses re-asks *within* a task. The same decision recurring in a
      *new* task is a different signal: it's not noise, it's a standing
      project fact the operator has now stated more than once. That earns
      a one-time "promote to memory?" suggestion, never a silent write —
      memory is the operator's data.

  Plus **compaction**: an append-only file grows, and a memory file large
  enough to crowd the context window stops being an asset. Past a
  threshold an LLM pass deduplicates and consolidates, and the
  pre-compaction text is archived first, because a lossy rewrite of the
  operator's own notes must always be recoverable.

Nothing here deletes or rewrites memory without leaving the previous
state on disk, and nothing writes to memory as a side effect of an LLM
call the operator can't see.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import Question, StepState, now_iso

# ------------------------------------------------------------- extraction

# A step is worth an extraction call only if it cost more than one clean
# pass. These are the marks the engine leaves on a step that struggled.
def step_struggled(st: StepState) -> bool:
    """True when the step hit a wall it had to work through.

    Deliberately cheap and mechanical — it reads the step state the engine
    already maintains, so it costs nothing and can't disagree with what
    actually happened.
    """
    return bool(
        st.attempt > 1                # failed validation at least once
        or st.generation > 1          # ran out of context and handed off
        or st.iteration > 0           # a convergence loop fired
        or st.criteria_diagnoses > 0  # the supervisor had to judge it
    )


def struggle_summary(st: StepState) -> str:
    """Plain-language account of *how* the step struggled, for the prompt."""
    bits = []
    if st.attempt > 1:
        bits.append(f"failed its criteria and retried ({st.attempt} attempts)")
    if st.generation > 1:
        bits.append(f"exhausted its context and handed off to a fresh "
                    f"session {st.generation - 1} time(s)")
    if st.iteration > 0:
        bits.append(f"went round a convergence loop {st.iteration} time(s)")
    if st.criteria_diagnoses > 0:
        bits.append("needed the supervisor to judge whether its checks "
                    "were satisfiable at all")
    return "; ".join(bits) or "completed without difficulty"


EXTRACT_PROMPT = """\
You are First Mate's learning-extraction step. A step of an autonomous \
coding task just succeeded, but only after difficulty. Your job is to \
decide whether that difficulty taught a durable, project-specific FACT \
worth writing to this project's long-term memory, where it will be \
injected into every future session for this repo.

## The step

id: {step_id}
goal of the task: {goal}

What the step was told to do:
{prompt}

## How it struggled

{struggle}

## Evidence from the run

{evidence}

## What to produce

Return ONLY a JSON object, no prose, with exactly these keys:

  "fact":   a single sentence stating the project-specific fact, or null
  "reason": one short sentence saying why you did or didn't record it

A fact qualifies ONLY if it is:
  - specific to THIS repository — a command, path, tool, version, \
convention, service, or gotcha that a newcomer to this repo could not \
guess and would waste time rediscovering;
  - durable — still true next week, not a description of this one task's \
state ("the PR is open", "the tests are failing right now");
  - actionable — it changes what a future session would DO.

Return null — which is the normal, expected answer — for anything that is:
  - generic engineering advice ("write tests", "read the error message", \
"check your assumptions", "commit small changes");
  - a restatement of what the step did or that it was hard;
  - already obvious from the repo's own README/CLAUDE.md/config files;
  - about the operator's preferences rather than the project's facts.

Most struggles teach nothing durable. Returning null is a correct, \
complete answer and is strongly preferred over recording filler — a \
memory file full of platitudes is worse than an empty one, because it \
crowds out the facts that matter and trains future sessions to skim it.

Write the fact so it stands alone, with no reference to "this step" or \
"the task" — a future session sees only the sentence.
"""


@dataclass
class Extraction:
    fact: str | None
    reason: str = ""
    errors: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


def _extract_json(text: str) -> dict | None:
    """Pull a JSON object out of a model reply, tolerating fenced code."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines()
                         if not l.startswith("```")).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# A "fact" shorter than this is never a fact; longer than this is an essay
# and won't survive as one memory line.
MIN_FACT_CHARS = 20
MAX_FACT_CHARS = 400

# Openers that mark generic advice slipping through the prompt's refusal.
# Cheap belt-and-braces: the model is asked not to produce these, and if it
# does anyway, they never reach the operator's memory file.
_GENERIC_MARKERS = (
    "always write", "be sure to", "make sure to", "remember to",
    "it is important to", "it's important to", "best practice",
    "in general,", "generally,", "you should always", "one should",
)


def parse_extraction(reply: str) -> Extraction:
    data = _extract_json(reply)
    if data is None:
        return Extraction(None, "", ["extraction reply was not JSON"], reply)
    reason = str(data.get("reason") or "").strip()
    raw_fact = data.get("fact")
    if raw_fact is None or not str(raw_fact).strip():
        return Extraction(None, reason or "nothing durable to record", [], reply)
    fact = " ".join(str(raw_fact).split())
    if len(fact) < MIN_FACT_CHARS or len(fact) > MAX_FACT_CHARS:
        return Extraction(None, f"rejected: fact was {len(fact)} chars", [],
                          reply)
    low = fact.lower()
    if any(low.startswith(m) or f" {m}" in low for m in _GENERIC_MARKERS):
        return Extraction(None, "rejected: generic advice, not a project fact",
                          [], reply)
    return Extraction(fact, reason, [], reply)


def build_extract_prompt(step_id: str, goal: str, step_prompt: str,
                         struggle: str, evidence: str) -> str:
    return EXTRACT_PROMPT.format(
        step_id=step_id,
        goal=goal.strip() or "(not recorded)",
        prompt=(step_prompt or "").strip()[:3000] or "(not recorded)",
        struggle=struggle.strip(),
        evidence=(evidence or "").strip()[:4000] or "(none captured)",
    )


def request_extraction(worktree: Path, step_id: str, goal: str,
                       step_prompt: str, struggle: str, evidence: str,
                       model: str, timeout: int = 180) -> Extraction:
    """Ask the model whether this struggle taught a durable project fact.

    Read-only by construction: no tools, so the call cannot touch the
    worktree it runs in.
    """
    prompt = build_extract_prompt(step_id, goal, step_prompt, struggle,
                                  evidence)
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--permission-mode", "dontAsk",
    ]
    try:
        proc = subprocess.run(cmd, cwd=worktree, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Extraction(None, "", [f"extraction timed out after {timeout}s"])
    except OSError as e:
        return Extraction(None, "", [f"extraction could not run: {e}"])
    if proc.returncode != 0:
        return Extraction(None, "", [f"extraction failed: exit "
                                     f"{proc.returncode}: {proc.stderr[-300:]}"])
    try:
        reply = json.loads(proc.stdout).get("result", "")
    except json.JSONDecodeError:
        return Extraction(None, "", ["extraction returned non-JSON envelope"],
                          proc.stdout[-500:])
    return parse_extraction(reply)


# Words that carry no distinguishing signal when comparing two statements
# of the same fact. "repo"/"project" are in here because every fact in a
# project memory file is about the project — their presence says nothing.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "being", "been",
    "to", "of", "in", "for", "and", "or", "but", "this", "that", "these",
    "those", "it", "its", "on", "with", "as", "at", "by", "from", "not",
    "must", "should", "when", "while", "you", "your", "we", "our", "use",
    "using", "used", "via", "because", "so", "if", "then", "than", "has",
    "have", "had", "do", "does", "did", "will", "would", "can", "could",
    "repo", "repository", "project", "here", "there", "which", "what",
    "run", "runs", "up", "out",
}


def _norm_words(text: str) -> set[str]:
    """Content words of a fact, for near-duplicate comparison.

    Trailing punctuation is stripped from each token — otherwise `up.` and
    `up` read as different words and two statements of one fact drift below
    the similarity threshold on nothing but a full stop.
    """
    import re

    words = re.findall(r"[a-z0-9_./:-]+", text.lower())
    out = set()
    for w in words:
        w = w.strip("./-:_")
        if len(w) > 1 and w not in _STOPWORDS:
            out.add(w)
    return out


def already_known(memory: str, fact: str, threshold: float = 0.7) -> bool:
    """Is this fact already in the project's memory, in substance?

    Memory is append-only, so without this a convergence loop that hits the
    same wall on four rounds writes the same sentence four times. Exact
    string matching is not enough — two extraction calls describing one
    fact rarely word it identically — so this compares content words and
    treats a heavy overlap as the same fact. Deliberately conservative in
    the direction of *writing* it: a duplicated line is untidy, while a
    dropped fact is the thing this whole module exists to prevent.
    """
    target = _norm_words(fact)
    if not target:
        return False
    for line in memory.splitlines():
        line = line.strip()
        if not line.startswith("- "):
            continue
        other = _norm_words(line)
        if not other:
            continue
        overlap = len(target & other) / len(target)
        if overlap >= threshold:
            return True
    return False


# -------------------------------------------------------------- promotion

@dataclass
class Promotion:
    """A recurring decision that looks like a standing project fact."""

    fingerprint: str
    fact: str
    answer: str
    question: str
    task_ids: list[str]
    occurrences: int


def recurring_answer(store, q: Question) -> Promotion | None:
    """Has this same decision now been made on more than one task?

    Within a task, an equivalent question is auto-answered from the earlier
    one and never reaches the operator twice — that is already live. Across
    tasks the suppression deliberately does NOT apply: a new task is a new
    context and reusing a stale decision silently would be worse than
    asking. But the operator answering the same question in a second task
    IS the signal that it was never task-specific to begin with, so it
    earns a one-time suggestion to make it permanent.
    """
    if not q.fingerprint or not q.answer or q.type == "fyi":
        return None
    # Auto-answered questions are echoes of a decision, not new statements
    # of it — counting them would let one answer promote itself.
    if q.answered_from:
        return None
    seen: dict[str, Question] = {}
    for prior in store.list_questions(status="answered"):
        if prior.fingerprint != q.fingerprint or not prior.answer:
            continue
        if prior.answered_from:
            continue
        # First answer per task: one operator, one decision, one vote.
        seen.setdefault(prior.task_id, prior)
    if q.task_id not in seen:
        seen[q.task_id] = q
    if len(seen) < 2:
        return None
    task_ids = sorted(seen)
    return Promotion(
        fingerprint=q.fingerprint,
        fact=phrase_promotion(q),
        answer=q.answer,
        question=q.question,
        task_ids=task_ids,
        occurrences=len(task_ids),
    )


def phrase_promotion(q: Question) -> str:
    """The memory line a promotion would write.

    Mechanical, not an LLM call: the operator's own words are the fact, and
    paraphrasing them would be both a cost and a chance to get it wrong.
    The question is kept as the context that makes the answer make sense.
    """
    question = " ".join((q.question or "").split())
    answer = " ".join((q.answer or "").split())
    if len(question) > 240:
        question = question[:237].rstrip() + "…"
    return f"Standing decision — {question} → {answer}"


# ------------------------------------------------------------- compaction

COMPACT_PROMPT = """\
You are First Mate's memory-compaction step. Below is a project's \
long-term memory file: append-only, dated entries, injected into every \
session that works on this repo. It has grown large enough to crowd the \
context it is meant to help. Consolidate it.

## The memory file

{memory}

## What to produce

Return ONLY the rewritten markdown, no prose about what you did, no code \
fence. Rules:

- **Never lose a fact.** Merging two entries that say the same thing is \
the point; dropping information is not. If in doubt, keep it.
- Preserve dated provenance: when you merge entries, keep the EARLIEST \
date (that is when the project learned it) and keep every entry's date \
prefix in the same `- YYYY-MM-DDTHH:MM:SS+00:00 — fact` shape as the \
input.
- Merge duplicates and near-duplicates into one entry stating the fact \
once, in its most complete form.
- When a later entry CONTRADICTS an earlier one, the later one wins: \
state the current truth and note what it replaced, so a session doesn't \
act on a superseded fact.
- Group related facts under `##` headings if that makes the file easier \
to skim; keep the `# Project memory: <name>` title line as-is.
- Do not add facts, advice, or commentary of your own. You are \
consolidating the operator's data, not authoring it.
- Do not remove an entry merely for being old. Age is not staleness.
"""


@dataclass
class Compaction:
    text: str | None
    before_bytes: int = 0
    after_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.text is not None and not self.errors


def _entry_lines(text: str) -> list[str]:
    return [l for l in text.splitlines() if l.strip().startswith("- ")]


def parse_compaction(before: str, reply: str) -> Compaction:
    """Accept a rewrite only if it still looks like the same memory file.

    Compaction is the one path that can *reduce* the operator's notes, so
    it is the one that has to be suspicious. A reply that lost most of the
    entries, or that came back as an apology instead of a file, is
    rejected and the original stands — an uncompacted file is merely large,
    while a truncated one has silently destroyed things the operator wrote.
    """
    text = (reply or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines()
                         if not l.startswith("```")).strip()
    if not text:
        return Compaction(None, len(before), 0, ["compaction returned nothing"])
    before_entries = len(_entry_lines(before))
    after_entries = len(_entry_lines(text))
    if before_entries and after_entries == 0:
        return Compaction(None, len(before), len(text),
                          ["compaction returned no memory entries"])
    # Consolidation should shrink the file, not gut it. Half the entries is
    # a generous floor for real dedup; below that, something went wrong.
    if before_entries >= 4 and after_entries * 2 < before_entries:
        return Compaction(
            None, len(before), len(text),
            [f"compaction dropped too much: {before_entries} entries → "
             f"{after_entries}"])
    if not text.endswith("\n"):
        text += "\n"
    return Compaction(text, len(before), len(text), [])


def request_compaction(cwd: Path, memory: str, model: str,
                       timeout: int = 300) -> Compaction:
    cmd = [
        "claude", "-p", COMPACT_PROMPT.format(memory=memory),
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--permission-mode", "dontAsk",
    ]
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return Compaction(None, len(memory), 0,
                          [f"compaction timed out after {timeout}s"])
    except OSError as e:
        return Compaction(None, len(memory), 0,
                          [f"compaction could not run: {e}"])
    if proc.returncode != 0:
        return Compaction(None, len(memory), 0,
                          [f"compaction failed: exit {proc.returncode}: "
                           f"{proc.stderr[-300:]}"])
    try:
        reply = json.loads(proc.stdout).get("result", "")
    except json.JSONDecodeError:
        return Compaction(None, len(memory), 0,
                          ["compaction returned non-JSON envelope"])
    return parse_compaction(memory, reply)


def archive_name(project: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{project}-{stamp}.md"


__all__ = [
    "Extraction", "Promotion", "Compaction",
    "step_struggled", "struggle_summary",
    "build_extract_prompt", "parse_extraction", "request_extraction",
    "recurring_answer", "phrase_promotion", "already_known",
    "parse_compaction", "request_compaction", "archive_name",
    "MIN_FACT_CHARS", "MAX_FACT_CHARS",
]
