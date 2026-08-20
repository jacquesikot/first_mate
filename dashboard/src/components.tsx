import { createContext, useContext, useState } from "react";
import type { ContextInfo, Question, StatusInfo, LivePayload } from "./api";
import { api } from "./api";
import { STATUS_GLYPH, ago, ctxColor, statusColor, tokens } from "./format";
import { Markdown } from "./markdown";

// ------------------------------------------------------------- app context

export interface AppState {
  status: StatusInfo | null;
  live: Record<string, LivePayload>;
  wsUp: boolean;
  refresh: () => void;
  toast: (msg: string, sub?: string, kind?: "ok" | "run" | "dim" | "bad") => void;
  go: (hash: string) => void;
}

export const AppCtx = createContext<AppState>({
  status: null,
  live: {},
  wsUp: false,
  refresh: () => {},
  toast: () => {},
  go: () => {},
});

export const useApp = () => useContext(AppCtx);

// ------------------------------------------------------------- primitives

export function Glyph({ status, animate }: { status: string; animate?: boolean }) {
  return (
    <span
      className={`mono${animate && status === "running" ? " pulse" : ""}`}
      style={{ color: statusColor(status), fontSize: 12, width: 12, display: "inline-block" }}
    >
      {STATUS_GLYPH[status] ?? "·"}
    </span>
  );
}

export function StateChip({ status }: { status: string }) {
  const attention = status === "blocked" || status === "validating";
  return (
    <span
      className="mono"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 7,
        fontSize: 10.5,
        padding: "2px 8px",
        borderRadius: 20,
        border: `1px solid ${attention ? "var(--acbd)" : "var(--bd2)"}`,
        background: status === "blocked" ? "var(--acbg)" : "var(--s3)",
        color: statusColor(status),
        textTransform: "uppercase",
        letterSpacing: "0.06em",
      }}
    >
      {status}
    </span>
  );
}

export function ContextMeter({
  ctx,
  width = 132,
  note,
}: {
  ctx: ContextInfo | null | undefined;
  width?: number | string;
  note?: boolean;
}) {
  const pct = ctx ? Math.min(100, ctx.percent) : 0;
  const color = ctx ? ctxColor(pct) : "var(--tx4)";
  const wallPct = ctx && ctx.limit ? Math.min(100, (100 * ctx.wall_tokens) / ctx.limit) : null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5, width }}>
      <div
        className="mono"
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: 10,
          letterSpacing: "0.06em",
          color: "var(--tx4)",
          textTransform: "uppercase",
        }}
      >
        <span>context</span>
        <span style={{ color }}>{ctx ? `${Math.round(pct)}%` : "—"}</span>
      </div>
      <div className="meter" style={{ position: "relative" }}>
        <div style={{ width: `${pct}%`, background: color }} />
        {wallPct != null && (
          <span
            style={{
              position: "absolute",
              top: -2,
              bottom: -2,
              left: `${wallPct}%`,
              width: 1,
              background: "var(--bd3)",
            }}
          />
        )}
      </div>
      {note && (
        <div className="mono dimmer" style={{ fontSize: 10 }}>
          {!ctx
            ? "no live session"
            : `${tokens(ctx.tokens)} · relay at ${tokens(ctx.wall_tokens)}`}
        </div>
      )}
    </div>
  );
}

export function Pips({ states }: { states: ("met" | "failed" | "pending")[] }) {
  return (
    <div className="pips">
      {states.map((s, i) => (
        <span
          key={i}
          style={{
            background:
              s === "met" ? "var(--ok)" : s === "failed" ? "var(--bad)" : "var(--s4)",
          }}
        />
      ))}
    </div>
  );
}

export function SectionHead({
  title,
  hint,
  accent,
}: {
  title: string;
  hint?: string;
  accent?: boolean;
}) {
  return (
    <div className="section-head">
      <span className={`section-title${accent ? " accent" : ""}`}>{title}</span>
      <span className={`section-rule${accent ? " accent" : ""}`} />
      {hint && <span className="section-hint">{hint}</span>}
    </div>
  );
}

