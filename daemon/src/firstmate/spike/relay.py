"""Phase 0 relay spike.

Exit criterion (PRD §8): a task deliberately overflowed at a tiny context
limit completes correctly across >=3 generations.

What one run exercises end to end:
  1. Headless spawner — `claude -p` + pinned session ID in a git worktree
     inside a tmux window, structured JSON output captured.
  2. Context tracking — token occupancy polled from the session transcript.
  3. Forced context wall — at --wall tokens the worker is interrupted.
  4. Handoff — the walled session is resumed headlessly and asked for a
     structured handoff brief (what's done / what remains / gotchas).
  5. Relay — generation N+1 spawns fresh, with contract + handoff injected
     via the SessionStart hook's additionalContext (a canary string proves
     whether the injection actually landed in context).
  6. Hook eventing — SessionStart/Stop/PreCompact hooks append to
     .fm/events.jsonl; PreCompact exits 2 (blocks auto-compaction).

The task is designed to overflow: for each item the worker must `cat` a
~28KB filler file ("reference material") before appending one transformed
line to task/output.md. Transform: word reversed, uppercased.

Run:  cd daemon && uv run fm-spike [--items 16] [--wall 50000] [--max-gens 8]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import random
import shlex
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

from ..exec import context, gitops, hooks, tmux
from .. import spawner

CANARY = "FM-CANARY-PELICAN"

CONTRACT = """\
# Task contract: spike-transform

Goal: for EVERY line in task/items.txt (format `item-NN: <word>`), append
exactly one line to task/output.md in the same order:

    item-NN: <TRANSFORMED>

where <TRANSFORMED> is <word> reversed and uppercased.
Example: `item-01: kayak` -> append `item-01: KAYAK` reversed = `KAYAK`
is a palindrome, so a clearer example: `item-02: stone` -> `item-02: ENOTS`.

Mandatory procedure (do not optimize it away — the reference step is a
compliance requirement of this contract):
1. Look at task/output.md to see which items are already done.
2. For each remaining item, IN ORDER, one item at a time:
   a. Run exactly: cat task/filler.txt   (required reference material —
      you must re-read it before EVERY item, every time)
   b. Append that single item's transformed line to task/output.md.
3. Never batch multiple items into one write. Never skip step 2a.
4. Create task/output.md if it does not exist. Never rewrite existing lines.

Completion: task/output.md contains one correct line per item, in order.
"""

WORKER_PROMPT = """\
You are First Mate worker generation {gen} for task 'spike-transform'.
Session context (contract summary, progress, and a handoff brief from the
previous generation, if any) was injected at session start; if you don't
see it, read .fm/inject.md.
The full contract is at task/contract.md — read it and follow it EXACTLY,
including its mandatory reference-material procedure.
Continue from the current state of task/output.md and work until the task
is complete. Do not stop early. Do not summarize; just do the work.
"""

HANDOFF_PROMPT = """\
STOP working on the task. You are out of context budget and are being
replaced by a fresh session (a relay). Write a handoff brief for your
replacement, as plain text with exactly these sections:

DONE: what has been completed (be specific: which items are written to
task/output.md, through which item number).
REMAINING: what is left to do, starting with the exact next item.
GOTCHAS: anything the contract requires that a fresh session might miss,
plus any mistakes to avoid.

