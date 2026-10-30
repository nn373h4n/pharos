import type { Request, Route } from "@playwright/test";

// The personal feeds part of the mocked API (docs/API.md, "Personal feeds"). It follows the
// backend's rules: every signed-in person has their own feeds and nobody else's, at most five, an
// address pointing inwards is refused with `url` as the field, a feed is unique by address, and an
// operator can switch the whole feature off (then nothing exists: 404).

export type MockFeed = {
  id: string;
  name: string;
  url: string;
  enabled: boolean;
  interval_seconds: number;
  next_fetch_at: string;
  last_fetched_at: string | null;
  last_success_at: string | null;
  consecutive_failures: number;
  last_error_code: string | null;
  created_at: string;
  updated_at: string;
};

export type FeedsOptions = {
  feeds?: MockFeed[];
  /** Make the list fail: "down" answers 503. */
  feedsFail?: "down";
  /** The operator switched personal feeds off: every endpoint answers 404. */
  feedsOff?: boolean;
};

export type FeedsState = { feeds: MockFeed[] };

const MAX = 5;

export function feed(n: number, changes: Partial<MockFeed> = {}): MockFeed {
  return {
    id: `31000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    name: `Feed ${n}`,
    url: `https://blog.example.org/feed-${n}.xml`,
    enabled: true,
    interval_seconds: 3600,
    next_fetch_at: "2026-10-30T10:00:00Z",
    last_fetched_at: "2026-10-30T09:00:00Z",
    last_success_at: "2026-10-30T09:00:00Z",
    consecutive_failures: 0,
    last_error_code: null,
    created_at: `2026-10-0${Math.min(n, 9)}T10:00:00Z`,
    updated_at: "2026-10-01T10:00:00Z",
    ...changes,
  };
}

/** A working feed and a failing one. */
export function defaultFeeds(): MockFeed[] {
  return [
    feed(1, { name: "My reading" }),
    feed(2, {
      name: "Broken blog",
      consecutive_failures: 3,
      last_error_code: "timed_out",
      last_success_at: null,
    }),
  ];
}

type Problem = (
  status: number,
  code: string,
  headers?: Record<string, string>,
) => Parameters<Route["fulfill"]>[0];

function refused(field: "name" | "url"): Parameters<Route["fulfill"]>[0] {
  return {
    status: 422,
    contentType: "application/problem+json",
    body: JSON.stringify({
      code: "validation_failed",
      status: 422,
      errors: [
        { loc: ["body", field], type: field === "name" ? "invalid_text" : "url_not_allowed" },
      ],
    }),
  };
}

const ARTICLES = [
  {
    id: "41000000-0000-4000-8000-000000000001",
    url: "https://blog.example.org/post-1",
    title: "First post",
    excerpt: "A first excerpt.",
    lang: null,
    published_at: "2026-10-20T10:00:00Z",
    fetched_at: "2026-10-20T11:00:00Z",
    status: "new",
    hold_reason: null,
  },
  {
    id: "41000000-0000-4000-8000-000000000002",
    url: "https://203.0.113.9/post-2",
    title: "<img src=x onerror=alert(1)> Flagged post",
    excerpt: "Hostile <b>text</b>.",
    lang: null,
    published_at: null,
    fetched_at: "2026-10-20T11:00:00Z",
    status: "held",
    hold_reason: "link_ip_host",
  },
  {
    id: "41000000-0000-4000-8000-000000000003",
    url: "javascript:alert(1)",
    title: "Not a web address",
    excerpt: "",
    lang: null,
    published_at: null,
    fetched_at: "2026-10-20T11:00:00Z",
    status: "new",
    hold_reason: null,
  },
];

export async function handleFeeds(
  route: Route,
  request: Request,
  path: string,
  state: FeedsState,
  options: FeedsOptions,
  problem: Problem,
): Promise<boolean> {
  if (!path.startsWith("/api/me/feeds")) return false;
  if (options.feedsOff) {
    await route.fulfill(problem(404, "not_found"));
    return true;
  }
  const method = request.method();

  if (path === "/api/me/feeds" && method === "GET") {
    if (options.feedsFail === "down") {
      await route.fulfill(problem(503, "service_unavailable"));
      return true;
    }
    await route.fulfill({ json: { items: state.feeds, total: state.feeds.length } });
    return true;
  }

  if (path === "/api/me/feeds" && method === "POST") {
    const body = request.postDataJSON() as Partial<MockFeed>;
    if (!body.name || body.name.trim() === "") {
      await route.fulfill(refused("name"));
      return true;
    }
    if (!/^https?:\/\/[a-z0-9-]+(\.[a-z0-9-]+)+\//.test(String(body.url ?? ""))) {
      await route.fulfill(refused("url"));
      return true;
    }
    if (state.feeds.length >= MAX) {
      await route.fulfill(problem(409, "source_limit"));
      return true;
    }
    if (state.feeds.some((found) => found.url === body.url)) {
      await route.fulfill(problem(409, "source_conflict"));
      return true;
    }
    const created = feed(state.feeds.length + 100, {
      name: body.name,
      url: body.url,
      interval_seconds: body.interval_seconds ?? 3600,
      enabled: body.enabled ?? true,
      last_fetched_at: null,
      last_success_at: null,
    });
    state.feeds.push(created);
    await route.fulfill({ status: 201, json: created });
    return true;
  }

  const match = /^\/api\/me\/feeds\/([^/]+)(?:\/(runs|items))?$/.exec(path);
  if (!match) return false;
  const found = state.feeds.find((candidate) => candidate.id === decodeURIComponent(match[1]!));
  const part = match[2];
  if (!found) {
    await route.fulfill(problem(404, "not_found"));
    return true;
  }
  if (part === undefined && method === "PATCH") {
    Object.assign(found, request.postDataJSON() as Partial<MockFeed>);
    await route.fulfill({ json: found });
    return true;
  }
  if (part === undefined && method === "DELETE") {
    state.feeds.splice(state.feeds.indexOf(found), 1);
    await route.fulfill({ status: 204 });
    return true;
  }
  if (part === "runs" && method === "GET") {
    await route.fulfill({
      json: {
        items: [
          {
            id: 1,
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
        ],
      },
    });
    return true;
  }
  if (part === "items" && method === "GET") {
    await route.fulfill({
      json: { items: found.consecutive_failures > 0 ? [] : ARTICLES, total: ARTICLES.length },
    });
    return true;
  }
  return false;
}
