import { useApp, ContextMeter, Glyph, QuestionCard, SectionHead } from "../components";
import { ago, repoName } from "../format";
import type { TaskRow } from "../api";

function goalOf(tasks: TaskRow[], id: string): string | undefined {
  return tasks.find((t) => t.id === id)?.goal;
}

export function NowView() {
  const { status, live, go } = useApp();
  const tasks = status?.tasks ?? [];
  const questions = (status?.questions ?? []).filter((q) => q.status === "open");
  // Every open non-fyi question is parking a task — surface all of them,
  // blocking (paged) first, then by age.
  const blocking = questions
    .filter((q) => q.type !== "fyi")
    .sort((a, b) =>
      a.urgency === b.urgency
        ? a.asked_at.localeCompare(b.asked_at)
        : a.urgency === "blocking"
          ? -1
          : 1
    );
  const liveTasks = tasks.filter((t) => t.status === "running" || t.status === "validating");
  const settled = tasks.filter((t) => ["done", "failed", "abandoned"].includes(t.status));
  const memoryHint = "fm remember appends · injected every session";

  const stat = (label: string, value: string | number, sub: string, accent?: boolean) => (
    <div
      key={label}
      style={{
        padding: "13px 15px",
        borderRadius: "var(--rad)",
        border: "1px solid var(--bd)",
        background: "linear-gradient(180deg,var(--s2),var(--s1))",
      }}
    >
      <div className="label">{label}</div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 7, marginTop: 7 }}>
        <span
          style={{
            fontSize: 26,
            fontWeight: 600,
            letterSpacing: "-0.02em",
            color: accent ? "var(--ac)" : "var(--tx)",
          }}
        >
          {value}
        </span>
        <span className="mono" style={{ fontSize: 11, color: "var(--tx3)" }}>
          {sub}
        </span>
      </div>
    </div>
  );

  return (
    <div
      style={{
        padding: "22px 22px 60px",
        display: "flex",
        flexDirection: "column",
        gap: 26,
        maxWidth: 1180,
      }}
    >
      <section
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit,minmax(176px,1fr))",
          gap: 12,
        }}
      >
        {stat("needs you", blocking.length, blocking.length ? "parked on you" : "clear", blocking.length > 0)}
        {stat(
          "running",
          liveTasks.length,
          `${status?.config?.max_workers ?? "—"} slots`
        )}
        {stat("open questions", questions.length, `${questions.length - blocking.length} non-blocking`)}
        {stat("tasks", tasks.length, `${settled.length} settled`)}
      </section>

      <section style={{ display: "flex", flexDirection: "column", gap: 11 }}>
        <SectionHead title="needs you" hint="ranked by urgency, then age" accent />
        {blocking.map((q) => (
          <QuestionCard key={q.id} q={q} taskGoal={goalOf(tasks, q.task_id)} />
        ))}
        {blocking.length === 0 && (
          <div className="empty">
            Nothing needs you.{" "}
            <span className="mono" style={{ fontSize: 12, color: "var(--tx4)" }}>
              {questions.length ? `${questions.length} non-blocking in the inbox` : "queue empty"}
            </span>
          </div>
        )}
      </section>

      <section style={{ display: "flex", flexDirection: "column", gap: 11 }}>
        <SectionHead title="live" hint="pushed over websocket" />
        {liveTasks.map((t) => {
          const lv = live[t.id];
          const tail = lv?.output
            ? lv.output.split("\n").filter((l) => l.trim()).slice(-4)
            : [];
          const ctx = lv?.context ?? t.context ?? null;
          return (
            <article key={t.id} className="card">
              <div style={{ display: "flex", alignItems: "flex-start", gap: 14, padding: "14px 16px" }}>
                <span style={{ paddingTop: 3 }}>
                  <Glyph status={t.status} animate />
                </span>
                <div style={{ minWidth: 0, flex: 1 }}>
                  <button
                    className="row-btn"
                    style={{ fontSize: 15, fontWeight: 500, letterSpacing: "-0.01em" }}
                    onClick={() => go(`#/task/${t.id}`)}
                  >
                    {t.goal}
                  </button>
                  <div
                    className="mono"
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      marginTop: 5,
                      fontSize: 11,
                      color: "var(--tx4)",
                      flexWrap: "wrap",
                    }}
                  >
                    <span style={{ color: "var(--tx3)" }}>{repoName(t.repo)}</span>
                    <span>·</span>
                    <span>{t.branch}</span>
                    <span>·</span>
                    <span style={{ color: "var(--tx3)" }}>{t.current_step ?? t.status}</span>
                    {(t.generation ?? 0) > 1 && (
                      <span
                        style={{
                          border: "1px solid var(--bd2)",
                          borderRadius: 4,
                          padding: "0 5px",
                          color: "var(--tx3)",
                        }}
                      >
                        gen {t.generation}
                      </span>
                    )}
                  </div>
                </div>
                <div style={{ flex: "0 0 auto" }}>
                  <ContextMeter ctx={ctx} note />
                </div>
              </div>
              {tail.length > 0 && (
                <div
                  style={{
                    borderTop: "1px solid var(--bd)",
                    background: "#07070a",
                    padding: "10px 16px",
                    display: "flex",
                    flexDirection: "column",
                    gap: 3,
                  }}
                >
                  {tail.map((l, i) => (
                    <div
                      key={i}
                      className="mono"
                      style={{
                        fontSize: 11.5,
                        lineHeight: 1.55,
                        color: "var(--tx3)",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {l}
                    </div>
                  ))}
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 10,
                      marginTop: 6,
                      paddingTop: 8,
                      borderTop: "1px solid #14141a",
                    }}
                  >
                    <span className="mono" style={{ fontSize: 10.5, color: "var(--tx3)" }}>
                      read-only stream
                    </span>
                    <button
                      className="mono"
                      style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--tx3)" }}
                      onClick={() => go(`#/task/${t.id}`)}
                    >
                      full output →
                    </button>
                  </div>
                </div>
              )}
            </article>
          );
        })}
        {liveTasks.length === 0 && <div className="empty">No live sessions.</div>}
      </section>

      {settled.length > 0 && (
        <section style={{ display: "flex", flexDirection: "column", gap: 11 }}>
          <SectionHead title="settled" hint={memoryHint} />
          <div className="card-flat">
            {settled.map((t) => (
              <button
                key={t.id}
                className="row-btn hoverable"
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 13,
                  padding: "11px 16px",
                  borderBottom: "1px solid var(--bd)",
                }}
                onClick={() => go(`#/task/${t.id}`)}
              >
                <Glyph status={t.status} />
                <span
                  style={{
                    fontSize: 13.5,
                    color: "var(--tx2)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {t.goal}
                </span>
                <span
                  className="mono"
                  style={{
                    marginLeft: "auto",
                    display: "flex",
                    gap: 16,
                    fontSize: 11,
                    color: "var(--tx4)",
                    flex: "0 0 auto",
                  }}
                >
                  <span>{repoName(t.repo)}</span>
                  <span>{ago(t.updated_at)} ago</span>
                </span>
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
