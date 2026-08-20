import { useEffect, useState } from "react";
import type { MemoryProject, MemorySuggestion } from "../api";
import { api } from "../api";
import { useApp, SectionHead } from "../components";
import { ago } from "../format";

export function MemoryView() {
  const { toast, refresh } = useApp();
  const [projects, setProjects] = useState<MemoryProject[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [text, setText] = useState<string>("");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [fact, setFact] = useState("");
  const [busy, setBusy] = useState(false);
  const [suggestions, setSuggestions] = useState<MemorySuggestion[]>([]);

  const loadList = () =>
    api
      .memory()
      .then((d) => {
        setProjects(d.projects);
        if (!selected && d.projects.length) setSelected(d.projects[0].project);
      })
      .catch(() => {});

  const loadSuggestions = () =>
    api
      .memorySuggestions()
      .then((d) => setSuggestions(d.suggestions))
      .catch(() => {});

  useEffect(() => {
    loadList();
    loadSuggestions();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selected) return;
    api
      .memoryFile(selected)
      .then((d) => setText(d.text))
      .catch(() => setText(""));
  }, [selected]);

  const append = async () => {
    if (!fact.trim() || !selected || busy) return;
    setBusy(true);
    try {
      const d = await api.remember(selected, fact.trim());
      setText(d.text);
      setFact("");
      toast("Appended to project memory", "injected at the start of every session from now on");
      loadList();
    } catch (e) {
      toast("Append failed", String(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  const promote = async (sug: MemorySuggestion) => {
    if (busy) return;
    setBusy(true);
    try {
      const d = await api.promoteSuggestion(sug.id);
      if (d.project === selected) setText(d.text);
      toast("Promoted to project memory", sug.fact);
      loadSuggestions();
      loadList();
      refresh(); // drops the sidebar badge now, not on the next poll
    } catch (e) {
      toast("Promotion failed", String(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  const dismiss = async (sug: MemorySuggestion) => {
    if (busy) return;
    setBusy(true);
    try {
      await api.dismissSuggestion(sug.id);
      toast("Dismissed", "This situation won't be suggested again");
      loadSuggestions();
      refresh();
    } catch (e) {
      toast("Dismiss failed", String(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  const compact = async () => {
    if (!selected || busy) return;
    setBusy(true);
    toast("Compacting…", "an LLM pass over the file — the current version is archived first");
    try {
      const d = await api.compactMemory(selected);
      setText(d.text);
      toast(
        "Memory compacted",
        `${d.before_bytes} → ${d.after_bytes} bytes${d.archived ? " · previous version archived" : ""}`,
      );
      loadList();
    } catch (e) {
      toast("Compaction failed", String(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!selected || busy) return;
    setBusy(true);
    try {
      const d = await api.saveMemory(selected, draft);
      setText(d.text);
      setEditing(false);
      toast("Memory saved", `${selected}.md rewritten`);
      loadList();
    } catch (e) {
      toast("Save failed", String(e), "bad");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="page" style={{ maxWidth: 960, gap: 18 }}>
      <div
        className="mono"
        style={{
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "12px 15px",
          borderRadius: "var(--rad)",
          border: "1px solid var(--bd)",
          background: "var(--s2)",
          fontSize: 11.5,
          flexWrap: "wrap",
        }}
      >
        <span style={{ color: "var(--tx2)" }}>
          ~/.firstmate/memory/{selected ?? "<project>"}.md
        </span>
        <span style={{ marginLeft: "auto", color: "var(--tx4)" }}>
          injected into every worker and scoping session
        </span>
      </div>

      {suggestions.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          <SectionHead
            title="promote to memory?"
            hint={`${suggestions.length} answer${
              suggestions.length === 1 ? "" : "s"
            } that recurred across tasks`}
          />
          {suggestions.map((sug) => (
            <div
              key={sug.id}
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 9,
                padding: "13px 15px",
                borderRadius: "var(--rad)",
                border: "1px solid var(--acbd)",
                background: "var(--acbg)",
              }}
            >
              <div style={{ fontSize: 13.5, color: "var(--tx)" }}>{sug.fact}</div>
              <div
                className="mono"
                style={{ fontSize: 11, color: "var(--tx4)", lineHeight: 1.6 }}
              >
                answered the same way on {sug.occurrences} tasks · {sug.project}
                <br />
                {sug.task_ids.join(" · ")}
              </div>
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button className="btn" onClick={() => dismiss(sug)} disabled={busy}>
                  Not a project fact
                </button>
                <button className="btn accent" onClick={() => promote(sug)} disabled={busy}>
                  Remember it
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {projects.length > 1 && (
        <div style={{ display: "flex", gap: 7, flexWrap: "wrap" }}>
          {projects.map((p) => (
            <button
              key={p.project}
              className="mono"
              style={{
                padding: "6px 12px",
                borderRadius: 7,
                fontSize: 11.5,
                border: `1px solid ${selected === p.project ? "var(--acbd)" : "var(--bd2)"}`,
                background: selected === p.project ? "var(--acbg)" : "transparent",
                color: selected === p.project ? "var(--ac)" : "var(--tx2)",
              }}
              onClick={() => {
                setSelected(p.project);
                setEditing(false);
              }}
            >
              {p.project}
              <span style={{ color: "var(--tx4)", marginLeft: 7 }}>{p.entries}</span>
            </button>
          ))}
        </div>
      )}

      {projects.length === 0 ? (
        <div className="empty">
          No project memory yet. Teach the system with{" "}
          <span className="mono" style={{ fontSize: 12 }}>
            fm remember "&lt;fact&gt;"
          </span>{" "}
          from a repo, or append below once a task creates one.
        </div>
      ) : (
        <>
          <SectionHead
            title="durable memory"
            hint={
              selected
                ? `${projects.find((p) => p.project === selected)?.entries ?? 0} entries · ${
                    projects.find((p) => p.project === selected)?.bytes ?? 0
                  } bytes · updated ${ago(projects.find((p) => p.project === selected)?.updated_at)} ago`
                : ""
            }
          />
          {editing ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <textarea
                className="editor"
                style={{ minHeight: 380 }}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
              />
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button className="btn" onClick={() => setEditing(false)} disabled={busy}>
                  Cancel
                </button>
                <button className="btn accent" onClick={save} disabled={busy}>
                  Save
                </button>
              </div>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div
                className="terminal"
                style={{
                  padding: "14px 16px",
                  fontFamily: "var(--sans)",
                  fontSize: 13.5,
                  color: "var(--tx)",
                  maxHeight: 480,
                  overflowY: "auto",
                }}
              >
                {text || "(empty)"}
              </div>
              <div
                style={{ display: "flex", justifyContent: "flex-end", gap: 8, alignItems: "center" }}
              >
                {projects.find((p) => p.project === selected)?.compact_due && (
                  <span
                    className="mono"
                    style={{ fontSize: 11, color: "var(--tx4)", marginRight: "auto" }}
                  >
                    large enough to crowd the context it feeds — consolidating keeps every
                    fact and archives this version first
                  </span>
                )}
                <button
                  className={
                    projects.find((p) => p.project === selected)?.compact_due
                      ? "btn accent"
                      : "btn"
                  }
                  onClick={compact}
                  disabled={busy}
                >
                  Compact
                </button>
                <button
                  className="btn"
                  onClick={() => {
                    setDraft(text);
                    setEditing(true);
                  }}
                >
                  Edit file
                </button>
              </div>
            </div>
          )}
        </>
      )}

      {selected && (
        <div
          style={{
            display: "flex",
            gap: 9,
            alignItems: "center",
            padding: 12,
            borderRadius: "var(--rad)",
            border: "1px solid var(--bd2)",
            background: "var(--s2)",
          }}
        >
          <span className="mono" style={{ fontSize: 11.5, color: "var(--tx4)", flex: "0 0 auto" }}>
            fm remember
          </span>
          <input
            className="line"
            placeholder="a project fact that would have saved a session time…"
            value={fact}
            onChange={(e) => setFact(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") append();
            }}
          />
          <button className="btn accent" style={{ flex: "0 0 auto" }} onClick={append} disabled={busy}>
            Append
          </button>
        </div>
      )}
    </div>
  );
}
