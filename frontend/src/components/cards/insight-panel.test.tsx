import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import { ApiProblem } from "@/lib/api";
import type { InsightAnswer, InteractionKind } from "@/lib/cards";

const getInsight = vi.fn<(id: string) => Promise<InsightAnswer>>();
const putInteraction =
  vi.fn<(id: string, kind: InteractionKind, csrf: string, minutes?: number) => Promise<void>>();

vi.mock("@/lib/cards", async (original) => ({
  ...(await original<typeof import("@/lib/cards")>()),
  getInsight: (id: string) => getInsight(id),
  putInteraction: (id: string, kind: InteractionKind, csrf: string, minutes?: number) =>
    putInteraction(id, kind, csrf, minutes),
}));

const { InsightPanel } = await import("./insight-panel");

const ID = "40000000-0000-4000-8000-000000000001";
const ready: InsightAnswer = {
  status: "ready",
  reason: null,
  insight: {
    profession: "accountant",
    language: "en",
    summary: "It shortens the month-end.",
    how_to_apply: ["Open the ledger.", "Ask for odd entries."],
    try_prompt: "List unusual entries in [LEDGER EXTRACT].",
    safety_note: "Do not paste real client data.",
  },
};
const pending: InsightAnswer = { status: "pending", reason: null, insight: null };

function show() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <InsightPanel itemId={ID} title="Story" csrf="token-1" />
    </NextIntlClientProvider>,
  );
}

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  getInsight.mockResolvedValue(ready);
  putInteraction.mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("the suggestion", () => {
  it("asks when it is opened, and shows the steps, the prompt and the notes", async () => {
    show();
    expect(screen.getByText(en.Cards.insight.loading)).toBeDefined();
    await tick(0);
    expect(getInsight).toHaveBeenCalledWith(ID);
    expect(screen.getByText("It shortens the month-end.")).toBeDefined();
    expect(screen.getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Open the ledger.",
      "Ask for odd entries.",
    ]);
    const field = screen.getByLabelText(en.Cards.insight.promptLabel) as HTMLTextAreaElement;
    expect(field.value).toBe("List unusual entries in [LEDGER EXTRACT].");
    expect(field.readOnly).toBe(true);
    expect(screen.getByText("Do not paste real client data.")).toBeDefined();
    expect(screen.getByText(en.Cards.insight.productNote)).toBeDefined(); // always, whatever the model said
  });

  it("waits while it is made, asks again every few seconds and gives up after a minute and a half", async () => {
    getInsight.mockResolvedValue(pending);
    show();
    await tick(0);
    expect(screen.getByText(en.Cards.insight.pending)).toBeDefined();
    await tick(3000);
    expect(getInsight).toHaveBeenCalledTimes(2);
    getInsight.mockResolvedValue(ready);
    await tick(3000);
    expect(screen.getByText("It shortens the month-end.")).toBeDefined();
    const calls = getInsight.mock.calls.length;
    await tick(30_000);
    expect(getInsight).toHaveBeenCalledTimes(calls); // it stops asking once it has the answer
  });

  it("gives up with words, not silence", async () => {
    getInsight.mockResolvedValue(pending);
    show();
    await tick(0);
    await tick(93_000);
    expect(screen.getByText(en.Cards.insight.gaveUp)).toBeDefined();
    const calls = getInsight.mock.calls.length;
    await tick(10_000);
    expect(getInsight).toHaveBeenCalledTimes(calls);
  });

  it.each(["off", "rejected", "invalid"] as const)(
    "says why there is none (%s)",
    async (reason) => {
      getInsight.mockResolvedValue({ status: "unavailable", reason, insight: null });
      show();
      await tick(0);
      expect(screen.getByText(en.Cards.insight.unavailable[reason])).toBeDefined();
      expect(screen.queryByLabelText(en.Cards.insight.promptLabel)).toBeNull();
    },
  );

  it("tells a failure in words and stops asking", async () => {
    getInsight.mockRejectedValue(new ApiProblem(503, "service_unavailable"));
    show();
    await tick(0);
    expect(screen.getByRole("alert").textContent).toBe(en.Problems.unavailable);
    await tick(10_000);
    expect(getInsight).toHaveBeenCalledTimes(1);
  });

  it("stops asking when it is closed", async () => {
    getInsight.mockResolvedValue(pending);
    const { unmount } = show();
    await tick(0);
    unmount();
    await tick(10_000);
    expect(getInsight).toHaveBeenCalledTimes(1);
  });
});

