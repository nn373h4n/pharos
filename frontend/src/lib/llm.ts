// The model page of the administrator (docs/API.md, "Model layer"). Every answer is checked for its
// shape before it is used; nothing here is secret, and the key is never part of an answer.

import { request } from "@/lib/api";
import { isIdentifier } from "@/lib/ids";

export const PROVIDERS = ["off", "fake", "anthropic", "openai", "azure"] as const;
export type Provider = (typeof PROVIDERS)[number];
export const STAGES = ["filter", "enrichment", "insight", "key_test"] as const;
export type Stage = (typeof STAGES)[number];
export const OUTCOMES = [
  "ok",
  "disabled",
  "not_configured",
  "rejected",
  "failed",
  "budget",
  "paced",
  "paused",
] as const;
export type Outcome = (typeof OUTCOMES)[number];

export type UsageDay = {
  day: string;
  stage: Stage;
  calls: number;
  refused: number;
  failed: number;
  tokens_in: number;
  tokens_out: number;
  cost_micro_usd: number;
};

export type LlmStatus = {
  provider: Provider;
  models: { filter: string; enrichment: string; insight: string };
  daily_cap_micro_usd: number;
  spent_micro_usd: number;
  warned: boolean;
  exhausted: boolean;
  key_check: { outcome: Outcome; checked_at: string } | null;
  usage: UsageDay[];
};

export type KeyTestStarted = { test_id: string; retry_after_seconds: number };
export type KeyTestAnswer = {
  status: "pending" | "done" | "unavailable";
  outcome: Outcome | null;
  checked_at: string | null;
};

/** How long the page waits for the worker's answer to a key check. */
export const KEY_POLL_MS = 2000;
export const KEY_GIVE_UP_MS = 60_000;

function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function count(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0;
}
function oneOf<T extends string>(list: readonly T[], value: unknown): value is T {
  return typeof value === "string" && (list as readonly string[]).includes(value);
}
function text(value: unknown): value is string {
  return typeof value === "string";
}

function isUsageDay(value: unknown): value is UsageDay {
  return (
    record(value) &&
    text(value.day) &&
    oneOf(STAGES, value.stage) &&
    count(value.calls) &&
    count(value.refused) &&
    count(value.failed) &&
    count(value.tokens_in) &&
    count(value.tokens_out) &&
    count(value.cost_micro_usd)
  );
}

export function isStatus(value: unknown): value is LlmStatus {
  if (!record(value) || !record(value.models)) return false;
  const check = value.key_check;
  return (
    oneOf(PROVIDERS, value.provider) &&
    text(value.models.filter) &&
    text(value.models.enrichment) &&
    text(value.models.insight) &&
    count(value.daily_cap_micro_usd) &&
    count(value.spent_micro_usd) &&
    typeof value.warned === "boolean" &&
    typeof value.exhausted === "boolean" &&
    (check === null ||
      (record(check) && oneOf(OUTCOMES, check.outcome) && text(check.checked_at))) &&
    Array.isArray(value.usage) &&
    value.usage.every(isUsageDay)
  );
}

export function isKeyStarted(value: unknown): value is KeyTestStarted {
  return record(value) && isIdentifier(value.test_id) && count(value.retry_after_seconds);
}

export function isKeyAnswer(value: unknown): value is KeyTestAnswer {
  return (
    record(value) &&
    oneOf(["pending", "done", "unavailable"] as const, value.status) &&
    (value.outcome === null || oneOf(OUTCOMES, value.outcome)) &&
    (value.checked_at === null || text(value.checked_at))
  );
}

async function read<T>(response: Response, check: (value: unknown) => value is T): Promise<T> {
  const body: unknown = await response.json();
  if (!check(body)) throw new Error("malformed_response");
  return body;
}

export async function getStatus(): Promise<LlmStatus> {
  return read(await request("/api/llm/status"), isStatus);
}

export async function startKeyTest(csrf: string): Promise<KeyTestStarted> {
  return read(await request("/api/llm/key-test", { method: "POST", csrf }), isKeyStarted);
}

export async function readKeyTest(testId: string): Promise<KeyTestAnswer> {
  if (!isIdentifier(testId)) throw new Error("malformed_identifier");
  return read(await request(`/api/llm/key-test/${testId}`), isKeyAnswer);
}

/** Millionths of a US dollar as dollars with up to four decimals (the unit the backend counts in). */
export function dollars(micro: number, locale: string): string {
  try {
    return new Intl.NumberFormat(locale, {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    }).format(micro / 1_000_000);
  } catch {
    return `$${(micro / 1_000_000).toFixed(2)}`;
  }
}

/** The share of the cap already spent, 0 to 100. */
export function spentPercent(spent: number, cap: number): number {
  if (cap <= 0) return spent > 0 ? 100 : 0;
  return Math.max(0, Math.min(100, Math.round((spent / cap) * 100)));
}
