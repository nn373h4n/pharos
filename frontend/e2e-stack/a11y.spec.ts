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
