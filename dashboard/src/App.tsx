import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { LivePayload, StatusInfo, WsMessage } from "./api";
import { api, socket } from "./api";
import { AppCtx } from "./components";
import { NowView } from "./views/Now";
import { TasksView } from "./views/Tasks";
import { TaskDetailView } from "./views/TaskDetail";
import { InboxView } from "./views/Inbox";
import { MemoryView } from "./views/Memory";
import { NewTaskView } from "./views/NewTask";

type Route =
  | { view: "now" }
  | { view: "tasks" }
  | { view: "task"; id: string }
  | { view: "inbox" }
  | { view: "memory" }
  | { view: "new" };

function parseHash(): Route {
  const h = location.hash.replace(/^#\/?/, "");
  if (h.startsWith("task/")) return { view: "task", id: h.slice(5) };
  if (h === "tasks") return { view: "tasks" };
  if (h === "inbox") return { view: "inbox" };
  if (h === "memory") return { view: "memory" };
  if (h === "new") return { view: "new" };
  return { view: "now" };
}

interface Toast {
  msg: string;
  sub?: string;
  kind: "ok" | "run" | "dim" | "bad";
}

export default function App() {
  const [route, setRoute] = useState<Route>(parseHash());
  const [status, setStatus] = useState<StatusInfo | null>(null);
  const [live, setLive] = useState<Record<string, LivePayload>>({});
  const [wsUp, setWsUp] = useState(false);
  const [toastState, setToastState] = useState<Toast | null>(null);
  const toastTimer = useRef<number | null>(null);
  const refreshTimer = useRef<number | null>(null);

  const refresh = useCallback(() => {
    api.status().then(setStatus).catch(() => {});
  }, []);

  // Debounced refresh — WS events can arrive in bursts.
  const refreshSoon = useCallback(() => {
    if (refreshTimer.current != null) return;
    refreshTimer.current = window.setTimeout(() => {
      refreshTimer.current = null;
      refresh();
    }, 250);
  }, [refresh]);

  useEffect(() => {
    refresh();
    const poll = window.setInterval(refresh, 10_000); // fallback; WS is primary
    const unsub = socket.subscribe((msg: WsMessage) => {
      if ("kind" in msg && msg.kind === "live") {
        setLive((prev) => ({ ...prev, [msg.task_id]: msg }));
        return;
      }
      if ("kind" in msg && msg.kind === "snapshot") {
        refreshSoon();
        return;
      }
      if ("event" in msg) {
        if (msg.event === "_ws_open") {
          setWsUp(true);
          refresh();
        } else if (msg.event === "_ws_close") {
          setWsUp(false);
        } else {
          refreshSoon();
        }
      }
    });
    const onHash = () => setRoute(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => {
      unsub();
      window.clearInterval(poll);
      window.removeEventListener("hashchange", onHash);
    };
  }, [refresh, refreshSoon]);

  const toast = useCallback((msg: string, sub?: string, kind: Toast["kind"] = "ok") => {
    if (toastTimer.current != null) window.clearTimeout(toastTimer.current);
    setToastState({ msg, sub, kind });
    toastTimer.current = window.setTimeout(() => setToastState(null), 4200);
  }, []);

  const go = useCallback((hash: string) => {
    location.hash = hash;
  }, []);

  const ctx = useMemo(
    () => ({ status, live, wsUp, refresh, toast, go }),
    [status, live, wsUp, refresh, toast, go]
  );

  const openQuestions = (status?.questions ?? []).filter((q) => q.status === "open");
  // Any open non-fyi question is parking its task (PRD §6.5) — that is
  // what "needs you" means, regardless of paging urgency.
  const blocking = openQuestions.filter((q) => q.type !== "fyi").length;
  const tasks = status?.tasks ?? [];
  const running = tasks.filter((t) => t.status === "running").length;
  const maxWorkers = status?.config?.max_workers ?? 3;

  const navItems: { key: Route["view"]; label: string; glyph: string; badge: number }[] = [
    { key: "now", label: "Now", glyph: "◆", badge: blocking },
    { key: "tasks", label: "Tasks", glyph: "≡", badge: running },
    { key: "inbox", label: "Inbox", glyph: "◇", badge: openQuestions.length },
    { key: "memory", label: "Memory", glyph: "▪", badge: 0 },
  ];

  const headTitle = {
    now: "Now",
    tasks: "Tasks",
    task: "Task",
    inbox: "Inbox",
    memory: "Memory",
    new: "New task",
  }[route.view];

  const headSub =
    route.view === "task" && "id" in route
      ? route.id
      : `${tasks.length} tasks · ${running} running · ${openQuestions.length} in queue`;

  return (
    <AppCtx.Provider value={ctx}>
      <div className="app">
        <aside className="sidebar">
          <div style={{ padding: "18px 16px 14px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
              <div
                className="mono"
                style={{
                  width: 22,
                  height: 22,
                  borderRadius: 6,
                  background: "linear-gradient(150deg,var(--ac),var(--acd))",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  fontSize: 12,
                  fontWeight: 500,
                  color: "#180f02",
                }}
              >
                fm
              </div>
              <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-0.01em" }}>
                First Mate
              </div>
              <div
                className="mono"
                style={{
                  marginLeft: "auto",
                  fontSize: 10,
                  color: "var(--tx4)",
                  border: "1px solid var(--bd)",
                  borderRadius: 5,
                  padding: "1px 5px",
                }}
              >
                v0.1
              </div>
            </div>
          </div>

          <nav style={{ display: "flex", flexDirection: "column", gap: 2, padding: "0 10px" }}>
            {navItems.map((n) => {
              const on =
                route.view === n.key || (n.key === "tasks" && route.view === "task");
              const urgent = (n.key === "now" || n.key === "inbox") && n.badge > 0;
              return (
                <button
                  key={n.key}
                  onClick={() => go(`#/${n.key === "now" ? "" : n.key}`)}
                  style={{
                    position: "relative",
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    padding: "8px 10px",
                    borderRadius: 7,
                    textAlign: "left",
                    background: on ? "var(--s3)" : "transparent",
                  }}
                >
                  <span
                    style={{
                      position: "absolute",
                      left: 0,
                      top: 9,
                      bottom: 9,
                      width: 2,
                      borderRadius: 2,
                      background: on ? "var(--ac)" : "transparent",
                    }}
                  />
                  <span
                    className="mono"
                    style={{ fontSize: 11, width: 12, color: on ? "var(--ac)" : "var(--tx4)" }}
                  >
                    {n.glyph}
                  </span>
                  <span
                    style={{
                      fontSize: 13,
                      fontWeight: 500,
                      color: on ? "var(--tx)" : "var(--tx2)",
                    }}
                  >
                    {n.label}
                  </span>
                  {n.badge > 0 && (
                    <span
                      className="mono"
                      style={{
                        marginLeft: "auto",
                        fontSize: 10,
                        padding: "1px 6px",
                        borderRadius: 20,
                        background: urgent ? "var(--acbg)" : "transparent",
                        color: urgent ? "var(--ac)" : "var(--tx3)",
                        border: `1px solid ${urgent ? "var(--acbd)" : "var(--bd)"}`,
                      }}
                    >
                      {n.badge}
                    </span>
                  )}
                </button>
              );
            })}
          </nav>

          <div style={{ padding: "14px 12px 6px" }}>
            <button
              className="btn accent"
              style={{
                width: "100%",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                gap: 8,
                padding: 9,
                fontSize: 13,
              }}
              onClick={() => go("#/new")}
            >
              <span className="mono" style={{ fontSize: 13 }}>
                +
              </span>
              <span>New task</span>
            </button>
          </div>

          <div
            style={{
              marginTop: "auto",
              padding: 12,
              borderTop: "1px solid var(--bd)",
              display: "flex",
              flexDirection: "column",
              gap: 9,
            }}
          >
            <div
              className="mono"
              style={{ display: "flex", flexDirection: "column", gap: 6, fontSize: 10.5, color: "var(--tx3)" }}
            >
              <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
                <span
                  className={wsUp ? "pulse" : ""}
                  style={{
                    width: 5,
                    height: 5,
                    borderRadius: "50%",
                    background: wsUp ? "var(--ok)" : "var(--bad)",
                  }}
                />
                <span>{wsUp ? "daemon connected" : status ? "ws reconnecting…" : "daemon unreachable"}</span>
              </div>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span>workers</span>
                <span style={{ display: "flex", gap: 3, alignItems: "center" }}>
                  {Array.from({ length: maxWorkers }, (_, i) => (
                    <span
                      key={i}
                      style={{
                        width: 9,
                        height: 5,
                        borderRadius: 1,
                        background: i < running ? "var(--ok)" : "var(--s4)",
                      }}
                    />
                  ))}
                  <span style={{ marginLeft: 5 }}>
                    {running}/{maxWorkers}
                  </span>
                </span>
              </div>
            </div>
            <div
              className="mono"
              style={{
                fontSize: 10,
                color: "var(--tx4)",
                paddingTop: 8,
                borderTop: "1px solid var(--bd)",
              }}
            >
              {location.host} · state in ~/.firstmate
            </div>
          </div>
        </aside>

        <main className="main">
          <header className="header">
            <div style={{ display: "flex", alignItems: "baseline", gap: 12, minWidth: 0 }}>
              <span style={{ fontSize: 15, fontWeight: 600, whiteSpace: "nowrap" }}>
                {headTitle}
              </span>
              <span
                className="mono"
                style={{
                  fontSize: 11,
                  color: "var(--tx3)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {headSub}
              </span>
            </div>
            <div style={{ marginLeft: "auto" }}>
              <button
                onClick={() => go("#/inbox")}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "6px 11px",
                  borderRadius: 7,
                  border: `1px solid ${blocking ? "var(--acbd)" : "var(--bd)"}`,
                  background: blocking ? "var(--acbg)" : "transparent",
                  color: blocking ? "var(--ac)" : "var(--tx3)",
                  fontSize: 12,
                  fontWeight: 500,
                }}
              >
                <span className="mono" style={{ fontSize: 11 }}>
                  {blocking ? "△" : "✓"}
                </span>
                <span>{blocking ? `${blocking} need you` : "all clear"}</span>
              </button>
            </div>
          </header>

          <div className="content">
            {route.view === "now" && <NowView />}
            {route.view === "tasks" && <TasksView />}
            {route.view === "task" && <TaskDetailView taskId={route.id} />}
            {route.view === "inbox" && <InboxView />}
            {route.view === "memory" && <MemoryView />}
            {route.view === "new" && <NewTaskView />}
          </div>
        </main>

        {toastState && (
          <div className="toast">
            <span
              className="mono"
              style={{
                fontSize: 12,
                color:
                  toastState.kind === "run"
                    ? "var(--ac)"
                    : toastState.kind === "bad"
                      ? "var(--bad)"
                      : toastState.kind === "dim"
                        ? "var(--tx3)"
                        : "var(--ok)",
              }}
            >
              {toastState.kind === "run"
                ? "●"
                : toastState.kind === "bad"
                  ? "✗"
                  : toastState.kind === "dim"
                    ? "·"
                    : "✓"}
            </span>
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              <span style={{ fontSize: 13 }}>{toastState.msg}</span>
              {toastState.sub && (
                <span className="mono" style={{ fontSize: 10.5, color: "var(--tx3)" }}>
                  {toastState.sub}
                </span>
              )}
            </div>
          </div>
        )}
      </div>
    </AppCtx.Provider>
  );
}
