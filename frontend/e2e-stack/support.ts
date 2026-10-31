import { execSync } from "node:child_process";
import { chmodSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { expect, type Page } from "@playwright/test";
import { en } from "../e2e/messages";

// Helpers for the stack specs that need the worker (scripts/stack-e2e.sh): the database as an
// operator sees it, the fake feed host and its files, and waiting for the worker's fetches.

export const STACK = process.env.STACK_URL ?? "";
export const ADMIN_USER = process.env.STACK_ADMIN_USER ?? "";
export const ADMIN_PASSWORD = process.env.STACK_ADMIN_PASSWORD ?? "";
const PSQL = process.env.STACK_PSQL ?? "";
const FEED_DIR = process.env.STACK_FEED_DIR ?? "";
/** Where the worker finds the fake feed host (a name with a dot: the API refuses dotless names). */
export const FEED_URL = process.env.STACK_FEED_URL ?? "";

/** Runs SQL as an operator would (psql inside the stack's database container). */
export function sql(statement: string): string {
  return execSync(PSQL, { input: statement, encoding: "utf8" }).trim();
}

export function sqlNumber(statement: string): number {
  return Number(sql(statement));
}

// --- the fake feed host -----------------------------------------------------------------------------

export type Story = { id: string; title: string; url?: string; summary?: string };

const escape = (value: string) =>
  value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export function rss(stories: Story[], title = "Fake feed"): string {
  const items = stories
    .map(
      (story) =>
        `<item><guid isPermaLink="false">${escape(story.id)}</guid>` +
        `<title>${escape(story.title)}</title>` +
        `<link>${escape(story.url ?? `https://news.example.org/${story.id}`)}</link>` +
        `<description>${escape(story.summary ?? "A summary.")}</description>` +
        `<pubDate>Tue, 20 Oct 2026 10:00:00 GMT</pubDate></item>`,
    )
    .join("");
  return `<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>${escape(title)}</title>${items}</channel></rss>`;
}

/** Puts a file where the fake feed host serves it. The process may run with a strict umask. */
export function serve(name: string, body: string) {
  if (!FEED_DIR) throw new Error("STACK_FEED_DIR is not set; run `make test-stack-e2e`");
  const path = join(FEED_DIR, name);
  writeFileSync(path, body);
  chmodSync(path, 0o644);
}

// --- sources and runs ----------------------------------------------------------------------------------

/** Makes a source due now, so the next dispatch (at most a minute away) fetches it. */
export function makeDue(whereClause: string) {
  sql(
    `UPDATE sources SET next_fetch_at = now() - interval '1 second', lease_until = NULL, lease_token = NULL WHERE ${whereClause}`,
  );
}

export const runs = (whereClause: string) =>
  sqlNumber(
    `SELECT count(*) FROM fetch_runs WHERE outcome <> 'running' AND source_id IN (SELECT id FROM sources WHERE ${whereClause})`,
  );

/** Waits until the worker has finished `count` fetches of the sources chosen by the clause. */
export async function waitForRuns(whereClause: string, count: number, seconds = 150) {
  await expect
    .poll(() => runs(whereClause), {
      timeout: seconds * 1000,
      intervals: [2000],
      message: whereClause,
    })
    .toBeGreaterThanOrEqual(count);
}

export const lastOutcome = (whereClause: string) =>
  sql(
    `SELECT outcome || ':' || coalesce(error_code, '') FROM fetch_runs WHERE source_id IN (SELECT id FROM sources WHERE ${whereClause}) AND outcome <> 'running' ORDER BY id DESC LIMIT 1`,
  );

// --- people ------------------------------------------------------------------------------------------------

export async function signInAsAdmin(page: Page) {
  await page.goto("/en/login");
  await page.getByLabel(en.Login.username).fill(ADMIN_USER);
  await page.getByLabel(en.Login.password, { exact: true }).fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: en.Login.submit }).click();
  await expect(page.getByRole("group", { name: en.Shell.account })).toBeVisible();
}

/** Marks the onboarding of the person chosen by the SQL condition as done (answers recorded). */
export function finishOnboarding(userCondition: string) {
  sql(`DELETE FROM user_profiles WHERE user_id IN (SELECT id FROM users WHERE ${userCondition})`);
  sql(
    `INSERT INTO user_profiles (user_id, org_id, profession_id, ai_level, interests, ` +
      `onboarding_started_at, onboarding_completed_at) ` +
      `SELECT u.id, u.org_id, (SELECT id FROM professions ORDER BY code LIMIT 1), 'beginner', ` +
      `'["research"]', now() - interval '1 minute', now() FROM users u WHERE ${userCondition}`,
  );
}

/** The identifier of a person chosen by the SQL condition. */
export const userId = (condition: string) => sql(`SELECT id FROM users WHERE ${condition}`);

/** A source written as an operator would, bypassing the API's checks (the worker still checks). */
export function insertSource(name: string, url: string, owner?: string) {
  sql(
    `INSERT INTO sources (org_id, owner_user_id, type, name, config, dedupe_key, enabled, interval_seconds) ` +
      `SELECT id, ${owner ? `'${owner}'` : "NULL"}, 'rss', '${name}', '{"url": "${url}"}'::jsonb, ` +
      `md5('${name}${url}') || md5('${url}'), true, 3600 FROM organizations ` +
      `WHERE slug = '${process.env.STACK_ORG_SLUG ?? "stack"}'`,
  );
}