export function CopyButton({ text, label }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      className="btn accent mono"
      style={{ fontSize: 12 }}
      onClick={() => {
        navigator.clipboard?.writeText(text).catch(() => {});
        setCopied(true);
        setTimeout(() => setCopied(false), 1600);
      }}
    >
      {copied ? "copied ✓" : label ?? text}
    </button>
  );
}

// ------------------------------------------------------------- questions

function evidenceEntries(evidence: Record<string, unknown>): [string, string][] {
  const out: [string, string][] = [];
  for (const [k, v] of Object.entries(evidence ?? {})) {
    if (v == null) continue;
    if (typeof v === "string" || typeof v === "number" || typeof v === "boolean") {
      const s = String(v);
      if (s.length <= 120 && !s.includes("\n")) out.push([k, s]);
    } else if (Array.isArray(v) && v.every((x) => typeof x === "string")) {
      out.push([k, (v as string[]).join(", ")]);
    } else if (k === "failing" && Array.isArray(v)) {
      const ids = v
        .map((x) => (typeof x === "object" && x ? String((x as any).id ?? "") : ""))
        .filter(Boolean);
      if (ids.length) out.push(["failing", ids.join(", ")]);
    }
  }
  return out;
}

function evidenceLong(evidence: Record<string, unknown>): [string, string][] {
  const out: [string, string][] = [];
  for (const [k, v] of Object.entries(evidence ?? {})) {
    if (typeof v === "string" && (v.includes("\n") || v.length > 120)) out.push([k, v]);
  }
  const failing = evidence?.failing;
  if (Array.isArray(failing)) {
    for (const f of failing) {
      if (typeof f === "object" && f) {
        const ff = f as any;
        const text = [ff.stdout_tail, ff.stderr_tail, ff.error].filter(Boolean).join("\n");
        if (text.trim()) out.push([`${ff.id ?? "check"} output`, text]);
      }
    }
  }
  return out;
}

