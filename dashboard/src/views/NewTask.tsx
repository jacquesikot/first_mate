import { useEffect, useState } from "react";
import type { BrowseInfo, RepoSuggestion } from "../api";
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

/** New task = pick the repo, state the goal. Starting creates the session
 * immediately (status `scoping`) and hands over to its task view — the
 * scoping conversation happens there, not in this form (PRD §6.8). */
export function NewTaskView() {
  const { toast, go, refresh } = useApp();
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
      refresh();
      toast(`Session opened · ${r.task.id}`, "scoping — reading the repo", "run");
      go(`#/task/${r.task.id}`);
    } catch (e) {
      toast("Could not start the session", String(e), "bad");
      setStarting(false);
    }
  };

  const { dir, name } = splitPath(repo);

  return (
    <div className="page narrow" style={{ gap: 16 }}>
      <div style={{ fontSize: 13, color: "var(--tx2)", lineHeight: 1.6, maxWidth: "68ch" }}>
        Pick the repository the task builds on and say what you want done. First Mate opens a
        session straight away, reads the repo and project memory, then proposes a scope, steps,
        and machine-checkable completion criteria for you to push back on — in the session.
      </div>

      <div
        className="card-flat"
        style={{ padding: 16, display: "flex", flexDirection: "column", gap: 14 }}
      >
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
              minWidth: 0,
            }}
          >
            {repo ? (
              <span
                className="mono"
                style={{ fontSize: 12.5, display: "flex", gap: 4, minWidth: 0 }}
              >
                <span className="truncate" style={{ color: "var(--tx4)" }}>
                  {dir}
                </span>
                <span style={{ color: "var(--tx)", fontWeight: 500, flex: "0 0 auto" }}>
                  {name}
                </span>
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

        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <button
            className="btn accent"
            style={{ padding: "9px 16px", fontSize: 13 }}
            disabled={!goal.trim() || !repo.trim() || starting}
            onClick={start}
          >
            {starting ? "Opening session…" : "Start scoping"}
          </button>
          <span className="mono" style={{ fontSize: 10.5, color: "var(--tx4)" }}>
            opens a session · reads memory + repo first · ⌘↵
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
          <span className="label" style={{ color: "var(--tx3)", flex: "0 0 auto" }}>
            pick a repository
          </span>
          <span
            className="mono truncate"
            style={{ marginLeft: "auto", fontSize: 11, color: "var(--tx3)", direction: "rtl" }}
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
                style={{
                  flex: 1,
                  minWidth: 0,
                  display: "flex",
                  alignItems: "center",
                  gap: 9,
                  padding: "8px 11px",
                }}
                onClick={() => (d.is_repo ? onPick(d.path) : load(d.path))}
              >
                <span
                  className="mono"
                  style={{
                    fontSize: 11,
                    color: d.is_repo ? "var(--ac)" : "var(--tx4)",
                    width: 24,
                    flex: "0 0 24px",
                  }}
                >
                  {d.is_repo ? "git" : "▸"}
                </span>
                <span
                  className="truncate"
                  style={{ fontSize: 13, color: d.is_repo ? "var(--tx)" : "var(--tx2)" }}
                >
                  {d.name}
                </span>
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
            overflowWrap: "anywhere",
          }}
        >
          {errors}
        </div>
      )}
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
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
