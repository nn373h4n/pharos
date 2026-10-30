import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import { ApiProblem } from "@/lib/api";
import type { Source, TestAnswer, TestStarted } from "@/lib/sources";

const startTestFetch = vi.fn<(id: string, csrf: string) => Promise<TestStarted>>();
const readTestFetch = vi.fn<(id: string, testId: string) => Promise<TestAnswer>>();

vi.mock("@/lib/sources", async (original) => ({
  ...(await original<typeof import("@/lib/sources")>()),
  startTestFetch: (id: string, csrf: string) => startTestFetch(id, csrf),
  readTestFetch: (id: string, testId: string) => readTestFetch(id, testId),
}));

const { TestPanel } = await import("./test-panel");

const ID = "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b";
const TEST = "7b2e1d3f-4c5e-4f60-9bac-1d2e3f4a5b6c";

const source = {
  id: ID,
  name: "Industry news",
} as Source;

const pending: TestAnswer = {
  status: "pending",
  outcome: null,
  error_code: null,
  http_status: null,
  entries_seen: null,
  entries_skipped: null,
  bytes_fetched: null,
  sample: [],
};
const done: TestAnswer = {
  status: "done",
  outcome: "ok",
  error_code: null,
  http_status: null,
  entries_seen: 8,
  entries_skipped: 1,
  bytes_fetched: 4096,
  sample: [
    {
      title: "First story",
      url: "https://news.example.org/1",
      published_at: "2026-10-20T10:00:00Z",
    },
    { title: "<b>Odd</b> story", url: "javascript:alert(1)", published_at: null },
  ],
};

function show() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <TestPanel source={source} csrf="token-1" />
    </NextIntlClientProvider>,
  );
}

const button = () =>
  screen.getByRole("button", {
    name: `${en.Sources.actions.test}: Industry news`,
  }) as HTMLButtonElement;
