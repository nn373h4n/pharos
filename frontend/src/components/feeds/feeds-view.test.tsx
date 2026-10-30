import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import { ApiProblem } from "@/lib/api";
import type { Feed, FeedPage } from "@/lib/feeds";

const listFeeds = vi.fn<() => Promise<FeedPage>>();
const createFeed = vi.fn<(body: unknown, csrf: string) => Promise<Feed>>();
const updateFeed = vi.fn<(id: string, body: unknown, csrf: string) => Promise<Feed>>();
const deleteFeed = vi.fn<(id: string, csrf: string) => Promise<void>>();

vi.mock("@/components/auth/session-provider", () => ({
  useSession: () => ({
    state: {
      status: "authenticated",
      session: { csrf_token: "token-1", user: { role: "member", display_name: "A" } },
    },
  }),
}));
vi.mock("@/components/sources/runs-panel", () => ({ RunsPanel: () => null }));
vi.mock("@/components/feeds/feed-articles", () => ({ FeedArticles: () => null }));
vi.mock("@/lib/feeds", async (original) => ({
  ...(await original<typeof import("@/lib/feeds")>()),
  listFeeds: () => listFeeds(),
  createFeed: (body: unknown, csrf: string) => createFeed(body, csrf),
  updateFeed: (id: string, body: unknown, csrf: string) => updateFeed(id, body, csrf),
  deleteFeed: (id: string, csrf: string) => deleteFeed(id, csrf),
}));

const { FeedsView } = await import("./feeds-view");

const id = (n: number) => `6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a${String(n).padStart(2, "0")}`;

function feed(n: number, changes: Partial<Feed> = {}): Feed {
  return {
    id: id(n),
    name: `Feed ${n}`,
    url: `https://blog.example.org/${n}.xml`,
    enabled: true,
    interval_seconds: 3600,
    next_fetch_at: "2026-10-30T10:00:00Z",
    last_fetched_at: "2026-10-30T09:00:00Z",
    last_success_at: "2026-10-30T09:00:00Z",
    consecutive_failures: 0,
    last_error_code: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    ...changes,
  };
}

const page = (items: Feed[], total = items.length): FeedPage => ({ items, total });

function show() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <FeedsView />
    </NextIntlClientProvider>,
  );
}

async function loaded() {
  await waitFor(() => expect(screen.queryByText(en.Feeds.loading)).toBeNull());
}

const act = (action: string, name: string) =>
  screen.getByRole("button", { name: `${action}: ${name}` }) as HTMLButtonElement;
const status = () => screen.getByRole("status");
const add = () => screen.getByRole("button", { name: en.Feeds.actions.add }) as HTMLButtonElement;

beforeEach(() => {
  listFeeds.mockResolvedValue(page([feed(1), feed(2)]));
  createFeed.mockResolvedValue(feed(3));
  updateFeed.mockResolvedValue(feed(1));
  deleteFeed.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the list", () => {
  it("shows each feed with its state and the controls for it", async () => {
    show();
    await loaded();
    expect(screen.getByText("Feed 1")).toBeDefined();
    expect(
      screen.getByText(en.Feeds.used.replace("{count}", "2").replace("{max}", "5")),
    ).toBeDefined();
    expect(act(en.Sources.actions.edit, "Feed 1")).toBeDefined();
    expect(act(en.Sources.actions.disable, "Feed 1")).toBeDefined();
    expect(act(en.Sources.actions.delete, "Feed 1")).toBeDefined();
  });

  it("shows a problem in words, not as a code", async () => {
    listFeeds.mockResolvedValue(
      page([feed(1, { consecutive_failures: 3, last_error_code: "timed_out" })]),
    );
    show();
    await loaded();
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("timed_out");
    expect(text).toContain(en.Sources.health.failing);
  });

  it("shows the address as text, not as a link", async () => {
    show();
    await loaded();
    expect(screen.getByText("https://blog.example.org/1.xml")).toBeDefined();
    expect(document.querySelectorAll("a")).toHaveLength(0);
  });

  it("invites to add the first one", async () => {
    listFeeds.mockResolvedValue(page([]));
    show();
    await loaded();
    expect(screen.getByText(en.Feeds.empty)).toBeDefined();
    expect(add().disabled).toBe(false);
  });

  it("does not let a sixth be added", async () => {
    listFeeds.mockResolvedValue(page([1, 2, 3, 4, 5].map((n) => feed(n))));
    show();
    await loaded();
    expect(add().disabled).toBe(true);
    expect(screen.getByText(en.Feeds.limitReached.replace("{max}", "5"))).toBeDefined();
  });
});

