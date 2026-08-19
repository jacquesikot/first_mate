"""fm daemon — REST + WebSocket API over the store and orchestrator.

Single local process (PRD §7); the browser, Slack connector, and CLI all
talk to this API so state is always consistent. Binds 127.0.0.1 only
(dashboard auth posture: localhost-only for v1).
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import scoping_api
from .exec import context as contexttrack
from .exec import gitops, tmux
from .models import (
    Contract, Question, QUESTION_TYPES, TERMINAL_TASK_STATUSES, Task,
    StepState, new_id, now_iso, validate_contract,
)
from .orchestrator import TaskRunner
from .store import Store

LIVE_POLL_SECONDS = 1.0  # pane-capture/context push cadence (PRD: <1s felt)
PANE_TAIL_LINES = 160


def dashboard_dist() -> Path | None:
    """Locate the built SPA. Override with FM_DASHBOARD_DIST; default is
    <repo>/dashboard/dist relative to this source tree."""
    env = os.environ.get("FM_DASHBOARD_DIST")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    p = Path(__file__).resolve().parents[3] / "dashboard" / "dist"
    return p if p.is_dir() else None


def live_session(task: Task) -> tuple[str | None, dict | None]:
    """(step_id, session record dict) of the task's live worker, if any."""
    for st in task.steps:
        for rec in st.sessions:
            if rec.ended_at is None and rec.window_id:
                return st.id, rec.to_dict()
    return None, None


def context_reading(task: Task, config: dict) -> dict | None:
    """Live-session context meter (PRD §6.3), or None when no session."""
    step_id, rec = live_session(task)
    if rec is None or not task.worktree:
        return None
    reading = contexttrack.read_context(Path(task.worktree), rec["session_id"])
    if reading is None:
        return None
    return {
        "step_id": step_id,
        "session_id": reading.session_id,
        "tokens": reading.tokens,
        "limit": reading.limit,
        "wall_tokens": int(config.get("wall_tokens") or 0),
        "percent": round(reading.percent, 1),
        "band": reading.band,
    }


