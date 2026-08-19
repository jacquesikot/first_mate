import { useEffect, useMemo, useState } from "react";
import type { GitRef, RefsInfo } from "../api";
import { api } from "../api";
import { ago } from "../format";

/** Where a task starts from.
 *
 * The operator's own checkout is often mid-work, so a task must not inherit
 * it by accident: every task states its starting point at input time, and the
 * worktree is cut from that point before scoping begins. The default is the
 * remote's default branch, freshly fetched — the "I usually pull latest from
 * origin/main" case — so the common path is zero clicks.
 */
export function StartPoint({
  repo,
  value,
  onChange,
  onInfo,
}: {
  repo: string;
  value: string;
  onChange: (base: string) => void;
  onInfo?: (info: RefsInfo | null) => void;
}) {
  const [info, setInfo] = useState<RefsInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [custom, setCustom] = useState("");
  const [customOpen, setCustomOpen] = useState(false);

  const load = (fetchFirst: boolean) => {
    if (!repo) return;
    setLoading(true);
    setError(null);
    api
      .refs(repo, fetchFirst)
      .then((d) => {
        setInfo(d);
        onInfo?.(d);
        // Pre-select the recommendation unless the operator already chose
        // something that still exists in this repo.
        const stillValid = value && d.refs.some((r) => r.name === value);
        if (!stillValid) onChange(d.recommended);
      })
      .catch((e) => {
        setError(String(e));
        setInfo(null);
        onInfo?.(null);
      })
      .finally(() => setLoading(false));
  };

  // Fetch on open so what we say about origin/… is true, not last week's.
  useEffect(() => {
    setShowAll(false);
    setCustomOpen(false);
    load(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [repo]);

  // Three named choices up front; the rest behind "more branches".
  const { primary, rest } = useMemo(() => {
    if (!info) return { primary: [] as GitRef[], rest: [] as GitRef[] };
    const head: GitRef[] = [];
    const seen = new Set<string>();
    const take = (r?: GitRef) => {
      if (r && !seen.has(r.name)) {
        seen.add(r.name);
        head.push(r);
      }
    };
    take(info.refs.find((r) => r.role === "default"));
    take(info.refs.find((r) => r.role === "current"));
    // One more: the most recent branch that isn't already listed.
    take(info.refs.find((r) => !seen.has(r.name)));
    return { primary: head, rest: info.refs.filter((r) => !seen.has(r.name)) };
  }, [info]);

  const isCustom = !!value && !!info && !info.refs.some((r) => r.name === value);
  const shown = showAll ? [...primary, ...rest] : primary;

  if (!repo) return null;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 7 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span className="label">start from</span>
        <span className="mono" style={{ fontSize: 10, color: "var(--tx4)" }}>
          {loading
            ? "reading branches…"
            : info?.fetched
              ? "fetched just now"
              : info?.fetch_error
                ? `not fetched · ${info.fetch_error}`
                : info
                  ? "local refs only"
                  : ""}
        </span>
        {info && !loading && (
          <button
            className="mono"
            style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--tx3)" }}
            onClick={() => load(true)}
            title="git fetch --prune, then re-read"
          >
            ↻ refetch
          </button>
        )}
      </div>

      {error && (
        <div
          className="mono"
          style={{ fontSize: 11.5, color: "var(--bad)", overflowWrap: "anywhere" }}
        >
          {error}
        </div>
      )}

      <div className="card-flat" style={{ overflow: "hidden" }}>
        {loading && !info && (
          <div className="mono dim" style={{ padding: "14px 13px", fontSize: 12 }}>
            loading…
          </div>
        )}

        {shown.map((r) => (
          <RefRow
            key={r.name}
            ref_={r}
            selected={value === r.name}
            dirty={r.role === "current" && !!info?.dirty}
            onPick={() => {
              setCustomOpen(false);
              onChange(r.name);
            }}
          />
        ))}

        {!showAll && rest.length > 0 && (
          <button
            className="row-btn hoverable mono"
            style={{
              padding: "9px 13px",
              fontSize: 11.5,
              color: "var(--tx3)",
              borderTop: "1px solid var(--bd)",
            }}
            onClick={() => setShowAll(true)}
          >
            {rest.length} more branch{rest.length > 1 ? "es" : ""} ▾
          </button>
        )}

        {info && (
          <div style={{ borderTop: "1px solid var(--bd)" }}>
            {customOpen || isCustom ? (
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 9,
                  padding: "9px 13px",
                  background: isCustom ? "var(--acbg)" : "transparent",
                }}
              >
                <span
                  className="mono"
                  style={{ fontSize: 11, color: "var(--tx4)", flex: "0 0 auto" }}
                >
                  ref
                </span>
                <input
                  className="line mono"
                  style={{ fontSize: 12.5 }}
                  placeholder="branch, tag, or commit sha…"
                  autoFocus
                  value={isCustom && !custom ? value : custom}
                  onChange={(e) => setCustom(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && custom.trim()) onChange(custom.trim());
                    if (e.key === "Escape") setCustomOpen(false);
                  }}
                />
                {custom.trim() && custom.trim() !== value && (
                  <button
                    className="mono"
                    style={{ fontSize: 11, color: "var(--ac)", flex: "0 0 auto" }}
                    onClick={() => onChange(custom.trim())}
                  >
                    use ↵
                  </button>
                )}
              </div>
            ) : (
              <button
                className="row-btn hoverable mono"
                style={{ padding: "9px 13px", fontSize: 11.5, color: "var(--tx3)" }}
                onClick={() => setCustomOpen(true)}
              >
                other branch, tag, or commit…
              </button>
            )}
          </div>
        )}
      </div>

      {/* One line saying plainly what will happen. */}
      {value && (
        <div
          className="mono"
          style={{ fontSize: 10.5, color: "var(--tx4)", lineHeight: 1.6 }}
        >
          a fresh worktree is cut from{" "}
          <span style={{ color: "var(--ac)" }}>{value}</span> onto the task's own
          branch — your checkout is never touched
        </div>
      )}
    </div>
  );
}

