import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { mockApi } from "./api-mock";
import { feed } from "./feeds-mock";
import { az, en, ru, tr } from "./messages";

const messages = { en, az, tr, ru } as const;
const locales = Object.keys(messages) as (keyof typeof messages)[];

async function listed(page: Page, locale: keyof typeof messages) {
  await expect(
    page.getByRole("heading", { level: 2, name: messages[locale].Feeds.results }),
  ).toBeVisible();
  await expect(page.getByText(messages[locale].Feeds.loading)).toHaveCount(0);
}

/** A button of one feed, named as a person using a screen reader hears it: "Action: name". */
function action(page: Page, label: string, name: string) {
  return page.getByRole("button", { name: `${label}: ${name}` });
}

/** The page-level notice region (each feed has no status region of its own). */
function notice(page: Page) {
  return page.getByRole("status").first();
}

// --- every language, both themes, both sizes (the projects) ---------------------------------------

for (const locale of locales) {
  const m = messages[locale];
  test.describe(`/${locale}/my-feeds`, () => {
    for (const colorScheme of ["light", "dark"] as const) {
      test.describe(`${colorScheme} theme`, () => {
        test.beforeEach(async ({ page }) => {
          await page.emulateMedia({ colorScheme });
        });

        test("the list has no WCAG 2.2 AA violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "member" });
          await page.goto(`/${locale}/my-feeds`);
          await listed(page, locale);
          await expect(page.getByRole("heading", { level: 1 })).toHaveText(m.Feeds.title);
          await expect(page.getByRole("article")).toHaveCount(2);
          expect(await violations(page)).toEqual([]);
        });

        test("the add dialog and the delete question have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "member" });
          await page.goto(`/${locale}/my-feeds`);
          await listed(page, locale);
          await page.getByRole("button", { name: m.Feeds.actions.add }).click();
          await expect(page.getByRole("dialog")).toBeVisible();
          expect(await violations(page)).toEqual([]);
          await page.keyboard.press("Escape");
          await expect(page.getByRole("dialog")).toHaveCount(0);
          await action(page, m.Sources.actions.delete, "My reading").click();
          await expect(page.getByRole("dialog")).toBeVisible();
          expect(await violations(page)).toEqual([]);
        });

        test("the articles and the latest fetches have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "member" });
          await page.goto(`/${locale}/my-feeds`);
          await listed(page, locale);
          const card = page.getByRole("article").filter({ hasText: "My reading" });
          await card.getByText(m.Feeds.articles.title, { exact: true }).click();
          await expect(card.getByText("First post")).toBeVisible();
          await card.getByText(m.Sources.actions.runs).click();
          await expect(card.getByRole("table")).toBeVisible();
          expect(await violations(page)).toEqual([]);
        });

        test("the empty list, the failure and the switched-off feature have no violations", async ({
          page,
        }) => {
          await mockApi(page, { signedIn: true, role: "member", feeds: [] });
          await page.goto(`/${locale}/my-feeds`);
          await expect(page.getByText(m.Feeds.empty)).toBeVisible();
          expect(await violations(page)).toEqual([]);
          await mockApi(page, { signedIn: true, role: "member", feedsFail: "down" });
          await page.reload();
          await expect(page.getByRole("main").getByRole("alert")).toContainText(
            m.Problems.unavailable,
          );
          expect(await violations(page)).toEqual([]);
          await mockApi(page, { signedIn: true, role: "member", feedsOff: true });
          await page.reload();
          await expect(page.getByRole("main").getByRole("alert")).toContainText(m.Feeds.off);
          expect(await violations(page)).toEqual([]);
        });
      });
    }

    test("the page is in its own language and in the navigation", async ({ page }) => {
      await mockApi(page, { signedIn: true, role: "member" });
      await page.goto(`/${locale}/my-feeds`);
      await listed(page, locale);
      await expect(page.locator("html")).toHaveAttribute("lang", locale);
      await expect(page).toHaveTitle(m.Feeds.metaTitle);
      const nav = page.getByRole("navigation", { name: m.Nav.main });
      await expect(nav.getByRole("link")).toHaveCount(3);
      await expect(nav.getByRole("link", { name: m.Nav.myFeeds })).toHaveAttribute(
        "aria-current",
        "page",
      );
    });
  });
}

// --- who sees it ------------------------------------------------------------------------------------

test.describe("roles", () => {
  for (const role of ["member", "editor", "admin"] as const) {
    test(`a ${role} has feeds of their own`, async ({ page }) => {
      await mockApi(page, { signedIn: true, role });
      await page.goto("/en/my-feeds");
      await listed(page, "en");
      await expect(page.getByRole("button", { name: en.Feeds.actions.add })).toBeVisible();
      await expect(action(page, en.Sources.actions.edit, "My reading")).toBeVisible();
    });
  }

  test("someone who is not signed in is sent to sign in", async ({ page }) => {
    await mockApi(page, { signedIn: false });
    await page.goto("/en/my-feeds");
    await expect(page).toHaveURL(/\/en\/login/);
  });
});

// --- the flows -------------------------------------------------------------------------------------------

