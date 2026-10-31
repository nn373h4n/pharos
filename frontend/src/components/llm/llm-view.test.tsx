import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import { ApiProblem } from "@/lib/api";
import type { KeyTestAnswer, KeyTestStarted, LlmStatus } from "@/lib/llm";

const getStatus = vi.fn<() => Promise<LlmStatus>>();
const startKeyTest = vi.fn<(csrf: string) => Promise<KeyTestStarted>>();
const readKeyTest = vi.fn<(id: string) => Promise<KeyTestAnswer>>();

vi.mock("@/components/auth/session-provider", () => ({
  useSession: () => ({
    state: {
      status: "authenticated",
      session: { csrf_token: "token-1", user: { role: "admin", display_name: "A" } },
    },
  }),
}));
vi.mock("@/lib/llm", async (original) => ({
  ...(await original<typeof import("@/lib/llm")>()),
  getStatus: () => getStatus(),
  startKeyTest: (csrf: string) => startKeyTest(csrf),
  readKeyTest: (id: string) => readKeyTest(id),
}));

const { LlmView } = await import("./llm-view");

const TEST = "7b2e1d3f-4c5e-4f60-9bac-1d2e3f4a5b6c";
const pending: KeyTestAnswer = { status: "pending", outcome: null, checked_at: null };
const base: LlmStatus = {
  provider: "anthropic",
  models: { filter: "small-model", enrichment: "big-model", insight: "" },
  daily_cap_micro_usd: 2_000_000,
  spent_micro_usd: 500_000,
  warned: false,
  exhausted: false,
  key_check: null,
  usage: [],
};

function show() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <LlmView />
    </NextIntlClientProvider>,
  );
}

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}
const testButton = () => screen.getByRole("button", { name: en.Llm.key.test }) as HTMLButtonElement;

beforeEach(() => {
  vi.useFakeTimers();
  getStatus.mockResolvedValue(base);
  startKeyTest.mockResolvedValue({ test_id: TEST, retry_after_seconds: 60 });
  readKeyTest.mockResolvedValue(pending);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("the model page", () => {
  it("shows the settings and the spending, and never a key", async () => {
    show();
    await tick(0);
    expect(screen.getByText("Anthropic (Claude)")).toBeDefined();
    expect(screen.getByText("small-model")).toBeDefined();
    expect(screen.getByText(en.Llm.setup.noModel)).toBeDefined(); // a stage without a model
    expect(screen.getByText("$0.50 of $2.00 used (25%).")).toBeDefined();
    expect(screen.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("25");
    expect(screen.getByText(en.Llm.key.never)).toBeDefined();
    expect(screen.getByText(en.Llm.usage.empty)).toBeDefined();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(document.body.querySelector("input")).toBeNull(); // there is no key field
  });

  it("warns at eighty percent and says plainly when the limit is reached", async () => {
    getStatus.mockResolvedValue({ ...base, warned: true });
    show();
    await tick(0);
    expect(screen.getByRole("alert").textContent).toBe(en.Llm.budget.warned);
    cleanup();
    getStatus.mockResolvedValue({ ...base, warned: true, exhausted: true });
    show();
    await tick(0);
    expect(screen.getByRole("alert").textContent).toBe(en.Llm.budget.exhausted);
  });

  it("says when no model is switched on", async () => {
    getStatus.mockResolvedValue({ ...base, provider: "off" });
    show();
    await tick(0);
    expect(screen.getByRole("note").textContent).toBe(en.Llm.offNote);
  });

  it("shows the last check and the usage rows", async () => {
    getStatus.mockResolvedValue({
      ...base,
      key_check: { outcome: "rejected", checked_at: "2026-10-31T12:00:00Z" },
      usage: [
        {
          day: "2026-10-31",
          stage: "enrichment",
          calls: 2,
          refused: 1,
          failed: 0,
          tokens_in: 300,
          tokens_out: 50,
          cost_micro_usd: 2000,
        },
      ],
    });
    show();
    await tick(0);
    expect(screen.getByText(/Last check: The provider refused the key/)).toBeDefined();
    const row = screen.getByRole("row", { name: /Enrichment/ });
    expect(row.textContent).toContain("300 / 50");
    expect(row.textContent).toContain("$0.002");
  });

  it("tests the key with the CSRF token, waits, shows the answer and reloads", async () => {
    show();
    await tick(0);
    fireEvent.click(testButton());
    await tick(0);
    expect(startKeyTest).toHaveBeenCalledWith("token-1");
    expect(screen.getByRole("status").textContent).toBe(en.Llm.key.waiting);
    expect(testButton().disabled).toBe(true);
    await tick(2000);
    expect(readKeyTest).toHaveBeenCalledWith(TEST);
    readKeyTest.mockResolvedValue({
      status: "done",
      outcome: "ok",
      checked_at: "2026-10-31T12:00:00Z",
    });
    await tick(2000);
    expect(screen.getByRole("status").textContent).toBe("Result: The key works.");
    expect(getStatus).toHaveBeenCalledTimes(2); // asked again to show the new last check
    await tick(10_000);
    expect(readKeyTest).toHaveBeenCalledTimes(2);
  });

  it("says so when the answer cannot be read, and when it takes too long", async () => {
    readKeyTest.mockResolvedValue({ status: "unavailable", outcome: null, checked_at: null });
    show();
    await tick(0);
    fireEvent.click(testButton());
    await tick(0);
    await tick(2000);
    expect(screen.getByRole("status").textContent).toBe(en.Llm.key.unavailable);
    readKeyTest.mockResolvedValue(pending);
    fireEvent.click(testButton());
    await tick(0);
    await tick(62_000);
    expect(screen.getByRole("status").textContent).toBe(en.Llm.key.gaveUp);
  });

  it("explains a refusal to start (too soon) instead of failing silently", async () => {
    startKeyTest.mockRejectedValue(new ApiProblem(429, "rate_limited", 42));
    show();
    await tick(0);
    fireEvent.click(testButton());
    await tick(0);
    expect(screen.getByRole("status").textContent).toContain("42");
  });

  it("shows a load failure with a way to try again", async () => {
    getStatus.mockRejectedValueOnce(new Error("down"));
    show();
    await tick(0);
    expect(screen.getByRole("alert").textContent).toBe(en.Llm.loadFailed);
    fireEvent.click(screen.getByRole("button", { name: en.Llm.retry }));
    await tick(0);
    expect(screen.getByText("small-model")).toBeDefined();
  });

  it("shows the refusal to someone who is not allowed", async () => {
    getStatus.mockRejectedValueOnce(new ApiProblem(403, "forbidden"));
    show();
    await tick(0);
    expect(screen.getByRole("alert").textContent).toBe(en.Problems.forbidden);
  });
});
