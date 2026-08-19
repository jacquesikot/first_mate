"""Browser scoping — daemon-mediated scoping conversations (PRD §6.8).

Resolves open question §10.1 with the API-conversation option: each turn
is one headless `claude -p` invocation, chained with --resume against the
transcript, so conversational continuity lives in Claude Code's session
files — the daemon only keeps a thin chat record, mirrored to
scoping.json in the chat's scoping directory (cat/jq debuggable, and
rehydratable after a daemon restart).

Same prompt, same allowlist, same `fm contract check` gate as the
terminal flow in scoping.py; the only difference is who hosts the
conversation.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import scoping
from .models import new_id, now_iso, slugify, validate_contract

TURN_TIMEOUT_S = 420  # a scoping turn reads the repo; give it room

# Statuses: thinking (a turn is running) · awaiting_operator ·
# contract_ready · approved · abandoned · failed
OPEN_STATUSES = {"thinking", "awaiting_operator", "contract_ready"}


@dataclass
class ScopingChat:
    id: str
    goal: str
    repo: str
    dir: str
    # Where the conversation actually reads. The task's worktree exists from
    # the moment scoping starts, so the assistant sees the clean starting
    # point the operator chose rather than whatever their repo checkout
    # happens to hold. `repo` stays the canonical path the contract names.
    workdir: str = ""
    base: str = ""       # the committish the worktree started from
    base_sha: str = ""   # what that resolved to, pinned for the record
    status: str = "thinking"
    session_id: str | None = None  # latest transcript id to --resume
    model: str | None = None
    messages: list[dict] = field(default_factory=list)
    contract: dict | None = None
    contract_errors: list[str] = field(default_factory=list)
    task_id: str | None = None
    created_at: str = field(default_factory=now_iso)

    @property
    def contract_path(self) -> Path:
        return Path(self.dir) / "contract.json"

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScopingChat":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def save(self) -> None:
        path = Path(self.dir) / "scoping.json"
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n")


def start_chat(home: Path, goal: str, repo: Path,
               model: str | None = None,
               task_id: str | None = None,
               workdir: Path | None = None,
               base: str = "", base_sha: str = "") -> ScopingChat:
    chat_dir = home / "scoping" / f"{slugify(goal)}-{uuid.uuid4().hex[:6]}"
    chat_dir.mkdir(parents=True, exist_ok=True)
    memory = None
    mem_file = home / "memory" / f"{repo.name}.md"
    if mem_file.exists():
        memory = mem_file.read_text()
    prompt = scoping.build_prompt(
        goal, repo, chat_dir / "contract.json", memory,
        finalize=scoping.BROWSER_FINALIZE,
        workdir=workdir, base=base,
    )
    (chat_dir / "prompt.md").write_text(prompt)
    chat = ScopingChat(
        id=new_id("scope"), goal=goal, repo=str(repo), dir=str(chat_dir),
        workdir=str(workdir) if workdir else "", base=base, base_sha=base_sha,
        model=model, task_id=task_id,
    )
    chat.save()
    return chat


def load_chat(home: Path, chat_id: str) -> ScopingChat | None:
    """Rehydrate a chat from disk (daemon restarts keep conversations
    resumable — the transcript belongs to Claude Code, not to us)."""
    root = home / "scoping"
    if not root.is_dir():
        return None
    for p in root.glob("*/scoping.json"):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("id") == chat_id:
            return ScopingChat.from_dict(data)
    return None


def find_chat_for_task(home: Path, task_id: str) -> ScopingChat | None:
    """The scoping conversation belonging to a task (task-first flow)."""
    root = home / "scoping"
    if not root.is_dir():
        return None
    for p in root.glob("*/scoping.json"):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("task_id") == task_id:
            return ScopingChat.from_dict(data)
    return None


def _allowed_tools(chat_dir: Path) -> list[str]:
    # Mirrors scoping.build_command: read-only repo access, writes only
    # into the chat's scoping dir, and the contract self-check.
    # Path rules for file modification must be spelled Edit(...) — the CLI
    # accepts Write(path) rules but never consults them (docs: permissions,
    # "Read and Edit"; found live when the scoping session couldn't write
    # its contract). `//` = absolute path.
    return [
        "Read", "Glob", "Grep",
        f"Edit(/{chat_dir}/**)",
        "Bash(fm contract check:*)",
    ]


def _turn_command(chat: ScopingChat, text: str) -> list[str]:
    # --add-dir: the scoping dir lives under FM_HOME, outside the session's
    # cwd (the repo) — without it, headless dontAsk denies the contract
    # Write outright (found live: the assistant reported its own denial).
    cmd = ["claude", "-p", text, "--output-format", "json",
           "--permission-mode", "dontAsk",
           "--add-dir", chat.dir,
           "--allowedTools", ",".join(_allowed_tools(Path(chat.dir)))]
    if chat.session_id:
        cmd += ["--resume", chat.session_id]
    if chat.model:
        cmd += ["--model", chat.model]
    return cmd


def run_turn_subprocess(chat: ScopingChat, text: str) -> tuple[str, str | None]:
    """One headless turn. Returns (assistant_text, new_session_id).
    Blocking — call from a thread."""
    env = dict(os.environ)
    fm = Path(sys.executable).with_name("fm")
    fm_dir = str(fm.parent) if fm.exists() else None
    if fm_dir is None and shutil.which("fm"):
        fm_dir = str(Path(shutil.which("fm")).parent)
    if fm_dir:  # the session self-checks with `fm contract check`
        env["PATH"] = f"{fm_dir}:{env.get('PATH', '')}"
    proc = subprocess.run(
        _turn_command(chat, text), cwd=(chat.workdir or chat.repo),
        capture_output=True, text=True, timeout=TURN_TIMEOUT_S, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}: {proc.stderr.strip()[-800:]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"non-JSON output from claude: {proc.stdout[-800:]}")
    reply = str(payload.get("result", "")).strip()
    # --resume mints a fresh session id per continuation; chain from the
    # one this turn reports or we lose the thread next turn.
    new_sid = payload.get("session_id") or None
    return reply, new_sid


def advance(chat: ScopingChat, text: str, runner=None) -> ScopingChat:
    """Run one turn (blocking) and fold the result into the chat record.
    `runner` is injectable for tests (resolved at call time so a
    monkeypatched run_turn_subprocess takes effect)."""
    runner = runner or run_turn_subprocess
    is_first = not chat.messages
    prompt = (Path(chat.dir) / "prompt.md").read_text() if is_first else text
    # The caller may have already recorded the operator turn (the browser
    # flow does, so the message and the thinking state appear immediately);
    # don't record it twice.
    already = (not is_first and chat.messages[-1].get("role") == "operator"
               and chat.messages[-1].get("text") == text)
    if not is_first and not already:
        chat.messages.append({"role": "operator", "text": text, "at": now_iso()})
    chat.status = "thinking"
    chat.save()
    try:
        reply, new_sid = runner(chat, prompt)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as e:
        chat.messages.append({"role": "system", "text": f"turn failed: {e}",
                              "at": now_iso()})
        if not _abandoned_meanwhile(chat):
            chat.status = "failed"
        chat.save()
        return chat
    if new_sid:
        chat.session_id = new_sid
    chat.messages.append({"role": "firstmate", "text": reply, "at": now_iso()})
    refresh_contract(chat)
    # A turn started before the operator abandoned (or approved) must not
    # resurrect the conversation when it lands — turns run off the request
    # path now, so that race is real. Found live.
    if _abandoned_meanwhile(chat):
        chat.save()
        return chat
    chat.status = "contract_ready" if (
        chat.contract is not None and not chat.contract_errors
    ) else "awaiting_operator"
    chat.save()
    return chat


def _abandoned_meanwhile(chat: ScopingChat) -> bool:
    """True if the chat reached a terminal status on disk while the turn ran.
    The record on disk is authoritative — this object was captured earlier."""
    path = Path(chat.dir) / "scoping.json"
    try:
        disk = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if disk.get("status") in ("abandoned", "approved"):
        chat.status = disk["status"]
        chat.task_id = disk.get("task_id") or chat.task_id
        return True
    return False


def refresh_contract(chat: ScopingChat) -> None:
    """Re-read contract.json from the scoping dir; the same gate POST
    /tasks enforces decides whether it counts as ready."""
    path = chat.contract_path
    if not path.exists():
        chat.contract = None
        chat.contract_errors = []
        return
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        chat.contract = None
        chat.contract_errors = [f"contract is not valid JSON: {e}"]
        return
    chat.contract = data
    errors = validate_contract(data)
    repo = str(data.get("repo", "")) if isinstance(data, dict) else ""
    if repo and not Path(repo).expanduser().is_dir():
        errors.append(f"repo path does not exist: {repo}")
    chat.contract_errors = errors


__all__ = [
    "ScopingChat", "start_chat", "load_chat", "find_chat_for_task", "advance",
    "refresh_contract", "run_turn_subprocess", "OPEN_STATUSES",
]
