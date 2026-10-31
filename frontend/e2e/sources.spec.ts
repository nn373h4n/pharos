import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { mockApi } from "./api-mock";
import { az, en, ru, tr } from "./messages";

const messages = { en, az, tr, ru } as const;
const locales = Object.keys(messages) as (keyof typeof messages)[];

async function listed(page: Page, locale: keyof typeof messages) {
  await expect(
    page.getByRole("heading", { level: 2, name: messages[locale].Sources.results }),
  ).toBeVisible();
  await expect(page.getByText(messages[locale].Sources.loading)).toHaveCount(0);
}

/** A button of one source, named as a person using a screen reader hears it: "Action: name". */
function action(page: Page, label: string, name: string) {
  return page.getByRole("button", { name: `${label}: ${name}` });
}

/** The page-level notice region (each trial fetch panel has its own status region after it). */
function notice(page: Page) {
  return page.getByRole("status").first();
}

// --- every language, both themes, both sizes (the projects) ---------------------------------------

for (const locale of locales) {
  const m = messages[locale].Sources;
  test.describe(`/${locale}/sources`, () => {
    for (const colorScheme of ["light", "dark"] as const) {
      test.describe(`${colorScheme} theme`, () => {
        test.beforeEach(async ({ page }) => {
          await page.emulateMedia({ colorScheme });
        });

        test("the list has no WCAG 2.2 AA violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "admin" });
          await page.goto(`/${locale}/sources`);
          await listed(page, locale);
          await expect(page.getByRole("heading", { level: 1 })).toHaveText(m.title);
          await expect(page.getByRole("article")).toHaveCount(4);
          expect(await violations(page)).toEqual([]);
        });

        test("the add dialog and the delete question have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "admin" });
          await page.goto(`/${locale}/sources`);
          await listed(page, locale);
          await page.getByRole("button", { name: m.actions.add }).click();
          await expect(page.getByRole("dialog")).toBeVisible();
          expect(await violations(page)).toEqual([]);
          await page.keyboard.press("Escape");
          await expect(page.getByRole("dialog")).toHaveCount(0);
          await action(page, m.actions.delete, "Industry news").click();
          await expect(page.getByRole("dialog")).toBeVisible();
          expect(await violations(page)).toEqual([]);
        });

        test("the latest fetches and the trial fetch have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "admin" });
          await page.goto(`/${locale}/sources`);
          await listed(page, locale);
          await page.getByText(m.actions.runs).first().click();
          await expect(page.getByRole("table").first()).toBeVisible();
          await action(page, m.actions.test, "Industry news").click();
          await expect(page.getByRole("heading", { name: m.test.title })).toBeVisible({
            timeout: 15_000,
          });
          expect(await violations(page)).toEqual([]);
        });

        test("the empty list, the failure and the refusal have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "admin", sources: [] });
          await page.goto(`/${locale}/sources`);
          await expect(page.getByText(m.emptyAdmin)).toBeVisible();
          expect(await violations(page)).toEqual([]);
          await mockApi(page, { signedIn: true, role: "admin", sourcesFail: "down" });
          await page.reload();
          await expect(page.getByRole("main").getByRole("alert")).toContainText(
            messages[locale].Problems.unavailable,
          );
          expect(await violations(page)).toEqual([]);
          await mockApi(page, { signedIn: true, role: "admin", sourcesFail: "forbidden" });
          await page.reload();
          await expect(page.getByRole("main").getByRole("alert")).toContainText(
            messages[locale].Problems.forbidden,
          );
          expect(await violations(page)).toEqual([]);
        });
      });
    }

    test("the page is in its own language", async ({ page }) => {
      await mockApi(page, { signedIn: true, role: "admin" });
      await page.goto(`/${locale}/sources`);
      await listed(page, locale);
      await expect(page.locator("html")).toHaveAttribute("lang", locale);
      await expect(page).toHaveTitle(m.metaTitle);
      await expect(
        page.getByRole("navigation", { name: messages[locale].Nav.main }).getByRole("link"),
      ).toHaveCount(4);
    });
  });
}

// --- who sees what ------------------------------------------------------------------------------------

