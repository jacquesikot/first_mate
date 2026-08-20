import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type {
  DiffInfo, TaskDetail, StepState, CriterionResult, LivePayload, Gate, GateState,
  CleanupCandidate, Task,
} from "../api";
import { api, socket } from "../api";
import {
  useApp,
  ContextMeter,
  CopyButton,
  QuestionCard,
  StateChip,
} from "../components";
import { ago, splitPath, statusColor, tokens } from "../format";
import { Markdown } from "../markdown";
import { ScopingPanel } from "./Scoping";

type Tab = "scoping" | "steps" | "contract" | "changes" | "output" | "questions";

export function TaskDetailView({ taskId }: { taskId: string }) {
  const { live, toast, refresh, go } = useApp();
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab | null>(null);
  const timer = useRef<number | null>(null);

  const load = useCallback(() => {
    api
      .task(taskId)
      .then((d) => {
        setDetail(d);
        setError(null);
      })
      .catch((e) => setError(String(e)));
  }, [taskId]);

  useEffect(() => {
    load();
    const unsub = socket.subscribe((msg) => {
      if ("event" in msg && msg.task_id === taskId) {
        if (timer.current != null) return;
        timer.current = window.setTimeout(() => {
          timer.current = null;
          load();
        }, 300);
      }
    });
    return () => {
      unsub();
      if (timer.current != null) window.clearTimeout(timer.current);
    };
  }, [taskId, load]);

  if (error)
    return (
      <div style={{ padding: 22 }}>
        <div className="empty">task {taskId}: {error}</div>
      </div>
    );
  if (!detail) return <div style={{ padding: 22 }} className="dim">loading…</div>;

  const { task, contract, questions, attach, scoping } = detail;
  // While the task is being scoped, the conversation IS the task view.
  const scopingNow = task.status === "scoping" && scoping != null;
  // A live payload is only meaningful while the task actually has a live
  // session — otherwise it's a leftover from the previous generation.
  const isLive = task.status === "running" || task.status === "validating";
  const lv = isLive ? live[taskId] : undefined;
  const ctx = lv?.context ?? (isLive ? detail.context : null);
  const openQs = questions.filter((q) => q.status === "open" && q.type !== "fyi");
  const canRun = ["ready", "paused", "blocked", "failed"].includes(task.status) && !openQs.length;

  const act = async (name: "run" | "pause" | "abandon") => {
    if (name === "abandon" && !window.confirm(`Abandon ${task.id}? The worktree is kept.`))
      return;
    try {
      await (name === "run" ? api.run(taskId) : name === "pause" ? api.pause(taskId) : api.abandon(taskId));
      toast(
        name === "run" ? "Task starting" : name === "pause" ? "Pause requested" : "Task abandoned",
        undefined,
        name === "run" ? "run" : "dim"
      );
      load();
      refresh();
    } catch (e) {
      toast(`${name} failed`, String(e), "bad");
    }
  };

  const tabs: [Tab, string, string][] = scopingNow
    ? [["scoping", "Scoping", String(scoping!.messages.length)]]
    : [
        ["steps", "Steps", String(task.steps.length)],
        ["contract", "Contract", contract?.criteria.length ? String(contract.criteria.length) : ""],
        ["changes", "Changes", ""],
        ["output", "Output", lv ? "live" : ""],
        ["questions", "Questions", String(questions.length)],
      ];
  // Default tab follows the phase: scoping first while scoping, steps after.
  const active: Tab =
    tab && tabs.some(([k]) => k === tab) ? tab : scopingNow ? "scoping" : "steps";

  return (
    <div style={{ paddingBottom: 60 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          flexWrap: "wrap",
          padding: "14px 22px",
          borderBottom: "1px solid var(--bd)",
          background: "var(--s1)",
        }}
      >
        <button className="mono dim" style={{ fontSize: 11.5 }} onClick={() => go("#/tasks")}>
          ← tasks
        </button>
        <span className="mono dimmer" style={{ fontSize: 11 }}>
          /
        </span>
        <span className="truncate" style={{ fontSize: 14, fontWeight: 500, flex: "1 1 200px" }}>
          {task.goal}
        </span>
        <StateChip status={task.status} />
        <div style={{ marginLeft: "auto", display: "flex", gap: 8, flexWrap: "wrap" }}>
          {!scopingNow &&
            (task.status === "running" || detail.running ? (
              <button className="btn" onClick={() => act("pause")}>
                Pause
              </button>
            ) : (
              <button className="btn" disabled={!canRun} onClick={() => act("run")}>
                Run
              </button>
            ))}
          <button className="btn danger" onClick={() => act("abandon")}>
            Abandon
          </button>
          {attach && <CopyButton text={attach} label={`fm attach ${task.id}`} />}
        </div>
      </div>

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 20,
          padding: "20px 22px",
          alignItems: "flex-start",
        }}
      >
        <div style={{ flex: "999 1 430px", minWidth: 0, display: "flex", flexDirection: "column", gap: 16 }}>
          {tabs.length > 1 && (
            <div className="tabs scroll-x">
              {tabs.map(([key, label, count]) => (
                <button
                  key={key}
                  className={`tab${active === key ? " on" : ""}`}
                  onClick={() => setTab(key)}
                >
                  <span>{label}</span>
                  {count && <span className="count">{count}</span>}
                </button>
              ))}
            </div>
          )}

          {active === "scoping" && (
            <ScopingPanel
              chat={scoping!}
              onApproved={() => {
                setTab("steps");
                load();
              }}
            />
          )}
          {active === "steps" && <StepsTab detail={detail} />}
          {active === "contract" && <ContractTab detail={detail} onSaved={load} />}
          {active === "changes" && <ChangesTab taskId={taskId} />}
          {active === "output" && <OutputTab detail={detail} lv={lv} />}
          {active === "questions" && (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {[...questions].reverse().map((q) => (
                <QuestionCard key={q.id} q={q} taskGoal={task.goal} />
              ))}
              {questions.length === 0 && <div className="empty">No questions asked yet.</div>}
            </div>
          )}
        </div>

        <Sidebar detail={detail} ctxOverride={ctx} />
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ steps

