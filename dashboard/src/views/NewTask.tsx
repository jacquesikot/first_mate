import { useEffect, useRef, useState } from "react";
import type { BrowseInfo, RepoSuggestion, ScopingChat } from "../api";
import { api } from "../api";
import { useApp, SectionHead } from "../components";
import { splitPath } from "../format";

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

/** New task = pick the repo, state the goal, then the scoping
 * conversation runs right here (daemon-mediated headless turns —
 * PRD §6.8 / open question §10.1 resolved). */
export function NewTaskView({ chatId }: { chatId?: string }) {
  return chatId ? <ScopingChatView chatId={chatId} /> : <StartForm />;
}

// ------------------------------------------------------------ start form

function StartForm() {
  const { toast, go } = useApp();
  const [goal, setGoal] = useState("");
  const [repo, setRepo] = useState("");
  const [suggestions, setSuggestions] = useState<RepoSuggestion[]>([]);
  const [browsing, setBrowsing] = useState(false);
  const [starting, setStarting] = useState(false);
  const [showPaste, setShowPaste] = useState(false);

  useEffect(() => {
    api.repos().then((d) => setSuggestions(d.repos)).catch(() => {});
  }, []);

  const start = async () => {
    if (!goal.trim() || !repo.trim() || starting) return;
    setStarting(true);
    try {
      const r = await api.scopeStart(goal.trim(), repo.trim());
      go(`#/new/${r.chat.id}`);
    } catch (e) {
      toast("Could not start scoping", String(e), "bad");
      setStarting(false);
    }
  };

  const { dir, name } = splitPath(repo);

  return (
    <div
      style={{
        padding: "22px 22px 60px",
        maxWidth: 760,
        display: "flex",
        flexDirection: "column",
        gap: 16,
      }}
    >
      {starting ? (
        <ThinkingCard goal={goal} repo={repo} first />
      ) : (
        <>
          <div style={{ fontSize: 13, color: "var(--tx2)", lineHeight: 1.6, maxWidth: "68ch" }}>
            Pick the repository the task builds on and say what you want done. First Mate reads
            the repo and project memory, then opens with a proposed scope, steps, and
            machine-checkable completion criteria for you to push back on.
          </div>

          <div className="card-flat" style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}>
            <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
              <span className="label">repository</span>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 9,
                  padding: "10px 12px",
                  borderRadius: 8,
                  border: `1px solid ${repo ? "var(--acbd)" : "var(--bd2)"}`,
                  background: "var(--s2)",
                }}
              >
                {repo ? (
                  <span className="mono" style={{ fontSize: 12.5, display: "flex", gap: 4, minWidth: 0 }}>
                    <span style={{ color: "var(--tx4)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {dir}
                    </span>
                    <span style={{ color: "var(--tx)", fontWeight: 500, flex: "0 0 auto" }}>{name}</span>
                  </span>
                ) : (
                  <span style={{ fontSize: 13, color: "var(--tx4)" }}>no repository selected</span>
                )}
                <button
                  className="btn"
                  style={{ marginLeft: "auto", flex: "0 0 auto" }}
                  onClick={() => setBrowsing(true)}
                >
                  Browse…
                </button>
              </div>
              {suggestions.length > 0 && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 2 }}>
                  {suggestions.slice(0, 10).map((s) => (
                    <button
                      key={s.path}
                      className="mono"
                      title={s.path}
                      style={{
                        fontSize: 11.5,
                        padding: "4px 10px",
                        borderRadius: 6,
                        border: `1px solid ${repo === s.path ? "var(--acbd)" : "var(--bd2)"}`,
                        background: repo === s.path ? "var(--acbg)" : "transparent",
                        color: repo === s.path ? "var(--ac)" : "var(--tx2)",
                      }}
                      onClick={() => setRepo(s.path)}
                    >
                      {s.name}
                      {s.source === "recent" && (
                        <span style={{ color: "var(--tx4)", marginLeft: 6 }}>recent</span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
              <span className="label">task</span>
              <textarea
                className="editor"
                style={{ minHeight: 86, fontFamily: "var(--sans)", fontSize: 14 }}
                placeholder="what should get built or fixed? one or two sentences is enough — scoping sharpens it"
                value={goal}
                onChange={(e) => setGoal(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) start();
                }}
              />
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <button
                className="btn accent"
                style={{ padding: "9px 16px", fontSize: 13 }}
                disabled={!goal.trim() || !repo.trim()}
                onClick={start}
              >
                Start scoping
              </button>
              <span className="mono" style={{ fontSize: 10.5, color: "var(--tx4)" }}>
                reads memory + repo first · refuses vague criteria · ⌘↵
              </span>
            </div>
          </div>

          <div>
            <button
              className="mono dim"
              style={{ fontSize: 11, borderBottom: "1px dotted var(--bd3)" }}
              onClick={() => setShowPaste(!showPaste)}
            >
              {showPaste ? "hide" : "already have a contract? paste it instead"}
            </button>
            {showPaste && <PasteContract />}
          </div>
        </>
      )}

      {browsing && (
        <BrowseModal
          onPick={(p) => {
            setRepo(p);
            setBrowsing(false);
          }}
          onClose={() => setBrowsing(false)}
          initial={repo || undefined}
        />
      )}
    </div>
  );
}

function BrowseModal({
  onPick,
  onClose,
  initial,
}: {
  onPick: (path: string) => void;
  onClose: () => void;
  initial?: string;
}) {
  const [info, setInfo] = useState<BrowseInfo | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = (path?: string) =>
    api
      .browse(path)
      .then((d) => {
        setInfo(d);
        setError(null);
      })
      .catch((e) => setError(String(e)));

  useEffect(() => {
    load(initial ? splitPath(initial).dir || initial : undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 90,
        background: "rgba(4,4,6,.7)",
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "center",
        paddingTop: "12vh",
      }}
      onClick={onClose}
    >
      <div
        style={{
          width: 560,
          maxWidth: "92vw",
          border: "1px solid var(--bd2)",
          borderRadius: 12,
          background: "linear-gradient(180deg,var(--s3),var(--s1))",
          boxShadow: "0 30px 80px rgba(0,0,0,.7)",
          overflow: "hidden",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "12px 15px",
            borderBottom: "1px solid var(--bd)",
          }}
        >
          <span className="label" style={{ color: "var(--tx3)" }}>
            pick a repository
          </span>
          <span
            className="mono"
            style={{
              marginLeft: "auto",
              fontSize: 11,
              color: "var(--tx3)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              maxWidth: 300,
            }}
          >
            {info?.path ?? "…"}
          </span>
        </div>
        <div style={{ maxHeight: 380, overflowY: "auto", padding: 6 }}>
          {error && <div style={{ padding: 12, color: "var(--bad)", fontSize: 12.5 }}>{error}</div>}
          {info?.parent && (
            <button
              className="row-btn mono"
              style={{ padding: "8px 11px", borderRadius: 8, fontSize: 12, color: "var(--tx3)" }}
              onClick={() => load(info.parent!)}
            >
              ← ..
            </button>
          )}
          {info?.dirs.map((d) => (
            <div
              key={d.path}
              style={{ display: "flex", alignItems: "center", gap: 8, borderRadius: 8 }}
              className="hoverable"
            >
              <button
                className="row-btn"
                style={{ flex: 1, display: "flex", alignItems: "center", gap: 9, padding: "8px 11px" }}
                onClick={() => (d.is_repo ? onPick(d.path) : load(d.path))}
              >
                <span className="mono" style={{ fontSize: 11, color: d.is_repo ? "var(--ac)" : "var(--tx4)", width: 24 }}>
                  {d.is_repo ? "git" : "▸"}
                </span>
                <span style={{ fontSize: 13, color: d.is_repo ? "var(--tx)" : "var(--tx2)" }}>{d.name}</span>
              </button>
              {d.is_repo && (
                <button
                  className="mono dim"
                  style={{ fontSize: 10.5, padding: "4px 10px", flex: "0 0 auto" }}
                  onClick={() => load(d.path)}
                >
                  open ▸
                </button>
              )}
            </div>
          ))}
          {info && info.dirs.length === 0 && (
            <div style={{ padding: 12, color: "var(--tx4)", fontSize: 12.5 }}>no subdirectories</div>
          )}
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "10px 15px",
            borderTop: "1px solid var(--bd)",
          }}
        >
          {info?.is_repo && (
            <button className="btn accent" onClick={() => onPick(info.path)}>
              Use this repository
            </button>
          )}
          <button className="btn" style={{ marginLeft: "auto" }} onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------- chat view

function ThinkingCard({ goal, repo, first }: { goal: string; repo: string; first?: boolean }) {
  return (
    <div
      className="mono rise"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "13px 15px",
        borderRadius: "var(--rad)",
        border: "1px solid var(--acbd)",
        background: "var(--acbg)",
        fontSize: 12,
      }}
    >
      <span
        className="pulse"
        style={{ width: 7, height: 7, borderRadius: "50%", background: "var(--ac)" }}
      />
      <div style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 0 }}>
        <span style={{ color: "var(--tx)", fontFamily: "var(--sans)", fontSize: 13.5 }}>
          {first ? "Reading the repo and project memory…" : "Thinking…"}
        </span>
        <span style={{ color: "var(--tx3)", fontSize: 10.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {goal} · {repo}
        </span>
      </div>
    </div>
  );
}

