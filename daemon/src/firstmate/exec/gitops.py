"""Git operations — the only module that shells out to git.

Worktree lifecycle at `<repo-parent>/<repo>-worktrees/<branch>` (PRD §7),
plus the read-side queries the orchestrator and dashboard need
(changed files, diffs, diff stats).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def worktrees_root(repo: Path) -> Path:
    repo = repo.resolve()
    return repo.parent / f"{repo.name}-worktrees"


def worktree_path(repo: Path, branch: str) -> Path:
    # Branch names may contain '/'; flatten for the directory name.
    return worktrees_root(repo) / branch.replace("/", "-")


def create_worktree(repo: Path, branch: str, base: str = "HEAD") -> Path:
    """Create (or reuse) a worktree for `branch`, branching off `base`."""
    path = worktree_path(repo, branch)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = (
        _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode
        == 0
    )
    if branch_exists:
        _git(repo, "worktree", "add", str(path), branch)
    else:
        _git(repo, "worktree", "add", "-b", branch, str(path), base)
    return path


def remove_worktree(repo: Path, branch: str, force: bool = False) -> None:
    path = worktree_path(repo, branch)
    if not path.exists():
        return
    args = ["worktree", "remove", str(path)]
    if force:
        args.append("--force")
    _git(repo, *args)


def list_worktrees(repo: Path) -> list[Path]:
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    return [
        Path(line.removeprefix("worktree "))
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]


def changed_files(worktree: Path) -> list[str]:
    """Files changed vs. the worktree's base (staged, unstaged, untracked)."""
    out = _git(worktree, "status", "--porcelain").stdout
    return [line[3:] for line in out.splitlines() if line]


def diff(worktree: Path) -> str:
    return _git(worktree, "diff", "HEAD").stdout


def diff_stat(worktree: Path) -> str:
    return _git(worktree, "diff", "HEAD", "--stat").stdout


# Paths that are never part of what the operator is reviewing: First Mate's
# own per-worktree state, and build/cache droppings a repo without a
# .gitignore would otherwise surface as "changes".
NOISE_PREFIXES = (".fm/",)
NOISE_DIRS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", ".DS_Store",
})
NOISE_SUFFIXES = (".pyc", ".pyo")


def is_review_noise(path: str) -> bool:
    """True for paths that shouldn't appear in the operator's diff."""
    if path.startswith(NOISE_PREFIXES) or path == ".fm":
        return True
    if path.endswith(NOISE_SUFFIXES):
        return True
    return any(part in NOISE_DIRS for part in path.split("/"))


def numstat_files(worktree: Path) -> list[dict]:
    """Per-file (added, deleted) vs HEAD for tracked changes, plus
    untracked files (line counts from the file itself). Binary files
    report None counts. First Mate's own `.fm/` state and build caches are
    filtered out — they are orchestration, not the work under review."""
    out = _git(worktree, "diff", "HEAD", "--numstat").stdout
    files: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if is_review_noise(parts[2]):
            continue
        files.append({
            "path": parts[2],
            "added": int(parts[0]) if parts[0].isdigit() else None,
            "deleted": int(parts[1]) if parts[1].isdigit() else None,
            "untracked": False,
        })
    ls = _git(worktree, "ls-files", "--others", "--exclude-standard").stdout
    for path in ls.splitlines():
        if not path.strip() or is_review_noise(path):
            continue
        added = None
        try:
            with (worktree / path).open("rb") as f:
                added = sum(1 for _ in f)
        except OSError:
            pass
        files.append({"path": path, "added": added, "deleted": 0, "untracked": True})
    return files


def diff_file(worktree: Path, path: str) -> str:
    """Unified diff vs HEAD for one file; untracked files diff against
    /dev/null (git exits 1 when the files differ — not an error)."""
    tracked = _git(
        worktree, "ls-files", "--error-unmatch", "--", path, check=False
    ).returncode == 0
    if tracked:
        return _git(worktree, "diff", "HEAD", "--", path).stdout
    proc = _git(worktree, "diff", "--no-index", "--", "/dev/null", path, check=False)
    return proc.stdout


def diff_numstat(worktree: Path) -> tuple[int, int]:
    """(added, deleted) line totals vs HEAD for tracked files, excluding
    review noise (see `is_review_noise`). Binary files report '-' in
    numstat and are counted as 0. Diff tripwires read these totals, so
    First Mate's own state must not count against the operator's budget."""
    out = _git(worktree, "diff", "HEAD", "--numstat").stdout
    added = deleted = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or is_review_noise(parts[2]):
            continue
        added += int(parts[0]) if parts[0].isdigit() else 0
        deleted += int(parts[1]) if parts[1].isdigit() else 0
    return added, deleted


def head_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def init_repo(path: Path) -> None:
    """Create a fresh repo with an initial commit (used by tests/spikes)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "commit", "--allow-empty", "-q", "-m", "init")