function latestResults(detail: TaskDetail): Record<string, CriterionResult> {
  const out: Record<string, CriterionResult> = {};
  for (const [key, rec] of Object.entries(detail.validations)) {
    if (key === "__task__") continue;
    for (const r of rec.results) out[r.id] = r;
  }
  const taskRec = detail.validations["__task__"];
  if (taskRec) for (const r of taskRec.results) out[r.id] = r;
  return out;
}

/** What First Mate is waiting for, and how long it has been at it.
 *  Waiting is deliberately shown as progress rather than as a problem —
 *  the task holds no session open while this is on screen. */
function GateWait({ gate, state }: { gate: Gate; state: GateState | null }) {
  const last = state?.diagnoses?.length
    ? state.diagnoses[state.diagnoses.length - 1]
    : null;
  const started = state?.first_probe_at ? new Date(state.first_probe_at).getTime() : null;
  const elapsed = started ? Math.max(0, Date.now() - started) / 1000 : 0;
  const pct = gate.ceiling > 0 ? Math.min(100, (elapsed / gate.ceiling) * 100) : 0;
  const mins = (n: number) => `${Math.floor(n / 60)}m`;
  return (
    <div
      style={{
        border: "1px solid var(--bd2)",
        borderLeft: "2px solid var(--bd)",
        borderRadius: 8,
        padding: "10px 12px",
        display: "flex",
        flexDirection: "column",
        gap: 7,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 9, flexWrap: "wrap" }}>
        <span
          className="mono"
          style={{
            fontSize: 10,
            letterSpacing: "0.08em",
            textTransform: "uppercase",
            color: "var(--tx3)",
          }}
        >
          waiting
        </span>
        <span style={{ fontSize: 12.5, color: "var(--tx2)" }}>
          for {gate.description || "a precondition"}
        </span>
        <span className="mono" style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--tx4)" }}>
          {mins(elapsed)} of {mins(gate.ceiling)} · {state?.probes ?? 0} checks · every{" "}
          {gate.interval}s
        </span>
      </div>
      <div style={{ height: 3, background: "var(--bd2)", borderRadius: 2, overflow: "hidden" }}>
        <div style={{ width: `${pct}%`, height: "100%", background: "var(--tx3)" }} />
      </div>
      <div className="mono" style={{ fontSize: 10.5, color: "var(--tx4)", wordBreak: "break-all" }}>
        {gate.command}
      </div>
      <div className="mono" style={{ fontSize: 10.5, color: "var(--tx4)" }}>
        no session running — this wait consumes no context and no worker slot
      </div>
      {last && (
        <div
          style={{
            borderTop: "1px solid var(--bd2)",
            paddingTop: 7,
            display: "flex",
            flexDirection: "column",
            gap: 4,
          }}
        >
          <div className="mono" style={{ fontSize: 10, letterSpacing: "0.08em", textTransform: "uppercase", color: "var(--tx3)" }}>
            supervisor checked this{state && state.supervisions > 1 ? ` ${state.supervisions}×` : ""} — {verdictLabel(last.verdict)}
          </div>
          <div style={{ fontSize: 12, color: "var(--tx2)", maxWidth: "80ch" }}>
            {last.findings}
          </div>
        </div>
      )}
    </div>
  );
}

function verdictLabel(v: string): string {
  if (v === "gate_wrong") return "the check itself was wrong";
  if (v === "still_waiting") return "genuinely still waiting";
  return "could not tell";
}

