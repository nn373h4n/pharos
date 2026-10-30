import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import type { Run, Source } from "@/lib/sources";

const listRuns = vi.fn<(id: string) => Promise<Run[]>>();
vi.mock("@/lib/sources", async (original) => ({
  ...(await original<typeof import("@/lib/sources")>()),
  listRuns: (id: string) => listRuns(id),
}));

const { RunsPanel } = await import("./runs-panel");

const source = { id: "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", name: "N" } as Source;
const run = (id: number, changes: Partial<Run> = {}): Run => ({
  id,
  started_at: "2026-10-30T09:00:00Z",
  finished_at: "2026-10-30T09:00:02Z",
  outcome: "ok",
  error_code: null,
  http_status: 200,
  duration_ms: 2000,
  bytes_fetched: 1000,
  items_seen: 5,
  items_new: 2,
  ...changes,
});

function show() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <RunsPanel source={source} />
    </NextIntlClientProvider>,
  );
}

function open() {
  const details = document.querySelector("details")!;
  details.open = true;
  act(() => {
    fireEvent(details, new Event("toggle"));
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the latest fetches", () => {
  it("are not loaded until the section is opened", () => {
    show();
    expect(screen.getByText(en.Sources.actions.runs)).toBeDefined();
    expect(listRuns).not.toHaveBeenCalled();
  });

  it("are a table with a caption, headings and the result in words", async () => {
    listRuns.mockResolvedValue([
      run(2, {
        outcome: "bad_status",
        error_code: "http_error",
        http_status: 500,
        items_seen: null,
        items_new: null,
      }),
      run(1),
    ]);
    show();
    open();
    await waitFor(() => expect(screen.getByRole("table")).toBeDefined());
    expect(listRuns).toHaveBeenCalledWith(source.id);
    expect(screen.getAllByRole("columnheader").map((cell) => cell.textContent)).toEqual([
      en.Sources.runs.started,
      en.Sources.runs.result,
      en.Sources.runs.http,
      en.Sources.runs.seen,
      en.Sources.runs.added,
    ]);
    const rows = screen.getAllByRole("row").slice(1);
    expect(rows[0]!.textContent).toContain("Server refused");
    expect(rows[0]!.textContent).toContain("The server refused the request.");
    expect(rows[0]!.textContent).not.toContain("http_error");
    expect(rows[1]!.textContent).toContain("Fetched");
    expect(rows[1]!.textContent).toContain("200");
    expect(
      screen.getByRole("region", { name: en.Sources.runs.title }).getAttribute("tabindex"),
    ).toBe("0");
  });

  it("say so when there are none", async () => {
    listRuns.mockResolvedValue([]);
    show();
    open();
    await waitFor(() => expect(screen.getByText(en.Sources.runs.empty)).toBeDefined());
  });

  it("say so when they could not be loaded, and are asked for again next time", async () => {
    listRuns.mockRejectedValueOnce(new Error("boom"));
    show();
    open();
    await waitFor(() => expect(screen.getByText(en.Sources.runs.failed)).toBeDefined());
    listRuns.mockResolvedValue([run(1)]);
    const details = document.querySelector("details")!;
    details.open = false;
    act(() => {
      fireEvent(details, new Event("toggle"));
    });
    open();
    await waitFor(() => expect(screen.getByRole("table")).toBeDefined());
    expect(listRuns).toHaveBeenCalledTimes(2);
  });

  it("are loaded once", async () => {
    listRuns.mockResolvedValue([run(1)]);
    show();
    open();
    await waitFor(() => expect(screen.getByRole("table")).toBeDefined());
    open();
    expect(listRuns).toHaveBeenCalledTimes(1);
  });

  it("do not touch the page after it went away", async () => {
    let finish: (value: Run[]) => void = () => {};
    listRuns.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const { unmount } = show();
    open();
    unmount();
    await act(async () => finish([run(1)]));
    expect(document.body.textContent).toBe("");
  });
});
