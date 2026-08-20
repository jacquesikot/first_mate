"""Cleanup — reclaiming disk from finished tasks (PRD §6.11).

A task's worktree is where the work physically happened, so deleting one
is the most destructive thing First Mate can do to something it cannot
recreate. Nothing here ever runs on its own: the orchestrator posts a
non-blocking notice when a task finishes, and everything else is the
operator asking (`fm clean`, or the dashboard's clean action).

The safety bar, applied before anything is removed:

  * a dirty tree, or a commit that exists on no remote, means the
    worktree holds work that only exists here — skip it and say why;
  * only `--force` overrides that, and only per invocation.

Judgement calls it does make on its own, because they destroy nothing
unrecoverable: dependency directories (node_modules, .venv, target) in a
worktree kept for its unpushed work are regenerable, so they can be
dropped once idle. Task state under ~/.firstmate is never deleted — it
is the audit trail, it is measured in kilobytes, and it is exactly what
makes a stuck task diagnosable after the fact.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .exec import gitops

# Regenerable, and the reason a worktree costs hundreds of megabytes.
DEP_DIRS = ("node_modules", ".venv", "venv", "target", ".next", "dist",
            "build", ".turbo", "__pycache__", ".pytest_cache")

# Task statuses whose worktree is no longer needed for the task to proceed.
FINISHED = {"done", "failed", "abandoned"}


def dir_size(path: Path) -> int:
    """Bytes under a directory. Best effort: unreadable entries are skipped
    rather than raising, since this only ever feeds a human-readable total."""
    total = 0
    if not path.exists():
        return 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


# Monorepos put node_modules under frontend/, packages/*/ and so on, so a
# root-only scan reports 0B for a worktree that is 450MB of dependencies.
# Bounded depth keeps this from walking the whole tree.
_DEP_SCAN_DEPTH = 3


def find_dep_dirs(worktree: Path, max_depth: int = _DEP_SCAN_DEPTH) -> list[Path]:
    """Dependency/build directories anywhere in the worktree, nearest first.

    Never descends INTO a match (no node_modules/x/node_modules), and never
    into `.git` or First Mate's own `.fm/`.
    """
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(d.iterdir())
        except OSError:
            return
        for e in entries:
            try:
                if not e.is_dir() or e.is_symlink():
                    continue
            except OSError:
                continue
            if e.name in (".git", ".fm"):
                continue
            if e.name in DEP_DIRS:
                found.append(e)
                continue  # don't recurse into a match
            walk(e, depth + 1)

    walk(worktree, 0)
    return found


def human(n: float) -> str:
    """Bytes as something a person can read at a glance."""
    size = float(n)
    for unit in ("B", "KB", "MB"):
        if size < 1024.0:
            return f"{int(size)}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}GB"


@dataclass
class Candidate:
    """One task's reclaimable disk, and whether it is safe to reclaim."""

    task_id: str
    status: str
    worktree: str = ""
    branch: str = ""
    repo: str = ""
    bytes: int = 0
    dep_bytes: int = 0
    # Why this cannot be cleaned without --force. Empty means safe.
    blockers: list[str] = field(default_factory=list)
    idle_days: float = 0.0

    @property
    def safe(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id, "status": self.status,
            "worktree": self.worktree, "branch": self.branch,
            "repo": self.repo, "bytes": self.bytes,
            "dep_bytes": self.dep_bytes, "blockers": list(self.blockers),
            "idle_days": round(self.idle_days, 1), "safe": self.safe,
            "size": human(self.bytes),
        }


def inspect(task) -> Candidate | None:
    """Assess one task's worktree. Returns None when there is nothing there.

    Deliberately conservative: anything it cannot establish counts as a
    blocker, because "I'm not sure" must never lead to a deletion.
    """
    if not task.worktree:
        return None
    wt = Path(task.worktree)
    if not wt.is_dir():
        return None
    cand = Candidate(
        task_id=task.id, status=task.status, worktree=str(wt),
        branch=task.branch, repo=task.repo,
    )
    cand.bytes = dir_size(wt)
    cand.dep_bytes = sum(dir_size(d) for d in find_dep_dirs(wt))
    # Idle = how long since anything the operator would recognise as work
    # was touched. Deliberately the worktree directory itself: `.git` gets
    # written by any background git command, so including it would keep a
    # long-abandoned worktree looking permanently fresh.
    try:
        newest = wt.stat().st_mtime
        for name in ("HEAD", "index"):
            g = wt / ".git" / name
            if g.is_file():
                newest = max(newest, g.stat().st_mtime)
        cand.idle_days = max(0.0, (time.time() - newest) / 86400.0)
    except OSError:
        cand.idle_days = 0.0

    if task.status not in FINISHED:
        cand.blockers.append(f"task is {task.status}, not finished")

    # Uncommitted files. `.fm/` is First Mate's own state and is excluded
    # from review elsewhere, so it must not count as the operator's work.
    try:
        dirty = [f for f in gitops.changed_files(wt)
                 if not gitops.is_review_noise(f)]
    except gitops.GitError:
        dirty = None
    if dirty is None:
        cand.blockers.append("could not read git status")
    elif dirty:
        shown = ", ".join(dirty[:3]) + ("…" if len(dirty) > 3 else "")
        cand.blockers.append(
            f"{len(dirty)} uncommitted file(s) exist only here: {shown}")

    # Commits reachable from no remote would be lost with the branch.
    unpushed = gitops.unpushed_commits(wt)
    if unpushed is None:
        cand.blockers.append("could not determine whether commits are pushed")
    elif unpushed:
        cand.blockers.append(
            f"{len(unpushed)} unpushed commit(s): {unpushed[0]}")
    return cand


