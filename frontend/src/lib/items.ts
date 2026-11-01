// The articles of the organization (docs/API.md, "Items"). Everything that comes back is checked
// before it is used: a shape the page does not expect is an error, not a guess.

import { request } from "@/lib/api";
import { isIdentifier } from "@/lib/ids";

export type ItemStatus = "new" | "held" | "hidden";
export type ItemAction = "hold" | "approve" | "hide" | "restore";

export type Item = {
  id: string;
  url: string;
  title: string;
  excerpt: string;
  lang: string | null;
  published_at: string | null;
  fetched_at: string;
  status: ItemStatus;
  hold_reason: string | null;
  /** Reviewers only. */
  updated_at: string | null;
  /** Reviewers only; empty for a member. */
  sources: { id: string; name: string }[];
  /** What the model said, as plain text. Empty until the article has been read. */
  summary?: string | null;
  category?: string | null;
  /** 1 to 5, reviewers only. */
  quality?: number | null;
};

export const CATEGORIES = [
  "tool",
  "technique",
  "case_study",
  "research",
  "policy",
  "tutorial",
  "news",
  "other",
] as const;

export type ItemPage = { items: Item[]; total: number };

export const PAGE_SIZE = 20;

/** The steps that make sense for an article in this state (the backend decides in the end). */
export const ACTIONS_FOR: Record<ItemStatus, ItemAction[]> = {
  new: ["hold", "hide"],
  held: ["approve", "hide"],
  hidden: ["restore"],
};

/** Where an article goes after each step. */
export const RESULT_OF: Record<ItemAction, ItemStatus> = {
  hold: "held",
  approve: "new",
  hide: "hidden",
  restore: "new",
};

const STATUSES: readonly unknown[] = ["new", "held", "hidden"];

function isText(value: unknown): value is string {
  return typeof value === "string";
}

function isTextOrNull(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

export function isItem(value: unknown): value is Item {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (
    isText(item.id) &&
    isIdentifier(item.id) &&
    isText(item.url) &&
    isText(item.title) &&
    isText(item.excerpt) &&
    isTextOrNull(item.lang) &&
    isTextOrNull(item.published_at) &&
    isText(item.fetched_at) &&
    STATUSES.includes(item.status) &&
    isTextOrNull(item.hold_reason) &&
    isTextOrNull(item.updated_at ?? null) &&
    isTextOrNull(item.summary ?? null) &&
    isTextOrNull(item.category ?? null) &&
    (item.quality === undefined ||
      item.quality === null ||
      (typeof item.quality === "number" &&
        Number.isInteger(item.quality) &&
        item.quality >= 1 &&
        item.quality <= 5)) &&
    Array.isArray(item.sources) &&
    item.sources.every(
      (source) =>
        !!source &&
        typeof source === "object" &&
        isText((source as Record<string, unknown>).id) &&
        isText((source as Record<string, unknown>).name),
    )
  );
}

export function isItemPage(value: unknown): value is ItemPage {
  if (!value || typeof value !== "object") return false;
  const { items, total } = value as { items?: unknown; total?: unknown };
  return (
    Array.isArray(items) &&
    items.every(isItem) &&
    typeof total === "number" &&
    Number.isInteger(total) &&
    total >= 0
  );
}

export type ItemQuery = { status?: ItemStatus; sourceId?: string; offset?: number };

export function itemsPath(query: ItemQuery): string {
  const params = new URLSearchParams({ limit: String(PAGE_SIZE) });
  params.set("offset", String(query.offset ?? 0));
  if (query.status) params.set("status", query.status);
  if (query.sourceId) params.set("source_id", query.sourceId);
  return `/api/items?${params.toString()}`;
}

export async function listItems(query: ItemQuery): Promise<ItemPage> {
  const body: unknown = await (await request(itemsPath(query))).json();
  if (!isItemPage(body)) throw new Error("malformed_response");
  return body;
}

/** One review step. Answers the article as it is now. */
export async function actOnItem(id: string, action: ItemAction, csrf: string): Promise<Item> {
  const response = await request(`/api/items/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    csrf,
  });
  const body: unknown = await response.json();
  if (!isItem(body)) throw new Error("malformed_response");
  return body;
}
