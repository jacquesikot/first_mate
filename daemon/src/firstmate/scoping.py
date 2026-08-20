"""Scoping — the interactive entry point (PRD §6.1), terminal chat first.

`fm task "<goal>"` launches an interactive Claude Code session primed
with a scoping prompt: it reads project memory and repo context, then
*proposes* scope, steps, and machine-checkable completion criteria for
the operator to push back on. It must not finalize until every criterion
is a concrete command — it self-checks with `fm contract check`, which
runs the same `validate_contract` gate the daemon enforces on POST /tasks.

The session's deliverable is a contract JSON written to a First-Mate-owned
scoping directory; after the session ends, the CLI validates it and
submits it to the daemon. Nothing downstream is interactive.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .models import slugify

CONTRACT_SCHEMA_EXAMPLE = """\
{
  "goal": "one-sentence outcome",
  "repo": "<absolute repo path — use the value given above verbatim>",
  "context": "facts a fresh session needs: key files, conventions, prior decisions",
  "scope_in": ["src/api/**", "tests/api/**"],
  "scope_out": ["src/legacy/**"],
  "tripwires": {},
  "steps": [
    {
      "id": "implement",
      "title": "short human title",
      "prompt": "precise instructions for a headless worker session — it sees ONLY the contract, project memory, and this prompt",
      "skill": null,
      "allowed_tools": ["Read", "Glob", "Grep", "Edit", "Write", "Bash(npm test:*)"],
      "criteria": ["tests"]
    },
    {
      "id": "verify",
      "title": "wait for CI, then report",
      "prompt": "instructions — note the wait already happened before this runs",
      "allowed_tools": ["Read", "Bash(gh pr checks:*)"],
      "criteria": ["ci_green"],
      "when": {
        "command": "test \"$(gh pr checks 42 --json state --jq '[.[]|select(.state==\"PENDING\" or .state==\"IN_PROGRESS\")]|length')\" = 0",
        "description": "CI to finish",
        "interval": 60,
        "ceiling": 3600
      },
      "on_failure": {"goto": "implement", "max_iterations": 5}
    }
  ],
  "criteria": [
    {"id": "tests", "command": "npm test", "cwd": ".", "timeout": 600},
    {"id": "ci_green", "command": "gh pr checks 42", "cwd": ".", "timeout": 300}
  ]
}"""

SCOPING_PROMPT = """\
You are First Mate's scoping assistant. First Mate runs coding tasks \
autonomously in headless sessions; your job in THIS conversation is to \
turn the operator's goal into a task contract — the single source of \
truth for what "done" means.

Goal as stated by the operator:
{goal}

Repository: {repo}
{workdir_note}
## How to run this conversation

1. Explore first, ask second. Read the repo (Read/Glob/Grep) and the \
project memory below, then OPEN WITH A PROPOSAL: scope (in/out path \
globs relative to the repo root), a step plan, and completion criteria. \
Do not open with a questionnaire.
2. Iterate with the operator until they approve. Keep the conversation \
short and concrete.
3. Every completion criterion MUST be machine-checkable: a shell command \
that exits 0 on success, run from the repo root. Refuse vague criteria \
("make X better", "should feel faster") and ask for a concrete check \
instead. If the repo has no test runner for the change, propose adding a \
check as part of the task.
4. Step prompts must be self-contained: each step runs in a fresh \
headless session that knows nothing you don't put in the contract \
(goal, context, scope, the step prompt itself).
5. Keep `allowed_tools` minimal per step (review-ish steps: read-only; \
implementation steps: Read/Glob/Grep/Edit/Write plus the specific \
Bash(...) commands the step needs, e.g. "Bash(npm test:*)").
6. The orchestrator executes every completion criterion itself after a \
step ends — step prompts must NOT tell the worker to run a check the \
step's allowed_tools does not permit (that parks the task on a needless \
question). If a step should run something itself (tests, a script), add \
it to allowed_tools explicitly, e.g. "Bash(./scripts/check.sh:*)".
7. Scope tripwires that will interrupt the operator unless pre-approved \
in the contract: dependency-manifest changes, migrations, git push, \
large diffs. If the task inherently needs one (e.g. adding a dependency), \
say so and set it in the contract, e.g. "tripwires": {{"dependency_manifests": false}} \
or widen nothing and let the worker ask at run time.
8. A worker may always write its own scratch artifacts (drafts, notes, a \
report it generated) under `.fm/artifacts/` with no approval and no scope \
entry. If a step needs to produce a working file that is not part of the \
deliverable, put it there — do NOT add it to scope_in, and do NOT invent a \
repo-root file for it.
9. If a step's work IS running one of the operator's skills (the goal says \
"using the reach-plan skill", or the work is exactly what a skill does), \
set that step's `"skill"` to the skill's name — do not leave it null and \
mention the skill only in the prompt. First Mate uses that field to grant \
the `Skill` tool, to keep the skill's own progress across context walls, \
and to preserve its phase through a relay. A skill-driven step also needs \
`"Skill"` in its `allowed_tools` (plus `Agent`/`ToolSearch` if the skill \
fans out or needs MCP tools), and it should be its OWN step: skills are \
long and multi-phase, so bundling other work into the same step wastes the \
relay.