class Manager:
    """Holds the running orchestrator loops, the global worker-slot cap,
    and the WebSocket fan-out."""

    def __init__(self, store: Store, config: dict, autostart: bool = True):
        self.store = store
        self.config = config
        self.autostart = autostart  # False in unit tests: never spawn workers
        self.daemon_url = f"http://127.0.0.1:{config['port']}"
        self.slots = asyncio.Semaphore(int(config["max_workers"]))
        self.runners: dict[str, TaskRunner] = {}
        self._loops: dict[str, asyncio.Task] = {}
        self.sockets: set[WebSocket] = set()
        self._live_task: asyncio.Task | None = None
        self._live_sent: dict[str, tuple] = {}  # task_id -> (output, tokens)

    async def broadcast(self, evt: dict) -> None:
        dead = []
        for ws in self.sockets:
            try:
                await ws.send_json(evt)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)

    def running(self, task_id: str) -> bool:
        loop = self._loops.get(task_id)
        return loop is not None and not loop.done()

    def start(self, task_id: str) -> bool:
        """Start (or no-op) the task's orchestrator loop. Idempotent."""
        if not self.autostart or self.running(task_id):
            return False
        runner = TaskRunner(
            self.store, self.config, task_id,
            slots=self.slots, daemon_url=self.daemon_url,
            broadcast=self.broadcast,
        )
        self.runners[task_id] = runner
        loop = asyncio.create_task(runner.run(), name=f"runner-{task_id}")

        def _cleanup(_t: asyncio.Task) -> None:
            self._loops.pop(task_id, None)
            self.runners.pop(task_id, None)

        loop.add_done_callback(_cleanup)
        self._loops[task_id] = loop
        return True

    def deliver(self, task_id: str, item: dict) -> bool:
        if self.running(task_id):
            self.runners[task_id].deliver(item)
            return True
        return False

    # ------------------------------------------------- live output streaming

    def capture_live(self, task: Task) -> dict | None:
        """One read-only pane capture + context reading for the task's live
        session (observability only — never used to infer state)."""
        step_id, rec = live_session(task)
        if rec is None:
            return None
        try:
            text = tmux.capture(tmux.Window(rec["window_id"], ""),
                                lines=PANE_TAIL_LINES)
        except tmux.TmuxError:
            return None
        return {
            "kind": "live",
            "task_id": task.id,
            "step_id": step_id,
            "session_id": rec["session_id"],
            "generation": rec["generation"],
            "output": text.rstrip("\n"),
            "context": context_reading(task, self.config),
        }

    async def _live_loop(self) -> None:
        """Push pane captures + context meters over the WebSocket while
        anyone is watching. tmux stays behind exec/tmux.py; this loop only
        fans results out."""
        while True:
            try:
                await asyncio.sleep(LIVE_POLL_SECONDS)
                if not self.sockets:
                    continue
                for row in self.store.list_tasks():
                    if row["status"] not in ("running", "validating"):
                        self._live_sent.pop(row["id"], None)
                        continue
                    task = self.store.load_task(row["id"])
                    if task is None:
                        continue
                    payload = await asyncio.to_thread(self.capture_live, task)
                    if payload is None:
                        continue
                    key = (payload["output"],
                           (payload["context"] or {}).get("tokens"))
                    if self._live_sent.get(task.id) == key:
                        continue
                    self._live_sent[task.id] = key
                    await self.broadcast(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue  # observability must never take the daemon down

    def start_live_loop(self) -> None:
        if self._live_task is None or self._live_task.done():
            self._live_task = asyncio.create_task(self._live_loop(),
                                                  name="live-stream")

    async def reconcile(self) -> None:
        """Boot recovery (acceptance criterion 8): mark sessions the dead
        daemon left in flight as orphaned, kill their tmux windows, and
        resume the tasks from state files."""
        for row in self.store.list_tasks():
            task = self.store.load_task(row["id"])
            if task is None:
                continue
            if task.status == "scoping":
                # A scoping turn is a subprocess of the dead daemon; nothing
                # will ever complete it. Hand the conversation back to the
                # operator — the transcript is intact, so the next message
                # resumes it.
                chat = scoping_api.find_chat_for_task(self.store.home, task.id)
                if chat is not None and chat.status == "thinking":
                    chat.messages.append({
                        "role": "system",
                        "text": "the daemon restarted mid-turn — send another "
                                "message to pick the conversation back up",
                        "at": now_iso()})
                    chat.status = "awaiting_operator"
                    chat.save()
                continue
            if task.status not in ("running", "validating"):
                continue
            for st in task.steps:
                for rec in st.sessions:
                    if rec.ended_at is None:
                        rec.ended_at = now_iso()
                        rec.outcome = "orphaned"
                        if rec.window_id:
                            tmux.kill_window(tmux.Window(rec.window_id, ""))
            self.store.save_task(task)
            self.store.append_event(task.id, "reconciled")
            self.start(task.id)

    async def shutdown(self) -> None:
        if self._live_task is not None:
            self._live_task.cancel()
        for loop in list(self._loops.values()):
            loop.cancel()


def create_app(store: Store | None = None, autostart: bool = True,
               port: int | None = None) -> FastAPI:
    store = store or Store()
    config = store.config()
    if port is not None:
        config["port"] = port
    manager = Manager(store, config, autostart=autostart)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if autostart:
            await manager.reconcile()
            manager.start_live_loop()
        yield
        await manager.shutdown()

    app = FastAPI(title="First Mate daemon", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.manager = manager

    def get_task_or_404(task_id: str):
        task = store.load_task(task_id)
        if task is None:
            raise HTTPException(404, f"unknown task: {task_id}")
        return task

    def discard_unused_worktree(task) -> None:
        """Remove a task's worktree + branch if nothing ever happened in it.
        Blocking (git); call from a thread. Cleanup must never fail an
        abandon, and anything that looks like work is left alone."""
        if not task.worktree or not Path(task.worktree).is_dir():
            return
        wt = Path(task.worktree)
        try:
            if gitops.changed_files(wt):
                return  # uncommitted work here — keep it for the operator
            if task.base_sha and gitops.head_commit(wt) != task.base_sha:
                return  # commits were made on the task branch — keep them
            gitops.remove_worktree(Path(task.repo), task.branch)
            gitops.delete_branch(Path(task.repo), task.branch)
            task.worktree = ""
        except gitops.GitError:
            return

    def live_attach(task) -> str | None:
        for st in task.steps:
            for rec in st.sessions:
                if rec.ended_at is None and rec.window_id:
                    return tmux.attach_command(tmux.Window(rec.window_id, ""))
        return None

    # ------------------------------------------------------------- basics

    @app.get("/health")
    async def health():
        return {"ok": True, "version": app.version}

    @app.get("/config")
    async def get_config():
        return manager.config

    @app.get("/status")
    async def status():
        tasks = []
        for row in store.list_tasks():
            task = store.load_task(row["id"])
            if task is None:
                continue
            gen = None
            if task.current_step:
                try:
                    gen = task.step_state(task.current_step).generation
                except KeyError:
                    pass
            tasks.append({**row, "generation": gen,
                          "running": manager.running(task.id),
                          "context": context_reading(task, manager.config)})
        questions = [q.to_dict() for q in store.list_questions(status="open")]
        return {"tasks": tasks, "questions": questions,
                "config": {"max_workers": manager.config["max_workers"],
                           "wall_tokens": manager.config["wall_tokens"]}}

    # -------------------------------------------------------------- tasks

    @app.get("/tasks")
    async def list_tasks():
        return {"tasks": store.list_tasks()}

    async def create_task_from_contract(data: dict | None, run: bool,
                                        base: str = ""):
        """Shared gate for POST /tasks and scoping approval."""
        errors = validate_contract(data or {})
        repo = str((data or {}).get("repo", ""))
        if repo and not Path(repo).expanduser().is_dir():
            errors.append(f"repo path does not exist: {repo}")
        if errors:
            raise HTTPException(400, {"errors": errors})
        contract = Contract.from_dict(data)
        contract.repo = str(Path(contract.repo).expanduser().resolve())
        rp = Path(contract.repo)
        # Same starting-point rule as the scoping flow: default to the remote
        # default branch rather than whatever the operator has checked out.
        explicit = bool(base)
        if not base:
            # Fetch before defaulting, or "the remote default branch" would
            # mean whatever the last fetch happened to leave behind. Only on
            # the implicit path — an explicit ref is taken at face value, so
            # a caller that wants no network can name one.
            if await asyncio.to_thread(gitops.has_remote, rp):
                await asyncio.to_thread(gitops.fetch, rp)
            base = await asyncio.to_thread(gitops.default_branch, rp) or "HEAD"
        base_sha = await asyncio.to_thread(gitops.resolve_ref, rp, base)
        if base_sha is None:
            # An explicit choice that doesn't resolve is an error; an
            # unresolvable *default* just means there is nothing to resolve
            # yet (a repo with no commits), so let the task be created and
            # let git report it at run time.
            if explicit:
                raise HTTPException(
                    400, {"errors": [f"cannot resolve starting point '{base}'"]})
            base = ""
        task = store.create_task(contract)
        task.base = base
        task.base_sha = base_sha or ""
        store.save_task(task)
        started = manager.start(task.id) if run else False
        evt = store.append_event(task.id, "task_ready",
                                 data={"started": started, "base": base,
                                       "base_sha": base_sha})
        await manager.broadcast(evt)
        return task, started

    @app.post("/tasks")
    async def create_task(request: Request):
        body = await request.json()
        task, started = await create_task_from_contract(
            body.get("contract"), bool(body.get("run")),
            base=str(body.get("base", "")).strip())
        return {"task": task.to_dict(), "started": started}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        task = get_task_or_404(task_id)
        contract = store.load_contract(task_id)
        handoffs = {}
        artifacts = {}
        for st in task.steps:
            latest = store.latest_handoff(task_id, st.id)
            if latest:
                handoffs[st.id] = {"generation": latest[0], "text": latest[1]}
            vdir = store.step_dir(task_id, st.id)
            if vdir.exists():
                artifacts[st.id] = sorted(p.name for p in vdir.iterdir())
        validations = {}
        for st in task.steps:
            d = store.step_dir(task_id, st.id)
            best = None
            if d.exists():
                for p in sorted(d.glob("validation-attempt*.json")):
                    best = p
            if best is not None:
                try:
                    validations[st.id] = json.loads(best.read_text())
                except json.JSONDecodeError:
                    pass
        tval = store.task_dir(task_id) / "validation.json"
        if tval.exists():
            try:
                validations["__task__"] = json.loads(tval.read_text())
            except json.JSONDecodeError:
                pass
        scoping_chat = None
        if task.status == "scoping":
            chat = (chats.get(task.scoping_chat_id or "")
                    or scoping_api.find_chat_for_task(store.home, task_id))
            if chat is not None:
                scoping_api.refresh_contract(chat)
                chats[chat.id] = chat
                scoping_chat = chat.to_dict()
        return {
            "task": task.to_dict(),
            "scoping": scoping_chat,
            "contract": contract.to_dict() if contract else None,
            "contract_md": (store.task_dir(task_id) / "contract.md").read_text()
            if (store.task_dir(task_id) / "contract.md").exists() else None,
            "questions": [q.to_dict() for q in store.list_questions(task_id=task_id)],
            "events": store.events_tail(task_id, n=200),
            "attach": live_attach(task),
            "running": manager.running(task_id),
            "context": context_reading(task, manager.config),
            "handoffs": handoffs,
            "validations": validations,
        }

    @app.post("/tasks/{task_id}/run")
    async def run_task(task_id: str):
        task = get_task_or_404(task_id)
        if task.status in TERMINAL_TASK_STATUSES:
            raise HTTPException(409, f"task is {task.status}")
        if task.status == "scoping":
            raise HTTPException(409, "task is still being scoped — approve its "
                                     "contract first")
        if task.status == "paused":
            task.status = "ready"
            store.save_task(task)
        started = manager.start(task_id)
        return {"started": started, "status": task.status}

    @app.post("/tasks/{task_id}/pause")
    async def pause_task(task_id: str):
        task = get_task_or_404(task_id)
        if manager.deliver(task_id, {"kind": "control", "action": "paused"}):
            return {"delivered": True}
        if task.status in ("ready", "running", "blocked"):
            task.status = "paused"
            store.save_task(task)
            evt = store.append_event(task_id, "task_status", data={"status": "paused"})
            await manager.broadcast(evt)
        return {"delivered": False, "status": task.status}

    @app.post("/tasks/{task_id}/abandon")
    async def abandon_task(task_id: str):
        task = get_task_or_404(task_id)
        if manager.deliver(task_id, {"kind": "control", "action": "abandoned"}):
            return {"delivered": True}
        if task.status == "scoping":
            # Close the conversation too — an orphaned chat would keep
            # resuming a session for a task nobody is waiting on.
            chat = (chats.get(task.scoping_chat_id or "")
                    or scoping_api.find_chat_for_task(store.home, task_id))
            if chat is not None and chat.status not in ("approved", "abandoned"):
                chat.status = "abandoned"
                chat.save()
                chats[chat.id] = chat
            task.scoping_chat_id = None
            # The worktree was created when scoping started; nothing ever ran
            # in it, so drop it rather than leaving a directory and a branch
            # behind for every abandoned conversation. Kept if it holds
            # anything at all.
            await asyncio.to_thread(discard_unused_worktree, task)
        task.status = "abandoned"
        store.save_task(task)
        evt = store.append_event(task_id, "task_status", data={"status": "abandoned"})
        await manager.broadcast(evt)
        return {"delivered": False, "status": task.status}

    @app.get("/tasks/{task_id}/diff")
    async def task_diff(task_id: str):
        task = get_task_or_404(task_id)
        if not task.worktree or not Path(task.worktree).is_dir():
            return {"files": [], "added": 0, "deleted": 0, "worktree": task.worktree}
        wt = Path(task.worktree)
        try:
            files = await asyncio.to_thread(gitops.numstat_files, wt)
            added, deleted = await asyncio.to_thread(gitops.diff_numstat, wt)
        except gitops.GitError as e:
            raise HTTPException(500, f"git error: {e}")
        return {"files": files, "added": added, "deleted": deleted,
                "worktree": task.worktree, "branch": task.branch}

    @app.get("/tasks/{task_id}/diff/file")
    async def task_diff_file(task_id: str, path: str):
        task = get_task_or_404(task_id)
        if not task.worktree or not Path(task.worktree).is_dir():
            raise HTTPException(404, "task has no worktree")
        try:
            text = await asyncio.to_thread(
                gitops.diff_file, Path(task.worktree), path)
        except gitops.GitError as e:
            raise HTTPException(500, f"git error: {e}")
        return {"path": path, "diff": text}

    @app.get("/tasks/{task_id}/output")
    async def task_output(task_id: str):
        task = get_task_or_404(task_id)
        payload = await asyncio.to_thread(manager.capture_live, task)
        if payload is None:
            return {"live": False, "output": None, "context": None}
        return {"live": True, **{k: v for k, v in payload.items() if k != "kind"}}

    @app.put("/tasks/{task_id}/contract")
    async def edit_contract(task_id: str, request: Request):
        """Contract edits between steps (PRD §6.1/§6.8): rejected while the
        task's orchestrator loop is live — answers are the only mid-run
        amendment path."""
        task = get_task_or_404(task_id)
        if manager.running(task_id):
            raise HTTPException(409, "task is running — pause it or wait for a "
                                     "resting state; mid-run amendments happen "
                                     "through answered questions")
        if task.status in TERMINAL_TASK_STATUSES:
            raise HTTPException(409, f"task is {task.status}")
        body = await request.json()
        data = body.get("contract")
        errors = validate_contract(data or {})
        if errors:
            raise HTTPException(400, {"errors": errors})
        contract = Contract.from_dict(data)
        contract.repo = str(Path(contract.repo).expanduser().resolve())
        store.save_contract(task_id, contract)
        # Keep runtime step state in sync: preserve state for surviving
        # step ids, add pending state for new ones, in contract order.
        existing = {st.id: st for st in task.steps}
        task.steps = [existing.get(s.id) or StepState(id=s.id)
                      for s in contract.steps]
        task.goal = contract.goal
        store.save_task(task)
        evt = store.append_event(task_id, "contract_edited",
                                 data={"by": str(body.get("by", "dashboard"))})
        await manager.broadcast(evt)
        return {"contract": contract.to_dict()}

    # -------------------------------------------- repo picker (localhost)

    SCAN_ROOTS = ["~/code", "~/Documents/code", "~/projects", "~/dev", "~/src"]

    @app.get("/fs/repos")
    async def fs_repos():
        """Repo suggestions for the New-task picker: repos of existing
        tasks first, then a shallow scan of common code directories."""
        out: list[dict] = []
        seen: set[tuple[int, int]] = set()  # (dev, ino) — case-insensitive fs

        def add(path: Path, source: str) -> None:
            try:
                st = path.stat()
            except OSError:
                return
            key = (st.st_dev, st.st_ino)
            if key in seen or not path.is_dir():
                return
            seen.add(key)
            out.append({"path": str(path), "name": path.name, "source": source})

        for row in store.list_tasks():
            add(Path(row["repo"]), "recent")
        for root in SCAN_ROOTS:
            rp = Path(root).expanduser()
            if not rp.is_dir():
                continue
            try:
                children = sorted(rp.iterdir())
            except OSError:
                continue
            for child in children:
                if len(out) >= 60:
                    break
                if child.is_dir() and (child / ".git").exists():
                    add(child, "scan")
        return {"repos": out}

    @app.get("/fs/refs")
    async def fs_refs(repo: str, fetch: bool = True):
        """Starting points for a new task, ranked, with freshness.

        The picker's whole job is to let the operator start from a known
        point instead of whatever their working tree happens to hold, so
        this fetches first (remote-tracking refs only — never the working
        tree) and reports what it found. A fetch failure is data, not an
        error: the refs are still returned, labelled stale."""
        rp = Path(repo).expanduser()
        if not rp.is_dir() or not (rp / ".git").exists():
            raise HTTPException(400, f"not a git repository: {repo}")
        rp = rp.resolve()
        fetch_error = None
        fetched = False
        if fetch and await asyncio.to_thread(gitops.has_remote, rp):
            fetch_error = await asyncio.to_thread(gitops.fetch, rp)
            fetched = fetch_error is None
        default = await asyncio.to_thread(gitops.default_branch, rp)
        current = await asyncio.to_thread(gitops.current_branch, rp)
        dirty = await asyncio.to_thread(gitops.is_dirty, rp)
        refs = await asyncio.to_thread(gitops.list_refs, rp)
        # First Mate's own task branches are outputs, not starting points —
        # they would otherwise pile up in the picker, one per task ever run.
        # (Still reachable through the "other ref" field if genuinely wanted.)
        refs = [r for r in refs
                if not r["name"].startswith("fm/")
                and "/fm/" not in r["name"]]
        by_name = {r["name"]: r for r in refs}

        # Rank: the remote default first (the "pull latest" case), then the
        # branch they have checked out, then everything else by recency.
        def rank(r: dict) -> tuple:
            if r["name"] == default:
                return (0,)
            if r["name"] == current:
                return (1,)
            return (2, r["remote"])

        ordered = sorted(refs, key=rank)
        for r in ordered:
            r["role"] = ("default" if r["name"] == default
                         else "current" if r["name"] == current else None)
        return {
            "repo": str(rp),
            "fetched": fetched,
            "fetch_error": fetch_error,
            "default_branch": default,
            "current_branch": current,
            "dirty": dirty,
            # What we'd pick with no input — the answer to "I usually pull
            # latest from origin/main".
            "recommended": default or current or "HEAD",
            "refs": ordered,
            "current_ref": by_name.get(current or ""),
        }

    @app.get("/fs/browse")
    async def fs_browse(path: str | None = None):
        """Directory listing for the picker's browse modal. The daemon is
        localhost-only; this is the operator browsing their own machine."""
        p = Path(path).expanduser() if path else Path.home()
        try:
            p = p.resolve()
        except OSError:
            raise HTTPException(400, f"cannot resolve path: {path}")
        if not p.is_dir():
            raise HTTPException(400, f"not a directory: {p}")
        dirs = []
        try:
            children = sorted(p.iterdir(), key=lambda c: c.name.lower())
        except PermissionError:
            raise HTTPException(403, f"permission denied: {p}")
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if not child.is_dir():
                    continue
                dirs.append({"name": child.name, "path": str(child),
                             "is_repo": (child / ".git").exists()})
            except OSError:
                continue
            if len(dirs) >= 300:
                break
        return {"path": str(p),
                "parent": str(p.parent) if p != p.parent else None,
                "is_repo": (p / ".git").exists(),
                "dirs": dirs}

    # ------------------------------------------- scoping in the browser

    chats: dict[str, scoping_api.ScopingChat] = {}
    chat_locks: dict[str, asyncio.Lock] = {}
    background_turns: set[asyncio.Task] = set()  # strong refs; GC would cancel

    def get_chat_or_404(chat_id: str) -> scoping_api.ScopingChat:
        chat = chats.get(chat_id) or scoping_api.load_chat(store.home, chat_id)
        if chat is None:
            raise HTTPException(404, f"unknown scoping chat: {chat_id}")
        chats[chat_id] = chat
        return chat

    async def run_chat_turn(chat: scoping_api.ScopingChat, text: str):
        lock = chat_locks.setdefault(chat.id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, "a turn is already running for this chat")
        async with lock:
            chat = await asyncio.to_thread(scoping_api.advance, chat, text)
        chats[chat.id] = chat
        await manager.broadcast({"kind": "scoping", "id": chat.id,
                                 "status": chat.status,
                                 "task_id": chat.task_id})
        if chat.task_id:
            # The chat lives inside a task session; nudge task subscribers.
            evt = {"ts": chat.messages[-1]["at"] if chat.messages else "",
                   "event": "scoping_turn", "task_id": chat.task_id,
                   "step_id": None, "data": {"status": chat.status}}
            await manager.broadcast(evt)
        return chat

    def spawn_chat_turn(chat: scoping_api.ScopingChat, text: str) -> None:
        """Fire a turn off the request path. Scoping turns take minutes
        (the assistant reads the repo); the browser follows along on the
        task page over the websocket instead of holding a request open."""
        async def go():
            try:
                await run_chat_turn(chat, text)
            except HTTPException:
                # A turn was already running (the endpoint's pre-check lost a
                # race). The operator's message is already recorded and the
                # chat is `thinking`, so leaving it here would hang forever —
                # hand it back instead.
                current = chats.get(chat.id, chat)
                if current.status == "thinking":
                    current.messages.append({
                        "role": "system",
                        "text": "another turn was already running — send that "
                                "again once it finishes",
                        "at": now_iso()})
                    current.status = "awaiting_operator"
                    current.save()
                    chats[current.id] = current
                    await manager.broadcast({"kind": "scoping", "id": current.id,
                                             "status": current.status,
                                             "task_id": current.task_id})
            except Exception as e:  # never lose the chat to an unexpected error
                current = chats.get(chat.id, chat)
                current.messages.append({
                    "role": "system", "text": f"turn failed: {e}",
                    "at": now_iso()})
                current.status = "failed"
                current.save()
                chats[current.id] = current

        task = asyncio.create_task(go())
        background_turns.add(task)
        task.add_done_callback(background_turns.discard)

    @app.post("/scoping")
    async def start_scoping(request: Request):
        body = await request.json()
        goal = str(body.get("goal", "")).strip()
        repo = str(body.get("repo", "")).strip()
        base = str(body.get("base", "")).strip()
        if not goal:
            raise HTTPException(400, "goal is required")
        rp = Path(repo).expanduser()
        if not repo or not rp.is_dir():
            raise HTTPException(400, f"repo path does not exist: {repo}")
        if not (rp / ".git").exists():
            raise HTTPException(400, f"not a git repository: {rp}")
        rp = rp.resolve()
        # Every task declares its starting point up front (the operator's own
        # checkout may be mid-work); default to the remote default branch,
        # which is the "pull latest from origin/main" case.
        explicit = bool(base)
        if not base:
            # The browser passes an explicit base from the picker (which has
            # already fetched); a caller that omits it still gets a truthful
            # "latest".
            if await asyncio.to_thread(gitops.has_remote, rp):
                await asyncio.to_thread(gitops.fetch, rp)
            base = await asyncio.to_thread(gitops.default_branch, rp) or "HEAD"
        base_sha = await asyncio.to_thread(gitops.resolve_ref, rp, base)
        if base_sha is None:
            if explicit:
                raise HTTPException(
                    400, f"cannot resolve starting point '{base}' in {rp.name}")
            base = ""  # repo with no commits — git decides at worktree time

        # Task-first: the session exists before the conversation does, so
        # scoping happens in a task view and shows up in the queue at once.
        placeholder = Contract(goal=goal, repo=str(rp))
        task = store.create_task(placeholder, status="scoping")
        # The worktree exists from now on, so the scoping conversation reads
        # the chosen starting point rather than the operator's working tree.
        try:
            worktree = await asyncio.to_thread(
                gitops.create_worktree, rp, task.branch, base_sha or "HEAD")
        except gitops.GitError as e:
            task.status = "failed"
            store.save_task(task)
            raise HTTPException(500, f"could not create worktree: {e}")
        task.worktree = str(worktree)
        task.base = base
        task.base_sha = base_sha or ""
        chat = scoping_api.start_chat(
            store.home, goal, rp,
            model=manager.config.get("scoping_model"),
            task_id=task.id, workdir=worktree, base=base, base_sha=base_sha,
        )
        chat.save()
        chats[chat.id] = chat
        task.scoping_chat_id = chat.id
        store.save_task(task)
        evt = store.append_event(
            task.id, "scoping_started",
            data={"chat_id": chat.id, "base": base, "base_sha": base_sha,
                  "worktree": str(worktree)})
        await manager.broadcast(evt)
        # The opening turn (assistant reads repo + memory, then proposes —
        # PRD §6.1, no questionnaire) runs in the background.
        spawn_chat_turn(chat, "")
        return {"chat": chat.to_dict(), "task": task.to_dict()}

    @app.get("/scoping/{chat_id}")
    async def get_scoping(chat_id: str):
        return {"chat": get_chat_or_404(chat_id).to_dict()}

    @app.post("/scoping/{chat_id}/message")
    async def scoping_message(chat_id: str, request: Request):
        chat = get_chat_or_404(chat_id)
        if chat.status in ("approved", "abandoned"):
            raise HTTPException(409, f"chat is {chat.status}")
        if chat_locks.setdefault(chat.id, asyncio.Lock()).locked():
            raise HTTPException(409, "a turn is already running for this chat")
        body = await request.json()
        text = str(body.get("text", "")).strip()
        if not text:
            raise HTTPException(400, "text is required")
        if bool(body.get("wait")):  # tests and the CLI want it synchronous
            chat = await run_chat_turn(chat, text)
            return {"chat": chat.to_dict()}
        # Record the operator's turn immediately so the browser sees its own
        # message and the thinking state without waiting on claude.
        chat.messages.append({"role": "operator", "text": text,
                              "at": now_iso()})
        chat.status = "thinking"
        chat.save()
        chats[chat.id] = chat
        spawn_chat_turn(chat, text)
        return {"chat": chat.to_dict()}

    @app.post("/scoping/{chat_id}/approve")
    async def scoping_approve(chat_id: str, request: Request):
        chat = get_chat_or_404(chat_id)
        if chat.status == "approved":
            raise HTTPException(409, "already approved")
        body = await request.json()
        run = bool(body.get("run", True))
        scoping_api.refresh_contract(chat)
        if chat.contract is None:
            raise HTTPException(400, "no contract written yet — keep scoping")
        errors = validate_contract(chat.contract)
        repo = str(chat.contract.get("repo", ""))
        if repo and not Path(repo).expanduser().is_dir():
            errors.append(f"repo path does not exist: {repo}")
        if errors:
            raise HTTPException(400, {"errors": errors})
        # The task already exists (created when scoping started) — adopt the
        # contract into it rather than minting a second task.
        task = store.load_task(chat.task_id) if chat.task_id else None
        if task is not None and task.status == "scoping":
            task = store.adopt_contract(task, Contract.from_dict(chat.contract))
            started = manager.start(task.id) if run else False
            evt = store.append_event(task.id, "task_ready",
                                     data={"started": started})
            await manager.broadcast(evt)
        else:
            task, started = await create_task_from_contract(chat.contract, run)
        chat.status = "approved"
        chat.task_id = task.id
        chat.save()
        await manager.broadcast({"kind": "scoping", "id": chat.id,
                                 "status": chat.status, "task_id": task.id})
        return {"task": task.to_dict(), "started": started,
                "chat": chat.to_dict()}

    @app.post("/scoping/{chat_id}/abandon")
    async def scoping_abandon(chat_id: str):
        chat = get_chat_or_404(chat_id)
        chat.status = "abandoned"
        chat.save()
        # A scoping task with no contract has nothing to keep.
        if chat.task_id:
            task = store.load_task(chat.task_id)
            if task is not None and task.status == "scoping":
                await asyncio.to_thread(discard_unused_worktree, task)
                task.status = "abandoned"
                task.scoping_chat_id = None
                store.save_task(task)
                evt = store.append_event(task.id, "task_abandoned",
                                         data={"reason": "scoping abandoned"})
                await manager.broadcast(evt)
        return {"chat": chat.to_dict()}

    # ------------------------------------------------------------- memory

    @app.get("/memory")
    async def list_memory():
        return {"projects": store.list_memory()}

    @app.get("/memory/{project}")
    async def get_memory(project: str):
        if "/" in project or project.startswith("."):
            raise HTTPException(400, "bad project name")
        text = store.memory_for_project(project)
        if text is None:
            raise HTTPException(404, f"no memory for project: {project}")
        return {"project": project, "text": text}

    @app.post("/memory/{project}")
    async def append_memory(project: str, request: Request):
        if "/" in project or project.startswith("."):
            raise HTTPException(400, "bad project name")
        body = await request.json()
        fact = str(body.get("fact", "")).strip()
        if not fact:
            raise HTTPException(400, "fact is required")
        store.remember(project, fact)
        return {"project": project, "text": store.memory_for_project(project)}

    @app.put("/memory/{project}")
    async def replace_memory(project: str, request: Request):
        if "/" in project or project.startswith("."):
            raise HTTPException(400, "bad project name")
        body = await request.json()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "text is required (memory is never "
                                     "silently deleted — abandon edits instead)")
        store.write_memory(project, text)
        return {"project": project, "text": store.memory_for_project(project)}

    # ---------------------------------------------------------- questions

    @app.get("/questions")
    async def list_questions(status: str | None = None, task: str | None = None):
        return {"questions": [q.to_dict()
                              for q in store.list_questions(task_id=task, status=status)]}

    @app.post("/questions/{qid}/answer")
    async def answer_question(qid: str, request: Request):
        body = await request.json()
        answer = str(body.get("answer", "")).strip()
        if not answer:
            raise HTTPException(400, "answer is required")
        q = store.load_question(qid)
        if q is None:
            raise HTTPException(404, f"unknown question: {qid}")
        if q.status == "answered":
            # First-write-wins with a notice (PRD §6.9).
            raise HTTPException(409, {"error": "already answered",
                                      "answer": q.answer, "by": q.answered_by})
        q = store.answer_question(qid, answer, str(body.get("by", "cli")))
        evt = store.append_event(q.task_id, "question_answered", step_id=q.step_id,
                                 data={"question_id": qid, "answer": answer})
        await manager.broadcast(evt)
        task = store.load_task(q.task_id)
        resumed = False
        if task is not None and task.status == "blocked":
            if (q.type in ("decision", "approval") and answer.lower() == "abandon"
                    and "abandon" in [o.lower() for o in q.options]):
                task.status = "abandoned"
                store.save_task(task)
                evt = store.append_event(task.id, "task_status",
                                         data={"status": "abandoned"})
                await manager.broadcast(evt)
            else:
                still_open = [oq for oq in store.list_questions(task_id=task.id,
                                                                status="open")
                              if oq.type != "fyi"]
                if not still_open:
                    resumed = manager.start(task.id)
        return {"question": q.to_dict(), "resumed": resumed}

    # ------------------------------------------- worker/hook callbacks

    @app.post("/internal/ask")
    async def internal_ask(request: Request):
        body = await request.json()
        task_id = str(body.get("task_id", ""))
        task = store.load_task(task_id)
        if task is None:
            raise HTTPException(404, f"unknown task: {task_id}")
        qtype = str(body.get("type", "clarification"))
        if qtype not in QUESTION_TYPES:
            raise HTTPException(400, f"unknown question type: {qtype}")
        q = Question(
            id=new_id("q"),
            task_id=task_id,
            step_id=body.get("step_id"),
            type=qtype,
            question=str(body.get("question", "")).strip(),
            urgency=str(body.get("urgency", "normal")),
            options=[str(o) for o in body.get("options") or []],
            default=body.get("default"),
            evidence=body.get("evidence") or {},
            status="noted" if qtype == "fyi" else "open",
        )
        if not q.question:
            raise HTTPException(400, "question text is required")
        store.save_question(q)
        evt = store.append_event(task_id, "question_asked", step_id=q.step_id,
                                 data={"question_id": q.id, "type": qtype,
                                       "question": q.question})
        await manager.broadcast(evt)
        if qtype == "fyi":
            return {"status": "recorded", "id": q.id,
                    "message": "FYI recorded. Continue working."}
        manager.deliver(task_id, {"kind": "ask", "question": q.to_dict()})
        return {"status": "parked", "id": q.id,
                "message": ("Question parked for the operator. STOP working now "
                            "and end the session; the orchestrator will resume "
                            "this task with the answer.")}

    @app.post("/internal/events")
    async def internal_events(request: Request):
        body = await request.json()
        task_id = str(body.get("task_id") or "")
        if not task_id or store.load_task(task_id) is None:
            return {"ok": False, "reason": "unknown task"}
        evt = store.append_event(
            task_id, f"hook.{body.get('event', 'unknown')}",
            step_id=body.get("step_id"),
            data={"payload": body.get("payload")},
        )
        await manager.broadcast(evt)
        return {"ok": True}

    # ----------------------------------------------------------- websocket

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        manager.sockets.add(ws)
        try:
            await ws.send_json({
                "kind": "snapshot",
                "tasks": store.list_tasks(),
                "questions": [q.to_dict() for q in store.list_questions(status="open")],
            })
            while True:
                await ws.receive_text()  # client pings; content ignored
        except WebSocketDisconnect:
            pass
        finally:
            manager.sockets.discard(ws)

    # ---------------------------------------------------------- dashboard
    # The built SPA (dashboard/dist) is served at /ui; the SPA itself is a
    # thin client that only talks to this API. Absent a build, / still
    # answers with a pointer instead of a 404.

    dist = dashboard_dist()
    if dist is not None:
        app.mount("/ui", StaticFiles(directory=str(dist), html=True),
                  name="dashboard")

        @app.get("/")
        async def root():
            return RedirectResponse("/ui/")
    else:

        @app.get("/")
        async def root():
            return {"ok": True,
                    "hint": "dashboard build not found — run `pnpm build` in "
                            "dashboard/ (or set FM_DASHBOARD_DIST)",
                    "api": "/status"}

    return app
