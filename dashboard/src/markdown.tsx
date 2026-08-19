// A small, dependency-free markdown renderer.
//
// Scoping replies and handoff briefs are written by Claude, so they arrive
// as markdown — headings, fenced code, tables, lists, bold/inline code.
// Rendering them as plain pre-wrap text showed the syntax raw. This covers
// the subset Claude actually emits; anything unrecognised falls through as
// text, so it can never lose content.
//
// Everything is built from React elements (never innerHTML), so the
// assistant's output can't inject markup.

import type { ReactNode } from "react";

// ------------------------------------------------------------- inline pass

type Inline =
  | { t: "text"; v: string }
  | { t: "code"; v: string }
  | { t: "strong"; v: Inline[] }
  | { t: "em"; v: Inline[] }
  | { t: "strike"; v: Inline[] }
  | { t: "link"; v: Inline[]; href: string };

/** Ordered so code spans win over emphasis (`**not bold**` inside a span).
 * `flank` marks a rule as needing CommonMark-ish flanking: the delimiter must
 * not sit between two word characters. Without it, identifiers Claude writes
 * constantly — `snake_case_thing`, `max_diff_lines`, `2*3*4` — would render
 * as emphasis with the underscores swallowed. */
type Rule = {
  re: RegExp;
  make: (m: RegExpExecArray) => Inline;
  flank?: boolean;
};

const WORD = /[\p{L}\p{N}]/u;

const INLINE_RULES: Rule[] = [
  { re: /^`([^`]+)`/, make: (m) => ({ t: "code", v: m[1] }) },
  { re: /^\*\*(?!\s)([\s\S]+?)\*\*/, make: (m) => ({ t: "strong", v: parseInline(m[1]) }) },
  { re: /^__(?!\s)([\s\S]+?)__/, make: (m) => ({ t: "strong", v: parseInline(m[1]) }), flank: true },
  { re: /^~~(?!\s)([\s\S]+?)~~/, make: (m) => ({ t: "strike", v: parseInline(m[1]) }) },
  { re: /^\*(?!\s)([^*\n]*[^*\s])\*/, make: (m) => ({ t: "em", v: parseInline(m[1]) }), flank: true },
  { re: /^_(?!\s)([^_\n]*[^_\s])_/, make: (m) => ({ t: "em", v: parseInline(m[1]) }), flank: true },
  {
    re: /^\[([^\]]*)\]\(([^)\s]+)\)/,
    make: (m) => ({ t: "link", v: parseInline(m[1]), href: m[2] }),
  },
];

function parseInline(src: string): Inline[] {
  const out: Inline[] = [];
  let buf = "";
  let i = 0;
  const flush = () => {
    if (buf) out.push({ t: "text", v: buf });
    buf = "";
  };
  while (i < src.length) {
    const rest = src.slice(i);
    // Only try the rules at characters that could start a token — keeps
    // this linear over ordinary prose.
    if ("`*_~[".includes(rest[0])) {
      let matched = false;
      for (const { re, make, flank } of INLINE_RULES) {
        const m = re.exec(rest);
        if (!m) continue;
        if (flank) {
          const before = src[i - 1];
          const after = src[i + m[0].length];
          // Intra-word delimiters (`snake_case`, `2*3*4`) are literal text.
          if (before && after && WORD.test(before) && WORD.test(after)) continue;
        }
        flush();
        out.push(make(m));
        i += m[0].length;
        matched = true;
        break;
      }
      if (matched) continue;
    }
    buf += rest[0];
    i += 1;
  }
  flush();
  return out;
}

/** Newlines inside a paragraph become visible breaks, not spaces. */
function withBreaks(v: string, key: string): ReactNode {
  if (!v.includes("\n")) return <span key={key}>{v}</span>;
  const parts = v.split("\n");
  return (
    <span key={key}>
      {parts.map((part, i) => (
        <span key={i}>
          {i > 0 && <br />}
          {part}
        </span>
      ))}
    </span>
  );
}

function renderInline(nodes: Inline[], keyBase = ""): ReactNode[] {
  return nodes.map((n, i) => {
    const key = `${keyBase}${i}`;
    switch (n.t) {
      case "text":
        return withBreaks(n.v, key);
      case "code":
        return (
          <code key={key} className="md-code">
            {n.v}
          </code>
        );
      case "strong":
        return <strong key={key}>{renderInline(n.v, `${key}-`)}</strong>;
      case "em":
        return <em key={key}>{renderInline(n.v, `${key}-`)}</em>;
      case "strike":
        return (
          <span key={key} style={{ textDecoration: "line-through", opacity: 0.7 }}>
            {renderInline(n.v, `${key}-`)}
          </span>
        );
      case "link":
        return (
          <a key={key} href={n.href} target="_blank" rel="noreferrer noopener">
            {renderInline(n.v, `${key}-`)}
          </a>
        );
    }
  });
}

export function MdText({ text }: { text: string }) {
  return <>{renderInline(parseInline(text))}</>;
}

// -------------------------------------------------------------- block pass

