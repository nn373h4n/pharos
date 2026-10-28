import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { mockApi } from "./api-mock";
import { az, en, ru, tr } from "./messages";

const messages = { en, az, tr, ru } as const;
const locales = Object.keys(messages) as (keyof typeof messages)[];

// The server in playwright.config.ts runs with PHAROS_DEFAULT_LOCALE=az. The home page needs a
// session, so the API answers as for a signed-in person (see api-mock.ts).
test.beforeEach(async ({ page }) => {
  await mockApi(page, { signedIn: true });
});

for (const locale of locales) {
  test.describe(`/${locale}`, () => {
    test("shows the home page in its own language", async ({ page }) => {
      await page.goto(`/${locale}`);
      await expect(page.locator("html")).toHaveAttribute("lang", locale);
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(messages[locale].Home.title);
      await expect(page.getByText(messages[locale].Footer.license)).toBeVisible();
      await expect(page).toHaveTitle(messages[locale].Metadata.title);
    });

    for (const colorScheme of ["light", "dark"] as const) {
      test(`has no WCAG 2.2 AA violations (${colorScheme} theme)`, async ({ page }) => {
        await page.emulateMedia({ colorScheme });
        await page.goto(`/${locale}`);
        const results = await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
          .analyze();
        expect(results.violations).toEqual([]);
      });
    }

    test("has a localized not-found page", async ({ page }) => {
      const response = await page.goto(`/${locale}/does-not-exist`);
      expect(response?.status()).toBe(404);
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(
        messages[locale].NotFound.title,
      );
      const results = await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
        .analyze();
      expect(results.violations).toEqual([]);
    });
  });
}

test.describe("language selection", () => {
  test("the installation default applies when the browser language is not supported", async ({
    browser,
  }) => {
    const context = await browser.newContext({ locale: "de-DE" });
    await mockApi(context, { signedIn: true });
    const page = await context.newPage();
    await page.goto("/");
    await expect(page).toHaveURL(/\/az$/);
    await context.close();
  });

  test("a supported browser language wins over the installation default", async ({ browser }) => {
    const context = await browser.newContext({ locale: "ru-RU" });
    await mockApi(context, { signedIn: true });
    const page = await context.newPage();
    await page.goto("/");
    await expect(page).toHaveURL(/\/ru$/);
    await context.close();
  });

  test("a path without a locale is redirected, keeping the path", async ({ browser }) => {
    const context = await browser.newContext({ locale: "tr-TR" });
    await mockApi(context, { signedIn: true });
    const page = await context.newPage();
    await page.goto("/does-not-exist");
    await expect(page).toHaveURL(/\/tr\/does-not-exist$/);
    await context.close();
  });

  test("an unknown language prefix is a 404, not a crash", async ({ page }) => {
    const response = await page.goto("/xx");
    expect(response?.status()).toBe(404);
  });
});

test.describe("keyboard and screen reader basics", () => {
  test("the first Tab stop is a skip link that moves focus to the main content", async ({
    page,
  }) => {
    await page.goto("/en");
    await page.keyboard.press("Tab");
    const skip = page.getByRole("link", { name: en.Common.skipToContent });
    await expect(skip).toBeFocused();
    await expect(skip).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(page.locator("main")).toBeFocused();
  });

  test("the language links are reachable and usable with the keyboard only", async ({ page }) => {
    await page.goto("/en");
    const turkish = page.getByRole("link", { name: "Türkçe" });
    await turkish.focus();
    await expect(turkish).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\/tr$/);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(tr.Home.title);
  });

  test("the language list is a labelled navigation and marks the current language", async ({
    page,
  }) => {
    await page.goto("/az");
    const nav = page.getByRole("navigation", { name: az.Common.language });
    await expect(nav.getByRole("link")).toHaveCount(4);
    await expect(nav.getByRole("link", { name: "Azərbaycanca" })).toHaveAttribute(
      "aria-current",
      "true",
    );
    await expect(nav.getByRole("link", { name: "Русский" })).toHaveAttribute("lang", "ru");
  });

  test("keeps the current page when switching language", async ({ page }) => {
    await page.goto("/en");
    await page.getByRole("link", { name: "Русский" }).click();
    await expect(page).toHaveURL(/\/ru$/);
  });
});

test.describe("web server", () => {
  test("answers the health check", async ({ request }) => {
    const response = await request.get("/healthz");
    expect(response.status()).toBe(200);
    expect(await response.json()).toEqual({ status: "ok" });
  });

  test("sends security headers and hides the framework", async ({ request }) => {
    const response = await request.get("/en");
    const headers = response.headers();
    expect(headers["x-content-type-options"]).toBe("nosniff");
    expect(headers["x-frame-options"]).toBe("DENY");
    expect(headers["referrer-policy"]).toBe("strict-origin-when-cross-origin");
    expect(headers["x-powered-by"]).toBeUndefined();
  });
});