const region = () => screen.getByRole("status");

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  startTestFetch.mockResolvedValue({ test_id: TEST, retry_after_seconds: 30 });
  readTestFetch.mockResolvedValue(pending);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("a trial fetch", () => {
  it("asks for it with the CSRF token, waits, and shows the answer as text", async () => {
    show();
    expect(region().textContent).toBe("");
    fireEvent.click(button());
    await tick(0);
    expect(startTestFetch).toHaveBeenCalledWith(ID, "token-1");
    expect(region().textContent).toBe(en.Sources.test.waiting);
    expect(button().disabled).toBe(true);
    await tick(2000);
    expect(readTestFetch).toHaveBeenCalledTimes(1);
    expect(readTestFetch).toHaveBeenCalledWith(ID, TEST);
    expect(region().textContent).toBe(en.Sources.test.waiting); // still pending: asks again
    readTestFetch.mockResolvedValue(done);
    await tick(2000);
    expect(readTestFetch).toHaveBeenCalledTimes(2);
    const text = region().textContent ?? "";
    expect(text).toContain("Result: Fetched");
    expect(text).toContain("Articles seen: 8, left out: 1");
    expect(text).toContain("First story");
    expect(text).toContain("<b>Odd</b> story"); // third-party text stays text
    expect(region().querySelector("b")).toBeNull();
    const links = region().querySelectorAll("a");
    expect(links).toHaveLength(1); // the address that is not a web address is not a link
    expect(links[0]!.getAttribute("href")).toBe("https://news.example.org/1");
    expect(links[0]!.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(links[0]!.getAttribute("target")).toBe("_blank");
    expect(region().textContent).toContain("news.example.org");
    expect(button().disabled).toBe(false);
    await tick(10_000);
    expect(readTestFetch).toHaveBeenCalledTimes(2); // it stops asking once it has the answer
  });

  it("can be closed", async () => {
    readTestFetch.mockResolvedValue(done);
    show();
    fireEvent.click(button());
    await tick(0);
    await tick(2000);
    fireEvent.click(screen.getByRole("button", { name: en.Sources.test.close }));
    expect(region().textContent).toBe("");
  });

  it("shows a failed fetch with words, not codes", async () => {
    readTestFetch.mockResolvedValue({
      ...done,
      outcome: "bad_status",
      error_code: "http_error",
      http_status: 404,
      entries_seen: null,
      sample: [],
    });
    show();
    fireEvent.click(button());
    await tick(0);
    await tick(2000);
    const text = region().textContent ?? "";
    expect(text).toContain("Result: Server refused");
    expect(text).toContain("Problem: The server refused the request.");
    expect(text).toContain("Server answer: 404");
    expect(text).not.toContain("bad_status");
    expect(text).not.toContain("http_error");
  });

  it("says when the answer of the worker could not be used", async () => {
    readTestFetch.mockResolvedValue({ ...pending, status: "unavailable" });
    show();
    fireEvent.click(button());
    await tick(0);
    await tick(2000);
    expect(region().textContent).toContain(en.Sources.test.unavailable);
  });

  it("gives up after two minutes", async () => {
    show();
    fireEvent.click(button());
    await tick(0);
    await tick(119_000);
    expect(region().textContent).toBe(en.Sources.test.waiting);
    await tick(4000);
    expect(region().textContent).toBe(en.Sources.test.gaveUp);
    const calls = readTestFetch.mock.calls.length;
    expect(calls).toBeGreaterThan(30);
    await tick(20_000);
    expect(readTestFetch).toHaveBeenCalledTimes(calls);
    expect(button().disabled).toBe(false); // and it can be tried again
  });

  it.each([
    [new ApiProblem(429, "rate_limited", 12), "Too many requests. Try again in 12 seconds."],
    [new ApiProblem(429, "rate_limited"), en.Problems.rateLimitedNoWait],
    [new ApiProblem(503, "service_unavailable"), en.Problems.unavailable],
    [new ApiProblem(422, "validation_failed"), en.Problems.validation],
    [new ApiProblem(403, "forbidden"), en.Problems.forbidden],
    [new Error("boom"), en.Problems.generic],
  ])("tells a refused start in words: %j", async (error, text) => {
    startTestFetch.mockRejectedValue(error);
    show();
    fireEvent.click(button());
    await tick(0);
    expect(region().textContent).toBe(text);
    expect(readTestFetch).not.toHaveBeenCalled();
    expect(button().disabled).toBe(false);
  });

  it("stops asking when asking fails", async () => {
    readTestFetch.mockRejectedValue(new ApiProblem(404, "not_found"));
    show();
    fireEvent.click(button());
    await tick(0);
    await tick(2000);
    expect(region().textContent).toBe(en.Problems.notFound);
    await tick(10_000);
    expect(readTestFetch).toHaveBeenCalledTimes(1);
  });

  it("stops asking when the page goes away", async () => {
    const { unmount } = show();
    fireEvent.click(button());
    await tick(0);
    await tick(2000);
    expect(readTestFetch).toHaveBeenCalledTimes(1);
    unmount();
    await tick(20_000);
    expect(readTestFetch).toHaveBeenCalledTimes(1);
  });

  it("does not start a second trial while one is on its way", async () => {
    show();
    fireEvent.click(button());
    await tick(0);
    fireEvent.click(button());
    await tick(0);
    expect(startTestFetch).toHaveBeenCalledTimes(1);
  });

  it("ignores an answer that arrives after a new start", async () => {
    let first: (value: TestAnswer) => void = () => {};
    readTestFetch.mockReturnValueOnce(new Promise((resolve) => (first = resolve)));
    show();
    fireEvent.click(button());
    await tick(0);
    await tick(2000); // the first question is out
    expect(readTestFetch).toHaveBeenCalledTimes(1);
    cleanup(); // the person left; the late answer must not touch anything
    await act(async () => first(done));
    expect(document.body.textContent).not.toContain("First story");
  });
});

describe("a poll still on its way when the page goes away", () => {
  it("does not ask again afterwards", async () => {
    let late: (value: TestAnswer) => void = () => {};
    readTestFetch.mockReturnValueOnce(new Promise((resolve) => (late = resolve)));
    const { unmount } = show();
    fireEvent.click(button());
    await tick(0);
    await tick(2000);
    expect(readTestFetch).toHaveBeenCalledTimes(1);
    unmount();
    await act(async () => late(pending));
    await tick(20_000);
    expect(readTestFetch).toHaveBeenCalledTimes(1);
  });
});