const BULLET = /^[ \t]*([-*+])[ \t]+(.*)$/;
const ORDERED = /^[ \t]*(\d+)[.)][ \t]+(.*)$/;
const HEADING = /^(#{1,6})[ \t]+(.*)$/;
const FENCE = /^[ \t]*(```+|~~~+)[ \t]*(\S*)/;
const QUOTE = /^[ \t]*>[ \t]?(.*)$/;
const RULE = /^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const TABLE_SEP = /^[ \t]*\|?[ \t]*:?-{1,}:?[ \t]*(\|[ \t]*:?-{1,}:?[ \t]*)+\|?[ \t]*$/;

/** A table is a row containing pipes immediately followed by a |---|---| rule. */
function isTableStart(lines: string[], i: number): boolean {
  return (
    lines[i].includes("|") &&
    i + 1 < lines.length &&
    TABLE_SEP.test(lines[i + 1])
  );
}

function indentOf(line: string): number {
  const m = /^[ \t]*/.exec(line);
  return m ? m[0].replace(/\t/g, "    ").length : 0;
}

function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  // Split on unescaped pipes.
  const cells: string[] = [];
  let cur = "";
  for (let i = 0; i < s.length; i++) {
    if (s[i] === "\\" && s[i + 1] === "|") {
      cur += "|";
      i++;
    } else if (s[i] === "|") {
      cells.push(cur.trim());
      cur = "";
    } else {
      cur += s[i];
    }
  }
  cells.push(cur.trim());
  return cells;
}

/** Renders a markdown block sequence. Recursive for nested lists/quotes. */
function renderBlocks(lines: string[], keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  let i = 0;
  const key = () => `${keyBase}${out.length}`;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i++;
      continue;
    }

    // fenced code
    const fence = FENCE.exec(line);
    if (fence) {
      const marker = fence[1][0].repeat(3);
      const lang = fence[2];
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) {
        body.push(lines[i]);
        i++;
      }
      i++; // closing fence (or EOF)
      out.push(
        <pre key={key()} className="md-pre">
          {lang && <span className="md-pre-lang">{lang}</span>}
          <code>{body.join("\n")}</code>
        </pre>
      );
      continue;
    }

    if (RULE.test(line)) {
      out.push(<hr key={key()} className="md-hr" />);
      i++;
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      const level = heading[1].length;
      out.push(
        <div key={key()} className={`md-h md-h${Math.min(level, 4)}`}>
          <MdText text={heading[2]} />
        </div>
      );
      i++;
      continue;
    }

    // table: a header row followed by a |---|---| separator
    if (isTableStart(lines, i)) {
      const header = splitRow(line);
      i += 2;
      const rows: string[][] = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      out.push(
        <div key={key()} className="md-table-wrap">
          <table className="md-table">
            <thead>
              <tr>
                {header.map((h, c) => (
                  <th key={c}>
                    <MdText text={h} />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r, ri) => (
                <tr key={ri}>
                  {header.map((_, c) => (
                    <td key={c}>
                      <MdText text={r[c] ?? ""} />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      );
      continue;
    }

    // blockquote
    if (QUOTE.test(line)) {
      const body: string[] = [];
      while (i < lines.length && (QUOTE.test(lines[i]) || (body.length && lines[i].trim()))) {
        const m = QUOTE.exec(lines[i]);
        body.push(m ? m[1] : lines[i].trim());
        i++;
      }
      out.push(
        <blockquote key={key()} className="md-quote">
          {renderBlocks(body, `${key()}-`)}
        </blockquote>
      );
      continue;
    }

    // lists — items collect their own continuation and nested lines
    const isItem = (l: string) => BULLET.test(l) || ORDERED.test(l);
    if (isItem(line)) {
      const ordered = ORDERED.test(line) && !BULLET.test(line);
      const baseIndent = indentOf(line);
      const items: string[][] = [];
      let start = 1;
      const firstOrdered = ORDERED.exec(line);
      if (ordered && firstOrdered) start = parseInt(firstOrdered[1], 10) || 1;
      while (i < lines.length) {
        const l = lines[i];
        if (!l.trim()) {
          // A blank line ends the list unless the next line continues it.
          const next = lines[i + 1];
          if (!next || indentOf(next) < baseIndent + 2 || !next.trim()) {
            if (!next || !isItem(next)) break;
          }
          i++;
          continue;
        }
        if (isItem(l) && indentOf(l) <= baseIndent) {
          const m = BULLET.exec(l) ?? ORDERED.exec(l)!;
          items.push([m[2]]);
          i++;
          continue;
        }
        if (!items.length) break;
        if (indentOf(l) > baseIndent) {
          // nested content — strip one indent level so recursion sees a
          // block starting at column 0
          items[items.length - 1].push(l.slice(Math.min(indentOf(l), baseIndent + 2)));
          i++;
          continue;
        }
        // lazy continuation of the current item's paragraph
        items[items.length - 1].push(l.trim());
        i++;
      }
      const listItems = items.map((body, li) => (
        <li key={li}>{renderBlocks(body, `${keyBase}${li}-`)}</li>
      ));
      out.push(
        ordered ? (
          <ol key={key()} className="md-list" start={start}>
            {listItems}
          </ol>
        ) : (
          <ul key={key()} className="md-list">
            {listItems}
          </ul>
        )
      );
      continue;
    }

    // paragraph: consume until a blank line or a block starter
    const para: string[] = [];
    while (i < lines.length && lines[i].trim()) {
      const l = lines[i];
      if (
        para.length &&
        (isItem(l) ||
          HEADING.test(l) ||
          FENCE.test(l) ||
          QUOTE.test(l) ||
          RULE.test(l) ||
          isTableStart(lines, i))
      )
        break;
      para.push(l.trim());
      i++;
    }
    out.push(
      <p key={key()} className="md-p">
        <MdText text={para.join("\n")} />
      </p>
    );
  }
  return out;
}

/** Render a markdown document. `className` lands on the wrapper so callers
 * control typography/measure. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const lines = (text ?? "").replace(/\r\n?/g, "\n").split("\n");
  return <div className={`md${className ? ` ${className}` : ""}`}>{renderBlocks(lines, "b")}</div>;
}
