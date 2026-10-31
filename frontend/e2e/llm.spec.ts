import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { mockApi } from "./api-mock";
import { az, en, ru, tr } from "./messages";

const messages = { en, az, tr, ru } as const;
const locales = Object.keys(messages) as (keyof typeof messages)[];

async function loaded(page: Page, locale: keyof typeof messages) {
  await expect(
    page.getByRole("heading", { level: 2, name: messages[locale].Llm.usage.title }),
  ).toBeVisible();
  await expect(page.getByText(messages[locale].Llm.loading)).toHaveCount(0);
}

/** The page itself: the route announcer of Next.js is an alert of its own. */
const main = (page: Page) => page.locator("main");

// --- every language, both themes, both sizes (the projects) ---------------------------------------

for (const locale of locales) {
  const m = messages[locale].Llm;
  test.describe(`/${locale}/llm`, () => {
    for (const colorScheme of ["light", "dark"] as const) {
      test.describe(`${colorScheme} theme`, () => {
        test.beforeEach(async ({ page }) => {
          await page.emulateMedia({ colorScheme });
        });

        test("the page has no WCAG 2.2 AA violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "admin" });
          await page.goto(`/${locale}/llm`);
          await loaded(page, locale);
          await expect(page.getByRole("heading", { level: 1 })).toHaveText(m.title);
          expect(await violations(page)).toEqual([]);
        });

        test("the warning and the reached limit have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "admin", llmExhausted: true });
          await page.goto(`/${locale}/llm`);
          await loaded(page, locale);
          await expect(main(page).getByRole("alert")).toHaveText(m.budget.exhausted);
          expect(await violations(page)).toEqual([]);
        });
      });
    }
  });
}

// --- behaviour (one language and one project are enough) -----------------------------------------

test.describe("the model page", () => {
  test("an administrator tests the key and sees the answer", async ({ page }) => {
    const state = await mockApi(page, { signedIn: true, role: "admin", llmWarned: true });
    await page.goto("/en/llm");
    await loaded(page, "en");
    await expect(main(page).getByRole("alert")).toHaveText(en.Llm.budget.warned);
    await page.getByRole("button", { name: en.Llm.key.test }).click();
    await expect(main(page).getByRole("status")).toHaveText(en.Llm.key.waiting);
    await expect(main(page).getByRole("status")).toHaveText("Result: The key works.", {
      timeout: 15_000,
    });
    expect(state.calls).toContain("POST /api/llm/key-test");
  });

  test("the link is in the navigation for an administrator only", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en");
    await expect(page.getByRole("link", { name: en.Nav.llm })).toBeVisible();
    await mockApi(page, { signedIn: true, role: "editor" });
    await page.goto("/en");
    await expect(page.getByRole("link", { name: en.Nav.sources })).toBeVisible();
    await expect(page.getByRole("link", { name: en.Nav.llm })).toHaveCount(0);
  });

  test("someone who is not an administrator is told so", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "editor" });
    await page.goto("/en/llm");
    await expect(main(page).getByRole("alert")).toHaveText(en.Problems.forbidden);
  });

  test("a model switched off is explained", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin", llmProvider: "off" });
    await page.goto("/en/llm");
    await loaded(page, "en");
    await expect(main(page).getByRole("note")).toHaveText(en.Llm.offNote);
  });

  test("a page that cannot load offers another try", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin", llmFail: "down" });
    await page.goto("/en/llm");
    await expect(main(page).getByRole("alert")).toHaveText(en.Llm.loadFailed);
    await expect(page.getByRole("button", { name: en.Llm.retry })).toBeVisible();
  });
});
