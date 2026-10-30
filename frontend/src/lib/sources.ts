// The sources of the organization and the trial fetch (docs/API.md, "Sources"). Every answer is
// checked for its shape before it is used; an identifier goes into a path, so it must be one.

import { request } from "@/lib/api";
import { isIdentifier } from "@/lib/ids";
import { safeHref } from "@/lib/links";

export const SOURCE_TYPES = ["rss", "github", "hackernews"] as const;
export type SourceType = (typeof SOURCE_TYPES)[number];

export type Source = {
  id: string;
  type: SourceType;
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

export type SourcePage = { items: Source[]; total: number };

export type Run = {
  id: number;
  started_at: string;
  finished_at: string | null;
  outcome: string;
  error_code: string | null;
  http_status: number | null;
  duration_ms: number | null;
  bytes_fetched: number | null;
  items_seen: number | null;
  items_new: number | null;
};

export type TestSample = { title: string; url: string; published_at: string | null };

export type TestAnswer = {
  status: "pending" | "done" | "unavailable";
  outcome: string | null;
  error_code: string | null;
  http_status: number | null;
  entries_seen: number | null;
  entries_skipped: number | null;
  bytes_fetched: number | null;
  sample: TestSample[];
};

export type TestStarted = { test_id: string; retry_after_seconds: number };

export const PAGE_SIZE = 20;
/** Offered as the time between two fetches: five minutes up to a week (the backend's limits). */
export const INTERVALS = [300, 900, 1800, 3600, 7200, 21600, 43200, 86400, 604800] as const;
export const DEFAULT_INTERVAL = 3600;
export const MAX_NAME = 200;
export const MAX_QUERY = 100;
export const MAX_POINTS = 10_000;
export const DEFAULT_POINTS = 20;
/** How long the page waits for the worker's answer to a trial fetch. */
export const TEST_POLL_MS = 2000;
export const TEST_GIVE_UP_MS = 120_000;

// --- shapes -----------------------------------------------------------------------------------------

function text(value: unknown): value is string {
  return typeof value === "string";
}
function textOrNull(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}
function count(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}
function countOrNull(value: unknown): value is number | null {
  return value === null || count(value);
}
function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

export function isSource(value: unknown): value is Source {
  if (!record(value)) return false;
  return (
    isIdentifier(value.id) &&
    (SOURCE_TYPES as readonly unknown[]).includes(value.type) &&
    text(value.name) &&
    record(value.config) &&
    typeof value.enabled === "boolean" &&
    count(value.interval_seconds) &&
    text(value.next_fetch_at) &&
    textOrNull(value.last_fetched_at) &&
    textOrNull(value.last_success_at) &&
    count(value.consecutive_failures) &&
    textOrNull(value.last_error_code) &&
    textOrNull(value.created_by) &&
    text(value.created_at) &&
    text(value.updated_at)
  );
}

export function isSourcePage(value: unknown): value is SourcePage {
  return (
    record(value) && Array.isArray(value.items) && value.items.every(isSource) && count(value.total)
  );
}

export function isRun(value: unknown): value is Run {
  return (
    record(value) &&
    count(value.id) &&
    text(value.started_at) &&
    textOrNull(value.finished_at) &&
    text(value.outcome) &&
    textOrNull(value.error_code) &&
    countOrNull(value.http_status) &&
    countOrNull(value.duration_ms) &&
    countOrNull(value.bytes_fetched) &&
    countOrNull(value.items_seen) &&
    countOrNull(value.items_new)
  );
}

function isSample(value: unknown): value is TestSample {
  return record(value) && text(value.title) && text(value.url) && textOrNull(value.published_at);
}

export function isTestAnswer(value: unknown): value is TestAnswer {
  return (
    record(value) &&
    (value.status === "pending" || value.status === "done" || value.status === "unavailable") &&
    textOrNull(value.outcome) &&
    textOrNull(value.error_code) &&
    countOrNull(value.http_status) &&
    countOrNull(value.entries_seen) &&
    countOrNull(value.entries_skipped) &&
    countOrNull(value.bytes_fetched) &&
    Array.isArray(value.sample) &&
    value.sample.length <= 5 &&
    value.sample.every(isSample)
  );
}

export function isTestStarted(value: unknown): value is TestStarted {
  return record(value) && isIdentifier(value.test_id) && count(value.retry_after_seconds);
}

// --- the configuration, as a form has it ----------------------------------------------------------------

export type ConfigFields = { url: string; repo: string; query: string; points: string };

export const NO_FIELDS: ConfigFields = { url: "", repo: "", query: "", points: "" };

/** What the form's fields mean for this type of source: the configuration the backend expects. */
export function configOf(type: SourceType, fields: ConfigFields): Record<string, unknown> {
  if (type === "rss") return { url: fields.url.trim() };
  if (type === "github") return { repo: fields.repo.trim() };
  const config: Record<string, unknown> = { query: fields.query.trim() };
  const points = fields.points.trim();
  // A blank number is left out, so the backend's default applies; text that is not a whole
  // number is sent as it is and refused by the backend with its own word.
  if (points !== "") config.min_points = /^\d{1,9}$/.test(points) ? Number(points) : points;
  return config;
}

/** The form's fields for a source that exists. */
export function fieldsOf(source: Source): ConfigFields {
  const given = (key: string) =>
    typeof source.config[key] === "string" ? (source.config[key] as string) : "";
  const points = source.config.min_points;
  return {
    url: given("url"),
    repo: given("repo"),
    query: given("query"),
    points: typeof points === "number" ? String(points) : "",
  };
}

/** One line that says what a source fetches: the address, the repository or the search words. */
export function summaryOf(source: Source): string {
  const { config } = source;
  if (source.type === "rss") return typeof config.url === "string" ? config.url : "";
  if (source.type === "github") return typeof config.repo === "string" ? config.repo : "";
  const query = typeof config.query === "string" ? config.query : "";
  const points = typeof config.min_points === "number" ? config.min_points : DEFAULT_POINTS;
  return `${query} (${points}+)`;
}

/** The address of a source of type rss as a link, for places that show it; null if not a link. */
export function feedLink(source: Source): string | null {
  return source.type === "rss" ? safeHref(source.config.url) : null;
}

export type Health = "off" | "failing" | "ok" | "new";

/** How a source is doing, in one word. */
export function healthOf(source: Source): Health {
  if (!source.enabled) return "off";
  if (source.consecutive_failures > 0) return "failing";
  return source.last_success_at === null ? "new" : "ok";
}

// --- calls ------------------------------------------------------------------------------------------------------

export type SourceQuery = { type?: SourceType; enabled?: boolean; offset?: number };

export function sourcesPath(query: SourceQuery): string {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  params.set("offset", String(query.offset ?? 0));
  if (query.type) params.set("type", query.type);
  if (query.enabled !== undefined) params.set("enabled", String(query.enabled));
  return `/api/sources?${params.toString()}`;
}

async function read<T>(response: Response, guard: (value: unknown) => value is T): Promise<T> {
  const body: unknown = await response.json();
  if (!guard(body)) throw new Error("malformed_response");
  return body;
}

function pathOf(id: string, rest = ""): string {
  if (!isIdentifier(id)) throw new Error("malformed_identifier");
  return `/api/sources/${id}${rest}`;
}

export async function listSources(query: SourceQuery): Promise<SourcePage> {
  return read(await request(sourcesPath(query)), isSourcePage);
}

export type NewSource = {
  type: SourceType;
  name: string;
  config: Record<string, unknown>;
  interval_seconds: number;
  enabled: boolean;
};

export async function createSource(body: NewSource, csrf: string): Promise<Source> {
  return read(await request("/api/sources", { method: "POST", body, csrf }), isSource);
}

export type SourceChange = Partial<
  Pick<NewSource, "name" | "config" | "interval_seconds" | "enabled">
>;

export async function updateSource(id: string, body: SourceChange, csrf: string): Promise<Source> {
  return read(await request(pathOf(id), { method: "PATCH", body, csrf }), isSource);
}

export async function deleteSource(id: string, csrf: string): Promise<void> {
  await request(pathOf(id), { method: "DELETE", csrf });
}

export async function listRuns(id: string): Promise<Run[]> {
  const body: unknown = await (await request(pathOf(id, "/runs?limit=20"))).json();
  if (!record(body) || !Array.isArray(body.items) || !body.items.every(isRun)) {
    throw new Error("malformed_response");
  }
  return body.items;
}

export async function startTestFetch(id: string, csrf: string): Promise<TestStarted> {
  return read(await request(pathOf(id, "/test-fetch"), { method: "POST", csrf }), isTestStarted);
}

export async function readTestFetch(id: string, testId: string): Promise<TestAnswer> {
  if (!isIdentifier(testId)) throw new Error("malformed_identifier");
  return read(await request(pathOf(id, `/test-fetch/${testId}`)), isTestAnswer);
}
