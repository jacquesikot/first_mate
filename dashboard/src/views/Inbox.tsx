import { useApp, QuestionCard, SectionHead } from "../components";

export function InboxView() {
  const { status } = useApp();
  const tasks = status?.tasks ?? [];
  const all = status?.questions ?? [];
  const open = all.filter((q) => q.status === "open");
  const blocking = open.filter((q) => q.urgency === "blocking");
  const rest = open.filter((q) => q.urgency !== "blocking");
  const goal = (id: string) => tasks.find((t) => t.id === id)?.goal;

  return (
    <div className="page" style={{ maxWidth: 1020, gap: 14 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 14,
          flexWrap: "wrap",
          padding: "12px 15px",
          borderRadius: "var(--rad)",
          border: "1px solid var(--bd)",
          background: "var(--s2)",
        }}
      >
        <span className="mono" style={{ fontSize: 11.5, color: "var(--tx2)" }}>
          {open.length} open · {blocking.length} blocking · {rest.length} batched
        </span>
        <span
          className="mono"
          style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--tx4)", textAlign: "right" }}
        >
          non-blocking questions batch at step boundaries · blocking pages immediately
        </span>
      </div>

      {blocking.length > 0 && (
        <>
          <SectionHead title="blocking" accent hint="parked tasks · consuming no worker slot" />
          {blocking.map((q) => (
            <QuestionCard key={q.id} q={q} taskGoal={goal(q.task_id)} />
          ))}
        </>
      )}

      {rest.length > 0 && (
        <>
          <SectionHead title="non-blocking" hint="ranked by age" />
          {rest.map((q) => (
            <QuestionCard key={q.id} q={q} taskGoal={goal(q.task_id)} />
          ))}
        </>
      )}

      {open.length === 0 && (
        <div className="empty" style={{ padding: 40 }}>
          <div className="mono" style={{ fontSize: 13, color: "var(--tx2)" }}>
            queue empty
          </div>
          <div style={{ fontSize: 12.5, color: "var(--tx4)", marginTop: 7 }}>
            Every answer went to the contract. Parked tasks have resumed.
          </div>
        </div>
      )}
    </div>
  );
}