test.describe("roles", () => {
  test("an administrator can do everything", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    await expect(page.getByRole("button", { name: en.Sources.actions.add })).toBeVisible();
    await expect(action(page, en.Sources.actions.edit, "Industry news")).toBeVisible();
    await expect(action(page, en.Sources.actions.disable, "Industry news")).toBeVisible();
    await expect(action(page, en.Sources.actions.enable, "Widget releases")).toBeVisible();
    await expect(action(page, en.Sources.actions.test, "Industry news")).toBeVisible();
  });

  test("an editor only looks", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "editor" });
    await page.goto("/en/sources");
    await listed(page, "en");
    await expect(page.getByRole("article")).toHaveCount(4);
    await expect(page.getByText(en.Sources.readOnly)).toBeVisible();
    await expect(page.getByRole("button", { name: en.Sources.actions.add })).toHaveCount(0);
    await expect(
      page.getByRole("button", {
        name: new RegExp(
          `^(${en.Sources.actions.edit}|${en.Sources.actions.delete}|${en.Sources.actions.test}):`,
        ),
      }),
    ).toHaveCount(0);
  });

  test("a member has no link and is told when the address is typed", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "member" });
    await page.goto("/en/items");
    await expect(
      page
        .getByRole("navigation", { name: en.Nav.main })
        .getByRole("link", { name: en.Nav.sources }),
    ).toHaveCount(0);
    await page.goto("/en/sources");
    await expect(page.getByRole("main").getByRole("alert")).toContainText(en.Problems.forbidden);
    await expect(page.getByRole("article")).toHaveCount(0);
  });
});

// --- the flows -------------------------------------------------------------------------------------------

test.describe("flows", () => {
  test("adding a feed, with the keyboard only", async ({ page }) => {
    const state = await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    await page.getByRole("button", { name: en.Sources.actions.add }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByLabel(en.Sources.form.name).fill("Fresh feed");
    await page.getByLabel(en.Sources.form.url).fill("https://fresh.example.org/feed.xml");
    await page.getByRole("button", { name: en.Sources.form.save }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(notice(page)).toContainText("Added: Fresh feed");
    await expect(page.getByRole("heading", { level: 2, name: en.Sources.results })).toBeFocused();
    await expect(page.getByRole("heading", { level: 3, name: "Fresh feed" })).toBeVisible();
    expect(state.calls).toContain("POST /api/sources");
  });

  test("a refused address is told at the field and the dialog stays", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    await page.getByRole("button", { name: en.Sources.actions.add }).click();
    await page.getByLabel(en.Sources.form.name).fill("Inside");
    await page.getByLabel(en.Sources.form.url).fill("https://internal/feed.xml");
    await page.getByRole("button", { name: en.Sources.form.save }).click();
    await expect(page.getByLabel(en.Sources.form.url)).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByRole("dialog")).toBeVisible();
  });

  test("a name that exists is told for the whole form", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    await page.getByRole("button", { name: en.Sources.actions.add }).click();
    await page.getByLabel(en.Sources.form.name).fill("Industry news");
    await page.getByLabel(en.Sources.form.url).fill("https://other.example.org/feed.xml");
    await page.getByRole("button", { name: en.Sources.form.save }).click();
    await expect(page.getByRole("dialog")).toContainText(en.Problems.sourceConflict);
  });

  test("Escape closes the dialog and focus goes back to the button", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    const add = page.getByRole("button", { name: en.Sources.actions.add });
    await add.click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(add).toBeFocused();
  });

  test("switching off and on", async ({ page }) => {
    const state = await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    await action(page, en.Sources.actions.disable, "Industry news").click();
    await expect(notice(page)).toContainText("Switched off: Industry news");
    await expect(action(page, en.Sources.actions.enable, "Industry news")).toBeVisible();
    expect(state.calls.filter((call) => call.startsWith("PATCH"))).toHaveLength(1);
  });

  test("deleting asks first; Cancel has focus and deletes nothing", async ({ page }) => {
    const state = await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    await action(page, en.Sources.actions.delete, "Broken feed").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("Broken feed");
    await expect(
      dialog.getByRole("button", { name: en.Sources.deleteDialog.cancel }),
    ).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(dialog).toHaveCount(0);
    expect(state.calls.some((call) => call.startsWith("DELETE"))).toBe(false);
    await action(page, en.Sources.actions.delete, "Broken feed").click();
    await dialog.getByRole("button", { name: en.Sources.deleteDialog.confirm }).click();
    await expect(notice(page)).toContainText("Deleted: Broken feed");
    await expect(page.getByRole("heading", { level: 2, name: en.Sources.results })).toBeFocused();
    await expect(page.getByRole("heading", { level: 3, name: "Broken feed" })).toHaveCount(0);
  });

  test("the filters narrow the list", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    await page.getByLabel(en.Sources.filters.type).selectOption("github");
    await expect(page.getByRole("article")).toHaveCount(1);
    await page.getByLabel(en.Sources.filters.type).selectOption("");
    await page.getByLabel(en.Sources.filters.state).selectOption("off");
    await expect(page.getByRole("article")).toHaveCount(1);
  });

  test("a failing source shows its problem in words, not as a code", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    const card = page.getByRole("article").filter({ hasText: "Broken feed" });
    await expect(card).toContainText(en.Sources.health.failing);
    await expect(card).not.toContainText("timed_out");
  });

  test("the articles of one source open from its card", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    await page
      .getByRole("article")
      .filter({ hasText: "Industry news" })
      .getByRole("link", { name: new RegExp(en.Sources.actions.articles) })
      .click();
    await expect(page).toHaveURL(/\/en\/items\?source=30000000-0000-4000-8000-000000000001$/);
    await expect(page.getByText(en.Items.source.only)).toBeVisible();
    await page.getByRole("link", { name: en.Items.source.all }).click();
    await expect(page).toHaveURL(/\/en\/items$/);
  });
});

