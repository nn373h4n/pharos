import { afterEach, describe, expect, it, vi } from "vitest";
import {
  configOf,
  createSource,
  deleteSource,
  fieldsOf,
  healthOf,
  isRun,
  isSource,
  isSourcePage,
  isTestAnswer,
  isTestStarted,
  listRuns,
  listSources,
  NO_FIELDS,
  readTestFetch,
  sourcesPath,
  startTestFetch,
  summaryOf,
  updateSource,
  type Source,
} from "@/lib/sources";

const ID = "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b";
const TEST = "7b2e1d3f-4c5e-4f60-9bac-1d2e3f4a5b6c";

function source(changes: Partial<Source> = {}): Source {
  return {
    id: ID,
    type: "rss",
    name: "Industry news",
    config: { url: "https://news.example.org/feed.xml" },
    enabled: true,
    interval_seconds: 3600,
    next_fetch_at: "2026-10-30T10:00:00Z",
    last_fetched_at: "2026-10-30T09:00:00Z",
    last_success_at: "2026-10-30T09:00:00Z",
    consecutive_failures: 0,
    last_error_code: null,
    created_by: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    ...changes,
  };
}

afterEach(() => vi.unstubAllGlobals());

function answer(body: unknown, status = 200) {
  return vi.fn(async () => new Response(status === 204 ? null : JSON.stringify(body), { status }));
}

describe("isSource", () => {
  it("accepts a source", () => {
    expect(isSource(source())).toBe(true);
    expect(isSource(source({ type: "github", config: { repo: "a/b" } }))).toBe(true);
    expect(isSource(source({ last_success_at: null, last_error_code: "timed_out" }))).toBe(true);
  });
  it.each([
    null,
    "text",
    [],
    {},
    { ...source(), id: "..", name: "x" },
    { ...source(), id: "i1" },
    { ...source(), type: "ftp" },
    { ...source(), name: 5 },
    { ...source(), config: [] },
    { ...source(), config: null },
    { ...source(), enabled: "yes" },
    { ...source(), interval_seconds: -1 },
    { ...source(), interval_seconds: 1.5 },
    { ...source(), consecutive_failures: "0" },
    { ...source(), last_success_at: 5 },
    { ...source(), next_fetch_at: null },
  ])("refuses %j", (value) => {
    expect(isSource(value)).toBe(false);
  });
});

describe("isSourcePage, isRun, isTestAnswer, isTestStarted", () => {
  const run = {
    id: 7,
    started_at: "2026-10-30T09:00:00Z",
    finished_at: null,
    outcome: "ok",
    error_code: null,
    http_status: 200,
    duration_ms: 120,
    bytes_fetched: 2048,
    items_seen: 3,
    items_new: 1,
  };
  const found = {
    status: "done",
    outcome: "ok",
    error_code: null,
    http_status: null,
    entries_seen: 3,
    entries_skipped: 0,
    bytes_fetched: 1024,
    sample: [{ title: "T", url: "https://a.example/", published_at: null }],
  };

  it("accepts what the backend sends", () => {
    expect(isSourcePage({ items: [source()], total: 1 })).toBe(true);
    expect(isRun(run)).toBe(true);
    expect(isTestAnswer(found)).toBe(true);
    expect(isTestAnswer({ ...found, status: "pending", outcome: null, sample: [] })).toBe(true);
    expect(isTestStarted({ test_id: TEST, retry_after_seconds: 30 })).toBe(true);
  });

  it.each([
    { items: [source()] },
    { items: [{}], total: 1 },
    { items: [source()], total: -1 },
    { items: "x", total: 1 },
  ])("refuses the page %j", (value) => {
    expect(isSourcePage(value)).toBe(false);
  });
  it.each([
    { ...run, id: "7" },
    { ...run, outcome: null },
    { ...run, items_seen: -1 },
    { ...run, http_status: "200" },
  ])("refuses the run %j", (value) => {
    expect(isRun(value)).toBe(false);
  });
  it.each([
    { ...found, status: "ok" },
    { ...found, sample: Array.from({ length: 6 }, () => found.sample[0]) },
    { ...found, sample: [{ title: "T" }] },
    { ...found, sample: "x" },
    { ...found, entries_seen: "3" },
    { ...found, outcome: 5 },
  ])("refuses the answer %j", (value) => {
    expect(isTestAnswer(value)).toBe(false);
  });
  it.each([
    { test_id: "x", retry_after_seconds: 30 },
    { test_id: TEST },
    { test_id: TEST, retry_after_seconds: -1 },
  ])("refuses the start %j", (value) => {
    expect(isTestStarted(value)).toBe(false);
  });
});