describe("states", () => {
  it("shows a loading state", () => {
    listFeeds.mockReturnValue(new Promise(() => {}));
    show();
    expect(status().textContent).toContain(en.Feeds.loading);
  });

  it("shows a failure and tries again", async () => {
    listFeeds.mockRejectedValueOnce(new Error("boom"));
    show();
    await waitFor(() => expect(screen.getByText(en.Feeds.loadFailed)).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: en.Sources.retry }));
    await waitFor(() => expect(screen.getByText("Feed 1")).toBeDefined());
  });

  it("says when the feature is switched off, and offers nothing to add", async () => {
    listFeeds.mockRejectedValue(new ApiProblem(404, "not_found"));
    show();
    await waitFor(() => expect(screen.getByText(en.Feeds.off)).toBeDefined());
    expect(screen.queryByRole("button", { name: en.Feeds.actions.add })).toBeNull();
  });

  it("shows a refusal without a retry button", async () => {
    listFeeds.mockRejectedValue(new ApiProblem(403, "forbidden"));
    show();
    await waitFor(() => expect(screen.getByText(en.Problems.forbidden)).toBeDefined());
    expect(screen.queryByRole("button", { name: en.Sources.retry })).toBeNull();
  });
});

describe("switching on and off", () => {
  it("sends the change with the CSRF token, reloads, says so and returns focus to the list", async () => {
    show();
    await loaded();
    fireEvent.click(act(en.Sources.actions.disable, "Feed 1"));
    await waitFor(() => expect(status().textContent).toContain("Switched off: Feed 1"));
    expect(updateFeed).toHaveBeenCalledWith(id(1), { enabled: false }, "token-1");
    expect(listFeeds).toHaveBeenCalledTimes(2);
    expect(document.activeElement).toBe(
      screen.getByRole("heading", { level: 2, name: en.Feeds.results }),
    );
  });

  it("tells a refusal as an alert", async () => {
    updateFeed.mockRejectedValue(new ApiProblem(503, "service_unavailable"));
    show();
    await loaded();
    fireEvent.click(act(en.Sources.actions.disable, "Feed 1"));
    expect((await screen.findByRole("alert")).textContent).toBe(en.Problems.unavailable);
  });

  it("locks everything while a step is on its way", async () => {
    let finish: (value: Feed) => void = () => {};
    updateFeed.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    show();
    await loaded();
    fireEvent.click(act(en.Sources.actions.disable, "Feed 1"));
    await waitFor(() => expect(act(en.Sources.actions.disable, "Feed 2").disabled).toBe(true));
    expect(add().disabled).toBe(true);
    finish(feed(1));
    await waitFor(() => expect(act(en.Sources.actions.disable, "Feed 2").disabled).toBe(false));
  });

  it("shows the newest request, not the one that answers last", async () => {
    let first: (value: FeedPage) => void = () => {};
    listFeeds.mockReturnValueOnce(new Promise((resolve) => (first = resolve)));
    show();
    await waitFor(() => expect(listFeeds).toHaveBeenCalledTimes(1));
    listFeeds.mockResolvedValue(page([feed(7)]));
    fireEvent.click(screen.getByRole("button", { name: en.Feeds.actions.add }));
    fireEvent.click(await screen.findByRole("button", { name: en.Sources.form.cancel }));
    first(page([feed(1)]));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText("Feed 7")).toBeNull();
  });
});