// --- the trial fetch -------------------------------------------------------------------------------------

test.describe("trial fetch", () => {
  test("waits for the worker and shows what was found as text and safe links", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/sources");
    await listed(page, "en");
    const card = page.getByRole("article").filter({ hasText: "Industry news" });
    await action(page, en.Sources.actions.test, "Industry news").click();
    await expect(card.getByRole("button", { name: /Try a fetch/ })).toBeDisabled();
    await expect(card.getByRole("heading", { name: en.Sources.test.title })).toBeVisible({
      timeout: 15_000,
    });
    const link = card.getByRole("link", { name: /First story/ });
    await expect(link).toHaveAttribute("href", "https://news.example.org/1");
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", "noopener noreferrer nofollow");
    await expect(card.getByText("Second story")).toBeVisible();
  });

  test("hostile text stays text and an unsafe address is no link", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin", testFetch: "hostile" });
    const dialogs: string[] = [];
    page.on("dialog", (dialog) => {
      dialogs.push(dialog.message());
      void dialog.dismiss();
    });
    await page.goto("/en/sources");
    await listed(page, "en");
    const card = page.getByRole("article").filter({ hasText: "Industry news" });
    await action(page, en.Sources.actions.test, "Industry news").click();
    await expect(card.getByRole("heading", { name: en.Sources.test.title })).toBeVisible({
      timeout: 15_000,
    });
    await expect(card.getByText("<img src=x onerror=alert(1)> Story")).toBeVisible();
    await expect(card.locator("img")).toHaveCount(0);
    await expect(card.getByRole("link", { name: /Story/ })).toHaveCount(0);
    await expect(card.getByRole("link", { name: /Looks like a link/ })).toHaveCount(0);
    expect(dialogs).toEqual([]);
  });

  test("a refused start is told with the time to wait", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin", testFetch: "limited" });
    await page.goto("/en/sources");
    await listed(page, "en");
    const card = page.getByRole("article").filter({ hasText: "Industry news" });
    await action(page, en.Sources.actions.test, "Industry news").click();
    await expect(card.getByRole("status")).toContainText("30");
    await expect(action(page, en.Sources.actions.test, "Industry news")).toBeEnabled();
  });
});

// --- small screens --------------------------------------------------------------------------------------

test("nothing scrolls sideways, even with long hostile names", async ({ page }) => {
  const long = "W".repeat(150);
  const { defaultSources, source } = await import("./sources-mock");
  await mockApi(page, {
    signedIn: true,
    role: "admin",
    sources: [
      ...defaultSources(),
      source(9, { name: long, config: { url: `https://example.org/${"a".repeat(300)}` } }),
    ],
  });
  await page.goto("/en/sources");
  await listed(page, "en");
  await page.getByText(en.Sources.actions.runs).first().click();
  const wide = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(wide).toBe(false);
});
