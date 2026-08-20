"""CLI — the hook-facing `fm _guard` and `fm contract check`."""

import io
import json
import sys

from firstmate.cli import main

UNREACHABLE = "http://127.0.0.1:1"  # connection refused instantly


def guard_config(tmp_path) -> str:
    path = tmp_path / "guard.json"
    path.write_text(json.dumps({
        "worktree": str(tmp_path),
        "scope_in": ["src/**"],
        "scope_out": [],
        "tripwire_allow": [],
        "tripwires": {},
    }))
    return str(path)


def run_guard(tmp_path, monkeypatch, payload: dict, fallback: str | None = None) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    argv = ["_guard", "--config", guard_config(tmp_path),
            "--task", "t1", "--step", "s1", "--url", UNREACHABLE]
    if fallback:
        argv += ["--fallback", fallback]
    return main(argv)


def test_guard_allows_in_scope(tmp_path, monkeypatch):
    code = run_guard(tmp_path, monkeypatch, {
        "tool_name": "Edit", "tool_input": {"file_path": "src/app.py"}})
    assert code == 0


def test_guard_blocks_out_of_scope(tmp_path, monkeypatch, capsys):
    fallback = str(tmp_path / "fallback.jsonl")
    code = run_guard(tmp_path, monkeypatch, {
        "tool_name": "Write", "tool_input": {"file_path": "docs/x.md"}},
        fallback=fallback)
    assert code == 2
    err = capsys.readouterr().err
    assert "scope guard: BLOCKED" in err and "fm ask" in err
    events = [json.loads(l) for l in open(fallback)]
    assert events[0]["event"] == "GuardBlock"
    assert events[0]["payload"]["code"] == "out_of_scope"


def test_guard_without_config_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert main(["_guard", "--config", str(tmp_path / "missing.json"),
                 "--task", "t1", "--url", UNREACHABLE]) == 0


def test_guard_garbage_stdin_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    argv = ["_guard", "--config", guard_config(tmp_path),
            "--task", "t1", "--url", UNREACHABLE]
    assert main(argv) == 0  # no tool_name → nothing to judge


def test_contract_check_command(tmp_path, capsys):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "goal": "g", "repo": str(tmp_path),
        "steps": [{"id": "s1", "prompt": "p", "criteria": ["c1"]}],
        "criteria": [{"id": "c1", "command": "true"}],
    }))
    assert main(["contract", "check", str(good)]) == 0
    assert "contract OK" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"goal": "", "repo": "", "steps": []}))
    assert main(["contract", "check", str(bad)]) == 1
    assert "goal is required" in capsys.readouterr().out


def test_serve_refuses_a_port_already_in_use(tmp_path, monkeypatch, capsys):
    """A second `fm serve` must fail loudly. Printing a success line and
    overwriting daemon.json with a pid that immediately dies leaves the
    operator believing they restarted when the old code is still serving
    (observed live 2026-08-20)."""
    import json as _json
    import socket

    from firstmate import cli

    monkeypatch.setenv("FM_HOME", str(tmp_path))
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.bind(("127.0.0.1", 0))
    port = held.getsockname()[1]
    held.listen(1)
    pointer = tmp_path / "daemon.json"
    pointer.write_text(_json.dumps({"url": f"http://127.0.0.1:{port}",
                                    "pid": 4242}) + "\n")
    try:
        rc = cli.main(["serve", "--port", str(port)])
    finally:
        held.close()

    assert rc == 1, "must exit non-zero, not pretend to have started"
    err = capsys.readouterr().err
    assert "already in use" in err
    assert "4242" in err, "names the pid from daemon.json so it can be found"
    assert f"-iTCP:{port} -sTCP:LISTEN" in err, (
        "must point at the LISTEN socket only — a bare `lsof -i :port` also "
        "lists client connections, so it never reads as free")
    # The existing pointer must not be clobbered by the failed attempt.
    assert _json.loads(pointer.read_text())["pid"] == 4242


# ------------------------------------------------ `fm serve` port claiming

def test_the_serve_probe_predicts_uvicorns_bind_not_a_stricter_one():
    """A TIME_WAIT socket left by the daemon we just stopped must not read
    as "another daemon is already running".

    The probe exists so a failed bind can't be mistaken for a successful
    restart. But without SO_REUSEADDR it is *stricter* than the bind it is
    predicting: uvicorn sets the option on its own listener, so a port
    holding only a TIME_WAIT socket is bindable in reality while the probe
    refuses it — and the operator is told a daemon is running when `lsof
    -sTCP:LISTEN` shows the port empty (observed live 2026-08-20).
    """
    import socket

    # Stand in for the just-stopped daemon: a connection closed from the
    # listening side leaves the local end in TIME_WAIT.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    client = socket.create_connection(("127.0.0.1", port))
    server, _ = listener.accept()
    server.close()          # active close → TIME_WAIT on the server side
    client.close()
    listener.close()

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))  # must not raise
    finally:
        probe.close()


def test_the_serve_probe_still_refuses_a_port_someone_is_listening_on():
    """The property the probe was added for, which the fix must not lose."""
    import socket

    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    port = held.getsockname()[1]
    held.listen(1)
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            raised = False
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                raised = True
            assert raised, "a listening socket must still block the bind"
        finally:
            probe.close()
    finally:
        held.close()
