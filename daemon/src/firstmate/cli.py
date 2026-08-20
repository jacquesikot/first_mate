"""fm CLI — the daemon API's scripting face (PRD §6.10), plus the
worker-facing `fm ask` and the hook-facing `fm _event`.

Deliberately light on imports at module level: `fm _event` runs inside
every hook invocation and `fm ask` inside worker Bash calls, so the
common path is stdlib-only (urllib); the daemon stack loads only for
`fm serve`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


class CliError(RuntimeError):
    pass


def fm_home() -> Path:
    return Path(os.environ.get("FM_HOME", str(Path.home() / ".firstmate")))


def daemon_url() -> str:
    env = os.environ.get("FM_DAEMON_URL")
    if env:
        return env.rstrip("/")
    dj = fm_home() / "daemon.json"
    if dj.exists():
        try:
            return str(json.loads(dj.read_text())["url"]).rstrip("/")
        except (json.JSONDecodeError, KeyError):
            pass
    return "http://127.0.0.1:8787"


def api(method: str, path: str, body: dict | None = None,
        base: str | None = None, timeout: float = 10.0) -> dict:
    url = (base or daemon_url()).rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:2000]
        raise CliError(f"{e.code} from {method} {path}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise CliError(f"daemon unreachable at {base or daemon_url()}: {e}") from e


# ------------------------------------------------------------- commands


def cmd_serve(args) -> int:
    import socket

    import uvicorn

    from .server import create_app
    from .store import Store

    store = Store()
    config = store.config()
    port = args.port or int(config["port"])
    url = f"http://127.0.0.1:{port}"

    # Claim the port BEFORE announcing anything or rewriting daemon.json.
    # Otherwise a second `fm serve` prints a success line, overwrites the
    # pointer with a pid that dies on the bind error, and leaves the
    # operator believing they restarted the daemon when the old one is
    # still running the old code (observed live 2026-08-20).
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        existing = ""
        pointer = fm_home() / "daemon.json"
        if pointer.exists():
            try:
                existing = f" (daemon.json says pid {json.loads(pointer.read_text()).get('pid')})"
            except (OSError, json.JSONDecodeError):
                pass
        # -sTCP:LISTEN matters: a bare `lsof -i :port` also lists every
        # client connection (a browser tab on the dashboard, say), so it
        # never reads as free and makes "wait for the port" loops hang.
        print(f"fm: port {port} is already in use{existing} — another daemon is "
              f"probably already running.\n"
              f"    Find it with: lsof -nP -iTCP:{port} -sTCP:LISTEN\n"
              f"    Then either stop it, or run `fm serve --port <other>`.",
              file=sys.stderr)
        return 1
    finally:
        probe.close()

    (fm_home() / "daemon.json").write_text(
        json.dumps({"url": url, "pid": os.getpid()}, indent=2) + "\n"
    )
    app = create_app(store, port=port)
    if args.open:
        import webbrowser

        webbrowser.open(url + "/")  # redirects to /ui/ when the SPA is built
    print(f"fm daemon on {url} (state: {store.home})")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


def _head_or_none(repo: Path) -> str | None:
    from .exec import gitops
    try:
        return gitops.head_commit(repo)
    except gitops.GitError:
        return None


def _resolve_base(repo: Path, requested: str | None,
                  fetch: bool = True) -> tuple[str, str | None]:
    """Decide the starting point for a task created from the terminal.

    Mirrors the daemon: default to the remote's default branch (fetched
    first, so "latest" means latest), and never silently inherit whatever
    the operator happens to have checked out."""
    from .exec import gitops

    if fetch and gitops.has_remote(repo):
        err = gitops.fetch(repo)
        if err:
            print(f"fm: fetch failed ({err}) — using local refs", file=sys.stderr)
    base = requested or gitops.default_branch(repo) or ""
    if not base:
        return "", None
    return base, gitops.resolve_ref(repo, base)


def _submit_contract(contract: dict, run: bool, source: Path | None = None,
                     base: str | None = None) -> int:
    body = {"contract": contract, "run": run}
    if base:
        body["base"] = base
    try:
        resp = api("POST", "/tasks", body)
    except CliError as e:
        if source is not None:
            print(f"fm: {e}", file=sys.stderr)
            print(f"contract saved at {source} — create the task later with:")
            print(f"  fm task add {source}{' --run' if run else ''}")
            return 1
        raise
    task = resp["task"]
    print(f"task created: {task['id']} (status {task['status']}"
          f"{', started' if resp.get('started') else ''})")
    if task.get("base"):
        print(f"starting from: {task['base']} ({task.get('base_sha', '')[:10]})")
    if not resp.get("started"):
        print(f"start it with: fm run {task['id']}")
    return 0


def cmd_task(args) -> int:
    if args.target == "add":
        if not args.contract:
            print("usage: fm task add <contract.json> [--run]", file=sys.stderr)
            return 2
        path = Path(args.contract)
        if not path.exists():
            print(f"no such file: {path}", file=sys.stderr)
            return 2
        return _submit_contract(json.loads(path.read_text()), args.run,
                                source=path, base=args.base)

    # `fm task "<goal>"` — interactive scoping conversation (PRD §6.1).
    from . import scoping

    goal = args.target if not args.contract else f"{args.target} {args.contract}"
    repo = scoping.repo_root(Path.cwd())
    home = fm_home()
    base, base_sha = _resolve_base(repo, args.base, fetch=not args.no_fetch)
    if base and base_sha is None:
        print(f"fm: cannot resolve starting point '{base}' in {repo.name}",
              file=sys.stderr)
        return 2
    model = None
    cfg = home / "config.json"
    if cfg.exists():
        try:
            model = json.loads(cfg.read_text()).get("scoping_model")
        except json.JSONDecodeError:
            pass
    print(f"scoping '{goal}' in {repo} — an interactive Claude session is starting;")
    print("push back on its proposal until the contract is right, then approve.")
    # This session reads your checkout, but the task will run from `base`.
    # Say so when those differ, so nothing is scoped against code the worker
    # will never see.
    note = ""
    if base:
        print(f"task will start from: {base} ({(base_sha or '')[:10]})")
        head = _head_or_none(repo)
        if head and base_sha and head != base_sha:
            drift = (f"NOTE: this conversation reads the operator's checkout, which is NOT "
                     f"the task's starting point.\n"
                     f"    Their checkout: {head[:10]}\n"
                     f"    Task starts at: {base} ({base_sha[:10]})\n"
                     f"Scope against what {base} contains; if you need to know how they "
                     f"differ, ask, or inspect with `git diff {base_sha[:10]}..HEAD`.\n")
            note = "\n" + drift
            print(f"warning: your checkout ({head[:10]}) differs from {base} "
                  f"({base_sha[:10]}) — scoping reads your checkout;")
            print("         the worker will run from the starting point. "
                  "Consider --from HEAD, or run this from a clean tree.")
    try:
        result = scoping.run_scoping(goal, repo, home, model=model,
                                     checkout_note=note)
    except FileNotFoundError:
        print("fm: `claude` not found on PATH — install Claude Code first.",
              file=sys.stderr)
        return 1
    if result.contract is None or result.errors:
        for err in result.errors:
            print(f"fm: {err}", file=sys.stderr)
        if result.contract_path.exists():
            print(f"fix the contract at {result.contract_path} and submit with:")
            print(f"  fm task add {result.contract_path}")
        return 1
    return _submit_contract(result.contract, args.run,
                            source=result.contract_path, base=base or None)


def cmd_contract(args) -> int:
    from .scoping import check_contract_file

    errors = check_contract_file(Path(args.path))
    if errors:
        for err in errors:
            print(f"✗ {err}")
        return 1
    data = json.loads(Path(args.path).read_text())
    print(f"✓ contract OK: {len(data.get('steps', []))} step(s), "
          f"{len(data.get('criteria', []))} criterion(s)")
    return 0


def cmd_run(args) -> int:
    resp = api("POST", f"/tasks/{args.task}/run")
    print(f"{args.task}: {'started' if resp.get('started') else 'not started'} "
          f"(status {resp.get('status')})")
    return 0


def cmd_status(_args) -> int:
    try:
        data = api("GET", "/status")
        offline = False
    except CliError:
        # Daemon down — read the state files directly (read-only).
        from .store import Store

        store = Store()
        data = {
            "tasks": [{**row, "generation": None, "running": False}
                      for row in store.list_tasks()],
            "questions": [q.to_dict() for q in store.list_questions(status="open")],
        }
        offline = True
    if offline:
        print("(daemon not running — reading state files)")
    tasks = data.get("tasks", [])
    if not tasks:
        print("no tasks")
    else:
        print(f"{'TASK':<36} {'STATUS':<11} {'STEP':<16} GEN")
        for t in tasks:
            gen = t.get("generation")
            print(f"{t['id']:<36} {t['status']:<11} "
                  f"{(t.get('current_step') or '-'):<16} {gen if gen else '-'}")
    questions = data.get("questions", [])
    if questions:
        print(f"\n{len(questions)} question(s) need input:")
        for q in questions:
            opts = f" [{'/'.join(q['options'])}]" if q.get("options") else ""
            print(f"  {q['id']}  ({q['type']}, task {q['task_id']}): "
                  f"{q['question']}{opts}")
            print(f"    answer with: fm answer {q['id']} <choice>")
    return 0


def cmd_attach(args) -> int:
    resp = api("GET", f"/tasks/{args.task}")
    attach = resp.get("attach")
    if not attach:
        print(f"{args.task}: no live worker session")
        return 1
    print(attach)
    if sys.stdout.isatty() and not args.print_only:
        parts = attach.replace("\\;", ";").split()
        os.execvp(parts[0], parts)
    return 0


def cmd_answer(args) -> int:
    resp = api("POST", f"/questions/{args.question}/answer",
               {"answer": " ".join(args.answer), "by": "cli"})
    q = resp["question"]
    print(f"answered {q['id']}: {q['answer']}"
          f"{' — task resuming' if resp.get('resumed') else ''}")
    return 0


def cmd_lifecycle(args) -> int:
    resp = api("POST", f"/tasks/{args.task}/{args.action}")
    if resp.get("delivered"):
        print(f"{args.task}: {args.action} signal delivered to running task")
    else:
        print(f"{args.task}: status {resp.get('status')}")
    return 0


def cmd_remember(args) -> int:
    from .store import Store

    project = args.project
    if not project:
        import subprocess

        proc = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True)
        top = proc.stdout.strip()
        project = Path(top).name if proc.returncode == 0 and top else Path.cwd().name
    path = Store().remember(project, args.fact)
    print(f"remembered for '{project}': {path}")
    return 0


def cmd_ask(args) -> int:
    task = args.task or os.environ.get("FM_TASK_ID")
    step = args.step or os.environ.get("FM_STEP_ID")
    url = args.url or os.environ.get("FM_DAEMON_URL")
    if not task:
        print("fm ask: no task context (FM_TASK_ID unset)", file=sys.stderr)
        return 1
    body = {
        "task_id": task, "step_id": step, "type": args.type,
        "question": args.question, "options": args.option or [],
        "default": args.default, "urgency": args.urgency,
    }
    if args.evidence:
        try:
            body["evidence"] = json.loads(args.evidence)
        except json.JSONDecodeError:
            body["evidence"] = {"text": args.evidence}
    try:
        resp = api("POST", "/internal/ask", body, base=url)
    except CliError as e:
        # Never strand a worker on a dead daemon.
        print(f"fm ask: daemon unreachable ({e}). Record this question in your "
              "output, proceed on the stated default, and continue.")
        return 0
    print(resp.get("message", resp.get("status", "ok")))
    return 0


def cmd_event(args) -> int:
    payload = None
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"raw": raw[:2000]}
    body = {"event": args.name, "task_id": args.task,
            "step_id": args.step, "payload": payload}
    try:
        api("POST", "/internal/events", body, base=args.url, timeout=3.0)
    except CliError:
        if args.fallback:
            try:
                with open(args.fallback, "a") as f:
                    f.write(json.dumps(body) + "\n")
            except OSError:
                pass
    return 0  # hooks must never fail the worker


def cmd_guard(args) -> int:
    """PreToolUse scope guard (PRD §6.4). Exit 0 allows the tool call;
    exit 2 blocks it and feeds stderr back to the agent in-band."""
    payload: dict = {}
    if not sys.stdin.isatty():
        raw = sys.stdin.read().strip()
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {}

    def notify(data: dict) -> None:
        body = {"event": "GuardBlock", "task_id": args.task,
                "step_id": args.step, "payload": data}
        try:
            api("POST", "/internal/events", body, base=args.url, timeout=3.0)
        except CliError:
            if args.fallback:
                try:
                    with open(args.fallback, "a") as f:
                        f.write(json.dumps(body) + "\n")
                except OSError:
                    pass

    config_path = Path(args.config)
    if not config_path.exists():
        return 0  # guard not configured for this worker
    try:
        from . import guard

        config = json.loads(config_path.read_text())
        decision = guard.evaluate(
            config,
            str(payload.get("tool_name", "") or ""),
            payload.get("tool_input") or {},
        )
    except Exception as e:  # fail closed — a silent bypass is worse than a question
        print(
            "First Mate scope guard: internal error while evaluating this call "
            f"({e!r}); blocking conservatively. If you cannot proceed, raise a "
            "scope_change question with `fm ask` and stop.",
            file=sys.stderr,
        )
        notify({"error": repr(e), "tool_name": payload.get("tool_name")})
        return 2
    if decision.allowed:
        return 0
    print(decision.message, file=sys.stderr)
    notify({"code": decision.code, "path": decision.path,
            "tripwire": decision.tripwire,
            "tool_name": payload.get("tool_name")})
    return 2


# --------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fm", description="First Mate")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("serve", help="start daemon + API")
    sp.add_argument("--port", type=int, default=None)
    sp.add_argument("--open", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser(
        "task",
        help='fm task "<goal>" starts a scoping conversation; '
             "fm task add <contract.json> submits a hand-written contract",
    )
    sp.add_argument("target", help="a goal string, or 'add'")
    sp.add_argument("contract", nargs="?", help="path to contract JSON (with 'add')")
    sp.add_argument("--run", action="store_true", help="start immediately")
    sp.add_argument(
        "--from", dest="base", default=None, metavar="REF",
        help="starting point for the task's worktree — a branch, tag, or "
             "commit (default: the remote's default branch, freshly fetched, "
             "so a task never inherits your working tree by accident)")
    sp.add_argument(
        "--no-fetch", action="store_true",
        help="skip the git fetch before resolving --from")
    sp.set_defaults(func=cmd_task)

    sp = sub.add_parser("contract", help="contract utilities")
    sp.add_argument("action", choices=["check"])
    sp.add_argument("path", help="path to contract JSON")
    sp.set_defaults(func=cmd_contract)

    sp = sub.add_parser("run", help="start/resume an approved task")
    sp.add_argument("task")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status", help="tasks, steps, questions at a glance")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("attach", help="jump into the task's live tmux window")
    sp.add_argument("task")
    sp.add_argument("--print-only", action="store_true")
    sp.set_defaults(func=cmd_attach)

    sp = sub.add_parser("answer", help="answer a question")
    sp.add_argument("question")
    sp.add_argument("answer", nargs="+")
    sp.set_defaults(func=cmd_answer)

    for action in ("pause", "abandon"):
        sp = sub.add_parser(action, help=f"{action} a task")
        sp.add_argument("task")
        sp.set_defaults(func=cmd_lifecycle, action=action)

    sp = sub.add_parser("remember", help="append a fact to project memory")
    sp.add_argument("fact")
    sp.add_argument("-p", "--project", default=None)
    sp.set_defaults(func=cmd_remember)

    sp = sub.add_parser("ask", help="(worker-facing) raise a question")
    sp.add_argument("--type", required=True,
                    choices=["clarification", "scope_change", "decision",
                             "approval", "fyi"])
    sp.add_argument("--question", required=True)
    sp.add_argument("--option", action="append")
    sp.add_argument("--default", default=None)
    sp.add_argument("--urgency", default="normal", choices=["blocking", "normal"])
    sp.add_argument("--evidence", default=None, help="JSON or free text")
    sp.add_argument("--task", default=None)
    sp.add_argument("--step", default=None)
    sp.add_argument("--url", default=None)
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("_event", help="(hook-facing) forward a hook event")
    sp.add_argument("name")
    sp.add_argument("--task", required=True)
    sp.add_argument("--step", default=None)
    sp.add_argument("--url", default=None)
    sp.add_argument("--fallback", default=None)
    sp.set_defaults(func=cmd_event)

    sp = sub.add_parser("_guard", help="(hook-facing) PreToolUse scope guard")
    sp.add_argument("--config", required=True, help="path to guard.json")
    sp.add_argument("--task", required=True)
    sp.add_argument("--step", default=None)
    sp.add_argument("--url", default=None)
    sp.add_argument("--fallback", default=None)
    sp.set_defaults(func=cmd_guard)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        print(f"fm: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
