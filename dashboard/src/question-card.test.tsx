/**
 * QuestionCard — multi-question rounds.
 *
 * Regression cover for the two defects a real reach-plan task hit
 * (STATUS 2026-08-20): four questions flattened into one unreadable blob,
 * and a phantom "See inline options per question" button that submitted
 * that literal string as the operator's binding answer.
 */
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CopyButton, QuestionCard } from "./components";
import type { Question, SubQuestion } from "./api";

const sub = (
  id: string,
  question: string,
  options: [string, boolean][] = []
): SubQuestion => ({
  id,
  question,
  options: options.map(([label, recommended]) => ({ label, recommended })),
  default: null,
  answer: null,
});

const question = (over: Partial<Question> = {}): Question => ({
  id: "q-abc123",
  task_id: "t1",
  step_id: "plan",
  type: "decision",
  question: "Verified the sidebar; some forks need settling.",
  urgency: "normal",
  options: [],
  questions: [],
  default: null,
  evidence: {},
  status: "open",
  answer: null,
  answered_by: null,
  asked_at: new Date().toISOString(),
  answered_at: null,
  fingerprint: "f",
  answered_from: null,
  ...over,
});

const html = (q: Question) => renderToStaticMarkup(<QuestionCard q={q} />);
const text = (q: Question) =>
  html(q)
    .replace(/<[^>]*>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/\s+/g, " ");

describe("a round", () => {
  const round = question({
    questions: [
      sub("q1", "Keep the existing generation-context tab?", [
        ["keep both", true],
        ["replace it", false],
      ]),
      sub("q2", "How should writing context be differentiated?", [
        ["sub-tabs", true],
        ["collapsible sections", false],
      ]),
    ],
  });

  it("renders each question separately, not as one blob", () => {
    const t = text(round);
    expect(t).toContain("Keep the existing generation-context tab?");
    expect(t).toContain("How should writing context be differentiated?");
    // each question is labelled by its own id, so answers are attributable
    expect(t).toContain("q1");
    expect(t).toContain("q2");
  });

  it("gives every question its own options", () => {
    const t = text(round);
    for (const label of ["keep both", "replace it", "sub-tabs", "collapsible sections"]) {
      expect(t).toContain(label);
    }
  });

  it("marks the worker's recommendation without burying it in prose", () => {
    expect(text(round)).toContain("rec");
  });

  it("will not send until every question is answered", () => {
    const t = text(round);
    expect(t).toContain("2 still unanswered");
    // the submit control is disabled while any question is unanswered
    expect(html(round)).toMatch(/disabled[^>]*>\s*2 still unanswered/);
  });

  it("offers to take every recommendation at once", () => {
    expect(text(round)).toContain("take every recommendation");
  });

  it("says the round costs one interruption", () => {
    expect(text(round)).toContain("2 decisions · one interruption");
  });

  it("never renders a whole-round option button", () => {
    // This is the phantom-button regression: a round's options live on its
    // sub-questions, so a top-level option must not appear as a submit.
    const withStray = question({
      options: ["See inline options per question"],
      questions: round.questions,
    });
    expect(text(withStray)).not.toContain("See inline options per question");
  });

  it("shows each decision after answering, not a run-together blob", () => {
    const answered = question({
      status: "answered",
      answered_by: "dashboard",
      answered_at: new Date().toISOString(),
      answer: "q1: … → keep both\nq2: … → sub-tabs",
      questions: [
        { ...round.questions[0], answer: "keep both" },
        { ...round.questions[1], answer: "sub-tabs" },
      ],
    });
    const t = text(answered);
    expect(t).toContain("keep both");
    expect(t).toContain("sub-tabs");
  });
});

describe("a plain question", () => {
  const plain = question({ options: ["red", "blue"], default: "red" });

  it("still renders its flat options", () => {
    const t = text(plain);
    expect(t).toContain("red");
    expect(t).toContain("blue");
    expect(t).toContain("default → red");
  });

  it("still offers the CLI equivalent", () => {
    expect(text(plain)).toContain("fm answer q-abc123");
  });

  it("tolerates a question with no sub-question list at all", () => {
    // Questions written to disk before rounds existed have no `questions`
    // key; the card must not crash on them.
    const { questions: _omitted, ...legacy } = plain;
    expect(() => html(legacy as Question)).not.toThrow();
  });
});

describe("CopyButton", () => {
  it("renders its label, not the whole payload", () => {
    // A 14KB plan draft was passed with no label, so the entire document
    // became the button's face and pushed the rendered markdown 3800px
    // down the page (STATUS 2026-08-20).
    const big = "## Implementation Plan\n" + "x".repeat(5000);
    const withLabel = renderToStaticMarkup(<CopyButton text={big} label="copy" />);
    expect(withLabel).toContain(">copy<");
    expect(withLabel).not.toContain("Implementation Plan");
    expect(withLabel.length).toBeLessThan(300);
  });
});
