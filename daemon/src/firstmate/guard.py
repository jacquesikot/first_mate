"""Scope guard — mechanical enforcement of the contract's scope (PRD §6.4).

Evaluates one PreToolUse hook invocation against a guard config compiled
from the contract (scope_in/scope_out globs, tripwires, operator-approved
allowances). Blocking is exit-code-2 with an in-band stderr message that
tells the agent to raise a `scope_change`/`approval` question via
`fm ask` — never a silent surprise.

Stdlib-only on purpose: `fm _guard` runs inside every guarded tool call,
so this module must import fast with no daemon dependencies.

Tripwires (PRD §6.4) evaluated here: dependency-manifest changes,
migrations, `git push`. Diff-size/deletion thresholds need a full diff
and are checked orchestrator-side at step boundaries (same config keys).
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# A worker's own bookkeeping (a plan it drafted, notes, a scratch report)
# is a runtime concern, not a change to the operator's deliverable. It gets
# a always-writable home inside the FM-owned dir so writing one never costs
# the operator a question. Never committed: .fm/ is gitignored per worktree.
SCRATCH_DIR = ".fm/artifacts"

FILE_TOOLS = {
    "Edit": "file_path",
    "Write": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
GUARDED_TOOLS_MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash"

# Hook-side tripwires (path- or command-shaped). Diff-shaped ones
# (max_diff_lines, max_deleted_lines) live in the orchestrator.
DEFAULT_TRIPWIRES = {
    "dependency_manifests": True,
    "migrations": True,
    "git_push": True,
    "max_diff_lines": 3000,
    "max_deleted_lines": 500,
}
KNOWN_TRIPWIRES = set(DEFAULT_TRIPWIRES)

# Files that DECLARE dependencies — editing one is a real dependency change.
DEP_MANIFEST_NAMES = {
    "package.json",
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
    "Gemfile", "go.mod",
    "Cargo.toml", "composer.json",
}
# Lockfiles are DERIVED. A bare `bun install`/`npm ci` in a fresh worktree
# rewrites one as a side effect of populating node_modules while changing no
# dependency at all — blocking that just interrupts the operator for
# something they cannot meaningfully approve. So a lockfile write passes the
# hook, and the orchestrator's step-boundary check is what catches a lockfile
# that actually diverged (guard.LOCKFILE_NAMES / orchestrator._lockfile_drift).
LOCKFILE_NAMES = {
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock",
    "uv.lock", "Pipfile.lock", "poetry.lock", "Gemfile.lock", "go.sum",
    "Cargo.lock", "composer.lock",
}
MIGRATION_GLOBS = [
    "**/migrations/**", "**/migrate/**", "**/alembic/versions/**",
    "migrations/**", "db/migrate/**",
]

# Scratch locations a worker may write freely (never part of the repo).
# Only consulted for paths OUTSIDE the worktree — a worktree that itself
# lives under /tmp still gets full scope enforcement.
TEMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/",
                 "/private/var/folders/", "/dev/")

_SPLIT_TOKENS = {"&&", "||", ";", "|", "&", ";;", "(", ")"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Commands whose non-flag args are (or include) write targets.
_WRITE_ALL_ARGS = {"rm", "rmdir", "mv", "mkdir", "touch", "tee", "truncate", "unlink"}
_WRITE_LAST_ARG = {"cp", "ln", "install"}


@dataclass
class GuardDecision:
    allowed: bool
    code: str = "ok"  # ok | outside_worktree | fm_owned | out_of_scope | tripwire_*
    path: str | None = None
    tripwire: str | None = None
    message: str = ""


ALLOW = GuardDecision(allowed=True)


# ------------------------------------------------------------- glob match


def glob_to_regex(pattern: str) -> str:
    """Translate a gitignore-ish glob (with **) to an anchored regex."""
    out, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if pattern[i:i + 3] == "**/":
                out.append(r"(?:.*/)?")
                i += 3
                continue
            if pattern[i:i + 2] == "**":
                out.append(r".*")
                i += 2
                continue
            out.append(r"[^/]*")
        elif ch == "?":
            out.append(r"[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def matches_any(rel_path: str, patterns: list[str]) -> bool:
    return any(re.fullmatch(glob_to_regex(p), rel_path) for p in patterns or [])


# ----------------------------------------------------------- path checks


def _relativize(config: dict, raw_path: str) -> tuple[str | None, GuardDecision | None]:
    """Resolve a tool-supplied path against the worktree. Returns
    (relative posix path, None) or (None, blocking decision)."""
    worktree = Path(os.path.normpath(config["worktree"]))
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        p = worktree / p
    resolved = Path(os.path.normpath(p))  # collapse .. without touching the fs
    s = str(resolved)
    try:
        rel = resolved.relative_to(worktree)
    except ValueError:
        # Outside the worktree: scratch locations are fine, anything else
        # is blocked. (Checked only for outside paths — a worktree that
        # itself lives under /tmp still gets full scope enforcement.)
        if s.startswith(TEMP_PREFIXES) or s == "/dev/null":
            return None, None
        return None, _block(
            config, "outside_worktree", path=s,
            detail=(f"'{raw_path}' resolves outside the task worktree "
                    f"({worktree}). All work happens inside the worktree."),
        )
    return rel.as_posix(), None


def check_path(config: dict, raw_path: str) -> GuardDecision:
    rel, blocked = _relativize(config, raw_path)
    if blocked is not None:
        return blocked
    if rel is None:
        return ALLOW
    if rel == SCRATCH_DIR or rel.startswith(SCRATCH_DIR + "/"):
        # The worker's own scratch space — always allowed, never in scope
        # questions, never committed.
        return ALLOW
    if rel == ".fm" or rel.startswith(".fm/"):
        return _block(
            config, "fm_owned", path=rel,
            detail=(f"'{rel}' is First-Mate-owned orchestration state; never "
                    f"modify it. If you need somewhere to write your own "
                    f"notes or artifacts, use '{SCRATCH_DIR}/' — it is always "
                    f"writable and needs no approval."),
        )
    scope_in = config.get("scope_in") or ["**"]
    scope_out = config.get("scope_out") or []
    if not matches_any(rel, scope_in) or matches_any(rel, scope_out):
        return _block(
            config, "out_of_scope", path=rel,
            detail=(f"'{rel}' is outside the contract's scope "
                    f"(in: {scope_in}, out: {scope_out or '(none)'}). "
                    f"If this is your own scratch artifact (notes, a draft, a "
                    f"report) rather than part of the deliverable, write it "
                    f"under '{SCRATCH_DIR}/' instead — that needs no approval."),
        )
    tripwires = _tripwires(config)
    allow = config.get("tripwire_allow") or []
    name = PurePosixPath(rel).name
    if tripwires.get("dependency_manifests") and name in DEP_MANIFEST_NAMES \
            and not matches_any(rel, allow):
        return _block(
            config, "tripwire_dependency_manifests", path=rel,
            tripwire="dependency_manifests",
            detail=f"'{rel}' is a dependency manifest — changing dependencies needs approval.",
        )
    if tripwires.get("migrations") and matches_any(rel, MIGRATION_GLOBS) \
            and not matches_any(rel, allow):
        return _block(
            config, "tripwire_migrations", path=rel, tripwire="migrations",
            detail=f"'{rel}' looks like a database migration — migrations need approval.",
        )
    return ALLOW


# ----------------------------------------------------------- bash checks


def _split_commands(command: str) -> list[list[str]]:
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        return []  # unparseable; caller falls back to raw-string checks
    cmds: list[list[str]] = []
    cur: list[str] = []
    for tok in tokens:
        if tok in _SPLIT_TOKENS or (tok and set(tok) <= set("|&;")):
            if cur:
                cmds.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        cmds.append(cur)
    return cmds


def _strip_env_assignments(tokens: list[str]) -> list[str]:
    i = 0
    while i < len(tokens) and _ENV_ASSIGN.match(tokens[i]):
        i += 1
    return tokens[i:]


def _dep_install_detail(tokens: list[str]) -> str | None:
    """Returns a human detail string when the command mutates dependencies."""
    if not tokens:
        return None
    name = PurePosixPath(tokens[0]).name
    rest = tokens[1:]
    args = [t for t in rest if not t.startswith("-")]
    if name in {"npm", "pnpm", "yarn", "bun"}:
        verbs = {"add", "remove", "uninstall", "rm", "link"}
        if any(a in verbs for a in args[:2]):
            return f"{name} {' '.join(rest)}"
        # A bare restore of already-locked deps (`bun install`, `npm ci`,
        # `pnpm install --frozen-lockfile`) changes no dependency — it just
        # populates node_modules, which every test/build/lint step needs in a
        # fresh worktree. Asking the operator to approve that is noise, so it
        # passes; the post-step lockfile check is what actually enforces
        # "nothing was added" (orchestrator._lockfile_tripwire).
        # `npm install <pkg>` DOES change the manifest and still trips.
        if args[:1] in (["install"], ["i"]) and len(args) > 1:
            return f"{name} {' '.join(rest)}"
        return None
    if name in {"pip", "pip3"} or (name == "uv" and args[:2][:1] == ["pip"]):
        seq = args[1:] if name == "uv" else args
        if seq[:1] == ["install"]:
            targets = _pip_install_targets(rest)
            if targets:
                return f"{name} install {' '.join(targets)}"
        return None
    if name == "uv" and args[:1] and args[0] in {"add", "remove"}:
        return f"uv {' '.join(rest)}"
    if name == "poetry" and args[:1] and args[0] in {"add", "remove"}:
        return f"poetry {' '.join(rest)}"
    if name == "cargo" and args[:1] and args[0] in {"add", "rm"}:
        return f"cargo {' '.join(rest)}"
    if name == "go" and args[:1] == ["get"]:
        return f"go {' '.join(rest)}"
    if name == "bundle" and args[:1] and args[0] in {"add", "remove"}:
        return f"bundle {' '.join(rest)}"
    return None


def _pip_install_targets(rest: list[str]) -> list[str]:
    """Package args of a pip install, ignoring `-r file` / `-e .` style
    installs of already-declared requirements."""
    if "install" not in rest:
        return []
    after = rest[rest.index("install") + 1:]
    targets, skip = [], False
    for tok in after:
        if skip:
            skip = False
            continue
        if tok in {"-r", "--requirement", "-c", "--constraint", "-e", "--editable"}:
            skip = True
            continue
        if tok.startswith("-") or tok == ".":
            continue
        targets.append(tok)
    return targets


def _bash_write_targets(tokens: list[str]) -> list[str]:
    """Candidate write-target paths in one simple command."""
    targets: list[str] = []
    # Redirections first (shlex punctuation mode yields '>' '>>' tokens).
    stripped: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in {">", ">>"} and i + 1 < len(tokens):
            targets.append(tokens[i + 1])
            i += 2
            continue
        if tok == "<" and i + 1 < len(tokens):
            i += 2  # read redirection — not a write
            continue
        stripped.append(tok)
        i += 1
    stripped = _strip_env_assignments(stripped)
    if not stripped:
        return targets
    name = PurePosixPath(stripped[0]).name
    args = [t for t in stripped[1:] if not t.startswith("-")]
    if name in _WRITE_ALL_ARGS:
        targets.extend(args)
    elif name in _WRITE_LAST_ARG and args:
        targets.append(args[-1])
    elif name == "sed" and any(t == "-i" or t.startswith("-i") for t in stripped[1:]):
        targets.extend(args)
    return [t for t in targets if t not in {"-", "/dev/null"}]


def check_bash(config: dict, command: str) -> GuardDecision:
    tripwires = _tripwires(config)
    cmds = _split_commands(command)
    if not cmds:
        # Unparseable command: fall back to a raw-string push check only —
        # the worker's Bash allowlist is the primary gate for exotic shell.
        if tripwires.get("git_push") and re.search(r"\bgit\b[^|;&]*\bpush\b", command):
            return _git_push_block(config, command)
        return ALLOW
    for tokens in cmds:
        simple = _strip_env_assignments(tokens)
        if not simple:
            continue
        name = PurePosixPath(simple[0]).name
        if tripwires.get("git_push") and name == "git" and "push" in simple[1:]:
            return _git_push_block(config, " ".join(simple))
        detail = _dep_install_detail(simple)
        if detail and tripwires.get("dependency_manifests"):
            return _block(
                config, "tripwire_dependency_manifests", tripwire="dependency_manifests",
                detail=f"`{detail}` changes project dependencies — that needs approval.",
            )
        for target in _bash_write_targets(tokens):
            decision = check_path(config, target)
            if not decision.allowed:
                return decision
    return ALLOW


def _git_push_block(config: dict, cmd: str) -> GuardDecision:
    return _block(
        config, "tripwire_git_push", tripwire="git_push",
        detail=f"`{cmd.strip()}` pushes to a remote — pushes need operator approval.",
    )


# ------------------------------------------------------------ evaluation


def _tripwires(config: dict) -> dict:
    merged = dict(DEFAULT_TRIPWIRES)
    merged.update(config.get("tripwires") or {})
    return merged


def _block(config: dict, code: str, path: str | None = None,
           tripwire: str | None = None, detail: str = "") -> GuardDecision:
    if tripwire:
        qtype = "approval"
        evidence: dict = {"tripwire": tripwire}
        if path:
            evidence["paths"] = [path]
    else:
        qtype = "scope_change"
        evidence = {"paths": [path]} if path else {}
    ev = json.dumps(evidence)
    message = (
        f"First Mate scope guard: BLOCKED. {detail}\n"
        f"If this is genuinely required to complete the step, raise it and stop:\n"
        f"  fm ask --type {qtype} --question \"<what you need and why>\" "
        f"--option allow --option deny --default deny --evidence '{ev}'\n"
        f"then STOP IMMEDIATELY and end the session — the orchestrator resumes "
        f"you with the operator's answer. Otherwise continue within scope."
    )
    return GuardDecision(allowed=False, code=code, path=path,
                         tripwire=tripwire, message=message)


def evaluate(config: dict, tool_name: str, tool_input: dict) -> GuardDecision:
    """Judge one tool call. Unknown tools pass (the allowlist gates them)."""
    if tool_name in FILE_TOOLS:
        raw = str(tool_input.get(FILE_TOOLS[tool_name], "") or "")
        if not raw:
            return ALLOW
        return check_path(config, raw)
    if tool_name == "Bash":
        return check_bash(config, str(tool_input.get("command", "") or ""))
    return ALLOW


def build_config(contract, global_config: dict, worktree: Path) -> dict:
    """Compile the contract + project defaults into the guard.json the
    hook evaluates. Contract-level tripwires override project defaults
    (operator approvals mutate the contract, so approvals flow in here)."""
    tripwires = dict(DEFAULT_TRIPWIRES)
    tripwires.update(global_config.get("tripwires") or {})
    tripwires.update(getattr(contract, "tripwires", None) or {})
    return {
        "worktree": str(worktree),
        "scope_in": list(contract.scope_in or ["**"]),
        "scope_out": list(contract.scope_out or []),
        "tripwire_allow": list(getattr(contract, "tripwire_allow", None) or []),
        "tripwires": tripwires,
    }
