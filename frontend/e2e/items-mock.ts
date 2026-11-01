import type { Request, Route } from "@playwright/test";

// The articles part of the mocked API (docs/API.md, "Items"). It follows the backend's rules:
// a member sees only current articles and gets 403 for anything else, only administrators and
// editors review, every step is a move of the same small state machine, and a step that does not
// exist answers 409 item_state.

export type MockItem = {
  id: string;
  url: string;
  title: string;
  excerpt: string;
  lang: string | null;
  published_at: string | null;
  fetched_at: string;
  status: "new" | "held" | "hidden";
  hold_reason: string | null;
  updated_at: string;
  sources: { id: string; name: string }[];
  summary?: string | null;
  category?: string | null;
  quality?: number | null;
};

export type ItemsOptions = {
  items?: MockItem[];
  /** Make the list fail: "down" answers 503, "forbidden" answers 403. */
  itemsFail?: "down" | "forbidden";
  /** The next review step is answered as if someone else had changed the article first. */
  conflictOnce?: boolean;
};

export type ItemsState = { items: MockItem[]; conflictOnce: boolean };

export const SOURCE = { id: "30000000-0000-4000-8000-000000000001", name: "Industry news" };

export function item(n: number, changes: Partial<MockItem> = {}): MockItem {
  const day = String(Math.max(1, 28 - n)).padStart(2, "0");
  return {
    id: `40000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    url: `https://news.example.org/story-${n}`,
    title: `Story ${n}`,
    excerpt: `Excerpt of story ${n}`,
    lang: null,
    published_at: `2026-10-${day}T10:00:00Z`,
    fetched_at: `2026-10-${day}T11:00:00Z`,
    status: "new",
    hold_reason: null,
    updated_at: `2026-10-${day}T11:00:00Z`,
    sources: [SOURCE],
    ...changes,
  };
}

/** Three current articles, one held by a rule and one hidden. */
export function defaultItems(): MockItem[] {
  return [
    item(1),
    item(2, {
      summary: "Cuts month-end work from three days to one.",
      category: "tool",
      quality: 4,
    }),
    item(3),
    item(4, { status: "held", hold_reason: "link_ip_host", url: "https://203.0.113.9/x" }),
    item(5, { status: "hidden" }),
  ];
}

const STEPS: Record<string, { from: string[]; to: MockItem["status"] }> = {
  hold: { from: ["new"], to: "held" },
  approve: { from: ["held"], to: "new" },
  hide: { from: ["new", "held"], to: "hidden" },
  restore: { from: ["hidden"], to: "new" },
};

const PAGE = 20;

function view(found: MockItem, reviewer: boolean) {
  return reviewer
    ? found
    : { ...found, sources: [], updated_at: null, hold_reason: null, quality: null };
}

export async function handleItems(
  route: Route,
  request: Request,
  path: string,
  state: ItemsState,
  options: ItemsOptions,
  role: "admin" | "editor" | "member",
  problem: (status: number, code: string) => Parameters<Route["fulfill"]>[0],
): Promise<boolean> {
  if (!path.startsWith("/api/items")) return false;
  const reviewer = role === "admin" || role === "editor";
  const url = new URL(request.url());

  if (path === "/api/items" && request.method() === "GET") {
    if (options.itemsFail === "down") {
      await route.fulfill(problem(503, "service_unavailable"));
      return true;
    }
    if (options.itemsFail === "forbidden") {
      await route.fulfill(problem(403, "forbidden"));
      return true;
    }
    const status = url.searchParams.get("status");
    if (!reviewer && status !== null && status !== "new") {
      await route.fulfill(problem(403, "forbidden"));
      return true;
    }
    const wanted = reviewer ? status : "new";
    const offset = Number.parseInt(url.searchParams.get("offset") ?? "0", 10);
    const matching = state.items
      .filter((found) => wanted === null || found.status === wanted)
      .sort((a, b) => (b.published_at ?? "").localeCompare(a.published_at ?? ""));
    await route.fulfill({
      json: {
        items: matching.slice(offset, offset + PAGE).map((found) => view(found, reviewer)),
        total: matching.length,
      },
    });
    return true;
  }

  const match = /^\/api\/items\/([^/]+)(?:\/(hold|approve|hide|restore))?$/.exec(path);
  if (!match) return false;
  const found = state.items.find((candidate) => candidate.id === decodeURIComponent(match[1]!));
  const action = match[2];

  if (request.method() === "GET" && action === undefined) {
    if (!found || (!reviewer && found.status !== "new")) {
      await route.fulfill(problem(404, "not_found"));
    } else {
      await route.fulfill({ json: view(found, reviewer) });
    }
    return true;
  }
  if (request.method() === "POST" && action !== undefined) {
    if (!reviewer) {
      await route.fulfill(problem(403, "forbidden"));
      return true;
    }
    if (!found) {
      await route.fulfill(problem(404, "not_found"));
      return true;
    }
    if (state.conflictOnce) {
      state.conflictOnce = false;
      found.status = "hidden"; // what the other person did
      await route.fulfill(problem(409, "item_state"));
      return true;
    }
    const step = STEPS[action]!;
    if (found.status !== step.to) {
      if (!step.from.includes(found.status)) {
        await route.fulfill(problem(409, "item_state"));
        return true;
      }
      found.status = step.to;
      found.hold_reason = step.to === "held" ? "editor_hold" : null;
    }
    await route.fulfill({ json: view(found, true) });
    return true;
  }
  return false;
}