describe("adding", () => {
  async function open() {
    show();
    await loaded();
    fireEvent.click(add());
    return screen.findByRole("dialog");
  }
  const fill = (label: string, value: string) =>
    fireEvent.change(screen.getByLabelText(label), { target: { value } });

  it("sends a new feed and shows it", async () => {
    await open();
    fill(en.Sources.form.name, "Fresh");
    fill(en.Sources.form.url, "https://fresh.example.org/feed.xml");
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.save }));
    await waitFor(() => expect(createFeed).toHaveBeenCalledTimes(1));
    expect(createFeed.mock.calls[0]![0]).toEqual({
      name: "Fresh",
      url: "https://fresh.example.org/feed.xml",
      interval_seconds: 3600,
      enabled: true,
    });
    expect(createFeed.mock.calls[0]![1]).toBe("token-1");
    await waitFor(() => expect(status().textContent).toContain("Added: Fresh"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows a refusal of the address at the field and keeps the dialog", async () => {
    await open();
    fill(en.Sources.form.name, "Fresh");
    fill(en.Sources.form.url, "http://127.1/");
    createFeed.mockRejectedValue(
      new ApiProblem(422, "validation_failed", null, [
        { loc: ["body", "url"], type: "url_not_allowed" },
      ]),
    );
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.save }));
    await waitFor(() =>
      expect(screen.getByLabelText(en.Sources.form.url).getAttribute("aria-invalid")).toBe("true"),
    );
    expect(screen.getByText(en.Sources.fieldErrors.urlNotAllowed)).toBeDefined();
    expect(screen.getByRole("dialog")).toBeDefined();
  });

  it.each([
    ["source_limit", en.Problems.sourceLimit],
    ["source_conflict", en.Problems.sourceConflict],
  ])("tells %s for the whole form", async (code, text) => {
    await open();
    fill(en.Sources.form.name, "Fresh");
    fill(en.Sources.form.url, "https://fresh.example.org/feed.xml");
    createFeed.mockRejectedValue(new ApiProblem(409, code));
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.save }));
    await waitFor(() => expect(within(screen.getByRole("dialog")).getByText(text)).toBeDefined());
  });

  it("closes with Cancel without sending", async () => {
    await open();
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.cancel }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(createFeed).not.toHaveBeenCalled();
  });
});

describe("editing", () => {
  it("sends the name, address and interval", async () => {
    show();
    await loaded();
    fireEvent.click(act(en.Sources.actions.edit, "Feed 1"));
    await screen.findByRole("dialog");
    fireEvent.change(screen.getByLabelText(en.Sources.form.name), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.save }));
    await waitFor(() => expect(updateFeed).toHaveBeenCalledTimes(1));
    const [sent, body] = updateFeed.mock.calls[0]!;
    expect(sent).toBe(id(1));
    expect(body).toEqual({
      name: "Renamed",
      url: "https://blog.example.org/1.xml",
      interval_seconds: 3600,
    });
    await waitFor(() => expect(status().textContent).toContain("Saved: Renamed"));
  });
});

describe("deleting", () => {
  async function ask(name = "Feed 1") {
    fireEvent.click(act(en.Sources.actions.delete, name));
    return screen.findByRole("dialog");
  }

  it("asks first, and Cancel deletes nothing", async () => {
    show();
    await loaded();
    const dialog = await ask();
    expect(within(dialog).getByText(/Feed 1/)).toBeDefined();
    fireEvent.click(within(dialog).getByRole("button", { name: en.Feeds.deleteDialog.cancel }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(deleteFeed).not.toHaveBeenCalled();
  });

  it("deletes after confirmation", async () => {
    show();
    await loaded();
    const dialog = await ask();
    fireEvent.click(within(dialog).getByRole("button", { name: en.Feeds.deleteDialog.confirm }));
    await waitFor(() => expect(deleteFeed).toHaveBeenCalledWith(id(1), "token-1"));
    await waitFor(() => expect(status().textContent).toContain("Deleted: Feed 1"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("does not close the question while the deletion is on its way", async () => {
    let finish: () => void = () => {};
    deleteFeed.mockReturnValue(new Promise<void>((resolve) => (finish = resolve)));
    show();
    await loaded();
    const dialog = await ask();
    fireEvent.click(within(dialog).getByRole("button", { name: en.Feeds.deleteDialog.confirm }));
    await waitFor(() => expect(deleteFeed).toHaveBeenCalled());
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(screen.queryByRole("dialog")).not.toBeNull();
    finish();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("tells a refusal and closes the question", async () => {
    deleteFeed.mockRejectedValue(new ApiProblem(404, "not_found"));
    show();
    await loaded();
    const dialog = await ask();
    fireEvent.click(within(dialog).getByRole("button", { name: en.Feeds.deleteDialog.confirm }));
    expect((await screen.findByRole("alert")).textContent).toBe(en.Problems.notFound);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("while the next list is on its way", () => {
  it("does not keep showing the previous one", async () => {
    show();
    await loaded();
    expect(screen.getByText("Feed 1")).toBeDefined();
    listFeeds.mockReturnValue(new Promise(() => {}));
    fireEvent.click(act(en.Sources.actions.disable, "Feed 1"));
    await waitFor(() => expect(status().textContent).toContain(en.Feeds.loading));
    expect(screen.queryByText("Feed 2")).toBeNull();
  });
});