function RefRow({
  ref_: r,
  selected,
  dirty,
  onPick,
}: {
  ref_: GitRef;
  selected: boolean;
  dirty: boolean;
  onPick: () => void;
}) {
  // Freshness is the whole point of the row: "main" alone doesn't tell you
  // whether it's the main you want.
  const facts: { text: string; tone?: "ok" | "warn" | "bad" }[] = [];
  if (r.role === "default") facts.push({ text: "remote default", tone: "ok" });
  else facts.push({ text: r.remote ? "remote" : "local" });
  if (r.behind) facts.push({ text: `${r.behind} behind ${r.upstream}`, tone: "warn" });
  if (r.ahead) facts.push({ text: `${r.ahead} unpushed`, tone: "warn" });
  if (r.gone) facts.push({ text: "upstream gone", tone: "bad" });
  if (dirty) facts.push({ text: "uncommitted changes here", tone: "warn" });

  const tone = (t?: "ok" | "warn" | "bad") =>
    t === "ok" ? "var(--ok)" : t === "warn" ? "var(--ac)" : t === "bad" ? "var(--bad)" : "var(--tx4)";

  return (
    <button
      className="row-btn hoverable"
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 10,
        padding: "10px 13px",
        borderTop: "1px solid var(--bd)",
        background: selected ? "var(--acbg)" : "transparent",
        borderLeft: `2px solid ${selected ? "var(--ac)" : "transparent"}`,
        minWidth: 0,
      }}
      onClick={onPick}
      title={r.subject}
    >
      <span
        className="mono"
        style={{ fontSize: 11, color: selected ? "var(--ac)" : "var(--tx4)", paddingTop: 2 }}
      >
        {selected ? "◉" : "○"}
      </span>
      <span style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 0, flex: 1 }}>
        <span style={{ display: "flex", alignItems: "baseline", gap: 9, minWidth: 0 }}>
          <span
            className="mono"
            style={{
              fontSize: 12.5,
              fontWeight: 500,
              color: selected ? "var(--tx)" : "var(--tx2)",
              overflowWrap: "anywhere",
            }}
          >
            {r.name}
          </span>
          {r.role === "current" && (
            <span className="mono" style={{ fontSize: 10, color: "var(--tx4)", flex: "0 0 auto" }}>
              checked out
            </span>
          )}
          <span
            className="mono"
            style={{ marginLeft: "auto", fontSize: 10.5, color: "var(--tx4)", flex: "0 0 auto" }}
          >
            {ago(r.committed_at)} old
          </span>
        </span>
        <span
          className="mono"
          style={{ display: "flex", flexWrap: "wrap", gap: 9, fontSize: 10.5 }}
        >
          {facts.map((f, i) => (
            <span key={i} style={{ color: tone(f.tone) }}>
              {f.text}
            </span>
          ))}
          <span style={{ color: "var(--tx4)" }}>{r.sha}</span>
        </span>
      </span>
    </button>
  );
}
