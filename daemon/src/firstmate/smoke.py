"""End-to-end smoke tests (real Claude workers — cost a few cents).

Scenario `phase1` (default): a throwaway repo + a two-step contract driven
through the whole orchestrator: worker execution → shell-command
validation → `fm ask` park mode (task blocked, worker torn down with a
handoff) → answer → respawn with the answer injected → task-boundary
validation → done.

Scenario `phase2`: acceptance criterion 3 — a worker makes an in-scope
write, then an out-of-scope write that the PreToolUse scope guard blocks
mechanically; the worker raises the prescribed `scope_change` question and
parks; answering "allow" widens the contract's scope; the respawned
generation completes the write and the task validates to done.

Run:  cd daemon && uv run fm-smoke [--scenario phase1|phase2] [--model sonnet]
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

PHASE2_CONTRACT = {
    "goal": "smoke2: scope guard block, park, allow, resume",
    "scope_in": ["src/**"],
    "steps": [
        {
            "id": "edit",
            "title": "One in-scope write, one out-of-scope write",
            "prompt": (
                "First, write a file src/inside.txt whose content is exactly "
                "the line: in-scope\n"
                "Then write a file outside/blocked.txt whose content is "
                "exactly the line: expansion\n"
                "If the scope guard BLOCKS a write, do NOT work around it: "
                "run the exact `fm ask` command the block message prescribes "
                "(keep its --evidence JSON verbatim, put the blocked path and "
                "your reason in the question text), then STOP IMMEDIATELY. "
                "If nothing blocks you — e.g. the operator already allowed "
                "the path — just finish both files."
            ),
            "criteria": ["inside", "outside"],
        }
    ],
    "criteria": [
        {"id": "inside", "command": "grep -qx 'in-scope' src/inside.txt"},
        {"id": "outside", "command": "grep -qx 'expansion' outside/blocked.txt"},
    ],
}


def main() -> None:
    ap = argparse.ArgumentParser(description="First Mate end-to-end smoke tests")
    ap.add_argument("--scenario", choices=["phase1", "phase2"], default="phase1")
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

    scenario = args.scenario
    base_contract = CONTRACT if scenario == "phase1" else PHASE2_CONTRACT
    answer = "red" if scenario == "phase1" else "allow"
    contract = dict(base_contract, repo=str(repo))
    resp = api("POST", "/tasks", {"contract": contract, "run": True}, base=base)
    tid = resp["task"]["id"]
    print(f"[smoke] task {tid} started (scenario {scenario})")

    deadline = time.monotonic() + args.timeout
    answered = False
    answered_q: dict | None = None
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
                print(f"[smoke] question ({q['type']}): {q['question']} "
                      f"{q['options']} — answering {answer!r}")
                api("POST", f"/questions/{q['id']}/answer",
                    {"answer": answer, "by": "smoke"}, base=base)
                answered = True
                answered_q = q
        time.sleep(3)

    worktree = gitops.worktree_path(repo, f"fm/{tid}")

    def read(rel: str) -> str:
        p = worktree / rel
        return p.read_text().strip() if p.exists() else "<missing>"

    evidence = store.task_dir(tid) / "validation.json"
    print("\n" + "=" * 60)
    print(f"{scenario.upper()} SMOKE REPORT")
    print(f"  final task status : {status}")
    print(f"  park/answer cycle : {'exercised' if answered else 'NOT exercised'}")
    if scenario == "phase1":
        hello, color = read("hello.txt"), read("color.txt")
        print(f"  hello.txt         : {hello!r}")
        print(f"  color.txt         : {color!r}")
        ok = (status == "done" and answered and color == "red"
              and "hello from first mate" in hello and evidence.exists())
    else:
        inside, outside = read("src/inside.txt"), read("outside/blocked.txt")
        final_contract = store.load_contract(tid)
        events = [e["event"] for e in store.events_tail(tid, n=500)]
        guard_blocked = "hook.GuardBlock" in events
        widened = any("outside" in g for g in final_contract.scope_in)
        q_type = (answered_q or {}).get("type")
        print(f"  src/inside.txt    : {inside!r}")
        print(f"  outside/blocked.txt: {outside!r}")
        print(f"  guard block event : {guard_blocked}")
        print(f"  question type     : {q_type}")
        print(f"  scope widened to  : {final_contract.scope_in}")
        ok = (status == "done" and answered and guard_blocked
              and q_type == "scope_change" and widened
              and inside == "in-scope" and outside == "expansion"
              and evidence.exists())
    print(f"  evidence          : {evidence} (exists: {evidence.exists()})")
    print(f"  state home        : {home}")
    print(f"  SMOKE PASSED      : {ok}")
    print("=" * 60)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
