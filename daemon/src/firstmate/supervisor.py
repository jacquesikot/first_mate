"""Supervisor — the thing that sits above a running task and thinks.

One of the named LLM decision points (PRD §6.2). The orchestrator is
deliberately dumb: it probes a gate, it counts failures, it rewinds a
loop. That is right for control flow, but it means a gate whose *premise*
is unsound stalls until its ceiling and then interrupts the operator over
something First Mate could have worked out for itself.

The motivating case (decision log 2026-08-20): a `verify` gate waited for
"a cubic review row whose commit_id equals HEAD". Cubic reviewed the
parent commit, found nothing new to say about the child, and filed no new
review — so the check went green while the review row never appeared. The
gate could never open. Every fact needed to see that was one `gh` call
away.

So: when a gate stalls, the supervisor investigates the real external
state with read-only tools, decides whether the GATE or the WORLD is at
fault, and if the gate is unsound it repairs the gate.

The boundary that makes this safe:

    A gate is First Mate's own measuring instrument. Fixing a broken
    instrument is a runtime decision and never needs the operator.
    A completion criterion is the operator's definition of "done".
    The supervisor may NEVER touch one.

That asymmetry is enforced mechanically in `apply_gate_repair` (only
`steps[i].when` may change) rather than trusted to the prompt.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .models import Contract, Gate

# Read-only tools. The supervisor investigates and reasons; it never edits
# the repo, never pushes, and never runs the project's own commands.
INVESTIGATE_TOOLS = [
    "Read", "Glob", "Grep",
    "Bash(gh pr view:*)", "Bash(gh pr checks:*)", "Bash(gh pr diff:*)",
    "Bash(gh api:*)", "Bash(gh run list:*)", "Bash(gh run view:*)",
    "Bash(git log:*)", "Bash(git rev-parse:*)", "Bash(git status:*)",
    "Bash(git diff:*)", "Bash(git show:*)", "Bash(git branch:*)",
]

VERDICTS = {"gate_wrong", "still_waiting", "cannot_tell"}

# Criterion verdicts. Note what is deliberately absent: there is no
# "criterion_wrong, here is a better one". The supervisor may say a check
# cannot be satisfied and explain why; changing the operator's definition
# of done stays the operator's decision, answered through the escalation.
CRITERION_VERDICTS = {"unsatisfiable", "needs_more_work", "cannot_tell"}

SUPERVISE_PROMPT = """\
You are First Mate's supervisor for one running task. A step is parked on \
a gate — a shell probe First Mate polls before running the step — and the \
probe is not passing. Your job is to work out WHY, using the real state of \
the world, and decide what should happen.

## The step that is waiting

Step id: {step_id}
What the gate claims to be waiting for: {description}

The probe First Mate is running (exit 0 = proceed):

```sh
{command}
```

It has run {probes} times over {elapsed_min} minutes and is still failing \
(last exit status: {last_exit}). Last output:

```
{last_output}
```

## The task

Goal: {goal}
Repository: {repo}
Working directory: your cwd IS the task's git worktree.

