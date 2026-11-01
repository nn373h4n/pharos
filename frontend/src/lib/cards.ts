// The Home feed and the insight of a card (docs/API.md, "Cards" and "Insights"). Every answer is
// checked for its shape before it is used, and every text in it is only ever shown as text. An
// identifier goes into a path, so it must be one.

import { request } from "@/lib/api";
import { isIdentifier } from "@/lib/ids";

export type Card = {
  id: string;
  url: string;
  host: string;
  title: string;
  published_at: string | null;
  summary: string;
  category: string;
  saved: boolean;
  /** 1 to 5, for administrators and editors only. */
  quality: number | null;
};

export type CardPage = { items: Card[]; next_cursor: string | null };

export type Insight = {
  profession: string;
  language: string;
  summary: string;
  how_to_apply: string[];
  try_prompt: string;
  safety_note: string;
};

export type InsightAnswer =
  | { status: "ready"; reason: null; insight: Insight }
  | { status: "pending"; reason: null; insight: null }
  | { status: "unavailable"; reason: "off" | "rejected" | "invalid"; insight: null };

export type InteractionKind = "view" | "save" | "useful" | "not_useful" | "tried" | "time_saved";
export type Undoable = "save" | "useful" | "not_useful";

export const MIN_MINUTES = 1;
export const MAX_MINUTES = 480;
/** How the page waits for an insight that is being made. */
export const INSIGHT_POLL_MS = 3000;
export const INSIGHT_GIVE_UP_MS = 90_000;

function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function text(value: unknown): value is string {
  return typeof value === "string";
}

export function isCard(value: unknown): value is Card {
  return (
    record(value) &&
    isIdentifier(value.id) &&
    text(value.url) &&
    text(value.host) &&
    text(value.title) &&
    (value.published_at === null || text(value.published_at)) &&
    text(value.summary) &&
    text(value.category) &&
    typeof value.saved === "boolean" &&
    (value.quality === null ||
      (typeof value.quality === "number" &&
        Number.isInteger(value.quality) &&
        value.quality >= 1 &&
        value.quality <= 5))
  );
}

export function isCardPage(value: unknown): value is CardPage {
  return (
    record(value) &&
    Array.isArray(value.items) &&
    value.items.every(isCard) &&
    (value.next_cursor === null || (text(value.next_cursor) && value.next_cursor.length <= 200))
  );
}

function isInsight(value: unknown): value is Insight {
  return (
    record(value) &&
    text(value.profession) &&
    text(value.language) &&
    text(value.summary) &&
    Array.isArray(value.how_to_apply) &&
    value.how_to_apply.length >= 1 &&
    value.how_to_apply.length <= 3 &&
    value.how_to_apply.every(text) &&
    text(value.try_prompt) &&
    text(value.safety_note)
  );
}

export function isInsightAnswer(value: unknown): value is InsightAnswer {
  if (!record(value)) return false;
  if (value.status === "ready") return value.reason === null && isInsight(value.insight);
  if (value.status === "pending") return value.reason === null && value.insight === null;
  if (value.status === "unavailable") {
    return (
      (value.reason === "off" || value.reason === "rejected" || value.reason === "invalid") &&
      value.insight === null
    );
  }
  return false;
}

async function read<T>(response: Response, check: (value: unknown) => value is T): Promise<T> {
  const body: unknown = await response.json();
  if (!check(body)) throw new Error("malformed_response");
  return body;
}

export function cardsPath(cursor?: string | null): string {
  return cursor ? `/api/cards?cursor=${encodeURIComponent(cursor)}` : "/api/cards";
}

export async function listCards(cursor?: string | null): Promise<CardPage> {
  return read(await request(cardsPath(cursor)), isCardPage);
}

export async function getInsight(itemId: string): Promise<InsightAnswer> {
  if (!isIdentifier(itemId)) throw new Error("malformed_identifier");
  return read(await request(`/api/items/${itemId}/insight`), isInsightAnswer);
}

function interactionPath(itemId: string, kind: InteractionKind): string {
  if (!isIdentifier(itemId)) throw new Error("malformed_identifier");
  return `/api/items/${itemId}/interactions/${kind}`;
}

export async function putInteraction(
  itemId: string,
  kind: InteractionKind,
  csrf: string,
  minutes?: number,
): Promise<void> {
  await request(interactionPath(itemId, kind), {
    method: "PUT",
    csrf,
    body: kind === "time_saved" ? { value: minutes } : undefined,
  });
}

export async function deleteInteraction(
  itemId: string,
  kind: Undoable,
  csrf: string,
): Promise<void> {
  await request(interactionPath(itemId, kind), { method: "DELETE", csrf });
}

/** Whole minutes from 1 to 480, or null. A typed value is text until it passes here. */
export function parseMinutes(value: string): number | null {
  if (!/^\d{1,3}$/.test(value.trim())) return null;
  const minutes = Number.parseInt(value, 10);
  return minutes >= MIN_MINUTES && minutes <= MAX_MINUTES ? minutes : null;
}
