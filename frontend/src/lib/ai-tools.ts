// The AI tools an administrator approves for "Open in" on a card (docs/API.md, "AI tools"). Every
// answer is checked for its shape before it is used; an identifier goes into a path, so it must be
// one; an address goes into an `href`, so it must pass `toolHref`.

import { request, type FieldProblem } from "@/lib/api";
import { isIdentifier } from "@/lib/ids";
import { safeHref } from "@/lib/links";

/** The same limits as the backend (aitools/schemas.py); the backend decides, these only help. */
export const NAME_LIMIT = 40;
export const URL_LIMIT = 300;
export const MAX_TOOLS = 10;

export type AiTool = { id: string; name: string; url: string };

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isAiTool(value: unknown): value is AiTool {
  return (
    record(value) &&
    isIdentifier(value.id) &&
    typeof value.name === "string" &&
    typeof value.url === "string"
  );
}

function isList(value: unknown): value is { items: AiTool[] } {
  return record(value) && Array.isArray(value.items) && value.items.every(isAiTool);
}

/**
 * The address as a link that is safe to put in `href`, or null. Stricter than `safeHref`: only
 * `https`, no IP address, no fragment, no credentials, however the backend stored it, because a
 * tool is opened with what a person has just copied from a card. A tool whose address fails stays
 * out of the card.
 */
export function toolHref(value: unknown): string | null {
  if (typeof value !== "string" || !value.startsWith("https://")) return null;
  // Control, format (zero-width, bidirectional), surrogate, private-use, unassigned and separator
  // characters: `safeHref` would percent-encode some of them and let them through, and they hide
  // where an address goes.
  if (/[\p{C}\p{Z}]/u.test(value)) return null;
  const href = safeHref(value);
  if (href === null || value.includes("#") || value.includes("@")) return null;
  // The backend refuses an IP address; this is the second line. The browser's URL parser turns
  // "2130706433" and "0x7f.1" into "127.0.0.1" and keeps an IPv6 address in brackets, so the
  // normalized host is what is looked at.
  const host = new URL(href).hostname;
  if (host.startsWith("[") || /^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return null;
  // "localhost" and every "*.localhost" are the person's own machine, whatever the DNS says.
  const bare = host.endsWith(".") ? host.slice(0, -1) : host;
  if (bare === "localhost" || bare.endsWith(".localhost")) return null;
  return href;
}

async function read<T>(response: Response, guard: (value: unknown) => value is T): Promise<T> {
  const body: unknown = await response.json();
  if (!guard(body)) throw new Error("malformed_response");
  return body;
}

function pathOf(id: string): string {
  if (!isIdentifier(id)) throw new Error("malformed_identifier");
  return `/api/ai-tools/${id}`;
}

export async function listAiTools(): Promise<AiTool[]> {
  return (await read(await request("/api/ai-tools"), isList)).items;
}

const CACHE_MS = 60_000;
let cache: { at: number; tools: Promise<AiTool[]> } | null = null;

/**
 * The approved tools for the "Open in" links of cards: asked for once a minute at most, however
 * many cards are open, and never more than once at the same time. A failed call is not kept.
 */
export function approvedTools(now: number = Date.now()): Promise<AiTool[]> {
  if (cache && now - cache.at < CACHE_MS) return cache.tools;
  const tools = listAiTools();
  cache = { at: now, tools };
  tools.catch(() => {
    if (cache?.tools === tools) cache = null;
  });
  return tools;
}

/** Forget what `approvedTools` kept: after the list was changed, and between tests. */
export function forgetApprovedTools(): void {
  cache = null;
}

export type NewAiTool = { name: string; url: string };

export async function createAiTool(body: NewAiTool, csrf: string): Promise<AiTool> {
  const created = await read(
    await request("/api/ai-tools", { method: "POST", body, csrf }),
    isAiTool,
  );
  forgetApprovedTools();
  return created;
}

export async function updateAiTool(
  id: string,
  body: Partial<NewAiTool>,
  csrf: string,
): Promise<AiTool> {
  const updated = await read(await request(pathOf(id), { method: "PATCH", body, csrf }), isAiTool);
  forgetApprovedTools();
  return updated;
}

export async function deleteAiTool(id: string, csrf: string): Promise<void> {
  await request(pathOf(id), { method: "DELETE", csrf });
  forgetApprovedTools();
}

// --- what is wrong with a form entry -------------------------------------------------------------

export type ToolField = "name" | "url";

export type ToolFieldKey =
  | "nameMissing"
  | "nameInvalid"
  | "nameTooLong"
  | "urlMissing"
  | "urlInvalid"
  | "urlHttpsOnly"
  | "urlCredentials"
  | "urlIp"
  | "urlLocal"
  | "urlTooLong";

/** Which message tells a person what is wrong with this part of the form. */
export function fieldKey(problem: FieldProblem): { field: ToolField; key: ToolFieldKey } | null {
  const where = problem.loc[problem.loc.length - 1];
  const type = problem.type;
  if (where === "name") {
    if (type === "invalid_text") return { field: "name", key: "nameInvalid" };
    if (type === "string_too_long") return { field: "name", key: "nameTooLong" };
    return { field: "name", key: "nameMissing" };
  }
  if (where === "url") {
    switch (type) {
      case "https_only":
        return { field: "url", key: "urlHttpsOnly" };
      case "credentials_not_allowed":
        return { field: "url", key: "urlCredentials" };
      case "ip_address_not_allowed":
        return { field: "url", key: "urlIp" };
      case "local_host_not_allowed":
        return { field: "url", key: "urlLocal" };
      case "string_too_long":
        return { field: "url", key: "urlTooLong" };
      case "string_too_short":
      case "missing":
        return { field: "url", key: "urlMissing" };
      default:
        return { field: "url", key: "urlInvalid" };
    }
  }
  return null;
}

/** What is wrong, by part of the form. The first problem of a part is the one that is shown. */
export function problemsByField(
  problems: FieldProblem[],
): Partial<Record<ToolField, ToolFieldKey>> {
  const found: Partial<Record<ToolField, ToolFieldKey>> = {};
  for (const problem of problems) {
    const entry = fieldKey(problem);
    if (entry && found[entry.field] === undefined) found[entry.field] = entry.key;
  }
  return found;
}
