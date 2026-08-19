import { useState } from "react";
import { useApp, ContextMeter, Glyph } from "../components";
import { ago, repoName } from "../format";
import type { TaskRow } from "../api";

const GROUPS: [string, string, string, string][] = [
  // [status, label, color, hint]
  ["blocked", "blocked — waiting on you", "var(--ac)", "parked · no worker slot held"],
  ["running", "running", "var(--ok)", "live sessions, streamed"],
  ["validating", "validating", "var(--tx2)", "criteria collected at task boundary"],
  ["ready", "ready to run", "var(--tx2)", "contract approved, awaiting a slot"],
  ["paused", "paused", "var(--tx2)", "resting · resume with fm run"],
  ["failed", "failed", "var(--bad)", "escalated after two attempts"],
  ["abandoned", "abandoned", "var(--tx3)", ""],
  ["done", "done", "var(--tx2)", "every criterion met"],
];

type Filter = "all" | "needs" | "active" | "settled";

export function TasksView() {
  const { status, live, go } = useApp();
  const [filter, setFilter] = useState<Filter>("all");
  const tasks = status?.tasks ?? [];
  const openQ = (status?.questions ?? []).filter((q) => q.status === "open");
  const qCount = (t: TaskRow) => openQ.filter((q) => q.task_id === t.id).length;

  const inFilter = (t: TaskRow) =>
    filter === "all"
      ? true
      : filter === "needs"
        ? qCount(t) > 0 || t.status === "blocked"
        : filter === "active"
          ? ["running", "blocked", "validating", "ready", "paused"].includes(t.status)
          : ["done", "failed", "abandoned"].includes(t.status);

  const filters: [Filter, string][] = [
    ["all", "all"],
    ["needs", "needs you"],
    ["active", "active"],
    ["settled", "settled"],
  ];

  return (
    <div
      style={{
        padding: "22px 22px 60px",
        display: "flex",
        flexDirection: "column",
        gap: 20,
        maxWidth: 1180,
      }}
    >
      <div
        style={{
          display: "flex",
          gap: 7,
          padding: 3,
          borderRadius: 9,
          background: "var(--s1)",
          border: "1px solid var(--bd)",
          width: "fit-content",
        }}
      >
        {filters.map(([f, label]) => (
          <button
            key={f}
            className="mono"
            style={{
              padding: "6px 12px",
              borderRadius: 6,
              fontSize: 11.5,
              background: filter === f ? "var(--s3)" : "transparent",
              color: filter === f ? "var(--tx)" : "var(--tx3)",
            }}
            onClick={() => setFilter(f)}
          >
            {label}
          </button>
        ))}
      </div>

      {tasks.length === 0 && (
        <div className="empty">
          No tasks yet. Start one with{" "}
          <span className="mono" style={{ fontSize: 12 }}>
            fm task "&lt;goal&gt;"
          </span>{" "}
          or the New task view.
        </div>
      )}

      {GROUPS.map(([st, label, color, hint]) => {
        const list = tasks.filter((t) => t.status === st && inFilter(t));
        if (!list.length) return null;
        return (
          <section key={st} style={{ display: "flex", flexDirection: "column", gap: 9 }}>
            <div className="section-head">
              <span className="section-title" style={{ color }}>
                {label}
              </span>
              <span className="mono" style={{ fontSize: 11, color: "var(--tx4)" }}>
                {list.length}
              </span>
              <span className="section-rule" />
              <span className="section-hint">{hint}</span>
            </div>
            {list.map((t) => {
              const nq = qCount(t);
              const ctx = live[t.id]?.context ?? t.context ?? null;
              return (
                <button
                  key={t.id}
                  className="row-btn card"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    flexWrap: "wrap",
                    gap: 14,
                    padding: "13px 16px",
                    borderLeft: `2px solid ${
                      t.status === "blocked"
                        ? "var(--ac)"
                        : t.status === "failed"
                          ? "var(--bad)"
                          : "var(--bd)"
                    }`,
                  }}
                  onClick={() => go(`#/task/${t.id}`)}
                >
                  <Glyph status={t.status} animate />
                  <div style={{ minWidth: 0, flex: "1 1 240px", textAlign: "left" }}>
                    <div
                      style={{
                        fontSize: 14.5,
                        fontWeight: 500,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {t.goal}
                    </div>
                    <div
                      className="mono"
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 7,
                        marginTop: 4,
                        fontSize: 11,
                        color: "var(--tx4)",
                      }}
                    >
                      <span style={{ color: "var(--tx3)" }}>{repoName(t.repo)}</span>
                      <span>/</span>
                      <span>{t.branch}</span>
                      <span>·</span>
                      <span>{t.id}</span>
                    </div>
                  </div>
                  <div style={{ flex: "0 0 auto", display: "flex", alignItems: "center", gap: 16 }}>
                    <div
                      className="mono"
                      style={{
                        width: 152,
                        fontSize: 11.5,
                        color: "var(--tx2)",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        textAlign: "left",
                      }}
                    >
                      {t.current_step ?? "—"}
                      {(t.generation ?? 0) > 1 ? ` · gen ${t.generation}` : ""}
                    </div>
                    <ContextMeter ctx={ctx} width={92} />
                    <div
                      className="mono"
                      style={{
                        width: 84,
                        textAlign: "right",
                        fontSize: 11,
                        color: nq ? "var(--ac)" : "var(--tx4)",
                      }}
                    >
                      {nq ? `${nq} question${nq > 1 ? "s" : ""}` : ""}
                    </div>
                    <div
                      className="mono"
                      style={{ width: 56, textAlign: "right", fontSize: 11, color: "var(--tx4)" }}
                    >
                      {ago(t.updated_at)}
                    </div>
                  </div>
                </button>
              );
            })}
          </section>
        );
      })}
    </div>
  );
}