describe("the configuration, as a form has it", () => {
  it("trims what was typed", () => {
    expect(configOf("rss", { ...NO_FIELDS, url: "  https://a.example/f  " })).toEqual({
      url: "https://a.example/f",
    });
    expect(configOf("github", { ...NO_FIELDS, repo: " acme/widget " })).toEqual({
      repo: "acme/widget",
    });
  });
  it("leaves out a blank number, so the default applies", () => {
    expect(configOf("hackernews", { ...NO_FIELDS, query: " sap ", points: "  " })).toEqual({
      query: "sap",
    });
  });
  it("sends a number as a number and anything else as typed, for the backend to refuse", () => {
    expect(configOf("hackernews", { ...NO_FIELDS, query: "sap", points: "30" })).toEqual({
      query: "sap",
      min_points: 30,
    });
    expect(configOf("hackernews", { ...NO_FIELDS, query: "sap", points: "3x" })).toEqual({
      query: "sap",
      min_points: "3x",
    });
    expect(configOf("hackernews", { ...NO_FIELDS, query: "sap", points: "-1" })).toEqual({
      query: "sap",
      min_points: "-1",
    });
    expect(configOf("hackernews", { ...NO_FIELDS, query: "sap", points: "9".repeat(12) })).toEqual({
      query: "sap",
      min_points: "9".repeat(12),
    });
  });
  it("sends only the settings of the type", () => {
    const fields = { url: "https://a.example/", repo: "a/b", query: "q", points: "5" };
    expect(Object.keys(configOf("rss", fields))).toEqual(["url"]);
    expect(Object.keys(configOf("github", fields))).toEqual(["repo"]);
    expect(Object.keys(configOf("hackernews", fields))).toEqual(["query", "min_points"]);
  });
  it("fills the form from a source, whatever its configuration holds", () => {
    expect(fieldsOf(source())).toEqual({ ...NO_FIELDS, url: "https://news.example.org/feed.xml" });
    expect(
      fieldsOf(source({ type: "hackernews", config: { query: "sap", min_points: 30 } })),
    ).toEqual({
      ...NO_FIELDS,
      query: "sap",
      points: "30",
    });
    expect(fieldsOf(source({ config: { url: 5, min_points: "x" } }))).toEqual(NO_FIELDS);
  });
  it("says in one line what a source fetches", () => {
    expect(summaryOf(source())).toBe("https://news.example.org/feed.xml");
    expect(summaryOf(source({ type: "github", config: { repo: "acme/widget" } }))).toBe(
      "acme/widget",
    );
    expect(summaryOf(source({ type: "hackernews", config: { query: "sap" } }))).toBe("sap (20+)");
    expect(summaryOf(source({ type: "hackernews", config: { query: "sap", min_points: 5 } }))).toBe(
      "sap (5+)",
    );
    expect(summaryOf(source({ config: {} }))).toBe("");
  });
});

describe("healthOf", () => {
  it("is off for a source that is switched off, whatever else is true", () => {
    expect(healthOf(source({ enabled: false, consecutive_failures: 3 }))).toBe("off");
  });
  it("is failing after a failure, working after a success, new before either", () => {
    expect(healthOf(source({ consecutive_failures: 1 }))).toBe("failing");
    expect(healthOf(source())).toBe("ok");
    expect(healthOf(source({ last_success_at: null }))).toBe("new");
  });
});

