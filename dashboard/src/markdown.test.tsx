import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { Markdown } from "./markdown";

const html = (text: string) => renderToStaticMarkup(<Markdown text={text} />);
/** All visible text, including code spans. */
const plain = (text: string) => strip(html(text));

/** Visible text with code/pre content removed — markdown characters are
 * literal inside a code span (the glob `.git/**` is not stray bold), so
 * syntax-leak assertions must look outside them. */
const plainNoCode = (text: string) =>
  strip(html(text).replace(/<(code|pre)\b[\s\S]*?<\/\1>/g, ""));

const strip = (markup: string) =>
  markup
    .replace(/<[^>]*>/g, "")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");

describe("inline", () => {
  it("renders bold, italic, inline code and links", () => {
    expect(html("**Goal:** ship it")).toContain("<strong>");
    expect(html("the *inner* stages")).toContain("<em>");
    expect(html("run `npm test`")).toContain('class="md-code"');
    expect(html("[docs](https://x.com)")).toContain('href="https://x.com"');
  });

  it("leaves intra-word delimiters alone", () => {
    // Claude writes identifiers like these constantly; treating the
    // underscores as emphasis silently deleted them.
    for (const src of ["scope_in", "max_diff_lines", "snake_case_thing", "2*3*4"]) {
      expect(plain(src)).toBe(src);
    }
  });

  it("keeps markdown characters inside code spans literal", () => {
    const out = html("the glob `.git/**` is excluded");
    expect(out).toContain(".git/**");
    expect(out).not.toContain("<em>");
  });

  it("never emits raw HTML from the assistant's text", () => {
    const out = html("<script>alert(1)</script>");
    expect(out).not.toContain("<script>");
    expect(out).toContain("&lt;script&gt;");
  });

  it("turns single newlines inside a paragraph into breaks", () => {
    // "Scope in: …\nScope out: …" ran together as one line without this.
    expect(html("Scope in: a\nScope out: b")).toContain("<br/>");
  });

  it("passes unmatched delimiters through as text", () => {
    expect(plain("this **is not closed")).toBe("this **is not closed");
    expect(plain("a * b * c")).toBe("a * b * c");
  });
});

describe("blocks", () => {
  it("renders headings, lists, quotes, rules and fenced code", () => {
    expect(html("## Proposal")).toContain('class="md-h md-h2"');
    expect(html("- one\n- two")).toContain("<ul");
    expect(html("3. three\n4. four")).toContain('start="3"');
    expect(html("> quoted")).toContain("<blockquote");
    expect(html("---")).toContain("<hr");
    const code = html("```python\nx = 1\n```");
    expect(code).toContain('class="md-pre"');
    expect(code).toContain("python");
  });

  it("renders a table", () => {
    const out = html("| id | command |\n|---|---|\n| tests | `pytest` |");
    expect(out).toContain('class="md-table"');
    expect(out).toContain("<th>");
    expect(out).toContain("tests");
  });

  it("starts a table that directly follows a paragraph line", () => {
    // Found live: the header row was swallowed by the paragraph above it and
    // the whole table rendered as "| id | command ||---|---||...".
    const out = html("**Completion criteria:**\n| id | command |\n|---|---|\n| a | b |");
    expect(out).toContain('class="md-table"');
    expect(plainNoCode("**Completion criteria:**\n| id | command |\n|---|---|\n| a | b |"))
      .not.toContain("|---|");
  });

  it("nests lists", () => {
    const out = html("- a\n  - b\n    - c");
    expect(out.match(/<ul/g)?.length).toBe(3);
  });

  it("closes an unterminated fence at end of input", () => {
    expect(plain("```\nsome code\nno close")).toContain("some code");
  });

  it("survives empty and whitespace-only input", () => {
    expect(html("")).toBe('<div class="md"></div>');
    expect(html("\n\n\n")).toBe('<div class="md"></div>');
  });

  it("loses no content on a realistic scoping reply", () => {
    const reply = `Tiny repo. \`calc.py\` has one function \`add(a, b)\`.

## Proposal

**Goal:** Add \`subtract(a, b)\` to \`calc.py\`.

**Scope in:** \`calc.py\`, \`test_calc.py\`
**Scope out:** everything else (\`README.md\`, \`.git/**\`)

**Completion criteria:**
| id | command |
|---|---|
| \`tests\` | \`python3 -m pytest -q\` |

Approve as-is, or tell me the pytest answer and I'll finalize.`;
    const text = plain(reply);
    for (const fragment of [
      "Tiny repo.",
      "Proposal",
      "Goal:",
      "Scope in:",
      "Scope out:",
      "everything else",
      "Completion criteria:",
      "Approve as-is",
    ]) {
      expect(text).toContain(fragment);
    }
    // ...and none of the syntax leaks through (outside code spans, where
    // markdown characters are legitimately literal)
    const outsideCode = plainNoCode(reply);
    expect(outsideCode).not.toContain("**");
    expect(outsideCode).not.toContain("|---|");
    expect(outsideCode).not.toContain("##");
  });
});
