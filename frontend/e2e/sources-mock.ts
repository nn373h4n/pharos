import type { Request, Route } from "@playwright/test";

// The sources part of the mocked API (docs/API.md, "Sources"). It follows the backend's rules:
// administrators and editors read, only administrators change, try or delete, a refused entry
// names its field, a name is unique, and a trial fetch is answered later by the "worker".

export type MockSource = {
  id: string;
  type: "rss" | "github" | "hackernews";
  name: string;
  config: Record<string, unknown>;
  enabled: boolean;
  interval_seconds: number;
  next_fetch_at: string;
  last_fetched_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  last_error_code: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string;
};

export type SourcesOptions = {
  sources?: MockSource[];
  /** Make the list fail: "down" answers 503, "forbidden" answers 403. */
  sourcesFail?: "down" | "forbidden";
  /**
   * What the trial fetch does: "ok" (default) answers after one more question, "hostile" answers
   * with hostile text, "never" stays pending, "limited" refuses the start with 429.
   */
  testFetch?: "ok" | "hostile" | "never" | "limited";
};

export type SourcesState = {
  sources: MockSource[];
  /** How many times the answer of each trial was asked for. */
  testReads: Record<string, number>;
};

export function source(n: number, changes: Partial<MockSource> = {}): MockSource {
  return {
    id: `30000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    type: "rss",
    name: `Source ${n}`,
    config: { url: `https://news.example.org/feed-${n}.xml` },
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

/** A working feed, a failing one, a switched-off repository and a search. */
export function defaultSources(): MockSource[] {
  return [
    source(1, { name: "Industry news" }),
    source(2, {
      name: "Broken feed",
      consecutive_failures: 4,
      last_error_code: "timed_out",
      last_success_at: null,
    }),
    source(3, {
      type: "github",
      name: "Widget releases",
      config: { repo: "acme/widget" },
      enabled: false,
    }),
    source(4, { type: "hackernews", name: "SAP talk", config: { query: "sap", min_points: 30 } }),
  ];
}

const PAGE = 20;
const SAFE_SAMPLE = [
  { title: "First story", url: "https://news.example.org/1", published_at: "2026-10-20T10:00:00Z" },
  { title: "Second story", url: "https://news.example.org/2", published_at: null },
];
const HOSTILE_SAMPLE = [
  { title: "<img src=x onerror=alert(1)> Story", url: "javascript:alert(1)", published_at: null },
  { title: "Looks like a link", url: "https://user:pass@evil.example/x", published_at: null },
];

type Problem = (
  status: number,
  code: string,
  headers?: Record<string, string>,
) => Parameters<Route["fulfill"]>[0];

function refused(path: string): Parameters<Route["fulfill"]>[0] {
  return {
    status: 422,
    contentType: "application/problem+json",
    body: JSON.stringify({
      code: "validation_failed",
      status: 422,
      errors: [
        { loc: ["body", path], type: path === "name" ? "string_too_short" : "url_not_allowed" },
      ],
    }),
  };
}

export async function handleSources(
  route: Route,
  request: Request,
  path: string,
  state: SourcesState,
  options: SourcesOptions,
  role: "admin" | "editor" | "member",
  problem: Problem,
): Promise<boolean> {
  if (!path.startsWith("/api/sources")) return false;
  const admin = role === "admin";
  if (role === "member") {
    await route.fulfill(problem(403, "forbidden"));
    return true;
  }
  const url = new URL(request.url());
  const method = request.method();

  if (path === "/api/sources" && method === "GET") {
    if (options.sourcesFail === "down") {
      await route.fulfill(problem(503, "service_unavailable"));
      return true;
    }
    if (options.sourcesFail === "forbidden") {
      await route.fulfill(problem(403, "forbidden"));
      return true;
    }
    const type = url.searchParams.get("type");
    const enabled = url.searchParams.get("enabled");
    const offset = Number.parseInt(url.searchParams.get("offset") ?? "0", 10);
    const matching = state.sources.filter(
      (found) =>
        (type === null || found.type === type) &&
        (enabled === null || found.enabled === (enabled === "true")),
    );
    await route.fulfill({
      json: { items: matching.slice(offset, offset + PAGE), total: matching.length },
    });
    return true;
  }

  if (path === "/api/sources" && method === "POST") {
    if (!admin) {
      await route.fulfill(problem(403, "forbidden"));
      return true;
    }
    const body = request.postDataJSON() as Partial<MockSource>;
    if (!body.name || body.name.trim() === "") {
      await route.fulfill(refused("name"));
      return true;
    }
    const address = String(body.config?.url ?? "");
    if (body.type === "rss" && !/^https:\/\/[a-z0-9-]+(\.[a-z0-9-]+)+\//.test(address)) {
      await route.fulfill(refused("config"));
      return true;
    }
    if (state.sources.some((found) => found.name === body.name)) {
      await route.fulfill(problem(409, "source_conflict"));
      return true;
    }
    const created = source(state.sources.length + 100, {
      type: body.type ?? "rss",
      name: body.name,
      config: body.config ?? {},
      interval_seconds: body.interval_seconds ?? 3600,
      enabled: body.enabled ?? true,
      last_fetched_at: null,
      last_success_at: null,
    });
    state.sources.unshift(created);
    await route.fulfill({ status: 201, json: created });
    return true;
  }

  const match = /^\/api\/sources\/([^/]+)(?:\/(runs|test-fetch)(?:\/([^/]+))?)?$/.exec(path);
  if (!match) return false;
  const found = state.sources.find((candidate) => candidate.id === decodeURIComponent(match[1]!));
  const part = match[2];

  if (!found) {
    await route.fulfill(problem(404, "not_found"));
    return true;
  }
  if (part === undefined && method === "PATCH") {
    if (!admin) {
      await route.fulfill(problem(403, "forbidden"));
      return true;
    }
    Object.assign(found, request.postDataJSON() as Partial<MockSource>);
    await route.fulfill({ json: found });
    return true;
  }
  if (part === undefined && method === "DELETE") {
    if (!admin) {
      await route.fulfill(problem(403, "forbidden"));
      return true;
    }
    state.sources.splice(state.sources.indexOf(found), 1);
    await route.fulfill({ status: 204 });
    return true;
  }
  if (part === "runs" && method === "GET") {
    await route.fulfill({
      json: {
        items: [
          {
            id: 2,
            started_at: "2026-10-30T09:00:00Z",
            finished_at: "2026-10-30T09:00:02Z",
            outcome: found.consecutive_failures > 0 ? "timeout" : "ok",
            error_code: found.last_error_code,
            http_status: found.consecutive_failures > 0 ? null : 200,
            duration_ms: 2000,
            bytes_fetched: found.consecutive_failures > 0 ? null : 2048,
            items_seen: found.consecutive_failures > 0 ? null : 12,
            items_new: found.consecutive_failures > 0 ? null : 3,
          },
          {
            id: 1,
            started_at: "2026-10-30T08:00:00Z",
            finished_at: "2026-10-30T08:00:01Z",
            outcome: "ok",
            error_code: null,
            http_status: 200,
            duration_ms: 900,
            bytes_fetched: 1024,
            items_seen: 10,
            items_new: 10,
          },
        ],
      },
    });
    return true;
  }
  if (part === "test-fetch" && method === "POST" && match[3] === undefined) {
    if (!admin) {
      await route.fulfill(problem(403, "forbidden"));
      return true;
    }
    if (options.testFetch === "limited") {
      await route.fulfill(problem(429, "rate_limited", { "Retry-After": "30" }));
      return true;
    }
    const testId = `50000000-0000-4000-8000-${String(found.id.slice(-12))}`;
    state.testReads[testId] = 0;
    await route.fulfill({ status: 202, json: { test_id: testId, retry_after_seconds: 30 } });
    return true;
  }
  if (part === "test-fetch" && method === "GET" && match[3] !== undefined) {
    if (!admin) {
      await route.fulfill(problem(403, "forbidden"));
      return true;
    }
    const testId = decodeURIComponent(match[3]);
    const reads = (state.testReads[testId] = (state.testReads[testId] ?? 0) + 1);
    const pending = options.testFetch === "never" || reads < 2;
    await route.fulfill({
      json: pending
        ? {
            status: "pending",
            outcome: null,
            error_code: null,
            http_status: null,
            entries_seen: null,
            entries_skipped: null,
            bytes_fetched: null,
            sample: [],
          }
        : {
            status: "done",
            outcome: "ok",
            error_code: null,
            http_status: null,
            entries_seen: 8,
            entries_skipped: 1,
            bytes_fetched: 4096,
            sample: options.testFetch === "hostile" ? HOSTILE_SAMPLE : SAFE_SAMPLE,
          },
    });
    return true;
  }
  return false;
}
