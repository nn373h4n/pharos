import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { mockApi } from "./api-mock";
import { card } from "./cards-mock";
import { az, en, ru, tr } from "./messages";

const messages = { en, az, tr, ru } as const;
const locales = Object.keys(messages) as (keyof typeof messages)[];

/** The page itself: the route announcer of Next.js is an alert and a status of its own. */
const main = (page: Page) => page.locator("main");

async function listed(page: Page, locale: keyof typeof messages) {
  await expect(
    main(page).getByRole("heading", { level: 2, name: messages[locale].Cards.results }),
  ).toBeVisible();
  await expect(main(page).getByRole("article").first()).toBeVisible();
}

/** The Try this button of a card, named as a person using a screen reader hears it. */
function tryThis(page: Page, locale: keyof typeof messages, title: string) {
  return page.getByRole("button", {
    name: `${messages[locale].Cards.tryThis}: ${title}`,
    exact: true,
  });
}

// --- every language, both themes, both sizes (the projects) ---------------------------------------

for (const locale of locales) {
  const m = messages[locale].Cards;
  test.describe(`/${locale} the cards`, () => {
    for (const colorScheme of ["light", "dark"] as const) {
      test.describe(`${colorScheme} theme`, () => {
        test.beforeEach(async ({ page }) => {
          await page.emulateMedia({ colorScheme });
        });

        test("the feed has no WCAG 2.2 AA violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "editor" });
          await page.goto(`/${locale}`);
          await listed(page, locale);
          await expect(main(page).getByRole("article")).toHaveCount(20);
          expect(await violations(page)).toEqual([]);
        });

        test("an opened suggestion has no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, insight: "ready" });
          await page.goto(`/${locale}`);
          await listed(page, locale);
          await tryThis(page, locale, "Card 1").click();
          await expect(page.getByLabel(m.insight.promptLabel).first()).toBeVisible();
          expect(await violations(page)).toEqual([]);
        });

        test("the waiting and the unavailable suggestion have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, insight: "never" });
          await page.goto(`/${locale}`);
          await listed(page, locale);
          await tryThis(page, locale, "Card 1").click();
          await expect(main(page).getByText(m.insight.pending)).toBeVisible();
          expect(await violations(page)).toEqual([]);
        });
      });
    }
  });
}

// --- behaviour (one language and one project are enough) -----------------------------------------

test.describe("the Home feed", () => {
  test("shows twenty cards and then the rest on request", async ({ page }) => {
    await mockApi(page, { signedIn: true });
    await page.goto("/en");
    await listed(page, "en");
    await expect(main(page).getByRole("article")).toHaveCount(20);
    await page.getByRole("button", { name: en.Cards.showMore }).click();
    await expect(main(page).getByRole("article")).toHaveCount(25);
    await expect(page.getByRole("button", { name: en.Cards.showMore })).toHaveCount(0);
  });

  test("says when there is nothing and when it could not load", async ({ page }) => {
    await mockApi(page, { signedIn: true, cards: [] });
    await page.goto("/en");
    await expect(main(page).getByText(en.Cards.empty)).toBeVisible();
    await mockApi(page, { signedIn: true, cardsFail: "down" });
    await page.goto("/en");
    await expect(main(page).getByRole("alert")).toHaveText(en.Cards.loadFailed);
    await expect(page.getByRole("button", { name: en.Cards.retry })).toBeVisible();
  });

  test("saving and hiding are sent with the CSRF token and can be taken back", async ({ page }) => {
    const state = await mockApi(page, { signedIn: true });
    await page.goto("/en");
    await listed(page, "en");
    const first = card(1);
    await page.getByRole("button", { name: `${en.Cards.save}: Card 1`, exact: true }).click();
    await expect(
      page.getByRole("button", { name: `${en.Cards.saved}: Card 1`, exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: `${en.Cards.notUseful}: Card 2`, exact: true }).click();
    await expect(main(page).getByText("Hidden: Card 2")).toBeVisible();
    const second = page.locator(`[aria-labelledby="card-${card(2).id}"]`);
    await expect(second).toHaveCount(0);
    await expect(
      main(page).getByRole("heading", { level: 2, name: en.Cards.results }),
    ).toBeFocused();
    await page.getByRole("button", { name: en.Cards.undo }).click();
    await expect(second).toHaveCount(1);
    expect(state.interactions).toEqual([
      `PUT save ${first.id}`,
      `PUT not_useful ${card(2).id}`,
      `DELETE not_useful ${card(2).id}`,
    ]);
  });

  test("the suggestion can be read, copied and reported on with the keyboard alone", async ({
    page,
    context,
  }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const state = await mockApi(page, { signedIn: true, insight: "slow" });
    await page.goto("/en");
    await listed(page, "en");
    const open = tryThis(page, "en", "Card 1");
    await open.focus();
    await page.keyboard.press("Enter");
    await expect(open).toHaveAttribute("aria-expanded", "true");
    await expect(main(page).getByText(en.Cards.insight.pending)).toBeVisible();
    await expect(page.getByLabel(en.Cards.insight.promptLabel)).toBeVisible({ timeout: 15_000 });
    const copy = page.getByRole("button", {
      name: `${en.Cards.insight.copy}: Card 1`,
      exact: true,
    });
    await copy.focus();
    await page.keyboard.press("Enter");
    await expect(main(page).getByText(en.Cards.insight.copied)).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
      "List unusual entries in [LEDGER EXTRACT] and explain why each looks unusual.",
    );
    await page.getByLabel(en.Cards.insight.minutesLabel).fill("45");
    await page
      .getByRole("button", { name: `${en.Cards.insight.minutesSave}: Card 1`, exact: true })
      .click();
    await expect(main(page).getByText(en.Cards.insight.minutesDone)).toBeVisible();
    expect(state.interactions).toContain(`PUT time_saved ${card(1).id}`);
    expect(state.interactions).toContain(`PUT view ${card(1).id}`);
  });

  test("a suggestion that cannot be made says why", async ({ page }) => {
    await mockApi(page, { signedIn: true, insight: "rejected" });
    await page.goto("/en");
    await listed(page, "en");
    await tryThis(page, "en", "Card 1").click();
    await expect(main(page).getByText(en.Cards.insight.unavailable.rejected)).toBeVisible();
    await expect(page.getByLabel(en.Cards.insight.promptLabel)).toHaveCount(0);
  });

  test("hostile text from a model stays text and does not break the page", async ({ page }) => {
    await mockApi(page, { signedIn: true, insight: "hostile" });
    await page.goto("/en");
    await listed(page, "en");
    await tryThis(page, "en", "Card 1").click();
    await expect(page.getByLabel(en.Cards.insight.promptLabel)).toBeVisible();
    await expect(main(page).getByText("<script>alert(2)</script>")).toBeVisible();
    await expect(page.locator("main img, main script, main b")).toHaveCount(0);
    const links = await main(page)
      .getByRole("link")
      .evaluateAll((a) => a.map((x) => x.textContent));
    expect(links.every((text) => !text?.includes("evil"))).toBe(true);
    const sideways = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    );
    expect(sideways).toBe(false);
  });
});