describe("text a model wrote", () => {
  it("stays text: no element, no link, whatever it contains", async () => {
    getInsight.mockResolvedValue({
      status: "ready",
      reason: null,
      insight: {
        profession: "accountant",
        language: "en",
        summary: "<img src=x onerror=alert(1)> see https://evil.example",
        how_to_apply: ["<script>alert(2)</script>", "[click](http://evil.example)"],
        try_prompt: "<b>bold</b> [TEXT]",
        safety_note: "<a href='http://evil.example'>go</a>",
      },
    });
    const { container } = show();
    await tick(0);
    expect(container.querySelector("img, script, b, a")).toBeNull();
    expect(container.textContent).toContain(
      "<img src=x onerror=alert(1)> see https://evil.example",
    );
    expect(container.textContent).toContain("<script>alert(2)</script>");
    expect(container.textContent).toContain("<a href='http://evil.example'>go</a>");
    expect((screen.getByLabelText(en.Cards.insight.promptLabel) as HTMLTextAreaElement).value).toBe(
      "<b>bold</b> [TEXT]",
    );
  });
});

describe("copying", () => {
  it("puts the prompt on the clipboard and says so", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    show();
    await tick(0);
    fireEvent.click(screen.getByRole("button", { name: `${en.Cards.insight.copy}: Story` }));
    await tick(0);
    expect(writeText).toHaveBeenCalledWith("List unusual entries in [LEDGER EXTRACT].");
    expect(screen.getByText(en.Cards.insight.copied)).toBeDefined();
  });

  it("selects the text and says what to press when the clipboard is not available", async () => {
    vi.stubGlobal("navigator", {});
    show();
    await tick(0);
    fireEvent.click(screen.getByRole("button", { name: `${en.Cards.insight.copy}: Story` }));
    await tick(0);
    expect(screen.getByText(en.Cards.insight.copyManual)).toBeDefined();
    expect(document.activeElement).toBe(screen.getByLabelText(en.Cards.insight.promptLabel));
  });
});

describe("feedback", () => {
  it("records that it was tried, once", async () => {
    show();
    await tick(0);
    const button = screen.getByRole("button", { name: `${en.Cards.insight.tried}: Story` });
    fireEvent.click(button);
    await tick(0);
    expect(putInteraction).toHaveBeenCalledWith(ID, "tried", "token-1", undefined);
    expect(screen.getByText(en.Cards.insight.triedDone)).toBeDefined();
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });

  it("records the minutes saved, and only whole minutes from 1 to 480", async () => {
    show();
    await tick(0);
    const field = screen.getByLabelText(en.Cards.insight.minutesLabel);
    const record = screen.getByRole("button", { name: `${en.Cards.insight.minutesSave}: Story` });
    for (const bad of ["", "0", "481", "2.5", "abc", "-3"]) {
      fireEvent.change(field, { target: { value: bad } });
      fireEvent.click(record);
      expect(screen.getByRole("alert").textContent).toBe(en.Cards.insight.minutesInvalid);
      expect(field.getAttribute("aria-invalid")).toBe("true");
    }
    expect(putInteraction).not.toHaveBeenCalled();
    fireEvent.change(field, { target: { value: "45" } });
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(record);
    await tick(0);
    expect(putInteraction).toHaveBeenCalledWith(ID, "time_saved", "token-1", 45);
    expect(screen.getByText(en.Cards.insight.minutesDone)).toBeDefined();
  });

  it("says in words when recording fails", async () => {
    putInteraction.mockRejectedValue(new ApiProblem(429, "rate_limited", 12));
    show();
    await tick(0);
    fireEvent.click(screen.getByRole("button", { name: `${en.Cards.insight.tried}: Story` }));
    await tick(0);
    expect(screen.getByText("Too many requests. Try again in 12 seconds.")).toBeDefined();
  });
});