Do not use any tools. Answer with the brief only.
"""


# ---------------------------------------------------------------- setup

def make_filler(path: Path, kilobytes: int = 28) -> None:
    rng = random.Random(1234)
    words = []
    while sum(len(w) + 1 for w in words) < kilobytes * 1024:
        words.append(
            "".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(rng.randint(4, 10)))
        )
    lines = [" ".join(words[i : i + 12]) for i in range(0, len(words), 12)]
    path.write_text(
        "REFERENCE MATERIAL (contract-required reading before every item)\n"
        + "\n".join(lines)
        + "\n"
    )


def make_items(path: Path, n: int) -> list[tuple[str, str]]:
    rng = random.Random(99)
    pool = (
        "stone river maple falcon ember quartz willow harbor cinder meadow "
        "onyx thistle branch copper drift lantern pebble summit velvet norska "
        "timber anchor breeze cascade dorval fenwick garnet hollow ivory jasper"
    ).split()
    items = [(f"item-{i:02d}", rng.choice(pool)) for i in range(1, n + 1)]
    path.write_text("".join(f"{iid}: {word}\n" for iid, word in items))
    return items


def expected_lines(items: list[tuple[str, str]]) -> list[str]:
    return [f"{iid}: {word[::-1].upper()}" for iid, word in items]


def write_hook_files(worktree: Path) -> Path:
    """Hook scripts + the worker settings file wiring them. Returns the
    settings file path (passed to `claude --settings`)."""
    fm = worktree / ".fm"
    hooks_dir = fm / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    events = fm / "events.jsonl"

    def log_cmd(event: str) -> str:
        return (
            f"printf '%s' \"$INPUT\" | jq -c '{{event: \"{event}\", session_id: .session_id, "
            f"source: (.source // null), ts: $ts}}' --arg ts \"$(date -u +%FT%TZ)\" "
            f">> {shlex.quote(str(events))}"
        )

    scripts = {
        "session_start.sh": textwrap.dedent(
            f"""\
            #!/bin/sh
            INPUT=$(cat)
            {log_cmd("SessionStart")}
            if [ -f {shlex.quote(str(fm / "inject.md"))} ]; then
              jq -n --rawfile ctx {shlex.quote(str(fm / "inject.md"))} \\
                '{{hookSpecificOutput: {{hookEventName: "SessionStart", additionalContext: $ctx}}}}'
            fi
            exit 0
            """
        ),
        "pre_compact.sh": textwrap.dedent(
            f"""\
            #!/bin/sh
            INPUT=$(cat)
            {log_cmd("PreCompact")}
            echo "First Mate: compaction blocked; the orchestrator relay owns context handoff." >&2
            exit 2
            """
        ),
        "stop.sh": textwrap.dedent(
            f"""\
            #!/bin/sh
            INPUT=$(cat)
            {log_cmd("Stop")}
            exit 0
            """
        ),
    }
    for name, body in scripts.items():
        p = hooks_dir / name
        p.write_text(body)
        p.chmod(0o755)

    settings = hooks.build_hooks_settings(
        {
            "SessionStart": [hooks.hook_entry(str(hooks_dir / "session_start.sh"), timeout=10)],
            "PreCompact": [hooks.hook_entry(str(hooks_dir / "pre_compact.sh"), timeout=10)],
            "Stop": [hooks.hook_entry(str(hooks_dir / "stop.sh"), timeout=10)],
        }
    )
    settings_file = fm / "settings.json"
    hooks.write_settings(settings_file, settings)
    return settings_file


# ---------------------------------------------------------------- checks

@dataclass
class Progress:
    correct: int
    total: int
    complete: bool
    first_error: str | None


def verify(worktree: Path, expected: list[str]) -> Progress:
    out = worktree / "task" / "output.md"
    actual = (
        [l.strip() for l in out.read_text().splitlines() if l.strip()]
        if out.exists()
        else []
    )
    correct = 0
    first_error = None
    for i, exp in enumerate(expected):
        if i < len(actual) and actual[i] == exp:
            correct += 1
        else:
            got = actual[i] if i < len(actual) else "<missing>"
            first_error = f"line {i + 1}: expected {exp!r}, got {got!r}"
            break
    extra = len(actual) > len(expected)
    complete = correct == len(expected) and not extra
    if extra and first_error is None:
        first_error = f"{len(actual) - len(expected)} unexpected extra line(s)"
    return Progress(correct, len(expected), complete, first_error)


def request_handoff(worktree: Path, session_id: str, model: str) -> str:
    """LLM decision point: resume the walled session and ask for a brief."""
    cmd = [
        "claude", "-p", HANDOFF_PROMPT,
        "--resume", session_id,
        "--output-format", "json",
        "--model", model,
        "--tools", "",
        "--permission-mode", "dontAsk",
    ]
    proc = subprocess.run(
        cmd, cwd=worktree, capture_output=True, text=True, timeout=300
    )
    if proc.returncode != 0:
        return f"(handoff request failed: exit {proc.returncode}: {proc.stderr[-500:]})"
    try:
        return json.loads(proc.stdout).get("result", "").strip()
    except json.JSONDecodeError:
        return f"(handoff request returned non-JSON: {proc.stdout[-500:]})"


def canary_landed(worktree: Path, session_id: str) -> bool:
    """Did SessionStart additionalContext actually reach the transcript?"""
    path = context.transcript_path(worktree, session_id)
    return path.exists() and CANARY in path.read_text()


# ------------------------------------------------------------------ run

def main() -> None:
    ap = argparse.ArgumentParser(description="First Mate Phase 0 relay spike")
    ap.add_argument("--items", type=int, default=16)
    ap.add_argument("--wall", type=int, default=50_000, help="context wall in tokens")
    ap.add_argument("--max-gens", type=int, default=8)
    ap.add_argument("--worker-model", default="sonnet")
    ap.add_argument("--root", type=Path, default=None, help="run directory (default ~/.firstmate/spike/run-<ts>)")
    args = ap.parse_args()

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    root = args.root or (Path.home() / ".firstmate" / "spike" / f"run-{stamp}")
    repo = root / "repo"
    print(f"[spike] run root: {root}")

    gitops.init_repo(repo)
    worktree = gitops.create_worktree(repo, "fm-spike-task")
    task_dir = worktree / "task"
    task_dir.mkdir(exist_ok=True)
    items = make_items(task_dir / "items.txt", args.items)
    make_filler(task_dir / "filler.txt")
    (task_dir / "contract.md").write_text(CONTRACT)
    expected = expected_lines(items)
    settings_file = write_hook_files(worktree)

    fm = worktree / ".fm"
    report: list[dict] = []
    handoff: str | None = None
    success = False

    for gen in range(1, args.max_gens + 1):
        inject = [f"{CANARY} (generation {gen})", "", CONTRACT]
        if handoff:
            inject += ["", "## Handoff from the previous generation", "", handoff]
        (fm / "inject.md").write_text("\n".join(inject) + "\n")

        spec = spawner.WorkerSpec(
            prompt=WORKER_PROMPT.format(gen=gen),
            cwd=worktree,
            name=f"spike-gen{gen}",
            settings_file=settings_file,
            allowed_tools=["Read", "Write", "Edit", "Bash(cat *)"],
            permission_mode="dontAsk",
            model=args.worker_model,
        )
        print(f"[gen {gen}] spawning session {spec.session_id}")
        window = spawner.spawn(spec)

        peak = 0

        def on_poll() -> bool:
            nonlocal peak
            reading = context.read_context(worktree, spec.session_id)
            if reading:
                peak = max(peak, reading.tokens)
                print(
                    f"\r[gen {gen}] context {reading.tokens:>7,} tok "
                    f"({reading.percent:4.1f}%) turns={reading.assistant_turns}",
                    end="", flush=True,
                )
                return reading.tokens >= args.wall
            return False

        result = spawner.wait(window, spec, poll_seconds=2.0, on_poll=on_poll)
        walled = result is None
        if walled:
            print(f"\n[gen {gen}] WALL at >={args.wall:,} tok — interrupting worker")
            result = spawner.interrupt_and_wait(window, spec)
        else:
            print(f"\n[gen {gen}] worker exited on its own (status {result.exit_status})")
        tmux.kill_window(window)

        progress = verify(worktree, expected)
        canary = canary_landed(worktree, spec.session_id)
        print(
            f"[gen {gen}] progress {progress.correct}/{progress.total}"
            f"{'' if progress.first_error is None else ' — ' + progress.first_error}"
            f" | injection landed: {canary} | peak {peak:,} tok"
        )
        report.append(
            {
                "generation": gen,
                "session_id": spec.session_id,
                "walled": walled,
                "peak_tokens": peak,
                "correct": progress.correct,
                "injection_landed": canary,
            }
        )

        if progress.complete:
            success = True
            break
        if progress.first_error and progress.correct == 0 and gen > 1:
            print(f"[gen {gen}] no progress and errors present — stopping early")
            break

        print(f"[gen {gen}] requesting handoff brief from walled session…")
        handoff = request_handoff(worktree, spec.session_id, args.worker_model)
        (fm / f"handoff-gen{gen}.md").write_text(handoff + "\n")
        print(f"[gen {gen}] handoff:\n{textwrap.indent(handoff, '    ')}")

    events_file = fm / "events.jsonl"
    events = (
        [json.loads(l)["event"] for l in events_file.read_text().splitlines() if l.strip()]
        if events_file.exists()
        else []
    )
    generations = len(report)
    final = verify(worktree, expected)

    print("\n" + "=" * 60)
    print("PHASE 0 RELAY SPIKE REPORT")
    print(f"  task complete & correct : {final.complete} ({final.correct}/{final.total})")
    print(f"  generations used        : {generations} (need >=3 for exit criterion)")
    print(f"  walls fired             : {sum(1 for r in report if r['walled'])}")
    print(f"  SessionStart injection  : {[r['injection_landed'] for r in report]}")
    print(f"  hook events logged      : { {e: events.count(e) for e in set(events)} }")
    print(f"  artifacts               : {worktree}")
    verdict = success and generations >= 3
    print(f"  EXIT CRITERION MET      : {verdict}")
    print("=" * 60)
    (fm / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    raise SystemExit(0 if verdict else 1)


if __name__ == "__main__":
    main()