function ScopingChatView({ chatId }: { chatId: string }) {
  const { toast, go, refresh } = useApp();
  const [chat, setChat] = useState<ScopingChat | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .scopeGet(chatId)
      .then((d) => setChat(d.chat))
      .catch((e) => setError(String(e)));
  }, [chatId]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [chat?.messages.length, busy]);

  if (error)
    return (
      <div style={{ padding: 22 }}>
        <div className="empty">{error}</div>
      </div>
    );
  if (!chat) return <div style={{ padding: 22 }} className="dim">loading…</div>;

  const send = async (text: string) => {
    if (!text.trim() || busy) return;
    setBusy(true);
    setDraft("");
    // optimistic operator bubble
    setChat({
      ...chat,
      status: "thinking",
      messages: [...chat.messages, { role: "operator", text: text.trim(), at: "" }],
    });
    try {
      const d = await api.scopeMessage(chat.id, text.trim());
      setChat(d.chat);
    } catch (e) {
      toast("Turn failed", String(e), "bad");
      api.scopeGet(chatId).then((d) => setChat(d.chat)).catch(() => {});
    } finally {
      setBusy(false);
    }
  };

  const approve = async (run: boolean) => {
    if (busy) return;
    setBusy(true);
    try {
      const d = await api.scopeApprove(chat.id, run);
      toast(
        `Contract approved · task ${d.task.id}`,
        run && d.started ? "worker spawning" : `start it with fm run ${d.task.id}`,
        "run"
      );
      refresh();
      go(`#/task/${d.task.id}`);
    } catch (e) {
      toast("Approve failed", String(e), "bad");
      setBusy(false);
    }
  };

  const abandon = async () => {
    try {
      await api.scopeAbandon(chat.id);
      go("#/new");
    } catch (e) {
      toast("Abandon failed", String(e), "bad");
    }
  };

  const contractObj = chat.contract as Record<string, any> | null;

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
          border: "1px solid var(--bd)",
          background: "var(--s2)",
          fontSize: 11,
          flexWrap: "wrap",
        }}
      >
        <span style={{ width: 6, height: 6, borderRadius: "50%", background: "var(--ac)" }} />
        <span style={{ color: "var(--tx2)" }}>scoping · {splitPath(chat.repo).name}</span>
        <span style={{ color: "var(--tx4)" }}>{chat.id}</span>
        <span style={{ marginLeft: "auto", color: "var(--tx4)" }}>
          {chat.status.replace("_", " ")}
        </span>
        {chat.status !== "approved" && (
          <button className="mono" style={{ fontSize: 10.5, color: "var(--tx3)" }} onClick={abandon}>
            abandon
          </button>
        )}
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
        {chat.messages.map((m, i) => (
          <div key={i} style={{ display: "flex", gap: 14 }} className="rise">
            <div style={{ flex: "0 0 74px", paddingTop: 2 }}>
              <span
                className="label"
                style={{
                  color:
                    m.role === "firstmate"
                      ? "var(--ac)"
                      : m.role === "system"
                        ? "var(--bad)"
                        : "var(--tx3)",
                }}
              >
                {m.role === "firstmate" ? "first mate" : m.role === "system" ? "error" : "you"}
              </span>
            </div>
            <div
              style={{
                flex: 1,
                minWidth: 0,
                fontSize: 14,
                lineHeight: 1.6,
                maxWidth: "72ch",
                whiteSpace: "pre-wrap",
                color: m.role === "system" ? "var(--bad)" : "var(--tx)",
              }}
            >
              {m.text}
            </div>
          </div>
        ))}
        {(busy || chat.status === "thinking") && (
          <div style={{ display: "flex", gap: 14 }}>
            <div style={{ flex: "0 0 74px" }} />
            <div style={{ flex: 1 }}>
              <ThinkingCard goal={chat.goal} repo={chat.repo} first={chat.messages.length === 0} />
            </div>
          </div>
        )}
      </div>

      {contractObj && (
        <div style={{ marginLeft: 88 }}>
          <div className="card-flat">
            <div
              style={{
                padding: "9px 14px",
                borderBottom: "1px solid var(--bd)",
                display: "flex",
                alignItems: "center",
                gap: 9,
              }}
            >
              <span className="label" style={{ color: "var(--tx3)" }}>
                contract
              </span>
              <span
                className="mono"
                style={{
                  marginLeft: "auto",
                  fontSize: 10.5,
                  color: chat.contract_errors.length ? "var(--bad)" : "var(--ok)",
                }}
              >
                {chat.contract_errors.length
                  ? `${chat.contract_errors.length} error(s)`
                  : `${(contractObj.criteria ?? []).length} criteria · all machine-checkable`}
              </span>
            </div>
            <div style={{ padding: "12px 14px", display: "flex", flexDirection: "column", gap: 8 }}>
              <ContractRow k="goal" v={String(contractObj.goal ?? "")} />
              <ContractRow k="scope" v={(contractObj.scope_in ?? []).join(" · ")} />
              <ContractRow
                k="steps"
                v={(contractObj.steps ?? []).map((s: any) => s.id).join(" → ")}
              />
              {(contractObj.criteria ?? []).map((c: any) => (
                <ContractRow key={c.id} k={c.id} v={c.command} accent />
              ))}
              {chat.contract_errors.map((e, i) => (
                <ContractRow key={i} k="✗" v={e} bad />
              ))}
            </div>
          </div>
        </div>
      )}

      {chat.status === "contract_ready" && (
        <div style={{ display: "flex", alignItems: "center", gap: 9, paddingLeft: 88 }}>
          <button
            className="btn accent"
            style={{ padding: "9px 16px", fontSize: 13 }}
            disabled={busy}
            onClick={() => approve(true)}
          >
            Approve and run
          </button>
          <button className="btn" disabled={busy} onClick={() => approve(false)}>
            Approve only
          </button>
          <span className="mono" style={{ fontSize: 10.5, color: "var(--tx4)", marginLeft: 4 }}>
            then nothing downstream is interactive by default
          </span>
        </div>
      )}

      {chat.status !== "approved" && chat.status !== "abandoned" && (
        <div
          style={{
            display: "flex",
            gap: 10,
            alignItems: "center",
            padding: "12px 14px",
            borderRadius: "var(--rad)",
            border: "1px solid var(--bd2)",
            background: "var(--s2)",
            marginLeft: 88,
          }}
        >
          <input
            className="line"
            placeholder="push back, add a constraint, or say it looks right…"
            value={draft}
            disabled={busy}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") send(draft);
            }}
          />
          <button
            className="mono"
            style={{ fontSize: 11.5, color: "var(--ac)", flex: "0 0 auto" }}
            disabled={busy}
            onClick={() => send(draft)}
          >
            send ↵
          </button>
        </div>
      )}
      <div ref={bottom} />
    </div>
  );
}

