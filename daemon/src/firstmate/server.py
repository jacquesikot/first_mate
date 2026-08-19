"""fm daemon — REST + WebSocket API over the store and orchestrator.

Single local process (PRD §7); the browser, Slack connector, and CLI all
talk to this API so state is always consistent. Binds 127.0.0.1 only
(dashboard auth posture: localhost-only for v1).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect

from .exec import tmux
from .models import (
    Contract, Question, QUESTION_TYPES, TERMINAL_TASK_STATUSES,
    new_id, now_iso, validate_contract,
)
from .orchestrator import TaskRunner
from .store import Store


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

    async def reconcile(self) -> None:
        """Boot recovery (acceptance criterion 8): mark sessions the dead
        daemon left in flight as orphaned, kill their tmux windows, and
        resume the tasks from state files."""
        for row in self.store.list_tasks():
            task = self.store.load_task(row["id"])
            if task is None or task.status not in ("running", "validating"):
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
                          "running": manager.running(task.id)})
        questions = [q.to_dict() for q in store.list_questions(status="open")]
        return {"tasks": tasks, "questions": questions}

    # -------------------------------------------------------------- tasks

    @app.get("/tasks")
    async def list_tasks():
        return {"tasks": store.list_tasks()}

    @app.post("/tasks")
    async def create_task(request: Request):
        body = await request.json()
        data = body.get("contract")
        errors = validate_contract(data or {})
        repo = str((data or {}).get("repo", ""))
        if repo and not Path(repo).expanduser().is_dir():
            errors.append(f"repo path does not exist: {repo}")
        if errors:
            raise HTTPException(400, {"errors": errors})
        contract = Contract.from_dict(data)
        contract.repo = str(Path(contract.repo).expanduser().resolve())
        task = store.create_task(contract)
        started = manager.start(task.id) if body.get("run") else False
        evt = store.append_event(task.id, "task_ready", data={"started": started})
        await manager.broadcast(evt)
        return {"task": task.to_dict(), "started": started}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        task = get_task_or_404(task_id)
        contract = store.load_contract(task_id)
        return {
            "task": task.to_dict(),
            "contract": contract.to_dict() if contract else None,
            "questions": [q.to_dict() for q in store.list_questions(task_id=task_id)],
            "events": store.events_tail(task_id),
            "attach": live_attach(task),
            "running": manager.running(task_id),
        }

    @app.post("/tasks/{task_id}/run")
    async def run_task(task_id: str):
        task = get_task_or_404(task_id)
        if task.status in TERMINAL_TASK_STATUSES:
            raise HTTPException(409, f"task is {task.status}")
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
        task.status = "abandoned"
        store.save_task(task)
        evt = store.append_event(task_id, "task_status", data={"status": "abandoned"})
        await manager.broadcast(evt)
        return {"delivered": False, "status": task.status}

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
            if q.type == "decision" and answer.lower() == "abandon":
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

    return app
