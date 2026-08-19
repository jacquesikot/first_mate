import { useState } from "react";
import { api } from "../api";
import { useApp, SectionHead } from "../components";

const TEMPLATE = `{
  "goal": "…what done means, in one sentence…",
  "repo": "/absolute/path/to/repo",
  "scope_in": ["src/**"],
  "steps": [
    { "id": "implement", "prompt": "…", "criteria": ["tests"] }
  ],
  "criteria": [
    { "id": "tests", "command": "npm test" }
  ]
}`;

/** Scoping-in-browser is an open question (PRD §10.1) — v1 keeps the
 * conversation in the terminal and lets the dashboard submit a finished
 * contract directly. */
export function NewTaskView() {
  const { toast, go, refresh } = useApp();
  const [draft, setDraft] = useState(TEMPLATE);
  const [run, setRun] = useState(true);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string | null>(null);

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setErrors(null);
    try {
      const contract = JSON.parse(draft);
      const r = await api.createTask(contract, run);
      toast(
        `Task created: ${r.task.id}`,
        r.started ? "worker spawning" : `start it with fm run ${r.task.id}`,
        r.started ? "run" : "ok"
      );
      refresh();
      go(`#/task/${r.task.id}`);
    } catch (e) {
      setErrors(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      style={{
        padding: "22px 22px 60px",
        maxWidth: 840,
        display: "flex",
        flexDirection: "column",
        gap: 16,
      }}
    >
      <div
        className="mono"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "11px 15px",
          borderRadius: "var(--rad)",
          border: "1px solid var(--acbd)",
          background: "var(--acbg)",
          fontSize: 12,
          flexWrap: "wrap",
        }}
      >
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--ac)" }} />
        <span style={{ color: "var(--tx)" }}>
          The scoping conversation lives in the terminal:
        </span>
        <span style={{ color: "var(--ac)" }}>fm task "&lt;goal&gt;"</span>
        <span style={{ marginLeft: "auto", color: "var(--tx3)" }}>
          reads memory + repo first · refuses vague criteria
        </span>
      </div>

      <div style={{ fontSize: 13, color: "var(--tx2)", lineHeight: 1.6, maxWidth: "72ch" }}>
        It proposes scope, steps, and machine-checkable completion criteria for you to push back
        on, then submits the contract here automatically. Already have a contract? Paste it below
        — it passes through the same machine-checkability gate.
      </div>

      <SectionHead title="submit a contract" hint="validated by POST /tasks" />

      <textarea
        className="editor"
        style={{ minHeight: 360 }}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        spellCheck={false}
      />

      {errors && (
        <div
          className="mono"
          style={{
            padding: "10px 14px",
            borderRadius: "var(--rad)",
            border: "1px solid rgba(207,107,96,.35)",
            background: "var(--badbg)",
            color: "var(--bad)",
            fontSize: 12,
            whiteSpace: "pre-wrap",
          }}
        >
          {errors}
        </div>
      )}

      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <button className="btn accent" style={{ padding: "9px 16px", fontSize: 13 }} onClick={submit} disabled={busy}>
          {run ? "Create and run" : "Create task"}
        </button>
        <label
          style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 12.5, color: "var(--tx2)", cursor: "pointer" }}
        >
          <input type="checkbox" checked={run} onChange={(e) => setRun(e.target.checked)} />
          start immediately
        </label>
        <span className="mono" style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--tx4)" }}>
          then nothing downstream is interactive by default
        </span>
      </div>
    </div>
  );
}