function ContractRow({
  k,
  v,
  accent,
  bad,
}: {
  k: string;
  v: string;
  accent?: boolean;
  bad?: boolean;
}) {
  return (
    <div style={{ display: "flex", gap: 14 }}>
      <span className="mono" style={{ fontSize: 11, color: "var(--tx4)", flex: "0 0 86px" }}>
        {k}
      </span>
      <span
        className="mono"
        style={{
          fontSize: 11.5,
          lineHeight: 1.55,
          flex: 1,
          minWidth: 0,
          color: bad ? "var(--bad)" : accent ? "var(--tx2)" : "var(--tx)",
          wordBreak: "break-word",
        }}
      >
        {v}
      </span>
    </div>
  );
}

// ------------------------------------------------- paste-a-contract path

function PasteContract() {
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
    <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 12 }}>
      <SectionHead title="submit a contract" hint="same gate as POST /tasks" />
      <textarea
        className="editor"
        style={{ minHeight: 280 }}
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
        <button className="btn accent" onClick={submit} disabled={busy}>
          {run ? "Create and run" : "Create task"}
        </button>
        <label
          style={{
            display: "flex",
            alignItems: "center",
            gap: 7,
            fontSize: 12.5,
            color: "var(--tx2)",
            cursor: "pointer",
          }}
        >
          <input type="checkbox" checked={run} onChange={(e) => setRun(e.target.checked)} />
          start immediately
        </label>
      </div>
    </div>
  );
}
