import { describe, expect, it } from "vitest";
import { dollars, isKeyAnswer, isKeyStarted, isStatus, spentPercent } from "@/lib/llm";

const TEST = "7b2e1d3f-4c5e-4f60-9bac-1d2e3f4a5b6c";
const status = {
  provider: "fake",
  models: { filter: "a", enrichment: "b", insight: "c" },
  daily_cap_micro_usd: 2_000_000,
  spent_micro_usd: 1000,
  warned: false,
  exhausted: false,
  key_check: { outcome: "ok", checked_at: "2026-10-31T12:00:00Z" },
  usage: [
    {
      day: "2026-10-31",
      stage: "filter",
      calls: 1,
      refused: 0,
      failed: 0,
      tokens_in: 3,
      tokens_out: 4,
      cost_micro_usd: 5,
    },
  ],
};

describe("the shape of the model page", () => {
  it("accepts what the backend sends", () => {
    expect(isStatus(status)).toBe(true);
    expect(isStatus({ ...status, key_check: null })).toBe(true);
  });

  it.each([
    ["an unknown provider", { provider: "evil" }],
    ["a negative spend", { spent_micro_usd: -1 }],
    ["a flag that is not a flag", { warned: "yes" }],
    ["an unknown outcome", { key_check: { outcome: "<script>", checked_at: "x" } }],
    ["a usage row of an unknown stage", { usage: [{ ...status.usage[0], stage: "x" }] }],
    ["no usage list", { usage: null }],
  ])("refuses %s", (_name, change) => {
    expect(isStatus({ ...status, ...change })).toBe(false);
  });

  it("checks the key test answers", () => {
    expect(isKeyStarted({ test_id: TEST, retry_after_seconds: 60 })).toBe(true);
    expect(isKeyStarted({ test_id: "../x", retry_after_seconds: 60 })).toBe(false);
    expect(isKeyAnswer({ status: "pending", outcome: null, checked_at: null })).toBe(true);
    expect(isKeyAnswer({ status: "done", outcome: "rejected", checked_at: "t" })).toBe(true);
    expect(isKeyAnswer({ status: "done", outcome: "anything", checked_at: "t" })).toBe(false);
    expect(isKeyAnswer({ status: "odd", outcome: null, checked_at: null })).toBe(false);
  });
});

describe("money and share", () => {
  it("shows millionths of a dollar as dollars", () => {
    expect(dollars(2_000_000, "en")).toBe("$2.00");
    expect(dollars(1500, "en")).toBe("$0.0015");
  });

  it("keeps the share between 0 and 100", () => {
    expect(spentPercent(500_000, 2_000_000)).toBe(25);
    expect(spentPercent(9_000_000, 2_000_000)).toBe(100);
    expect(spentPercent(0, 0)).toBe(0);
    expect(spentPercent(5, 0)).toBe(100);
  });
});
