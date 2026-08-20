"""Re-planning — turning an operator's answer into a contract edit.

One of the named LLM decision points (PRD §6.2/§6.8). Every other
amendment path is mechanical: an "allow" widens exactly the glob its
evidence names, a refusal appends a correction to the step prompt. But an
operator answering a blocking escalation in their own words is often
saying something no glob can express — "the review found new issues, go
round again", "drop that check, it can't pass", "stop writing that file
and use Linear instead".

Before this existed those answers were recorded and then ignored: the
amendment log grew, the contract didn't change, and the next generation
walked into the identical wall. That is what made a stuck task feel
unrecoverable without abandoning it.

The contract is the source of truth for "done", so a rewrite is never
silently trusted: the result must pass the same `validate_contract` gate
as a freshly scoped contract, only whitelisted fields may move, and the
before/after is persisted as a diff the operator can read and revert.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import Contract, validate_contract

# Fields a re-plan may touch. Deliberately excludes `repo` and `goal` —
# a failed step is not a mandate to change what the task IS, and letting
# an LLM rewrite the goal would make the contract stop being a contract.
EDITABLE = {"steps", "criteria", "scope_in", "scope_out", "tripwires",
            "tripwire_allow", "context"}

REPLAN_PROMPT = """\
You are First Mate's re-planning step. A task is BLOCKED and the operator \
has answered the blocking question in their own words. Your job is to \
express that answer as a concrete edit to the task contract, so the \
orchestrator can carry on autonomously.

## The contract as it stands

```json
{contract}
```

## What blocked the task

{situation}

## The operator's answer (binding — this is the whole point)

{answer}

## What to produce

Return ONLY a JSON object, no prose, with exactly these keys:

  "contract": the COMPLETE updated contract object (same shape as above)
  "summary":  one or two sentences saying what you changed and why

Rules:
- You may change: {editable}. Do NOT change "goal" or "repo".
- Every criterion must stay machine-checkable: a shell command that exits \
0 on success. Never "relax" a criterion by making it trivially true \
(`true`, `exit 0`) — if a check genuinely cannot be satisfied, remove it \
from the step that can't meet it and say so in the summary.
- Prefer the smallest edit that unblocks the task. Keep step ids stable \
where you can; the orchestrator carries per-step runtime state keyed by id.
- If the operator is describing an iterative process ("wait for the \
review, fix what it finds, repeat"), express it structurally: give the \
waiting step a `when` gate (a shell probe First Mate polls before running \
the step — it holds no session open, so waiting is free) and give the \
verifying step an `on_failure` loop edge, e.g.
    "when": {{"command": "gh pr checks 42 --json state --jq '...'", \
"interval": 60, "ceiling": 3600, "description": "the review to finish"}}
    "on_failure": {{"goto": "fix", "max_iterations": 5}}
  That is how a convergence loop is meant to be expressed — do NOT instead \
write a step prompt telling a worker to sleep in a bash loop.
- If the operator's answer is a factual claim that contradicts a check \
(e.g. "I can see the review, your check is wrong"), the likely fix is the \
check's command, not the work. Correct the command.
- If you genuinely cannot express the answer as a contract edit, return \
the contract UNCHANGED and say why in the summary.
"""


@dataclass
class ReplanResult:
    contract: Contract | None
    summary: str
    errors: list[str]
    raw: str = ""

    @property
    def ok(self) -> bool:
        return self.contract is not None and not self.errors


def build_prompt(contract: Contract, situation: str, answer: str) -> str:
    return REPLAN_PROMPT.format(
        contract=json.dumps(contract.to_dict(), indent=2),
        situation=situation.strip(),
        answer=answer.strip(),
        editable=", ".join(sorted(EDITABLE)),
    )


def _sanitize(current: Contract, proposed: dict) -> dict:
    """Take only the editable fields from the proposal; keep the rest of the
    contract exactly as it was."""
    merged = current.to_dict()
    for key in EDITABLE:
        if key in proposed:
            merged[key] = proposed[key]
    return merged


def _extract_json(text: str) -> dict | None:
    """Pull the JSON object out of a model reply, tolerating fenced code."""
    text = text.strip()
    if text.startswith("```"):
        lines = [ln for ln in text.splitlines() if not ln.startswith("```")]
        text = "\n".join(lines).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def parse_reply(current: Contract, reply: str) -> ReplanResult:
    """Validate a model reply into a contract we'd be willing to run."""
    data = _extract_json(reply)
    if data is None:
        return ReplanResult(None, "", ["re-plan reply was not JSON"], reply)
    proposed = data.get("contract")
    summary = str(data.get("summary") or "").strip()
    if not isinstance(proposed, dict):
        return ReplanResult(None, summary,
                            ["re-plan reply had no 'contract' object"], reply)
    merged = _sanitize(current, proposed)
    errors = validate_contract(merged)
    if errors:
        return ReplanResult(None, summary, errors, reply)
    return ReplanResult(Contract.from_dict(merged), summary, [], reply)


def request_replan(worktree: Path, contract: Contract, situation: str,
                   answer: str, model: str, timeout: int = 300) -> ReplanResult:
    """Ask the model for a contract edit expressing the operator's answer."""
    prompt = build_prompt(contract, situation, answer)
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
        return ReplanResult(None, "", [f"re-plan timed out after {timeout}s"])
    except OSError as e:
        return ReplanResult(None, "", [f"re-plan could not run: {e}"])
    if proc.returncode != 0:
        return ReplanResult(
            None, "",
            [f"re-plan failed: exit {proc.returncode}: {proc.stderr[-500:]}"])
    try:
        reply = json.loads(proc.stdout).get("result", "")
    except json.JSONDecodeError:
        return ReplanResult(None, "", ["re-plan returned non-JSON envelope"],
                            proc.stdout[-1000:])
    return parse_reply(contract, reply)


def diff_contracts(before: Contract, after: Contract) -> str:
    """A readable unified diff of the contract, for the operator to audit."""
    import difflib

    a = json.dumps(before.to_dict(), indent=2, sort_keys=True).splitlines()
    b = json.dumps(after.to_dict(), indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(
        a, b, fromfile="contract (before)", tofile="contract (after)",
        lineterm="", n=3))


__all__ = ["ReplanResult", "request_replan", "parse_reply", "build_prompt",
           "diff_contracts", "EDITABLE"]
