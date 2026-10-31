import { execSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";
import { en } from "../e2e/messages";

// Onboarding against the real stack: a person who signs in through Keycloak for the first time is
// sent to the four questions, answers them with the keyboard alone, and the answers end up in the
// database. The accessibility checks of these pages are in a11y.spec.ts.
const PSQL = process.env.STACK_PSQL ?? "";
const SUBMIT = "button[type=submit]";
const ALICE_EMAIL = "alice@example.test";

function sql(statement: string): string {
  return execSync(PSQL, { input: statement, encoding: "utf8" }).trim();
}

// Presses Tab until the focused element matches the selector: proves the keyboard reaches it.
async function tabTo(page: Page, selector: string) {
  for (let presses = 0; presses < 40; presses += 1) {
    await page.keyboard.press("Tab");
    if (await page.evaluate((wanted) => document.activeElement?.matches(wanted) ?? false, selector))
      return;
  }
  throw new Error(`${selector} cannot be reached with the Tab key`);
}

test.beforeEach(() => {
  // Alice starts every test as someone who has not answered anything.
  sql(
    `DELETE FROM user_profiles WHERE user_id IN (SELECT id FROM users WHERE email = '${ALICE_EMAIL}')`,
  );
  sql(`UPDATE users SET locale = NULL WHERE email = '${ALICE_EMAIL}'`);
});

test("a new person is sent to the questions, answers them by keyboard in under two minutes", async ({
  page,
}) => {
  await page.goto("/en/login");
  await page
    .getByRole("link", { name: en.Login.ssoButton.replace("{provider}", "Keycloak (development)") })
    .click();
  await page.locator("#username").fill("alice");
  await page.locator("#password").fill("dev-only-password");
  await page.locator("#kc-login").click();

  // The app sends someone who has not answered to the questions, not to the start page.
  await expect(page).toHaveURL(/\/en\/onboarding/);
  await expect(page.getByRole("heading", { level: 2 })).toHaveText(
    en.Onboarding.profession.heading,
  );
  const started = Date.now();

  await tabTo(page, "input[name=profession]");
  await page.keyboard.press("Space");
  await tabTo(page, SUBMIT);
  await page.keyboard.press("Enter");

  await expect(page.getByRole("heading", { level: 2 })).toHaveText(en.Onboarding.interests.heading);
  await expect(page.getByRole("heading", { level: 2 })).toBeFocused();
  await tabTo(page, "input[name=interests]");
  await page.keyboard.press("Space");
  await tabTo(page, SUBMIT);
  await page.keyboard.press("Enter");

  await expect(page.getByRole("heading", { level: 2 })).toHaveText(en.Onboarding.language.heading);
  await tabTo(page, "input[name=language]"); // the first language is English: no reload
  await page.keyboard.press("Space");
  await tabTo(page, SUBMIT);
  await page.keyboard.press("Enter");

  await expect(page.getByRole("heading", { level: 2 })).toHaveText(en.Onboarding.aiLevel.heading);
  await tabTo(page, "input[name=ai-level]");
  await page.keyboard.press("Space");
  await tabTo(page, SUBMIT);
  await page.keyboard.press("Enter");

  await expect(page).toHaveURL(/\/en$/);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(en.Home.title);
  expect(Date.now() - started).toBeLessThan(120_000);

  const stored = sql(
    `SELECT p.interests::text || '|' || u.locale || '|' || p.ai_level || '|' || ` +
      `(p.onboarding_completed_at >= p.onboarding_started_at)::text || '|' || ` +
      `(p.profession_id IS NOT NULL)::text ` +
      `FROM user_profiles p JOIN users u ON u.id = p.user_id WHERE u.email = '${ALICE_EMAIL}'`,
  );
  const [interests, locale, level, ordered, hasProfession] = stored.split("|");
  expect(JSON.parse(interests ?? "[]").length).toBeGreaterThan(0);
  expect(locale).toBe("en");
  expect(["beginner", "intermediate", "advanced"]).toContain(level);
  expect(ordered).toBe("true"); // started before completed, so the time people need can be measured
  expect(hasProfession).toBe("true");
});

test("a person who has finished is not asked again", async ({ page }) => {
  await page.goto("/en/login");
  await page
    .getByRole("link", { name: en.Login.ssoButton.replace("{provider}", "Keycloak (development)") })
    .click();
  await page.locator("#username").fill("alice");
  await page.locator("#password").fill("dev-only-password");
  await page.locator("#kc-login").click();
  await expect(page).toHaveURL(/\/en\/onboarding/);

  sql(
    `INSERT INTO user_profiles (user_id, org_id, profession_id, ai_level, interests, ` +
      `onboarding_started_at, onboarding_completed_at) ` +
      `SELECT u.id, u.org_id, (SELECT id FROM professions ORDER BY code LIMIT 1), 'beginner', ` +
      `'["research"]', now() - interval '1 minute', now() FROM users u WHERE u.email = '${ALICE_EMAIL}'`,
  );
  // The page redirects at once; do not wait for a load that the redirect interrupts.
  await page.goto("/en/onboarding", { waitUntil: "commit" });
  await expect(page).toHaveURL(/\/en$/);
});

test("someone who is not signed in cannot reach the questions", async ({ page }) => {
  await page.goto("/en/onboarding");
  await expect(page).toHaveURL(/\/en\/login/);
});