function GenerationRail({ step, wall }: { step: StepState; wall: number }) {
  if (!step.sessions.length) return null;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 7 }}>
      {step.sessions.map((s, i) => {
        const liveNow = s.ended_at === null;
        const pct = wall ? Math.min(100, Math.round((100 * s.peak_tokens) / wall)) : 0;
        const label = `gen ${s.generation} · ${
          s.peak_tokens ? `${tokens(s.peak_tokens)}` : "…"
        }${liveNow ? " · live" : s.outcome ? ` · ${s.outcome}` : ""}`;
        return (
          <div key={i} style={{ display: "contents" }}>
            {i > 0 && (
              <span className="mono" style={{ fontSize: 11, color: "var(--tx4)", flex: "0 0 auto" }}>
                →
              </span>
            )}
            <div style={{ flex: Math.max(pct, 18), minWidth: 0 }}>
              <div
                style={{
                  height: 22,
                  borderRadius: 5,
                  border: `1px solid ${liveNow ? "var(--acbd)" : "var(--bd2)"}`,
                  background: "var(--s3)",
                  overflow: "hidden",
                  position: "relative",
                }}
              >
                <div
                  style={{
                    position: "absolute",
                    inset: "0 auto 0 0",
                    width: `${pct}%`,
                    background: liveNow
                      ? "linear-gradient(90deg,rgba(242,168,59,.30),rgba(242,168,59,.16))"
                      : "linear-gradient(90deg,#2a2a33,#33333d)",
                    transition: "width .9s linear",
                  }}
                />
                <div
                  className="mono"
                  style={{
                    position: "absolute",
                    inset: 0,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 10.5,
                    color: liveNow ? "var(--tx)" : "var(--tx3)",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                  }}
                >
                  {label}
                </div>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function StepsTab({ detail }: { detail: TaskDetail }) {
  const { task, contract, handoffs } = detail;
  const [openHandoff, setOpenHandoff] = useState<string | null>(null);
  const results = latestResults(detail);
  const wall = detail.context?.wall_tokens ?? 150_000;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
        {task.steps.map((st, i) => {
          const spec = contract?.steps.find((s) => s.id === st.id);
          const handoff = handoffs[st.id];
          const isOpen = openHandoff === st.id;
          return (
            <div key={st.id} className="card">
              <div style={{ display: "flex", alignItems: "center", gap: 11, padding: "12px 15px" }}>
                <span className="mono" style={{ fontSize: 11, color: "var(--tx4)", width: 16 }}>
                  {String(i + 1).padStart(2, "0")}
                </span>
                <span className="mono" style={{ fontSize: 13.5, fontWeight: 500 }}>
                  {spec?.title || st.id}
                </span>
                {spec?.skill && <span className="chip">{spec.skill}</span>}
                {spec?.when && (
                  <span className="chip" title={spec.when.command}>
                    waits for {spec.when.description || "a precondition"}
                  </span>
                )}
                {spec?.on_failure && (
                  <span className="chip" title={`up to ${spec.on_failure.max_iterations} rounds`}>
                    ↻ {spec.on_failure.goto}
                    {st.iteration > 0
                      ? ` · round ${st.iteration}/${spec.on_failure.max_iterations}`
                      : ""}
                  </span>
                )}
                <span
                  className="mono"
                  style={{
                    marginLeft: "auto",
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    fontSize: 11,
                  }}
                >
                  <span style={{ color: "var(--tx4)" }}>
                    {st.sessions.length
                      ? `${st.sessions.length} session${st.sessions.length > 1 ? "s" : ""} · attempt ${st.attempt}`
                      : "not started"}
                  </span>
                  <span
                    style={{
                      color: statusColor(st.status),
                      textTransform: "uppercase",
                      letterSpacing: "0.05em",
                    }}
                  >
                    {st.status}
                  </span>
                </span>
              </div>
              {st.status === "waiting" && spec?.when && (
                <div style={{ padding: "0 15px 12px" }}>
                  <GateWait gate={spec.when} state={st.gate} />
                </div>
              )}
              {st.sessions.length > 0 && (
                <div style={{ padding: "0 15px 12px", display: "flex", flexDirection: "column", gap: 7 }}>
                  <GenerationRail step={st} wall={wall} />
                  {handoff && (
                    <div
                      style={{
                        border: "1px solid var(--bd2)",
                        borderLeft: "2px solid var(--acbd)",
                        borderRadius: 8,
                        background: "rgba(242,168,59,.04)",
                        overflow: "hidden",
                      }}
                    >
                      <button
                        style={{
                          width: "100%",
                          display: "flex",
                          alignItems: "center",
                          gap: 9,
                          padding: "9px 12px",
                          textAlign: "left",
                        }}
                        onClick={() => setOpenHandoff(isOpen ? null : st.id)}
                      >
                        <span
                          className="mono"
                          style={{
                            fontSize: 10,
                            letterSpacing: "0.08em",
                            textTransform: "uppercase",
                            color: "var(--ac)",
                          }}
                        >
                          handoff → gen {handoff.generation + 1}
                        </span>
                        <span className="mono" style={{ fontSize: 10.5, color: "var(--tx4)" }}>
                          {handoff.text.split(/\s+/).length} words · injected at session start
                        </span>
                        <span style={{ marginLeft: "auto", color: "var(--tx3)", fontSize: 10 }}>
                          {isOpen ? "▴" : "▾"}
                        </span>
                      </button>
                      {isOpen && (
                        <div
                          style={{
                            padding: "0 13px 12px",
                            fontSize: 12.5,
                            color: "var(--tx2)",
                            maxWidth: "78ch",
                            minWidth: 0,
                          }}
                        >
                          <Markdown text={handoff.text} />
                        </div>
                      )}
                    </div>
                  )}
                  {st.last_failure && (
                    <div style={{ fontSize: 12.5, color: "var(--bad)", paddingLeft: 2 }}>
                      last failure: {st.last_failure}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {contract && contract.criteria.length > 0 && (
        <div className="card-flat">
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "11px 15px",
              borderBottom: "1px solid var(--bd)",
              background: "rgba(0,0,0,.2)",
            }}
          >
            <span className="label" style={{ color: "var(--tx3)" }}>
              completion criteria
            </span>
            <span className="mono" style={{ marginLeft: "auto", fontSize: 11, color: "var(--tx4)" }}>
              {contract.criteria.filter((c) => results[c.id]?.passed).length}/{contract.criteria.length} met
            </span>
          </div>
          {contract.criteria.map((c) => {
            const r = results[c.id];
            const state = r == null ? "not run" : r.passed ? "met" : "failed";
            const color =
              state === "met" ? "var(--ok)" : state === "failed" ? "var(--bad)" : "var(--tx4)";
            return (
              <CriterionRow key={c.id} id={c.id} command={c.command} state={state} color={color} result={r} />
            );
          })}
        </div>
      )}
    </div>
  );
}

function CriterionRow({
  id,
  command,
  state,
  color,
  result,
}: {
  id: string;
  command: string;
  state: string;
  color: string;
  result?: CriterionResult;
}) {
  const [open, setOpen] = useState(false);
  const evidence = result ? [result.stdout, result.stderr, result.error].filter(Boolean).join("\n").trim() : "";
  return (
    <div style={{ borderBottom: "1px solid var(--bd)" }}>
      <div style={{ display: "flex", alignItems: "flex-start", gap: 12, padding: "12px 15px" }}>
        <span className="mono" style={{ fontSize: 12, color, width: 12, paddingTop: 1 }}>
          {state === "met" ? "✓" : state === "failed" ? "✗" : "○"}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13.5, lineHeight: 1.45 }}>{id}</div>
          <div
            className="mono"
            style={{ display: "flex", gap: 9, marginTop: 5, fontSize: 11, color: "var(--tx4)", flexWrap: "wrap" }}
          >
            <span className="chip">shell</span>
            <span style={{ paddingTop: 2 }}>{command}</span>
          </div>
        </div>
        <div style={{ flex: "0 0 auto", textAlign: "right", display: "flex", flexDirection: "column", gap: 4, alignItems: "flex-end" }}>
          <span className="mono" style={{ fontSize: 11, color }}>
            {state}
            {result?.exit_status != null ? ` · exit ${result.exit_status}` : ""}
          </span>
          {evidence && (
            <button
              className="mono dim"
              style={{ fontSize: 10.5, borderBottom: "1px dotted var(--bd3)" }}
              onClick={() => setOpen(!open)}
            >
              {open ? "hide evidence" : "view evidence"}
            </button>
          )}
        </div>
      </div>
      {open && evidence && (
        <div style={{ padding: "0 15px 12px 39px" }}>
          <div className="terminal" style={{ padding: "8px 12px", maxHeight: 260, overflowY: "auto" }}>
            {evidence}
          </div>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------- contract

function ContractTab({ detail, onSaved }: { detail: TaskDetail; onSaved: () => void }) {
  const { toast } = useApp();
  const { contract, running, task } = detail;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  if (!contract) return <div className="empty">No contract on disk.</div>;

  const editable = !running && !["done", "failed", "abandoned"].includes(task.status);

  const save = async () => {
    setBusy(true);
    try {
      const parsed = JSON.parse(draft);
      await api.editContract(task.id, parsed);
      toast("Contract saved", "step state reconciled · takes effect next run", "ok");
      setEditing(false);
      onSaved();
    } catch (e) {
      toast("Contract rejected", String(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  const rows: [string, ReactNode][] = [
    ["goal", <span style={{ fontSize: 13.5 }}>{contract.goal}</span>],
    [
      "in scope",
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        {contract.scope_in.map((g) => (
          <span key={g} className="mono" style={{ fontSize: 12, color: "var(--tx2)" }}>
            {g}
          </span>
        ))}
      </div>,
    ],
    [
      "out of scope",
      contract.scope_out.length ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {contract.scope_out.map((g) => (
            <span
              key={g}
              className="mono"
              style={{ fontSize: 12, color: "var(--tx2)", overflowWrap: "anywhere" }}
            >
              {g}
            </span>
          ))}
        </div>
      ) : (
        <span style={{ fontSize: 13, color: "var(--tx3)" }}>
          everything else — the guard blocks the write and the agent must raise a scope_change
          question
        </span>
      ),
    ],
    [
      "steps",
      <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        {contract.steps.map((s, i) => (
          <div
            key={s.id}
            style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap", minWidth: 0 }}
          >
            <span className="mono" style={{ fontSize: 11, color: "var(--tx4)" }}>
              {String(i + 1).padStart(2, "0")}
            </span>
            <span className="mono" style={{ fontSize: 12.5 }}>
              {s.id}
            </span>
            {s.skill && <span className="chip">{s.skill}</span>}
            {s.criteria.length > 0 && (
              <span className="mono" style={{ fontSize: 11, color: "var(--tx4)" }}>
                criteria: {s.criteria.join(", ")}
              </span>
            )}
          </div>
        ))}
      </div>,
    ],
    [
      "criteria",
      <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
        {contract.criteria.map((c) => (
          <div
            key={c.id}
            className="mono"
            style={{ fontSize: 12, color: "var(--tx2)", overflowWrap: "anywhere" }}
          >
            <span style={{ color: "var(--tx3)" }}>{c.id}</span> · {c.command}
          </div>
        ))}
      </div>,
    ],
  ];
  if (Object.keys(contract.tripwires ?? {}).length || contract.tripwire_allow.length) {
    rows.push([
      "tripwires",
      <div className="mono" style={{ fontSize: 12, color: "var(--tx2)", display: "flex", flexDirection: "column", gap: 4 }}>
        {Object.entries(contract.tripwires).map(([k, v]) => (
          <span key={k}>
            {k} = {String(v)}
          </span>
        ))}
        {contract.tripwire_allow.length > 0 && (
          <span style={{ color: "var(--tx3)" }}>exempt: {contract.tripwire_allow.join(", ")}</span>
        )}
      </div>,
    ]);
  }
  if (contract.context) {
    rows.push([
      "known context",
      <Markdown text={contract.context} className="md-tight" />,
    ]);
  }
  if (contract.amendments.length) {
    rows.push([
      "amendments",
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {contract.amendments.map((a, i) => (
          <div key={i} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <span style={{ fontSize: 12.5, color: "var(--tx2)" }}>{a.question}</span>
            <span className="mono" style={{ fontSize: 11.5, color: "var(--ac)" }}>
              → {a.answer}{" "}
              <span style={{ color: "var(--tx4)" }}>
                · {a.by} · {ago(a.at)} ago
              </span>
            </span>
          </div>
        ))}
      </div>,
    ]);
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 11,
          padding: "11px 14px",
          borderRadius: "var(--rad)",
          border: "1px solid var(--bd2)",
          background: "var(--s2)",
        }}
      >
        <span className="mono" style={{ fontSize: 11, color: "var(--tx3)" }}>
          {editable
            ? "editable — no live session"
            : running
              ? "locked while a session is live · answers still amend it"
              : `read-only — task is ${task.status}`}
        </span>
        {editable && !editing && (
          <button
            className="btn"
            style={{ marginLeft: "auto" }}
            onClick={() => {
              setDraft(JSON.stringify(contract, null, 2));
              setEditing(true);
            }}
          >
            Edit contract
          </button>
        )}
        {editing && (
          <span style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
            <button className="btn" onClick={() => setEditing(false)} disabled={busy}>
              Cancel
            </button>
            <button className="btn accent" onClick={save} disabled={busy}>
              Save
            </button>
          </span>
        )}
      </div>

      {editing ? (
        <textarea
          className="editor"
          style={{ minHeight: 420 }}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          spellCheck={false}
        />
      ) : (
        <div className="card-flat">
          {rows.map(([k, node]) => (
            <div key={k} style={{ display: "flex", gap: 16, padding: "13px 16px", borderBottom: "1px solid var(--bd)" }}>
              <span className="label" style={{ flex: "0 0 118px", paddingTop: 2 }}>
                {k}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>{node}</div>
            </div>
          ))}
        </div>
      )}

      <div className="mono dimmer" style={{ display: "flex", gap: 10, fontSize: 10.5 }}>
        <span>~/.firstmate/tasks/{task.id}/contract.md</span>
        <span>·</span>
        <span>
          {contract.amendments.length} amendment{contract.amendments.length === 1 ? "" : "s"} from
          answered questions
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- changes

function ChangesTab({ taskId }: { taskId: string }) {
  const [info, setInfo] = useState<DiffInfo | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [diffText, setDiffText] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .diff(taskId)
      .then((d) => {
        setInfo(d);
        if (d.files.length && !selected) setSelected(d.files[0].path);
      })
      .catch((e) => setError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  useEffect(() => {
    if (!selected) return;
    api
      .diffFile(taskId, selected)
      .then((d) => setDiffText(d.diff))
      .catch((e) => setDiffText(`(diff unavailable: ${e})`));
  }, [taskId, selected]);

  if (error) return <div className="empty">{error}</div>;
  if (!info) return <div className="dim">loading…</div>;
  if (!info.files.length)
    return <div className="empty">No changes in the worktree yet.</div>;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div
        className="mono"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16,
          padding: "11px 15px",
          borderRadius: "var(--rad)",
          border: "1px solid var(--bd)",
          background: "var(--s2)",
          fontSize: 11.5,
        }}
      >
        <span style={{ color: "var(--tx3)" }}>
          {info.files.length} file{info.files.length > 1 ? "s" : ""}
        </span>
        <span style={{ color: "var(--ok)" }}>+{info.added}</span>
        <span style={{ color: "var(--bad)" }}>−{info.deleted}</span>
        <span style={{ marginLeft: "auto", color: "var(--tx4)" }}>{info.branch}</span>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "flex-start" }}>
        <div
          className="card-flat"
          style={{ flex: "1 1 260px", minWidth: 0, maxHeight: "70vh", overflowY: "auto" }}
        >
          {info.files.map((f) => {
            const { dir, name } = splitPath(f.path);
            return (
              <button
                key={f.path}
                className="row-btn"
                style={{
                  minWidth: 0,
                  maxWidth: "100%",
                  padding: "10px 12px",
                  borderBottom: "1px solid var(--bd)",
                  background: selected === f.path ? "var(--s3)" : "transparent",
                  display: "flex",
                  flexDirection: "column",
                  gap: 3,
                }}
                onClick={() => setSelected(f.path)}
              >
                <div
                  style={{
                    display: "flex",
                    alignItems: "baseline",
                    gap: 6,
                    minWidth: 0,
                    maxWidth: "100%",
                    flexWrap: "wrap",
                  }}
                  title={f.path}
                >
                  {dir && (
                    <span
                      className="mono truncate"
                      style={{ fontSize: 11, color: "var(--tx4)", maxWidth: "100%" }}
                    >
                      {dir}
                    </span>
                  )}
                  <span
                    className="mono"
                    style={{
                      fontSize: 12,
                      fontWeight: 500,
                      minWidth: 0,
                      overflowWrap: "anywhere",
                    }}
                  >
                    {name}
                  </span>
                </div>
                <div className="mono" style={{ display: "flex", gap: 9, fontSize: 10.5 }}>
                  <span style={{ color: "var(--ok)" }}>
                    +{f.added ?? "—"}
                  </span>
                  <span style={{ color: f.deleted ? "var(--bad)" : "var(--tx4)" }}>
                    −{f.deleted ?? "—"}
                  </span>
                  {f.untracked && <span style={{ color: "var(--tx3)" }}>new</span>}
                </div>
              </button>
            );
          })}
        </div>
        <div
          style={{
            flex: "999 1 420px",
            minWidth: 0,
            border: "1px solid var(--bd)",
            borderRadius: "var(--rad)",
            background: "#07070a",
            overflow: "hidden",
          }}
        >
          <div
            className="mono"
            style={{
              padding: "9px 13px",
              borderBottom: "1px solid var(--bd)",
              fontSize: 11,
              color: "var(--tx3)",
              background: "var(--s1)",
              overflowWrap: "anywhere",
            }}
          >
            {selected}
          </div>
          <div className="scroll-x" style={{ padding: "8px 0", maxHeight: "70vh", overflowY: "auto" }}>
            <DiffLines text={diffText} />
          </div>
        </div>
      </div>
    </div>
  );
}

function DiffLines({ text }: { text: string }) {
  const lines = text.split("\n");
  // min-width: max-content so the +/- background stripes span the full
  // scrolled width instead of stopping at the visible edge.
  return (
    <div style={{ minWidth: "max-content" }}>
      {lines.map((l, i) => {
        const kind = l.startsWith("+++") || l.startsWith("---")
          ? "meta"
          : l.startsWith("@@")
            ? "hunk"
            : l.startsWith("+")
              ? "add"
              : l.startsWith("-")
                ? "del"
                : "ctx";
        const bg =
          kind === "add"
            ? "rgba(107,179,137,.07)"
            : kind === "del"
              ? "rgba(207,107,96,.07)"
              : "transparent";
        const color =
          kind === "add"
            ? "#b8d9c4"
            : kind === "del"
              ? "#d9b3ae"
              : kind === "hunk"
                ? "var(--ac)"
                : kind === "meta"
                  ? "var(--tx4)"
                  : "var(--tx2)";
        return (
          <div
            key={i}
            className="mono"
            style={{
              fontSize: 11.5,
              lineHeight: 1.65,
              background: bg,
              color,
              whiteSpace: "pre",
              padding: "0 13px",
            }}
          >
            {l || " "}
          </div>
        );
      })}
    </div>
  );
}

// ----------------------------------------------------------------- output

function OutputTab({ detail, lv }: { detail: TaskDetail; lv?: LivePayload }) {
  const [fallback, setFallback] = useState<string | null>(null);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!lv) {
      api
        .output(detail.task.id)
        .then((d) => setFallback(d.output))
        .catch(() => {});
    }
  }, [detail.task.id, lv]);

  useEffect(() => {
    const el = scroller.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lv?.output, fallback]);

  const text = lv?.output ?? fallback;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div
        className="mono"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "10px 15px",
          borderRadius: "var(--rad)",
          border: "1px solid var(--bd)",
          background: "var(--s2)",
          fontSize: 11,
        }}
      >
        <span
          className={lv ? "pulse" : ""}
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: lv ? "var(--ok)" : "var(--s4)",
          }}
        />
        <span style={{ color: "var(--tx2)" }}>
          {lv
            ? `gen ${lv.generation} · ${lv.session_id.slice(0, 12)}… · pane capture`
            : "no live session"}
        </span>
        <span style={{ marginLeft: "auto", color: "var(--tx4)" }}>
          read-only · tmux owns the terminal
        </span>
      </div>
      <div
        ref={scroller}
        className="terminal"
        style={{ padding: "14px 16px", minHeight: 340, maxHeight: 560, overflowY: "auto" }}
      >
        {text || "(no output captured)"}
        {lv && (
          <span
            style={{
              display: "inline-block",
              width: 7,
              height: 14,
              background: "var(--ac)",
              verticalAlign: "text-bottom",
              marginLeft: 2,
              animation: "fmblink 1.1s step-end infinite",
            }}
          />
        )}
      </div>
      {detail.attach && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "12px 15px",
            borderRadius: "var(--rad)",
            border: "1px dashed var(--bd2)",
            background: "var(--s1)",
          }}
        >
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <span style={{ fontSize: 12.5, color: "var(--tx2)" }}>Need to drive it yourself?</span>
            <span className="mono" style={{ fontSize: 11, color: "var(--tx4)" }}>
              attaches to the live tmux window — the session keeps running when you detach
            </span>
          </div>
          <span style={{ marginLeft: "auto" }}>
            <CopyButton text={detail.attach} label={`fm attach ${detail.task.id}`} />
          </span>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- sidebar

function Sidebar({ detail, ctxOverride }: { detail: TaskDetail; ctxOverride: TaskDetail["context"] }) {
  const { task, questions } = detail;
  if (task.status === "scoping") return <ScopingSidebar detail={detail} />;
  const openQ = questions.filter((q) => q.status === "open" && q.type !== "fyi");
  const liveSession = task.steps
    .flatMap((s) => s.sessions)
    .find((s) => s.ended_at === null);
  const totalSessions = task.steps.reduce((n, s) => n + s.sessions.length, 0);
  const peak = task.steps.reduce(
    (n, s) => n + s.sessions.reduce((m, x) => m + x.peak_tokens, 0),
    0
  );

  return (
    <div style={{ flex: "1 1 300px", minWidth: 0, display: "flex", flexDirection: "column", gap: 12 }}>
      {openQ.map((q) => (
        <QuestionCard key={q.id} q={q} taskGoal={task.goal} />
      ))}

      <div className="card-flat">
        <div
          style={{
            padding: "10px 14px",
            borderBottom: "1px solid var(--bd)",
            display: "flex",
            alignItems: "center",
            gap: 9,
          }}
        >
          <span className="label" style={{ color: "var(--tx3)" }}>
            session
          </span>
          <span className="mono" style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--tx4)" }}>
            {task.id}
          </span>
        </div>
        <div style={{ padding: "13px 14px", display: "flex", flexDirection: "column", gap: 12 }}>
          <Meta k="state" v={task.status} color={statusColor(task.status)} sub={task.current_step ?? undefined} />
          <Meta
            k="started from"
            v={task.base || "HEAD"}
            color="var(--tx2)"
            sub={task.base_sha ? task.base_sha.slice(0, 10) : undefined}
          />
          <Meta
            k="sessions"
            v={`${totalSessions} total${liveSession ? ` · gen ${liveSession.generation} live` : ""}`}
            color="var(--tx)"
          />
          {liveSession && (
            <Meta k="session id" v={`${liveSession.session_id.slice(0, 14)}…`} color="var(--tx2)" sub="pinned at spawn" />
          )}
          <Meta k="tokens" v={`${tokens(peak)} across sessions`} color="var(--tx2)" />
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 6,
              paddingTop: 11,
              borderTop: "1px solid var(--bd)",
            }}
          >
            <ContextMeter ctx={ctxOverride} width="100%" note />
          </div>
        </div>
      </div>

      <div className="card-flat" style={{ padding: "13px 14px", display: "flex", flexDirection: "column", gap: 9 }}>
        <span className="label" style={{ color: "var(--tx3)" }}>
          worktree
        </span>
        <div className="mono" style={{ fontSize: 11.5, lineHeight: 1.6, wordBreak: "break-all", color: "var(--tx2)" }}>
          {task.worktree || "(created on first run)"}
        </div>
        <div
          className="mono"
          style={{ display: "flex", flexWrap: "wrap", gap: 8, fontSize: 10.5, color: "var(--tx4)" }}
        >
          <span>{task.base || "HEAD"}</span>
          <span>→</span>
          <span style={{ color: "var(--tx3)" }}>{task.branch}</span>
        </div>
        {task.worktree && <Reclaim task={task} />}
      </div>
    </div>
  );
}

