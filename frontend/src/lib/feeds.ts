// A person's own feeds (docs/API.md, "Personal feeds"). Every answer is checked for its shape before
// it is used; an identifier goes into a path, so it must be one.

import { request } from "@/lib/api";
import type { FieldProblem } from "@/lib/api";
import { isIdentifier } from "@/lib/ids";
import { isRun, type Run } from "@/lib/sources";

export type Feed = {
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

export type FeedPage = { items: Feed[]; total: number };

export type FeedItem = {
  id: string;
  url: string;
  title: string;
  excerpt: string;
  lang: string | null;
  published_at: string | null;
  fetched_at: string;
  status: "new" | "held" | "hidden";
  hold_reason: string | null;
};

export type FeedItemPage = { items: FeedItem[]; total: number };

/** At most this many feeds per person (the backend and the database enforce it). */
export const MAX_FEEDS = 5;
export const PAGE_SIZE = 20;
/** Offered as the time between two fetches: an hour up to a week (a feed is fetched hourly at most). */
export const FEED_INTERVALS = [3600, 7200, 21600, 43200, 86400, 604800] as const;
export const DEFAULT_FEED_INTERVAL = 3600;
export const MAX_URL = 2048;

// --- shapes -----------------------------------------------------------------------------------------

function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function text(value: unknown): value is string {
  return typeof value === "string";
}
function textOrNull(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}
function count(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}

export function isFeed(value: unknown): value is Feed {
  if (!record(value)) return false;
  return (
    isIdentifier(value.id) &&
    text(value.name) &&
    text(value.url) &&
    typeof value.enabled === "boolean" &&
    count(value.interval_seconds) &&
    text(value.next_fetch_at) &&
    textOrNull(value.last_fetched_at) &&
    textOrNull(value.last_success_at) &&
    count(value.consecutive_failures) &&
    textOrNull(value.last_error_code) &&
    text(value.created_at) &&
    text(value.updated_at)
  );
}

export function isFeedPage(value: unknown): value is FeedPage {
  return (
    record(value) && Array.isArray(value.items) && value.items.every(isFeed) && count(value.total)
  );
}

export function isFeedItem(value: unknown): value is FeedItem {
  if (!record(value)) return false;
  return (
    isIdentifier(value.id) &&
    text(value.url) &&
    text(value.title) &&
    text(value.excerpt) &&
    textOrNull(value.lang) &&
    textOrNull(value.published_at) &&
    text(value.fetched_at) &&
    (value.status === "new" || value.status === "held" || value.status === "hidden") &&
    textOrNull(value.hold_reason)
  );
}

export function isFeedItemPage(value: unknown): value is FeedItemPage {
  return (
    record(value) &&
    Array.isArray(value.items) &&
    value.items.every(isFeedItem) &&
    count(value.total)
  );
}

/** How a feed is doing, in one word. */
export type Health = "off" | "failing" | "ok" | "new";

export function healthOf(feed: Feed): Health {
  if (!feed.enabled) return "off";
  if (feed.consecutive_failures > 0) return "failing";
  return feed.last_success_at === null ? "new" : "ok";
}

/**
 * The backend names the address field `url`. The form's messages are those of the sources
 * form, where the settings of a source are called `config`: say it their way.
 */
export function asSourceProblems(problems: FieldProblem[]): FieldProblem[] {
  return problems.map((problem) =>
    problem.loc[problem.loc.length - 1] === "url"
      ? { ...problem, loc: [...problem.loc.slice(0, -1), "config"] }
      : problem,
  );
}

// --- calls ------------------------------------------------------------------------------------------------------

export function feedsPath(offset = 0): string {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
  return `/api/me/feeds?${params.toString()}`;
}

async function read<T>(response: Response, guard: (value: unknown) => value is T): Promise<T> {
  const body: unknown = await response.json();
  if (!guard(body)) throw new Error("malformed_response");
  return body;
}

function pathOf(id: string, rest = ""): string {
  if (!isIdentifier(id)) throw new Error("malformed_identifier");
  return `/api/me/feeds/${id}${rest}`;
}

export async function listFeeds(offset = 0): Promise<FeedPage> {
  return read(await request(feedsPath(offset)), isFeedPage);
}

export type NewFeed = { name: string; url: string; interval_seconds: number; enabled: boolean };

export async function createFeed(body: NewFeed, csrf: string): Promise<Feed> {
  return read(await request("/api/me/feeds", { method: "POST", body, csrf }), isFeed);
}

export type FeedChange = Partial<Pick<NewFeed, "name" | "url" | "interval_seconds" | "enabled">>;

export async function updateFeed(id: string, body: FeedChange, csrf: string): Promise<Feed> {
  return read(await request(pathOf(id), { method: "PATCH", body, csrf }), isFeed);
}

export async function deleteFeed(id: string, csrf: string): Promise<void> {
  await request(pathOf(id), { method: "DELETE", csrf });
}

export async function listFeedRuns(id: string): Promise<Run[]> {
  const body: unknown = await (await request(pathOf(id, "/runs?limit=20"))).json();
  if (!record(body) || !Array.isArray(body.items) || !body.items.every(isRun)) {
    throw new Error("malformed_response");
  }
  return body.items;
}

export async function listFeedItems(id: string, offset = 0): Promise<FeedItemPage> {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
  return read(await request(pathOf(id, `/items?${params.toString()}`)), isFeedItemPage);
}