The step that will run once the gate opens, and the criteria it must then \
satisfy (these are the operator's definition of done — you are NOT \
evaluating or changing them, they are here so you understand what the \
gate is a precondition for):

{step_context}

## What to do

1. INVESTIGATE the actual state, don't speculate. You have read-only \
tools: `gh pr view/checks/diff/api`, `gh run list/view`, `git \
log/rev-parse/status/diff/show`, and file reads. Run the probe's own \
sub-commands yourself and look at what they really return. Compare what \
the probe ASSERTS against what is actually true.
2. Decide which of these it is:
   - **gate_wrong** — the thing the gate is *meant* to wait for has \
already happened (or can never happen as written), but the probe cannot \
observe it. Classic causes: it keys on a record that only exists in some \
cases (a review row is only filed when there is something to say); it \
pins an identifier that has since moved (a SHA, a run id); it uses a \
login/field spelled differently by the API it queries; it asserts \
something the provider never populates.
   - **still_waiting** — the gate is correct and the world genuinely \
hasn't got there yet. Be honest about this; waiting is free and a \
premature "fix" is worse than waiting.
   - **cannot_tell** — you could not establish the facts.
3. If and only if the verdict is **gate_wrong**, write a REPLACEMENT \
probe. It must:
   - wait for the same real-world condition the operator's step needs, \
expressed in terms the API actually reports — for a CI/review provider \
that usually means a *terminal check state* (`gh pr checks` reporting no \
PENDING/IN_PROGRESS/QUEUED for the relevant check) rather than the \
existence of a record;
   - be a single shell command, exit 0 to proceed, safe to run repeatedly, \
and read-only (no pushes, no writes, no mutations);
   - be strictly a precondition, NOT the completion check. Never make it \
trivially true (`true`, `exit 0`) and never fold the step's criteria into \
it — the criteria still have to pass on their own merits afterwards. If \
the honest answer is "there is nothing left to wait for", say so with an \
empty command and set "drop_gate": true.

Return ONLY a JSON object, no prose:

{{
  "verdict": "gate_wrong" | "still_waiting" | "cannot_tell",
  "findings": "what you actually checked and what you found — cite the \
concrete values (SHAs, check names, states, counts) you observed",
  "reasoning": "why that leads to your verdict",
  "new_command": "the replacement probe, or \\"\\" if none",
  "drop_gate": false,
  "confidence": "high" | "medium" | "low"
}}
"""


@dataclass
class Diagnosis:
    verdict: str = "cannot_tell"
    findings: str = ""
    reasoning: str = ""
    new_command: str = ""
    drop_gate: bool = False
    confidence: str = "low"
    errors: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def repairs_gate(self) -> bool:
        """True when this diagnosis prescribes a concrete gate change."""
        return (
            not self.errors
            and self.verdict == "gate_wrong"
            and (self.drop_gate or bool(self.new_command.strip()))
        )

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict, "findings": self.findings,
            "reasoning": self.reasoning, "new_command": self.new_command,
            "drop_gate": self.drop_gate, "confidence": self.confidence,
            "errors": self.errors,
        }


def build_prompt(contract: Contract, step_id: str, gate: Gate,
                 probes: int, elapsed_s: float, last_exit: int | None,
                 last_output: str) -> str:
    step = next((s for s in contract.steps if s.id == step_id), None)
    lines = []
    if step is not None:
        lines.append(f"Step prompt:\n{step.prompt.strip()[:2000]}")
        if step.criteria:
            lines.append("\nCriteria it must satisfy afterwards:")
            for cid in step.criteria:
                try:
                    c = contract.criterion(cid)
                except KeyError:
                    continue
                lines.append(f"  - {c.id}: {c.command}")
    return SUPERVISE_PROMPT.format(
        step_id=step_id,
        description=gate.description or "(no description given)",
        command=gate.command,
        probes=probes,
        elapsed_min=int(elapsed_s // 60),
        last_exit=last_exit,
        last_output=(last_output or "(no output)")[-1500:],
        goal=contract.goal,
        repo=contract.repo,
        step_context="\n".join(lines) or "(step not found in contract)",
    )


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(ln for ln in text.splitlines()
                         if not ln.startswith("```")).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# Shapes that would make a "gate" meaningless — a probe that always passes
# turns the wait into a no-op while pretending a precondition was checked.
_TRIVIAL = {"", "true", ":", "exit 0", "/bin/true"}


def parse_reply(reply: str) -> Diagnosis:
    data = _extract_json(reply)
    if data is None:
        return Diagnosis(errors=["supervisor reply was not JSON"], raw=reply)
    d = Diagnosis(
        verdict=str(data.get("verdict") or "cannot_tell"),
        findings=str(data.get("findings") or "").strip(),
        reasoning=str(data.get("reasoning") or "").strip(),
        new_command=str(data.get("new_command") or "").strip(),
        drop_gate=bool(data.get("drop_gate")),
        confidence=str(data.get("confidence") or "low"),
        raw=reply,
    )
    if d.verdict not in VERDICTS:
        d.errors.append(f"unknown verdict: {d.verdict!r}")
    if d.verdict == "gate_wrong" and not d.drop_gate:
        if not d.new_command:
            d.errors.append("verdict 'gate_wrong' with no replacement probe")
        elif d.new_command.strip().lower() in _TRIVIAL:
            d.errors.append(
                "replacement probe is trivially true — that removes the "
                "precondition instead of correcting it")
    return d


def investigate(worktree: Path, contract: Contract, step_id: str, gate: Gate,
                probes: int, elapsed_s: float, last_exit: int | None,
                last_output: str, model: str,
                timeout: int = 600) -> Diagnosis:
    """Run the supervisor against a stalled gate."""
    prompt = build_prompt(contract, step_id, gate, probes, elapsed_s,
                          last_exit, last_output)
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--allowedTools", ",".join(INVESTIGATE_TOOLS),
        "--permission-mode", "dontAsk",
    ]
    try:
        proc = subprocess.run(cmd, cwd=worktree, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return Diagnosis(errors=[f"supervisor timed out after {timeout}s"])
    except OSError as e:
        return Diagnosis(errors=[f"supervisor could not run: {e}"])
    if proc.returncode != 0:
        return Diagnosis(errors=[f"supervisor failed: exit {proc.returncode}: "
                                 f"{proc.stderr[-500:]}"])
    try:
        reply = json.loads(proc.stdout).get("result", "")
    except json.JSONDecodeError:
        return Diagnosis(errors=["supervisor returned non-JSON envelope"],
                         raw=proc.stdout[-1000:])
    return parse_reply(reply)


def apply_gate_repair(contract: Contract, step_id: str,
                      diagnosis: Diagnosis) -> Gate | None:
    """Apply a gate repair to the contract, in place.

    Deliberately narrow: this can only ever replace `steps[i].when`. The
    supervisor has no path to steps, criteria, scope, or tripwires — so a
    confused diagnosis can stall or skip a wait, but can never quietly
    redefine what "done" means. Returns the gate as it now stands (None if
    it was dropped), or raises ValueError if the repair isn't applicable.
    """
    if not diagnosis.repairs_gate:
        raise ValueError("diagnosis does not prescribe a gate repair")
    step = next((s for s in contract.steps if s.id == step_id), None)
    if step is None:
        raise ValueError(f"unknown step: {step_id}")
    if step.when is None:
        raise ValueError(f"step '{step_id}' has no gate to repair")
    if diagnosis.drop_gate and not diagnosis.new_command:
        step.when = None
        return None
    old = step.when
    # Keep the operator-visible framing and the timing envelope; only the
    # probe itself was wrong.
    step.when = Gate(
        command=diagnosis.new_command,
        kind=old.kind,
        cwd=old.cwd,
        interval=old.interval,
        ceiling=old.ceiling,
        timeout=old.timeout,
        description=old.description,
    )
    return step.when


CRITERION_PROMPT = """\
You are First Mate's supervisor for one running task. A step has failed \
its completion criteria twice. Before First Mate spends several more \
rounds re-doing the work, your job is to establish whether more work can \
actually fix this — or whether the CHECK is asserting something that can \
never become true, in which case looping is pointless and the operator \
should be told now.

## The step and what failed

Step id: {step_id}
Step instructions:
{step_prompt}

Failing checks (the operator's definition of "done" for this step):

{failures}

{loop_note}
## The task

Goal: {goal}
Repository: {repo}
Working directory: your cwd IS the task's git worktree.

## What to do

1. INVESTIGATE with your read-only tools (`gh pr view/checks/diff/api`, \
`gh run list/view`, `git log/rev-parse/status/diff/show`, file reads). Run \
the failing check's own sub-commands and look at what they really return. \
Establish whether the thing the check asserts is *reachable at all* from \
here.
2. Decide:
   - **unsatisfiable** — no amount of further work by this task can make \
this check pass. Typical causes: it asserts a record that only gets \
created in some circumstances (a review is only filed when there is \
something to say); it pins an identifier that has moved on; the thing it \
queries is now closed/merged/deleted so the state it wants can never be \
produced; it queries a field the provider never populates; it requires a \
permission or resource this environment does not have. Be strict: \
"unsatisfiable" means impossible, NOT merely "hard" or "not done yet".
   - **needs_more_work** — the check is sound and failing honestly. The \
work genuinely is not finished, so looping back is the right move. This is \
the default; prefer it whenever the check could plausibly pass after \
another attempt.
   - **cannot_tell** — you could not establish the facts.
3. If unsatisfiable, explain it so the operator can decide what to do, and \
say what you believe the check was *trying* to assert and how that could \
be expressed instead. You are NOT applying that change — a completion \
criterion is the operator's statement of what they wanted, and only they \
may alter it. You are giving them what they need to choose.

Return ONLY a JSON object, no prose:

{{
  "verdict": "unsatisfiable" | "needs_more_work" | "cannot_tell",
  "criterion_id": "the id of the check you judged, from the list above",
  "findings": "what you actually checked and found — cite concrete values \
(SHAs, states, counts, ids) you observed",
  "reasoning": "why more work can or cannot fix this",
  "suggestion": "if unsatisfiable: what the check appears to be trying to \
assert, and a command that would express it correctly — as a RECOMMENDATION \
for the operator, not something you are applying",
  "confidence": "high" | "medium" | "low"
}}
"""


@dataclass
class CriterionDiagnosis:
    verdict: str = "cannot_tell"
    criterion_id: str = ""
    findings: str = ""
    reasoning: str = ""
    suggestion: str = ""
    confidence: str = "low"
    errors: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def blocks_loop(self) -> bool:
        """True when looping would be futile, so the task should escalate
        now instead of burning its remaining rounds."""
        return (not self.errors
                and self.verdict == "unsatisfiable"
                # A low-confidence "impossible" is not a good enough reason
                # to stop trying — looping is cheap compared to a wrong stop.
                and self.confidence in ("high", "medium"))

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict, "criterion_id": self.criterion_id,
            "findings": self.findings, "reasoning": self.reasoning,
            "suggestion": self.suggestion, "confidence": self.confidence,
            "errors": self.errors,
        }


def build_criterion_prompt(contract: Contract, step_id: str,
                           failures: list, iteration: int = 0,
                           max_iterations: int = 0) -> str:
    step = next((s for s in contract.steps if s.id == step_id), None)
    rendered = []
    for r in failures:
        rendered.append(
            f"- id: {r.id}\n"
            f"  command: {r.command}\n"
            f"  exit status: {r.exit_status}"
            + (f"\n  error: {r.error}" if getattr(r, "error", None) else "")
            + (f"\n  stdout: {(r.stdout or '').strip()[-800:]}" if r.stdout else "")
            + (f"\n  stderr: {(r.stderr or '').strip()[-800:]}" if r.stderr else "")
        )
    loop_note = ""
    if max_iterations:
        loop_note = (
            f"If more work could fix this, First Mate would loop back and "
            f"retry (round {iteration + 1} of {max_iterations}). That is what "
            f"you are deciding for or against.\n\n")
    return CRITERION_PROMPT.format(
        step_id=step_id,
        step_prompt=(step.prompt.strip()[:2500] if step else "(step not found)"),
        failures="\n".join(rendered) or "(none)",
        loop_note=loop_note,
        goal=contract.goal,
        repo=contract.repo,
    )


def parse_criterion_reply(reply: str) -> CriterionDiagnosis:
    data = _extract_json(reply)
    if data is None:
        return CriterionDiagnosis(
            errors=["supervisor reply was not JSON"], raw=reply)
    d = CriterionDiagnosis(
        verdict=str(data.get("verdict") or "cannot_tell"),
        criterion_id=str(data.get("criterion_id") or "").strip(),
        findings=str(data.get("findings") or "").strip(),
        reasoning=str(data.get("reasoning") or "").strip(),
        suggestion=str(data.get("suggestion") or "").strip(),
        confidence=str(data.get("confidence") or "low"),
        raw=reply,
    )
    if d.verdict not in CRITERION_VERDICTS:
        d.errors.append(f"unknown verdict: {d.verdict!r}")
    if d.verdict == "unsatisfiable" and not d.findings:
        d.errors.append(
            "verdict 'unsatisfiable' with no findings — a claim that a check "
            "can never pass has to be evidenced")
    return d


def investigate_criteria(worktree: Path, contract: Contract, step_id: str,
                         failures: list, model: str, iteration: int = 0,
                         max_iterations: int = 0,
                         timeout: int = 600) -> CriterionDiagnosis:
    """Judge whether a twice-failing step can ever pass its criteria."""
    prompt = build_criterion_prompt(contract, step_id, failures,
                                    iteration, max_iterations)
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--allowedTools", ",".join(INVESTIGATE_TOOLS),
        "--permission-mode", "dontAsk",
    ]
    try:
        proc = subprocess.run(cmd, cwd=worktree, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CriterionDiagnosis(
            errors=[f"supervisor timed out after {timeout}s"])
    except OSError as e:
        return CriterionDiagnosis(errors=[f"supervisor could not run: {e}"])
    if proc.returncode != 0:
        return CriterionDiagnosis(
            errors=[f"supervisor failed: exit {proc.returncode}: "
                    f"{proc.stderr[-500:]}"])
    try:
        reply = json.loads(proc.stdout).get("result", "")
    except json.JSONDecodeError:
        return CriterionDiagnosis(
            errors=["supervisor returned non-JSON envelope"],
            raw=proc.stdout[-1000:])
    return parse_criterion_reply(reply)


__all__ = ["Diagnosis", "CriterionDiagnosis", "investigate",
           "investigate_criteria", "parse_reply", "parse_criterion_reply",
           "build_prompt", "build_criterion_prompt", "apply_gate_repair",
           "INVESTIGATE_TOOLS"]