## Waiting and iterating — use the contract, not the step prompt

First Mate can wait, and it can loop. Both are declared structurally, and \
you should reach for them rather than working around them:

- **Waiting on something slow and external** (an AI reviewer, CI, a deploy, \
a queue): give the step a `when` gate. First Mate polls that command \
itself, on its own interval, while the task holds no session open — so \
waiting 20 minutes costs nothing and cannot run out of context. NEVER \
write a step prompt that tells a worker to sleep in a bash loop or poll for \
minutes: that burns the session's context on waiting and is the one thing \
gates exist to replace. Set `ceiling` generously (the operator is asked \
only if it is exceeded).
- **Iterating until something is clean** ("fix what the review finds, then \
re-review, until it passes"): give the verifying step an `on_failure` loop \
edge pointing back at the step that does the work. When the verifying \
step's criteria fail, First Mate re-runs everything from the target step \
onward — so the fix is re-made, re-pushed and re-verified automatically. \
It stops on its own if the rounds stop making progress or `max_iterations` \
is hit.

So for a goal like "fix the review findings and keep going until the PR is \
green": do NOT propose a single pass, and do NOT ask the operator to choose \
between a single pass and a polling loop. Express it as fix → push → \
(gate: wait for the reviewer) verify, with `on_failure` from verify back to \
fix. That IS the loop. Only ask the operator about things that genuinely \
change the outcome of their work (which findings count, whether to \
force-push, what "green" means) — never about the mechanics of waiting or \
iterating.

A criterion on a looping step should assert the END state you actually \
want ("zero unresolved findings on the current head"), because failing it \
is what drives another round. That is a feature, not a problem to design \
around.

## Finalizing

When the operator approves the plan:
1. Write the contract as JSON to exactly this path: {contract_path}
2. Run: fm contract check {contract_path}
3. Fix any errors it reports and re-check until it passes.
4. Confirm to the operator that the contract is ready, with a one-screen \
summary. {finalize}

Contract JSON shape (all fields shown; tripwires/scope_out may be empty):

```json
{schema}
```

## Project memory (accumulated lessons — respect them)

{memory}
"""


@dataclass
class ScopingResult:
    contract_path: Path
    contract: dict | None
    errors: list[str]


def repo_root(cwd: Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    top = proc.stdout.strip()
    return Path(top) if proc.returncode == 0 and top else cwd


TERMINAL_FINALIZE = "The task is created automatically when this session ends."
BROWSER_FINALIZE = ("The operator reviews and approves the contract in the "
                    "dashboard; once `fm contract check` passes, tell them "
                    "it is ready for approval.")


WORKDIR_NOTE = """
You are reading the task's own git worktree, already checked out at the \
starting point the operator chose:

    Working directory: {workdir}
    Started from:      {base}

