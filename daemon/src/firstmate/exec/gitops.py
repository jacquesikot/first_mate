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
    """Create (or reuse) a worktree for `branch`, branching off `base`.

    `base` is any committish — a local branch, a remote-tracking ref like
    `origin/main`, a tag, or a SHA. The task's branch is always its own; the
    base only decides where it starts, so the operator's own branches are
    never moved (see STATUS.md decision log)."""
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


# --------------------------------------------------------- starting points


def has_remote(repo: Path, name: str = "origin") -> bool:
    out = _git(repo, "remote", check=False).stdout
    return name in out.split()


def fetch(repo: Path, remote: str = "origin", timeout: int = 60) -> str | None:
    """`git fetch --prune`. Returns None on success, else a short reason.

    Read-only with respect to the working tree — it only updates
    remote-tracking refs, so it is safe to run against a dirty repo."""
    if not has_remote(repo, remote):
        return "no remote configured"
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "fetch", "--prune", "--quiet", remote],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"fetch timed out after {timeout}s"
    except OSError as e:
        return str(e)
    if proc.returncode != 0:
        return (proc.stderr.strip() or "fetch failed").splitlines()[-1][:200]
    return None


def default_branch(repo: Path, remote: str = "origin") -> str | None:
    """The remote's default branch (`origin/main`, `origin/master`, …).

    `origin/HEAD` is only set by `git clone`, so it is often missing in
    repos created another way; fall back to asking the remote, then to the
    conventional names."""
    ref = _git(repo, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD",
               check=False).stdout.strip()
    if ref:
        return ref.removeprefix("refs/remotes/")
    head = _git(repo, "ls-remote", "--symref", remote, "HEAD",
                check=False).stdout
    for line in head.splitlines():
        if line.startswith("ref:"):
            name = line.split()[1].removeprefix("refs/heads/")
            return f"{remote}/{name}"
    for name in ("main", "master", "trunk", "develop"):
        if _git(repo, "rev-parse", "--verify", "--quiet",
                f"refs/remotes/{remote}/{name}", check=False).returncode == 0:
            return f"{remote}/{name}"
    return None


def current_branch(repo: Path) -> str | None:
    """The checked-out branch, or None when detached."""
    name = _git(repo, "rev-parse", "--abbrev-ref", "HEAD",
                check=False).stdout.strip()
    return None if name in ("", "HEAD") else name


def is_dirty(repo: Path) -> bool:
    return bool(_git(repo, "status", "--porcelain", check=False).stdout.strip())


_REF_FORMAT = "%(refname:short)%09%(objectname)%09%(committerdate:iso8601)%09%(upstream:short)%09%(upstream:track)%09%(subject)"


def list_refs(repo: Path, limit: int = 60) -> list[dict]:
    """Local branches and remote-tracking branches, newest commit first.

    Each entry carries what the operator needs to choose a starting point
    knowingly: how old the tip is, and how it relates to its upstream."""
    out = _git(repo, "for-each-ref", f"--format={_REF_FORMAT}",
               "--sort=-committerdate", f"--count={limit}",
               "refs/heads", "refs/remotes", check=False).stdout
    remotes = set(_git(repo, "remote", check=False).stdout.split())
    refs: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        name, sha, date, upstream, track, subject = parts[:6]
        if name.endswith("/HEAD"):  # origin/HEAD is an alias, not a choice
            continue
        ahead = behind = None
        for token in track.strip("[]").split(", "):
            if token.startswith("ahead "):
                ahead = int(token.removeprefix("ahead "))
            elif token.startswith("behind "):
                behind = int(token.removeprefix("behind "))
        refs.append({
            "name": name,
            "sha": sha[:10],
            "committed_at": date,
            "remote": name.split("/", 1)[0] in remotes,
            "upstream": upstream or None,
            "ahead": ahead,
            "behind": behind,
            "gone": "gone" in track,
            "subject": subject[:120],
        })
    return refs


def resolve_ref(repo: Path, ref: str) -> str | None:
    """The commit a starting point names, or None if it doesn't resolve."""
    proc = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
                check=False)
    sha = proc.stdout.strip()
    return sha or None


def remove_worktree(repo: Path, branch: str, force: bool = False) -> None:
    path = worktree_path(repo, branch)
    if not path.exists():
        return
    args = ["worktree", "remove", str(path)]
    if force:
        args.append("--force")
    _git(repo, *args)


def delete_branch(repo: Path, branch: str, force: bool = False) -> None:
    """Delete a local branch. Quiet no-op if it doesn't exist or still has
    unmerged commits (without `force`) — callers use this for cleanup, where
    keeping a branch is always safer than losing it."""
    _git(repo, "branch", "-D" if force else "-d", branch, check=False)


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


def unpushed_commits(worktree: Path) -> list[str] | None:
    """Commits in this worktree that exist on no remote.

    Returns a list of `<sha> <subject>` lines (empty when everything is
    reachable from some remote ref), or None when it cannot be determined —
    callers treat None as "assume unpushed" so cleanup stays conservative.
    """
    # `HEAD` is required: without a positive revision, `--not --remotes`
    # gives git nothing to subtract FROM and it reports nothing at all —
    # which would declare a worktree holding unpushed commits safe to
    # delete. Caught by a test, not by reading.
    proc = _git(worktree, "log", "--oneline", "HEAD", "--not", "--remotes",
                check=False)
    if proc.returncode != 0:
        return None
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def head_commit(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def init_repo(path: Path) -> None:
    """Create a fresh repo with an initial commit (used by tests/spikes)."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    _git(path, "commit", "--allow-empty", "-q", "-m", "init")
