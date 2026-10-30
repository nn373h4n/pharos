import { afterEach, describe, expect, it, vi } from "vitest";
import {
  asSourceProblems,
  createFeed,
  deleteFeed,
  feedsPath,
  healthOf,
  isFeed,
  isFeedItem,
  isFeedItemPage,
  isFeedPage,
  listFeedItems,
  listFeedRuns,
  listFeeds,
  updateFeed,
  type Feed,
} from "@/lib/feeds";

const ID = "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b";

function feed(changes: Partial<Feed> = {}): Feed {
  return {
    id: ID,
    name: "My reading",
    url: "https://blog.example.org/feed.xml",
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

const article = {
  id: ID,
  url: "https://news.example.org/1",
  title: "T",
  excerpt: "E",
  lang: null,
  published_at: null,
  fetched_at: "2026-10-30T09:00:00Z",
  status: "new",
  hold_reason: null,
};

function answer(body: unknown, status = 200) {
  return vi.fn(async () => new Response(status === 204 ? null : JSON.stringify(body), { status }));
}

afterEach(() => vi.unstubAllGlobals());

describe("isFeed", () => {
  it("accepts a feed", () => {
    expect(isFeed(feed())).toBe(true);
    expect(isFeed(feed({ last_success_at: null, last_error_code: "timed_out" }))).toBe(true);
  });
  it.each([
    null,
    "text",
    [],
    {},
    { ...feed(), id: ".." },
    { ...feed(), name: 5 },
    { ...feed(), url: null },
    { ...feed(), enabled: "yes" },
    { ...feed(), interval_seconds: -1 },
    { ...feed(), consecutive_failures: 1.5 },
    { ...feed(), next_fetch_at: null },
    { ...feed(), last_success_at: 5 },
  ])("refuses %j", (value) => {
    expect(isFeed(value)).toBe(false);
  });
});

describe("pages and articles", () => {
  it("accepts what the backend sends", () => {
    expect(isFeedPage({ items: [feed()], total: 1 })).toBe(true);
    expect(isFeedItem(article)).toBe(true);
    expect(isFeedItemPage({ items: [article], total: 1 })).toBe(true);
  });
  it.each([{ items: [feed()] }, { items: [{}], total: 1 }, { items: [feed()], total: -1 }])(
    "refuses the page %j",
    (value) => {
      expect(isFeedPage(value)).toBe(false);
    },
  );
  it.each([
    { ...article, id: "x" },
    { ...article, status: "deleted" },
    { ...article, title: 1 },
    { ...article, hold_reason: 5 },
  ])("refuses the article %j", (value) => {
    expect(isFeedItem(value)).toBe(false);
  });
});

describe("healthOf", () => {
  it("is off for a feed that is switched off, whatever else is true", () => {
    expect(healthOf(feed({ enabled: false, consecutive_failures: 3 }))).toBe("off");
  });
  it("is failing after a failure, working after a success, new before either", () => {
    expect(healthOf(feed({ consecutive_failures: 1 }))).toBe("failing");
    expect(healthOf(feed())).toBe("ok");
    expect(healthOf(feed({ last_success_at: null }))).toBe("new");
  });
});

describe("asSourceProblems", () => {
  it("calls the address what the form of sources calls its settings", () => {
    expect(asSourceProblems([{ loc: ["body", "url"], type: "url_not_allowed" }])).toEqual([
      { loc: ["body", "config"], type: "url_not_allowed" },
    ]);
  });
  it("leaves the other parts alone", () => {
    const others = [
      { loc: ["body", "name"], type: "invalid_text" },
      { loc: ["body", "interval_seconds"], type: "greater_than_equal" },
    ];
    expect(asSourceProblems(others)).toEqual(others);
  });
});

describe("the calls", () => {
  it("asks for the first page", () => {
    expect(feedsPath()).toBe("/api/me/feeds?limit=20&offset=0");
  });

  it("reads the list and refuses another shape", async () => {
    vi.stubGlobal("fetch", answer({ items: [feed()], total: 1 }));
    expect((await listFeeds()).total).toBe(1);
    vi.stubGlobal("fetch", answer({ items: [{ nope: true }], total: 1 }));
    await expect(listFeeds()).rejects.toThrow("malformed_response");
  });

  it("adds a feed with the CSRF token", async () => {
    const fetchMock = answer(feed(), 201);
    vi.stubGlobal("fetch", fetchMock);
    const body = {
      name: "N",
      url: "https://a.example/feed.xml",
      interval_seconds: 3600,
      enabled: true,
    };
    expect((await createFeed(body, "token-1")).id).toBe(ID);
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect([path, init.method]).toEqual(["/api/me/feeds", "POST"]);
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("token-1");
    expect(JSON.parse(init.body as string)).toEqual(body);
  });

  it("changes and deletes a feed", async () => {
    const changed = answer(feed({ enabled: false }));
    vi.stubGlobal("fetch", changed);
    expect((await updateFeed(ID, { enabled: false }, "t")).enabled).toBe(false);
    const [path, init] = changed.mock.calls[0] as unknown as [string, RequestInit];
    expect([path, init.method, init.body]).toEqual([
      `/api/me/feeds/${ID}`,
      "PATCH",
      '{"enabled":false}',
    ]);
    const removed = answer(null, 204);
    vi.stubGlobal("fetch", removed);
    await expect(deleteFeed(ID, "t")).resolves.toBeUndefined();
    expect((removed.mock.calls[0] as unknown as [string, RequestInit])[1].method).toBe("DELETE");
  });

  it("reads the runs and the articles", async () => {
    vi.stubGlobal("fetch", answer({ items: [] }));
    expect(await listFeedRuns(ID)).toEqual([]);
    vi.stubGlobal("fetch", answer({ items: [{ id: "x" }] }));
    await expect(listFeedRuns(ID)).rejects.toThrow("malformed_response");
    const fetchMock = answer({ items: [article], total: 1 });
    vi.stubGlobal("fetch", fetchMock);
    expect((await listFeedItems(ID, 20)).total).toBe(1);
    expect((fetchMock.mock.calls[0] as unknown as [string])[0]).toBe(
      `/api/me/feeds/${ID}/items?limit=20&offset=20`,
    );
  });

  it.each(["..", "a/b", "", "i1", `${ID}/x`])("never builds a path from %j", async (id) => {
    const fetchMock = answer({});
    vi.stubGlobal("fetch", fetchMock);
    await expect(updateFeed(id, { enabled: true }, "t")).rejects.toThrow("malformed_identifier");
    await expect(deleteFeed(id, "t")).rejects.toThrow("malformed_identifier");
    await expect(listFeedRuns(id)).rejects.toThrow("malformed_identifier");
    await expect(listFeedItems(id)).rejects.toThrow("malformed_identifier");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
