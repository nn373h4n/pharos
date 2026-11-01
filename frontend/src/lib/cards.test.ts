import { describe, expect, it } from "vitest";
import {
  cardsPath,
  isCard,
  isCardPage,
  isInsightAnswer,
  parseMinutes,
  type Card,
} from "@/lib/cards";

const ID = "40000000-0000-4000-8000-000000000001";
const card: Card = {
  id: ID,
  url: "https://news.example.org/a",
  host: "news.example.org",
  title: "A story",
  published_at: "2026-10-30T10:00:00Z",
  summary: "A summary.",
  category: "tool",
  saved: false,
  quality: null,
};
const insight = {
  profession: "accountant",
  language: "en",
  summary: "Why it matters.",
  how_to_apply: ["One.", "Two."],
  try_prompt: "Do [THIS].",
  safety_note: "Careful.",
};

describe("the shape of a card", () => {
  it("accepts what the backend sends", () => {
    expect(isCard(card)).toBe(true);
    expect(isCard({ ...card, quality: 4, published_at: null })).toBe(true);
    expect(isCardPage({ items: [card], next_cursor: null })).toBe(true);
    expect(isCardPage({ items: [], next_cursor: "abc" })).toBe(true);
  });

  it.each([
    ["an identifier that is not one", { id: "../x" }],
    ["a number for a title", { title: 5 }],
    ["a saved flag that is not a flag", { saved: "yes" }],
    ["a quality out of range", { quality: 6 }],
    ["a quality that is not whole", { quality: 2.5 }],
    ["no summary", { summary: undefined }],
  ])("refuses %s", (_name, change) => {
    expect(isCard({ ...card, ...change })).toBe(false);
  });

  it.each([
    null,
    "x",
    [],
    { items: "x", next_cursor: null },
    { items: [{}], next_cursor: null },
    { items: [], next_cursor: 5 },
    { items: [], next_cursor: "x".repeat(201) },
    { items: [] },
  ])("refuses a page like %j", (value) => {
    expect(isCardPage(value)).toBe(false);
  });
});

describe("the shape of an insight answer", () => {
  it("accepts the three kinds", () => {
    expect(isInsightAnswer({ status: "ready", reason: null, insight })).toBe(true);
    expect(isInsightAnswer({ status: "pending", reason: null, insight: null })).toBe(true);
    for (const reason of ["off", "rejected", "invalid"]) {
      expect(isInsightAnswer({ status: "unavailable", reason, insight: null })).toBe(true);
    }
  });

  it.each([
    { status: "ready", reason: null, insight: null },
    { status: "ready", reason: null, insight: { ...insight, how_to_apply: [] } },
    { status: "ready", reason: null, insight: { ...insight, how_to_apply: ["a", "b", "c", "d"] } },
    { status: "ready", reason: null, insight: { ...insight, how_to_apply: [1] } },
    { status: "ready", reason: null, insight: { ...insight, try_prompt: undefined } },
    { status: "pending", reason: null, insight },
    { status: "pending", reason: "off", insight: null },
    { status: "unavailable", reason: "because", insight: null },
    { status: "unavailable", reason: null, insight: null },
    { status: "maybe", reason: null, insight: null },
    null,
    "ready",
  ])("refuses %j", (value) => {
    expect(isInsightAnswer(value)).toBe(false);
  });
});

describe("paths and minutes", () => {
  it("asks for the first page or the next", () => {
    expect(cardsPath()).toBe("/api/cards");
    expect(cardsPath("a+b/c=")).toBe("/api/cards?cursor=a%2Bb%2Fc%3D");
  });

  it.each([
    ["1", 1],
    ["30", 30],
    [" 480 ", 480],
    ["0", null],
    ["481", null],
    ["", null],
    ["-5", null],
    ["2.5", null],
    ["1e2", null],
    ["١٢", null],
    ["12 min", null],
    ["0012", null],
  ])("reads %j as %j", (value, expected) => {
    expect(parseMinutes(value)).toBe(expected);
  });
});