/** Nothing has run yet: what the operator needs is the repo, the branch the
 * work will land on, and what happens on approval. */
function ScopingSidebar({ detail }: { detail: TaskDetail }) {
  const { task, scoping } = detail;
  return (
    <div style={{ flex: "1 1 300px", minWidth: 0, display: "flex", flexDirection: "column", gap: 12 }}>
      <div className="card-flat">
        <div
          style={{
            padding: "10px 14px",
            borderBottom: "1px solid var(--bd)",
            display: "flex",
            alignItems: "center",
            gap: 9,
          }}
        >
          <span className="label" style={{ color: "var(--tx3)" }}>
            session
          </span>
          <span className="mono truncate" style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--tx4)" }}>
            {task.id}
          </span>
        </div>
        <div style={{ padding: "13px 14px", display: "flex", flexDirection: "column", gap: 12 }}>
          <Meta k="state" v="scoping" color="var(--ac)" sub="agreeing what done means" />
          <Meta k="repository" v={splitPath(task.repo).name} color="var(--tx)" sub={task.repo} />
          <Meta
            k="started from"
            v={task.base || "HEAD"}
            color="var(--ac)"
            sub={task.base_sha ? task.base_sha.slice(0, 10) : undefined}
          />
          <Meta k="branch" v={task.branch} color="var(--tx2)" sub="cut, nothing committed yet" />
          {scoping?.model && <Meta k="model" v={scoping.model} color="var(--tx2)" />}
        </div>
      </div>
      <div
        className="card-flat"
        style={{ padding: "13px 14px", display: "flex", flexDirection: "column", gap: 8 }}
      >
        <span className="label" style={{ color: "var(--tx3)" }}>
          what happens next
        </span>
        <div style={{ fontSize: 12.5, lineHeight: 1.6, color: "var(--tx2)" }}>
          The worktree is already cut from{" "}
          <span className="mono" style={{ color: "var(--ac)", fontSize: 11.5 }}>
            {task.base || "HEAD"}
          </span>
          , so this conversation is reading that starting point — not your working copy.
          Approving the contract spawns the first worker there and validates every criterion
          mechanically. Nothing runs until you approve.
        </div>
      </div>
      {task.worktree && (
        <div
          className="card-flat"
          style={{ padding: "13px 14px", display: "flex", flexDirection: "column", gap: 9 }}
        >
          <span className="label" style={{ color: "var(--tx3)" }}>
            worktree
          </span>
          <div
            className="mono"
            style={{ fontSize: 11.5, lineHeight: 1.6, wordBreak: "break-all", color: "var(--tx2)" }}
          >
            {task.worktree}
          </div>
        </div>
      )}
    </div>
  );
}