This is a clean checkout of that starting point — NOT the operator's own \
checkout, so nothing you see here is their uncommitted work in progress. \
Read here, and write the `repo` field of the contract as the repository \
path given above (not this worktree path); First Mate resolves the \
worktree itself when it runs the task.
"""


def build_prompt(goal: str, repo: Path, contract_path: Path,
                 memory: str | None, finalize: str = TERMINAL_FINALIZE,
                 workdir: Path | None = None, base: str = "",
                 checkout_note: str = "") -> str:
    return SCOPING_PROMPT.format(
        goal=goal,
        repo=repo,
        workdir_note=(
            WORKDIR_NOTE.format(workdir=workdir, base=base or "HEAD")
            if workdir else checkout_note
        ),
        contract_path=contract_path,
        finalize=finalize,
        schema=CONTRACT_SCHEMA_EXAMPLE.replace(
            '"<absolute repo path — use the value given above verbatim>"',
            json.dumps(str(repo)),
        ),
        memory=(memory or "(no project memory yet)").rstrip(),
    )


def build_command(prompt: str, scoping_dir: Path, model: str | None = None) -> list[str]:
    # Interactive session — the operator is present, so anything beyond
    # this allowlist simply falls back to a normal permission prompt.
    # `//` prefix = absolute path in permission-rule syntax (a single `/`
    # would be settings-file-relative).
    allowed = [
        "Read", "Glob", "Grep",
        # Edit(...) rules gate ALL file-modification tools (incl. Write);
        # a Write(path) rule is accepted but never consulted.
        f"Edit(/{scoping_dir}/**)",
        "Bash(fm contract check:*)",
    ]
    # --add-dir: the scoping dir is outside the repo cwd; without it the
    # contract Write is treated as out-of-project access.
    cmd = ["claude", prompt, "--add-dir", str(scoping_dir),
           "--allowedTools", ",".join(allowed)]
    if model:
        cmd += ["--model", model]
    return cmd


def run_scoping(goal: str, repo: Path, home: Path,
                model: str | None = None,
                checkout_note: str = "") -> ScopingResult:
    """Run the interactive scoping session; returns the contract (or the
    reasons there isn't one). Raises FileNotFoundError if claude is absent."""
    from .models import validate_contract

    scoping_dir = home / "scoping" / f"{slugify(goal)}-{uuid.uuid4().hex[:6]}"
    scoping_dir.mkdir(parents=True, exist_ok=True)
    contract_path = scoping_dir / "contract.json"
    memory = None
    mem_file = home / "memory" / f"{repo.name}.md"
    if mem_file.exists():
        memory = mem_file.read_text()
    # No worktree here: the terminal flow has no task id yet (see the
    # STATUS.md decision log on why it stays contract-first), so the session
    # reads the operator's own checkout. The prompt says so explicitly, and
    # the CLI warns when that checkout isn't the chosen starting point.
    prompt = build_prompt(goal, repo, contract_path, memory,
                          checkout_note=checkout_note)
    (scoping_dir / "prompt.md").write_text(prompt)

    subprocess.run(build_command(prompt, scoping_dir, model), cwd=repo)

    if not contract_path.exists():
        return ScopingResult(contract_path, None,
                             ["scoping session ended without writing a contract"])
    try:
        data = json.loads(contract_path.read_text())
    except json.JSONDecodeError as e:
        return ScopingResult(contract_path, None, [f"contract is not valid JSON: {e}"])
    errors = validate_contract(data)
    return ScopingResult(contract_path, data, errors)


def check_contract_file(path: Path) -> list[str]:
    """The `fm contract check` gate — validate_contract plus a repo-path
    existence check (mirrors what POST /tasks enforces)."""
    from .models import validate_contract

    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return [f"no such file: {path}"]
    except json.JSONDecodeError as e:
        return [f"not valid JSON: {e}"]
    errors = validate_contract(data)
    repo = str(data.get("repo", "")) if isinstance(data, dict) else ""
    if repo and not Path(repo).expanduser().is_dir():
        errors.append(f"repo path does not exist: {repo}")
    return errors


__all__ = [
    "ScopingResult", "run_scoping", "build_prompt", "build_command",
    "check_contract_file", "repo_root",
]