export function QuestionCard({ q, taskGoal }: { q: Question; taskGoal?: string }) {
  const { refresh, toast, go } = useApp();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [showDetail, setShowDetail] = useState(false);
  const answered = q.status !== "open";
  const accent = q.type === "scope_change" || q.type === "decision";
  const chips = evidenceEntries(q.evidence);
  const long = evidenceLong(q.evidence);

  const submit = async (answer: string) => {
    if (!answer.trim() || busy) return;
    setBusy(true);
    try {
      const r = await api.answer(q.id, answer.trim());
      // A free-text answer can rewrite the contract; say so plainly, because
      // "recorded" would understate what just happened.
      if (r.replan?.applied) {
        toast(
          "Contract updated from your answer",
          `${r.replan.summary || "the plan was re-planned"}${
            r.resumed ? " · task resuming" : ""
          }`,
          "run"
        );
      } else if (r.replan && !r.replan.applied) {
        toast(
          "Answer recorded — the plan was not changed",
          r.replan.errors?.join("; ") ||
            r.replan.summary ||
            "no contract edit could be derived from it",
          "ok"
        );
      } else {
        toast(
          `Answer recorded · ${answer.trim()}`,
          `appended to the contract · ${q.id}${r.resumed ? " · task resuming" : ""}`,
          r.resumed ? "run" : "ok"
        );
      }
      refresh();
    } catch (e) {
      toast("Answer failed", String(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  return (
    <article
      className="card rise"
      style={{ borderLeft: `2px solid ${accent && !answered ? "var(--ac)" : "var(--bd3)"}` }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          padding: "10px 15px",
          borderBottom: "1px solid var(--bd)",
          background: "rgba(0,0,0,.18)",
          flexWrap: "wrap",
        }}
      >
        <span className={`chip${accent && !answered ? " accent" : ""}`}>
          {q.type.replace("_", " ")}
        </span>
        {q.urgency === "blocking" && !answered && (
          <span className="mono" style={{ fontSize: 10, color: "var(--ac)" }}>
            blocking
          </span>
        )}
        <button
          className="truncate"
          onClick={() => go(`#/task/${q.task_id}`)}
          style={{ fontSize: 13, color: "var(--tx2)", flex: "1 1 120px", textAlign: "left" }}
        >
          {taskGoal ?? q.task_id}
        </button>
        <span
          className="mono"
          style={{
            marginLeft: "auto",
            display: "flex",
            gap: 10,
            fontSize: 10.5,
            color: "var(--tx4)",
            flex: "0 0 auto",
          }}
        >
          <span>{q.id}</span>
          <span>{ago(q.asked_at)} ago</span>
        </span>
      </div>

      {!answered ? (
        <div style={{ padding: 15 }}>
          <Markdown
            text={q.question}
            className="question-body"
          />
          {chips.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 7, marginTop: 11 }}>
              {chips.map(([k, v]) => (
                <span key={k} className="evidence-chip" style={{ maxWidth: "100%" }}>
                  <span style={{ color: "var(--tx4)", flex: "0 0 auto" }}>{k}</span>
                  <span style={{ color: "var(--tx2)", overflowWrap: "anywhere" }}>{v}</span>
                </span>
              ))}
            </div>
          )}
          {long.length > 0 && (
            <div style={{ marginTop: 10 }}>
              <button
                className="mono dim"
                style={{ fontSize: 10.5, borderBottom: "1px dotted var(--bd3)" }}
                onClick={() => setShowDetail(!showDetail)}
              >
                {showDetail ? "hide evidence" : "show evidence"}
              </button>
              {showDetail &&
                long.map(([k, v]) => (
                  <div key={k} style={{ marginTop: 8 }}>
                    <div className="label" style={{ marginBottom: 4 }}>
                      {k}
                    </div>
                    <div className="terminal" style={{ padding: "8px 12px", maxHeight: 220, overflowY: "auto" }}>
                      {v}
                    </div>
                  </div>
                ))}
            </div>
          )}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginTop: 14 }}>
            {q.options.map((o) => (
              <button
                key={o}
                className={`btn${o === q.default || (!q.default && o === q.options[0]) ? " accent" : ""}`}
                style={{ padding: "8px 14px", fontWeight: 500, fontSize: 13 }}
                disabled={busy}
                onClick={() => submit(o)}
              >
                {o}
              </button>
            ))}
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              marginTop: 13,
              paddingTop: 12,
              borderTop: "1px solid var(--bd)",
            }}
          >
            <input
              className="line"
              placeholder="or answer in your own words — it becomes a binding amendment…"
              value={text}
              disabled={busy}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") submit(text);
              }}
            />
            <button
              className="mono"
              style={{ fontSize: 11.5, color: "var(--ac)", flex: "0 0 auto" }}
              disabled={busy}
              onClick={() => submit(text)}
            >
              send ↵
            </button>
          </div>
          <div
            className="mono dimmer"
            style={{ display: "flex", gap: 14, marginTop: 10, fontSize: 10.5, flexWrap: "wrap" }}
          >
            {q.default && <span>default → {q.default}</span>}
            <span>
              {q.type === "fyi"
                ? "non-blocking · recorded only"
                : "parked · consuming no worker slot"}
            </span>
            <span style={{ marginLeft: "auto", color: "var(--tx3)" }}>
              or: fm answer {q.id} "{q.options[0] ?? "…"}"
            </span>
          </div>
        </div>
      ) : (
        <div
          style={{
            padding: "14px 15px",
            display: "flex",
            alignItems: "center",
            gap: 11,
            background: "var(--okbg)",
          }}
        >
          <span className="mono" style={{ color: "var(--ok)", fontSize: 13 }}>
            ✓
          </span>
          <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <span style={{ fontSize: 13 }}>{q.answer ?? "noted"}</span>
            <span className="mono" style={{ fontSize: 10.5, color: "var(--tx3)" }}>
              {q.answered_from
                ? "reused your earlier answer — you were not asked again"
                : q.answered_by
                  ? `by ${q.answered_by}`
                  : q.status}{" "}
              · {ago(q.answered_at ?? q.asked_at)} ago
            </span>
          </div>
        </div>
      )}
    </article>
  );
}
