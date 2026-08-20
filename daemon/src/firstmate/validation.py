"""Validation — run a contract's machine-checkable criteria (PRD §6.7).

Phase 1 supports kind == "shell": the command runs in the worktree and
passes iff it exits 0. Results carry the evidence (exit status, duration,
output) for the store to persist alongside the step.
"""

from __future__ import annotations

import dataclasses
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .models import Criterion, Gate

OUTPUT_CAP = 20_000  # chars kept per stream; enough to debug, still jq-able


@dataclass
class CriterionResult:
    id: str
    passed: bool
    command: str
    exit_status: int | None = None
    duration_s: float = 0.0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def run_criterion(worktree: Path, crit: Criterion) -> CriterionResult:
    cwd = (worktree / crit.cwd).resolve()
    start = time.monotonic()
    try:
        proc = subprocess.run(
            crit.command, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=crit.timeout,
        )
        return CriterionResult(
            id=crit.id,
            passed=proc.returncode == 0,
            command=crit.command,
            exit_status=proc.returncode,
            duration_s=round(time.monotonic() - start, 2),
            stdout=proc.stdout[-OUTPUT_CAP:],
            stderr=proc.stderr[-OUTPUT_CAP:],
        )
    except subprocess.TimeoutExpired:
        return CriterionResult(
            id=crit.id, passed=False, command=crit.command,
            duration_s=round(time.monotonic() - start, 2),
            error=f"timed out after {crit.timeout}s",
        )
    except OSError as e:
        return CriterionResult(
            id=crit.id, passed=False, command=crit.command,
            duration_s=round(time.monotonic() - start, 2), error=str(e),
        )


def run_criteria(worktree: Path, criteria: list[Criterion]) -> list[CriterionResult]:
    return [run_criterion(worktree, c) for c in criteria]


def run_gate(worktree: Path, gate: Gate) -> CriterionResult:
    """Probe a step's `when` gate once. Passing means the step may run.

    Deliberately reuses the criterion machinery: a gate is the same kind
    of machine-checkable shell fact, just evaluated before the work rather
    than after it.
    """
    return run_criterion(
        worktree,
        Criterion(id="gate", command=gate.command, kind=gate.kind,
                  cwd=gate.cwd, timeout=gate.timeout),
    )
