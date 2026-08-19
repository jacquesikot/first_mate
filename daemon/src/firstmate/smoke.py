"""Phase 1 end-to-end smoke test (real Claude workers — costs a few cents).

Creates a throwaway repo + a two-step contract, boots the daemon
in-process on a side port, and drives one task through the whole
orchestrator: worker execution → shell-command validation → `fm ask`
park mode (task blocked, worker torn down with a handoff) → answer →
respawn with the answer injected → task-boundary validation → done.

Run:  cd daemon && uv run fm-smoke [--model sonnet] [--port 8791]
Artifacts land under ~/.firstmate/smoke/run-<ts>/ (state home + repo).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import threading
import time
from pathlib import Path

from .cli import api, CliError
from .exec import gitops
from .store import Store

CONTRACT = {
    "goal": "smoke: greeting and color files",
    "steps": [
        {
            "id": "greet",
            "title": "Write the greeting file",
            "prompt": (
                "Create a file named hello.txt in the repository root whose "
                "content is exactly the line: hello from first mate"
            ),
            "criteria": ["hello"],
        },
        {
            "id": "color",
            "title": "Ask the operator, then write the color file",
            "prompt": (
                "Look at the '## Operator answers' section of your injected "
                "context. If it contains an answer about which color to use, "
                "write ONLY that color word into a file named color.txt and "
                "you are done. If there is NO such answer yet, you MUST run "
                "exactly this command and then STOP IMMEDIATELY without "
                "creating color.txt:\n"
                '  fm ask --type decision --question "Which color should '
                'color.txt contain?" --option red --option blue --default red'
            ),
            "criteria": ["color"],
        },
    ],
    "criteria": [
        {"id": "hello", "command": "grep -q 'hello from first mate' hello.txt"},
        {"id": "color", "command": "grep -Eq '^(red|blue)$' color.txt"},
    ],
}


def main() -> None:
    ap = argparse.ArgumentParser(description="First Mate Phase 1 smoke test")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--timeout", type=int, default=1200, help="overall seconds")
    args = ap.parse_args()

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path.home() / ".firstmate" / "smoke" / f"run-{stamp}"
    home, repo = root / "home", root / "repo"
    home.mkdir(parents=True)
    print(f"[smoke] root: {root}")

    gitops.init_repo(repo)
    (home / "config.json").write_text(json.dumps({
        "port": args.port, "max_workers": 2, "worker_model": args.model,
        "wall_tokens": 150_000, "max_generations": 4,
        "worker_timeout_s": 600, "poll_seconds": 2.0,
    }, indent=2) + "\n")

    store = Store(home)
    base = f"http://127.0.0.1:{args.port}"

    import uvicorn

    from .server import create_app

    app = create_app(store, port=args.port)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
                                           port=args.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            api("GET", "/health", base=base, timeout=2)
            break
        except CliError:
            time.sleep(0.2)
    else:
        raise SystemExit("daemon did not come up")
    print(f"[smoke] daemon up on {base}")

    contract = dict(CONTRACT, repo=str(repo))
    resp = api("POST", "/tasks", {"contract": contract, "run": True}, base=base)
    tid = resp["task"]["id"]
    print(f"[smoke] task {tid} started")

    deadline = time.monotonic() + args.timeout
    answered = False
    last_line = ""
    status = "?"
    while time.monotonic() < deadline:
        data = api("GET", "/status", base=base)
        task = next(t for t in data["tasks"] if t["id"] == tid)
        status = task["status"]
        line = f"status={status} step={task.get('current_step')} gen={task.get('generation')}"
        if line != last_line:
            print(f"[smoke] {line}")
            last_line = line
        if status in ("done", "failed", "abandoned"):
            break
        if status == "blocked" and not answered:
            open_qs = [q for q in data["questions"] if q["task_id"] == tid]
            if open_qs:
                q = open_qs[0]
                print(f"[smoke] question: {q['question']} {q['options']} — answering 'red'")
                api("POST", f"/questions/{q['id']}/answer",
                    {"answer": "red", "by": "smoke"}, base=base)
                answered = True
        time.sleep(3)

    worktree = gitops.worktree_path(repo, f"fm/{tid}")
    hello = (worktree / "hello.txt").read_text().strip() if (worktree / "hello.txt").exists() else "<missing>"
    color = (worktree / "color.txt").read_text().strip() if (worktree / "color.txt").exists() else "<missing>"
    evidence = store.task_dir(tid) / "validation.json"

    print("\n" + "=" * 60)
    print("PHASE 1 SMOKE REPORT")
    print(f"  final task status : {status}")
    print(f"  park/answer cycle : {'exercised' if answered else 'NOT exercised'}")
    print(f"  hello.txt         : {hello!r}")
    print(f"  color.txt         : {color!r}")
    print(f"  evidence          : {evidence} (exists: {evidence.exists()})")
    print(f"  state home        : {home}")
    ok = (status == "done" and answered and color == "red"
          and "hello from first mate" in hello and evidence.exists())
    print(f"  SMOKE PASSED      : {ok}")
    print("=" * 60)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