/** Reclaim a finished task's disk. Deliberately explicit: the numbers are
 *  shown before anything is offered, removal is refused while work exists
 *  only here, and dropping dependencies is presented as the safe option
 *  because it is — an install rebuilds them. */
function Reclaim({ task }: { task: Task }) {
  const { refresh, toast } = useApp();
  const [cand, setCand] = useState<CleanupCandidate | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .cleanupReport()
      .then((r) => {
        if (alive) setCand(r.candidates.find((c) => c.task_id === task.id) ?? null);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [task.id, task.status, task.worktree]);

  if (!cand || !cand.bytes) return null;

  const act = async (mode: "worktree" | "deps", force = false) => {
    if (busy) return;
    if (mode === "worktree") {
      const warn = force
        ? `Remove this worktree AND lose work that exists nowhere else?\n\n${cand.blockers.join("\n")}`
        : `Remove ${cand.size} at ${cand.worktree}?`;
      if (!window.confirm(warn)) return;
    }
    setBusy(true);
    try {
      const r = await api.cleanupTask(task.id, mode, force);
      toast(
        mode === "deps" ? `Dropped ${r.size} of dependencies` : `Freed ${r.size}`,
        mode === "deps"
          ? "the code and git history are untouched — an install rebuilds these"
          : "worktree and task branch removed",
        "ok"
      );
      refresh();
      setCand(null);
    } catch (e) {
      toast("Cleanup failed", String(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  const dep = cand.dep_bytes > 0;
  return (
    <div
      style={{
        borderTop: "1px solid var(--bd2)",
        paddingTop: 9,
        display: "flex",
        flexDirection: "column",
        gap: 7,
      }}
    >
      <span className="mono" style={{ fontSize: 10.5, color: "var(--tx3)" }}>
        using {cand.size}
        {dep ? ` · ${humanBytes(cand.dep_bytes)} of it rebuildable` : ""}
      </span>
      {!cand.safe && (
        <span style={{ fontSize: 11.5, color: "var(--tx3)", overflowWrap: "anywhere" }}>
          keeping it: {cand.blockers.join("; ")}
        </span>
      )}
      <div style={{ display: "flex", gap: 7, flexWrap: "wrap" }}>
        {dep && (
          <button className="btn" disabled={busy} onClick={() => act("deps")}>
            drop dependencies
          </button>
        )}
        {cand.safe ? (
          <button className="btn" disabled={busy} onClick={() => act("worktree")}>
            remove worktree
          </button>
        ) : (
          <button
            className="btn"
            disabled={busy}
            onClick={() => act("worktree", true)}
            title="Destroys the uncommitted or unpushed work listed above"
          >
            remove anyway
          </button>
        )}
      </div>
    </div>
  );
}

function humanBytes(n: number): string {
  let size = n;
  for (const unit of ["B", "KB", "MB"]) {
    if (size < 1024) return unit === "B" ? `${Math.round(size)}B` : `${size.toFixed(1)}${unit}`;
    size /= 1024;
  }
  return `${size.toFixed(1)}GB`;
}

function Meta({ k, v, color, sub }: { k: string; v: string; color: string; sub?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <span className="label">{k}</span>
      <span className="mono" style={{ fontSize: 12.5, color, overflowWrap: "anywhere" }}>
        {v}
      </span>
      {sub && (
        <span style={{ fontSize: 11.5, color: "var(--tx3)", overflowWrap: "anywhere" }}>{sub}</span>
      )}
    </div>
  );
}
