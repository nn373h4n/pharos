import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ACTIONS_FOR,
  RESULT_OF,
  actOnItem,
  isItem,
  isItemPage,
  itemsPath,
  listItems,
  type Item,
} from "@/lib/items";

const item: Item = {
  id: "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b",
  url: "https://news.example.org/a",
  title: "Release 1.2",
  excerpt: "Notes",
  lang: null,
  published_at: "2026-10-20T10:00:00Z",
  fetched_at: "2026-10-20T11:00:00Z",
  status: "new",
  hold_reason: null,
  updated_at: null,
  sources: [{ id: "s1", name: "Industry news" }],
};

afterEach(() => vi.unstubAllGlobals());

describe("itemsPath", () => {
  it("asks for one page", () => {
    expect(itemsPath({})).toBe("/api/items?limit=20&offset=0");
  });
  it("adds only what was asked for", () => {
    expect(itemsPath({ status: "held", offset: 40, sourceId: "a b" })).toBe(
      "/api/items?limit=20&offset=40&status=held&source_id=a+b",
    );
  });
});

describe("the review steps", () => {
  it("lead to a different state from the one they start in", () => {
    for (const [status, actions] of Object.entries(ACTIONS_FOR)) {
      for (const action of actions) expect(RESULT_OF[action]).not.toBe(status);
    }
  });
  it("match the backend's state machine", () => {
    expect(ACTIONS_FOR).toEqual({
      new: ["hold", "hide"],
      held: ["approve", "hide"],
      hidden: ["restore"],
    });
    expect(RESULT_OF).toEqual({ hold: "held", approve: "new", hide: "hidden", restore: "new" });
  });
});

describe("isItem", () => {
  it("accepts an article, with or without the reviewer fields", () => {
    expect(isItem(item)).toBe(true);
    expect(isItem({ ...item, updated_at: "2026-10-20T11:00:00Z" })).toBe(true);
    const member: Partial<Item> = { ...item };
    delete member.updated_at;
    expect(isItem(member)).toBe(true);
  });
  it.each([
    null,
    "text",
    [],
    {},
    { ...item, id: 5 },
    { ...item, url: null },
    { ...item, title: undefined },
    { ...item, status: "approved" },
    { ...item, published_at: 5 },
    { ...item, hold_reason: 5 },
    { ...item, sources: "x" },
    { ...item, sources: [null] },
    { ...item, sources: [{ id: "s" }] },
    { ...item, sources: [{ id: 1, name: "n" }] },
  ])("refuses %j", (value) => {
    expect(isItem(value)).toBe(false);
  });
});

describe("an article's identifier goes into a path, so it must be one", () => {
  it.each([
    "",
    "i1",
    "..",
    "../hold",
    "a/b",
    "a b",
    `${"6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b"}/x`,
    "6A1F0C2E3B4D4E5F8A9B0C1D2E3F4A5B",
  ])("refuses %j", (id) => {
    expect(isItem({ ...item, id })).toBe(false);
  });
  it("accepts an identifier in either case", () => {
    expect(isItem({ ...item, id: item.id.toUpperCase() })).toBe(true);
  });
});

describe("isItemPage", () => {
  it("accepts a page", () => {
    expect(isItemPage({ items: [item], total: 1 })).toBe(true);
    expect(isItemPage({ items: [], total: 0 })).toBe(true);
  });
  it.each([
    null,
    {},
    { items: [item] },
    { items: [item], total: -1 },
    { items: [item], total: 1.5 },
    { items: [item], total: "1" },
    { items: [{}], total: 1 },
    { items: "x", total: 1 },
  ])("refuses %j", (value) => {
    expect(isItemPage(value)).toBe(false);
  });
});

function answer(body: unknown, status = 200) {
  return vi.fn(async () => new Response(JSON.stringify(body), { status }));
}

describe("listItems and actOnItem", () => {
  it("reads a page", async () => {
    vi.stubGlobal("fetch", answer({ items: [item], total: 1 }));
    expect(await listItems({ status: "new" })).toEqual({ items: [item], total: 1 });
  });
  it("refuses an answer of another shape", async () => {
    vi.stubGlobal("fetch", answer({ items: [{ nope: true }], total: 1 }));
    await expect(listItems({})).rejects.toThrow("malformed_response");
  });
  it("sends a step with the CSRF token and reads the article back", async () => {
    const fetchMock = answer({ ...item, status: "held" });
    vi.stubGlobal("fetch", fetchMock);
    const result = await actOnItem("a/b", "hold", "token-1");
    expect(result.status).toBe("held");
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe("/api/items/a%2Fb/hold");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("token-1");
  });
  it("refuses a step whose answer is not an article", async () => {
    vi.stubGlobal("fetch", answer({ ok: true }));
    await expect(actOnItem("6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", "hide", "t")).rejects.toThrow(
      "malformed_response",
    );
  });
});
