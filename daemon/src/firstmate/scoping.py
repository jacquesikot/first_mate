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
    }
  ],
  "criteria": [
    {"id": "tests", "command": "npm test", "cwd": ".", "timeout": 600}
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


def build_prompt(goal: str, repo: Path, contract_path: Path,
                 memory: str | None, finalize: str = TERMINAL_FINALIZE) -> str:
    return SCOPING_PROMPT.format(
        goal=goal,
        repo=repo,
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
                model: str | None = None) -> ScopingResult:
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
    prompt = build_prompt(goal, repo, contract_path, memory)
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