describe("sourcesPath", () => {
  it("asks for one page", () => {
    expect(sourcesPath({})).toBe("/api/sources?limit=20&offset=0");
  });
  it("adds what was asked for", () => {
    expect(sourcesPath({ type: "github", enabled: false, offset: 40 })).toBe(
      "/api/sources?limit=20&offset=40&type=github&enabled=false",
    );
    expect(sourcesPath({ enabled: true })).toContain("enabled=true");
  });
});

describe("the calls", () => {
  it("reads a page and refuses another shape", async () => {
    vi.stubGlobal("fetch", answer({ items: [source()], total: 1 }));
    expect((await listSources({})).total).toBe(1);
    vi.stubGlobal("fetch", answer({ items: [{ nope: true }], total: 1 }));
    await expect(listSources({})).rejects.toThrow("malformed_response");
  });

  it("adds a source with the CSRF token", async () => {
    const fetchMock = answer(source(), 201);
    vi.stubGlobal("fetch", fetchMock);
    const body = {
      type: "rss" as const,
      name: "N",
      config: { url: "https://a.example/" },
      interval_seconds: 3600,
      enabled: false,
    };
    expect((await createSource(body, "token-1")).id).toBe(ID);
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect([path, init.method]).toEqual(["/api/sources", "POST"]);
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("token-1");
    expect(JSON.parse(init.body as string)).toEqual(body);
  });

  it("changes a source and deletes one", async () => {
    const changed = answer(source({ enabled: false }));
    vi.stubGlobal("fetch", changed);
    expect((await updateSource(ID, { enabled: false }, "t")).enabled).toBe(false);
    const [path, init] = changed.mock.calls[0] as unknown as [string, RequestInit];
    expect([path, init.method, init.body]).toEqual([
      `/api/sources/${ID}`,
      "PATCH",
      '{"enabled":false}',
    ]);
    const removed = answer(null, 204);
    vi.stubGlobal("fetch", removed);
    await expect(deleteSource(ID, "t")).resolves.toBeUndefined();
    expect((removed.mock.calls[0] as unknown as [string, RequestInit])[1].method).toBe("DELETE");
  });

  it("reads the runs", async () => {
    vi.stubGlobal("fetch", answer({ items: [] }));
    expect(await listRuns(ID)).toEqual([]);
    vi.stubGlobal("fetch", answer({ items: [{ id: "x" }] }));
    await expect(listRuns(ID)).rejects.toThrow("malformed_response");
  });

  it("starts a trial and reads its answer", async () => {
    const started = answer({ test_id: TEST, retry_after_seconds: 30 }, 202);
    vi.stubGlobal("fetch", started);
    expect((await startTestFetch(ID, "t")).test_id).toBe(TEST);
    expect((started.mock.calls[0] as unknown as [string])[0]).toBe(`/api/sources/${ID}/test-fetch`);
    const read = answer({
      status: "pending",
      outcome: null,
      error_code: null,
      http_status: null,
      entries_seen: null,
      entries_skipped: null,
      bytes_fetched: null,
      sample: [],
    });
    vi.stubGlobal("fetch", read);
    expect((await readTestFetch(ID, TEST)).status).toBe("pending");
    expect((read.mock.calls[0] as unknown as [string])[0]).toBe(
      `/api/sources/${ID}/test-fetch/${TEST}`,
    );
  });

  it.each(["..", "a/b", "", "i1", `${ID}/x`])("never builds a path from %j", async (id) => {
    const fetchMock = answer({});
    vi.stubGlobal("fetch", fetchMock);
    await expect(updateSource(id, { enabled: true }, "t")).rejects.toThrow("malformed_identifier");
    await expect(deleteSource(id, "t")).rejects.toThrow("malformed_identifier");
    await expect(listRuns(id)).rejects.toThrow("malformed_identifier");
    await expect(startTestFetch(id, "t")).rejects.toThrow("malformed_identifier");
    await expect(readTestFetch(id, TEST)).rejects.toThrow("malformed_identifier");
    await expect(readTestFetch(ID, id)).rejects.toThrow("malformed_identifier");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