test.describe("flows", () => {
  test("adding a feed, with the keyboard only", async ({ page }) => {
    const state = await mockApi(page, { signedIn: true, role: "member" });
    await page.goto("/en/my-feeds");
    await listed(page, "en");
    await page.getByRole("button", { name: en.Feeds.actions.add }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByLabel(en.Sources.form.name).fill("Fresh blog");
    await page.getByLabel(en.Sources.form.url).fill("https://fresh.example.org/feed.xml");
    await page.getByRole("button", { name: en.Sources.form.save }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(notice(page)).toContainText("Added: Fresh blog");
    await expect(page.getByRole("heading", { level: 2, name: en.Feeds.results })).toBeFocused();
    await expect(page.getByRole("heading", { level: 3, name: "Fresh blog" })).toBeVisible();
    expect(state.calls).toContain("POST /api/me/feeds");
  });

  test("an address pointing inwards is told at the field and the dialog stays", async ({
    page,
  }) => {
    await mockApi(page, { signedIn: true, role: "member" });
    await page.goto("/en/my-feeds");
    await listed(page, "en");
    await page.getByRole("button", { name: en.Feeds.actions.add }).click();
    await page.getByLabel(en.Sources.form.name).fill("Inside");
    await page.getByLabel(en.Sources.form.url).fill("http://intranet/feed.xml");
    await page.getByRole("button", { name: en.Sources.form.save }).click();
    await expect(page.getByLabel(en.Sources.form.url)).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByText(en.Sources.fieldErrors.urlNotAllowed)).toBeVisible();
    await expect(page.getByRole("dialog")).toBeVisible();
  });

  test("the sixth feed is not offered, and the limit is told", async ({ page }) => {
    await mockApi(page, {
      signedIn: true,
      role: "member",
      feeds: [1, 2, 3, 4, 5].map((n) => feed(n)),
    });
    await page.goto("/en/my-feeds");
    await listed(page, "en");
    await expect(page.getByRole("button", { name: en.Feeds.actions.add })).toBeDisabled();
    await expect(page.getByText(en.Feeds.limitReached.replace("{max}", "5"))).toBeVisible();
  });

  test("an address that is followed already is a conflict told for the whole form", async ({
    page,
  }) => {
    await mockApi(page, { signedIn: true, role: "member" });
    await page.goto("/en/my-feeds");
    await listed(page, "en");
    await page.getByRole("button", { name: en.Feeds.actions.add }).click();
    await page.getByLabel(en.Sources.form.name).fill("Same again");
    await page.getByLabel(en.Sources.form.url).fill("https://blog.example.org/feed-1.xml");
    await page.getByRole("button", { name: en.Sources.form.save }).click();
    await expect(page.getByRole("dialog")).toContainText(en.Problems.sourceConflict);
  });

  test("switching off and on", async ({ page }) => {
    const state = await mockApi(page, { signedIn: true, role: "member" });
    await page.goto("/en/my-feeds");
    await listed(page, "en");
    await action(page, en.Sources.actions.disable, "My reading").click();
    await expect(notice(page)).toContainText("Switched off: My reading");
    await expect(action(page, en.Sources.actions.enable, "My reading")).toBeVisible();
    expect(state.calls.filter((call) => call.startsWith("PATCH"))).toHaveLength(1);
  });

  test("deleting asks first; Cancel has focus and deletes nothing", async ({ page }) => {
    const state = await mockApi(page, { signedIn: true, role: "member" });
    await page.goto("/en/my-feeds");
    await listed(page, "en");
    await action(page, en.Sources.actions.delete, "Broken blog").click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("Broken blog");
    await expect(dialog.getByRole("button", { name: en.Feeds.deleteDialog.cancel })).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(dialog).toHaveCount(0);
    expect(state.calls.some((call) => call.startsWith("DELETE"))).toBe(false);
    await action(page, en.Sources.actions.delete, "Broken blog").click();
    await dialog.getByRole("button", { name: en.Feeds.deleteDialog.confirm }).click();
    await expect(notice(page)).toContainText("Deleted: Broken blog");
    await expect(page.getByRole("heading", { level: 2, name: en.Feeds.results })).toBeFocused();
    await expect(page.getByRole("heading", { level: 3, name: "Broken blog" })).toHaveCount(0);
  });

  test("a failing feed shows its problem in words, not as a code", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "member" });
    await page.goto("/en/my-feeds");
    await listed(page, "en");
    const card = page.getByRole("article").filter({ hasText: "Broken blog" });
    await expect(card).toContainText(en.Sources.health.failing);
    await expect(card).not.toContainText("timed_out");
  });

  test("hostile article text stays text and an unsafe address is no link", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "member" });
    const dialogs: string[] = [];
    page.on("dialog", (dialog) => {
      dialogs.push(dialog.message());
      void dialog.dismiss();
    });
    await page.goto("/en/my-feeds");
    await listed(page, "en");
    const card = page.getByRole("article").filter({ hasText: "My reading" });
    await card.getByText(en.Feeds.articles.title, { exact: true }).click();
    await expect(card.getByText("First post")).toBeVisible();
    await expect(card.getByText("<img src=x onerror=alert(1)> Flagged post")).toBeVisible();
    await expect(card.locator("img")).toHaveCount(0);
    await expect(card.getByText(en.Feeds.articles.flagged)).toBeVisible();
    await expect(card.getByText(en.Items.reasons.link_ip_host)).toBeVisible();
    const link = card.getByRole("link", { name: /First post/ });
    await expect(link).toHaveAttribute("href", "https://blog.example.org/post-1");
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", "noopener noreferrer nofollow");
    await expect(card.getByRole("link", { name: /Not a web address/ })).toHaveCount(0);
    expect(dialogs).toEqual([]);
  });
});

// --- small screens --------------------------------------------------------------------------------------

test("nothing scrolls sideways, even with long hostile names", async ({ page }) => {
  await mockApi(page, {
    signedIn: true,
    role: "member",
    feeds: [
      feed(1, { name: "W".repeat(150), url: `https://example.org/${"a".repeat(300)}` }),
      feed(2),
    ],
  });
  await page.goto("/en/my-feeds");
  await listed(page, "en");
  await page.getByText(en.Sources.actions.runs).first().click();
  const wide = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(wide).toBe(false);
});
