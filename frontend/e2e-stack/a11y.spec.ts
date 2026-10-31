import { execSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { az, en, ru, tr } from "../e2e/messages";

// Accessibility of the pages a signed-in person sees, with REAL data from the real stack, in four
// languages, both themes and both viewport sizes (the desktop and mobile projects).
const STACK = process.env.STACK_URL ?? "";
const ADMIN_USER = process.env.STACK_ADMIN_USER ?? "";
const ADMIN_PASSWORD = process.env.STACK_ADMIN_PASSWORD ?? "";
const PSQL = process.env.STACK_PSQL ?? "";
const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

const messages = { en, az, tr, ru } as const;
const locales = Object.keys(messages) as (keyof typeof messages)[];

function sql(statement: string): string {
  return execSync(PSQL, { input: statement, encoding: "utf8" }).trim();
}

// One sign-in per project, reused by every test (the sign-in itself is covered in sign-in.spec.ts).
const stateFile = join(mkdtempSync(join(tmpdir(), "pharos-a11y-")), "state.json");
test.use({ storageState: stateFile });

test.beforeAll(async ({ browser }) => {
  // storageState: undefined, because this is the context that creates the state file.
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
    baseURL: STACK,
    storageState: undefined,
  });
  const page = await context.newPage();
  await page.goto("/en/login");
  await page.getByLabel(en.Login.username).fill(ADMIN_USER);
  await page.getByLabel(en.Login.password, { exact: true }).fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: en.Login.submit }).click();
  await expect(page.getByRole("group", { name: en.Shell.account })).toBeVisible();
  await context.storageState({ path: stateFile });
  await context.close();
});

const adminId = () => `(SELECT id FROM users WHERE subject = '${ADMIN_USER}')`;

function setUnfinished() {
  sql(`DELETE FROM user_profiles WHERE user_id = ${adminId()}`);
}

function setFinished() {
  setUnfinished();
  sql(
    `INSERT INTO user_profiles (user_id, org_id, profession_id, ai_level, interests, ` +
      `onboarding_started_at, onboarding_completed_at) ` +
      `SELECT u.id, u.org_id, (SELECT id FROM professions ORDER BY code LIMIT 1), 'beginner', ` +
      `'["research"]', now() - interval '1 minute', now() FROM users u WHERE u.subject = '${ADMIN_USER}'`,
  );
}

for (const locale of locales) {
  const text = messages[locale];
  for (const colorScheme of ["light", "dark"] as const) {
    test(`the start page of a signed-in person has no WCAG 2.2 AA violations (${locale}, ${colorScheme})`, async ({
      page,
    }) => {
      setFinished();
      await page.emulateMedia({ colorScheme });
      await page.goto(`/${locale}`);
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(text.Home.title);
      await expect(page.getByRole("group", { name: text.Shell.account })).toBeVisible();
      const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
      expect(results.violations).toEqual([]);
    });

    test(`the first question has no WCAG 2.2 AA violations (${locale}, ${colorScheme})`, async ({
      page,
    }) => {
      setUnfinished();
      await page.emulateMedia({ colorScheme });
      await page.goto(`/${locale}/onboarding`);
      await expect(page.getByRole("heading", { level: 2 })).toHaveText(
        text.Onboarding.profession.heading,
      );
      // The real professions of the installation are listed, in this language.
      await expect(page.locator("input[name=profession]").first()).toBeVisible();
      const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
      expect(results.violations).toEqual([]);
    });
  }
}

// --- the pages of milestone M3, with an article, a source and a personal feed in the database ------

const SLUG = process.env.STACK_ORG_SLUG ?? "stack";

function seedIngestion() {
  setFinished();
  sql(
    `INSERT INTO sources (org_id, type, name, config, dedupe_key, enabled, interval_seconds, ` +
      `next_fetch_at) SELECT id, 'rss', 'A11y source', '{"url": "https://news.example.org/feed.xml"}'::jsonb, ` +
      `md5('a11y-source') || md5('a11y-source-2'), false, 3600, now() + interval '30 days' ` +
      `FROM organizations WHERE slug = '${SLUG}' ON CONFLICT DO NOTHING`,
  );
  sql(
    `INSERT INTO items (org_id, canonical_url, url_hash, title, excerpt, content_sha256) ` +
      `SELECT id, 'https://news.example.org/a11y', md5('a11y-item') || md5('a11y-item-2'), ` +
      `'A11y article', 'An excerpt of the article.', md5('a11y-content') || md5('a11y-content-2') ` +
      `FROM organizations WHERE slug = '${SLUG}' ON CONFLICT DO NOTHING`,
  );
  sql(
    `INSERT INTO sources (org_id, owner_user_id, type, name, config, dedupe_key, enabled, ` +
      `interval_seconds, next_fetch_at) SELECT u.org_id, u.id, 'rss', 'A11y personal feed', ` +
      `'{"url": "https://blog.example.org/feed.xml"}'::jsonb, md5('a11y-personal') || md5('a11y-personal-2'), ` +
      `false, 3600, now() + interval '30 days' FROM users u WHERE u.subject = '${ADMIN_USER}' ` +
      `ON CONFLICT DO NOTHING`,
  );
}

const m3Pages = [
  {
    path: "items",
    heading: (t: typeof en) => t.Items.title,
    ready: (t: typeof en) => t.Items.results,
  },
  {
    path: "sources",
    heading: (t: typeof en) => t.Sources.title,
    ready: (t: typeof en) => t.Sources.results,
  },
  {
    path: "my-feeds",
    heading: (t: typeof en) => t.Feeds.title,
    ready: (t: typeof en) => t.Feeds.results,
  },
] as const;

for (const locale of locales) {
  const text = messages[locale];
  for (const colorScheme of ["light", "dark"] as const) {
    for (const found of m3Pages) {
      test(`/${found.path} has no WCAG 2.2 AA violations (${locale}, ${colorScheme})`, async ({
        page,
      }) => {
        seedIngestion();
        await page.emulateMedia({ colorScheme });
        await page.goto(`/${locale}/${found.path}`);
        await expect(page.getByRole("heading", { level: 1 })).toHaveText(
          found.heading(text as unknown as typeof en),
        );
        await expect(
          page.getByRole("heading", { level: 2, name: found.ready(text as unknown as typeof en) }),
        ).toBeVisible();
        await page.addStyleTag({
          content:
            "*, *::before, *::after { transition: none !important; animation: none !important; }",
        });
        const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
        expect(results.violations).toEqual([]);
      });
    }
  }
}