def candidates(store) -> list[Candidate]:
    """Every task with a worktree on disk, largest first."""
    out = []
    for row in store.list_tasks():
        task = store.load_task(row["id"])
        if task is None:
            continue
        cand = inspect(task)
        if cand is not None:
            out.append(cand)
    return sorted(out, key=lambda c: c.bytes, reverse=True)


def remove(store, task, force: bool = False) -> tuple[bool, str]:
    """Remove one task's worktree and its fm/* branch.

    Returns (removed, message). Refuses unsafe candidates unless forced —
    the message then says exactly what stopped it, so `fm clean --all`
    reports rather than silently skipping.
    """
    cand = inspect(task)
    if cand is None:
        return False, "no worktree on disk"
    if not cand.safe and not force:
        return False, "; ".join(cand.blockers)
    repo = Path(task.repo)
    try:
        gitops.remove_worktree(repo, task.branch, force=True)
    except gitops.GitError as e:
        return False, f"git refused: {e}"
    # Only ever delete the task's own fm/* branch, and only when merged
    # (or forced) — `delete_branch` is a quiet no-op otherwise, which is
    # the safe direction.
    if task.branch.startswith("fm/"):
        gitops.delete_branch(repo, task.branch, force=force)
    freed = cand.bytes
    task.worktree = ""
    store.save_task(task)
    return True, f"removed {cand.worktree} ({human(freed)})"


def drop_deps(worktree: Path) -> tuple[int, list[str]]:
    """Delete regenerable dependency/build directories. Returns (bytes
    freed, names removed). The code and git history are untouched, so this
    costs an install to undo and nothing more."""
    freed, removed = 0, []
    for d in find_dep_dirs(worktree):
        size = dir_size(d)
        try:
            shutil.rmtree(d)
        except OSError:
            continue
        freed += size
        try:
            removed.append(str(d.relative_to(worktree)))
        except ValueError:
            removed.append(d.name)
    return freed, removed


def prune_smoke_runs(home: Path, keep_days: float = 3.0) -> tuple[int, int]:
    """Delete smoke-test run directories older than keep_days.

    These are throwaway by construction — a scratch repo plus a state home
    per run — and nothing references them once the run has reported.
    Returns (runs removed, bytes freed).
    """
    root = home / "smoke"
    if not root.is_dir():
        return 0, 0
    cutoff = time.time() - keep_days * 86400.0
    runs, freed = 0, 0
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not d.name.startswith("run-"):
            continue
        try:
            if d.stat().st_mtime > cutoff:
                continue
            size = dir_size(d)
            shutil.rmtree(d)
        except OSError:
            continue
        runs += 1
        freed += size
    return runs, freed


def archive_task_state(store, task_id: str) -> Path | None:
    """Compress a finished task's state directory into `archive/`.

    The data is kept — it is the audit trail, and it is what makes a task
    diagnosable months later — but it stops cluttering `tasks/`. Returns
    the archive path, or None if there was nothing to archive.
    """
    d = store.task_dir(task_id)
    if not d.is_dir():
        return None
    archive_dir = store.home / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    base = archive_dir / task_id
    try:
        made = shutil.make_archive(str(base), "gztar",
                                   root_dir=str(d.parent), base_dir=d.name)
    except (OSError, shutil.Error):
        return None
    try:
        shutil.rmtree(d)
    except OSError:
        # Archive exists but the original could not be removed: leave both
        # rather than lose the state.
        return Path(made)
    return Path(made)


def maintenance(store, deps_after_days: float = 3.0,
                archive_after_days: float = 14.0,
                smoke_keep_days: float = 3.0,
                dry_run: bool = False) -> dict:
    """Idle housekeeping: drop rebuildable deps from long-idle worktrees,
    archive long-finished task state, prune old smoke runs.

    Never removes a worktree and never deletes task state — the only
    things it deletes are regenerable (dependency directories) or
    throwaway (smoke runs). Worktree removal always needs the operator.
    """
    report: dict = {"deps": [], "archived": [], "smoke_runs": 0,
                    "freed": 0, "dry_run": dry_run}
    for cand in candidates(store):
        if cand.dep_bytes and cand.idle_days >= deps_after_days:
            report["deps"].append({"task_id": cand.task_id,
                                   "bytes": cand.dep_bytes,
                                   "idle_days": round(cand.idle_days, 1)})
            report["freed"] += cand.dep_bytes
            if not dry_run:
                drop_deps(Path(cand.worktree))

    now = time.time()
    for row in store.list_tasks():
        if row.get("status") not in FINISHED:
            continue
        d = store.task_dir(row["id"])
        if not d.is_dir():
            continue
        try:
            age_days = (now - d.stat().st_mtime) / 86400.0
        except OSError:
            continue
        if age_days < archive_after_days:
            continue
        # Never archive state for a task whose worktree is still around —
        # it is still in play as far as the operator is concerned.
        task = store.load_task(row["id"])
        if task is not None and task.worktree and Path(task.worktree).is_dir():
            continue
        report["archived"].append({"task_id": row["id"],
                                   "age_days": round(age_days, 1)})
        if not dry_run:
            archive_task_state(store, row["id"])

    if not dry_run:
        runs, freed = prune_smoke_runs(store.home, keep_days=smoke_keep_days)
        report["smoke_runs"] = runs
        report["freed"] += freed
    return report


__all__ = ["Candidate", "inspect", "candidates", "remove", "drop_deps",
           "prune_smoke_runs", "archive_task_state", "maintenance",
           "dir_size", "find_dep_dirs", "human", "DEP_DIRS", "FINISHED"]
