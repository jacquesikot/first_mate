// Presentation helpers — status glyphs, colors, and time formatting,
// matching the prototype's agreed visual language.

export const STATUS_GLYPH: Record<string, string> = {
  running: "●",
  // Parked on an external precondition — no session, no tokens, no slot.
  waiting: "◔",
  blocked: "◐",
  validating: "◍",
  ready: "○",
  paused: "◌",
  scoping: "○",
  failed: "✗",
  done: "✓",
  abandoned: "—",
};

export function statusColor(status: string): string {
  switch (status) {
    case "running":
      return "var(--ok)";
    case "blocked":
    case "validating":
      return "var(--ac)";
    case "waiting":
      // Deliberately not the attention colour: waiting is healthy progress,
      // not something the operator needs to act on.
      return "var(--tx2)";
    case "failed":
      return "var(--bad)";
    case "done":
      return "var(--tx3)";
    default:
      return "var(--tx3)";
  }
}

export function ctxColor(percent: number): string {
  if (percent >= 85) return "var(--bad)";
  if (percent >= 60) return "var(--ac)";
  return "var(--ok)";
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 48) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d`;
}

export function tokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`;
  return String(n);
}

export function repoName(repoPath: string): string {
  const parts = repoPath.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? repoPath;
}

export function splitPath(path: string): { dir: string; name: string } {
  const i = path.lastIndexOf("/");
  if (i < 0) return { dir: "", name: path };
  return { dir: path.slice(0, i + 1), name: path.slice(i + 1) };
}

export const QUESTION_ORDER: Record<string, number> = {
  blocking: 0,
  normal: 1,
};
