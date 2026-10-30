import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./api-mock";
import { defaultItems, item } from "./items-mock";
import { az, en, ru, tr } from "./messages";

const messages = { en, az, tr, ru } as const;
const locales = Object.keys(messages) as (keyof typeof messages)[];
const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

async function listed(page: Page, locale: keyof typeof messages) {
  await expect(
    page.getByRole("heading", { level: 2, name: messages[locale].Items.results }),
  ).toBeVisible();
  await expect(page.getByText(messages[locale].Items.loading)).toHaveCount(0);
}

/** The heading of one story (the notices say "Held: Story 1" too, so text alone is not enough). */
function story(page: Page, n: number) {
  return page.getByRole("heading", { level: 3, name: new RegExp(`^Story ${n}\\b`) });
}

async function violations(page: Page) {
  return (await new AxeBuilder({ page }).withTags(TAGS).analyze()).violations;
}

// --- every language, both themes, both sizes (the projects) ---------------------------------------

for (const locale of locales) {
  test.describe(`/${locale}/items`, () => {
    for (const colorScheme of ["light", "dark"] as const) {
      test.describe(`${colorScheme} theme`, () => {
        test.beforeEach(async ({ page }) => {
          await page.emulateMedia({ colorScheme });
        });

        test("a member's list has no WCAG 2.2 AA violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "member" });
          await page.goto(`/${locale}/items`);
          await listed(page, locale);
          await expect(page.getByRole("heading", { level: 1 })).toHaveText(
            messages[locale].Items.title,
          );
          await expect(page.getByRole("article")).toHaveCount(3);
          expect(await violations(page)).toEqual([]);
        });

        test("an editor's list and the held list have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "editor" });
          await page.goto(`/${locale}/items`);
          await listed(page, locale);
          expect(await violations(page)).toEqual([]);
          await page.getByRole("button", { name: messages[locale].Items.filter.held }).click();
          await expect(page.getByText(messages[locale].Items.reasons.link_ip_host)).toBeVisible();
          expect(await violations(page)).toEqual([]);
        });

        test("the empty list has no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "editor", items: [] });
          await page.goto(`/${locale}/items`);
          await expect(page.getByText(messages[locale].Items.empty.new)).toBeVisible();
          expect(await violations(page)).toEqual([]);
        });

        test("the failure and the refusal have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "member", itemsFail: "down" });
          await page.goto(`/${locale}/items`);
          await expect(page.getByRole("main").getByRole("alert")).toContainText(
            messages[locale].Problems.unavailable,
          );
          expect(await violations(page)).toEqual([]);
          await mockApi(page, { signedIn: true, role: "member", itemsFail: "forbidden" });
          await page.reload();
          await expect(page.getByRole("main").getByRole("alert")).toContainText(
            messages[locale].Problems.forbidden,
          );
          expect(await violations(page)).toEqual([]);
        });
      });
    }

    test("the page is in its own language", async ({ page }) => {
      await mockApi(page, { signedIn: true, role: "editor" });
      await page.goto(`/${locale}/items`);
      await listed(page, locale);
      await expect(page.locator("html")).toHaveAttribute("lang", locale);
      await expect(page).toHaveTitle(messages[locale].Items.metaTitle);
      await expect(
        page.getByRole("navigation", { name: messages[locale].Nav.main }).getByRole("link"),
      ).toHaveCount(3);
      await expect(
        page.getByRole("button", { name: `${messages[locale].Items.actions.hold}: Story 1` }),
      ).toBeVisible();
    });
  });
}

// --- who sees what ------------------------------------------------------------------------------------

test.describe("roles", () => {
  test("a member reads the current articles and has nothing to review with", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "member" });
    await page.goto("/en/items");
    await listed(page, "en");
    await expect(page.getByRole("article")).toHaveCount(3);
    await expect(page.getByRole("group", { name: en.Items.filter.label })).toHaveCount(0);
    await expect(page.getByRole("button", { name: /^(Hold|Hide|Approve|Restore):/ })).toHaveCount(
      0,
    );
    await expect(page.getByText(en.Items.reasons.link_ip_host)).toHaveCount(0);
    await expect(page.getByText("Industry news")).toHaveCount(0);
    await expect(story(page, 4)).toHaveCount(0); // held
    await expect(story(page, 5)).toHaveCount(0); // hidden
  });

  test("an administrator sees the state, the reason and the source of every article", async ({
    page,
  }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/items");
    await listed(page, "en");
    const first = page.getByRole("article").filter({ hasText: "Story 1" });
    await expect(first.getByText(en.Items.status.new)).toBeVisible();
    await expect(first.getByText("From Industry news")).toBeVisible();
    await page.getByRole("button", { name: en.Items.filter.held }).click();
    const held = page.getByRole("article").filter({ hasText: "Story 4" });
    await expect(held.getByText(en.Items.status.held)).toBeVisible();
    await expect(held.getByText(en.Items.reasons.link_ip_host)).toBeVisible();
    await expect(held.getByRole("button")).toHaveText([
      en.Items.actions.approve,
      en.Items.actions.hide,
    ]);
  });
});

// --- review, with the keyboard only ---------------------------------------------------------------------

test.describe("review", () => {
  test("an editor holds, approves and restores with the keyboard only", async ({ page }) => {
    const state = await mockApi(page, { signedIn: true, role: "editor" });
    await page.goto("/en/items");
    await listed(page, "en");

    const hold = page.getByRole("button", { name: "Hold: Story 1" });
    await hold.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("main").getByRole("status")).toHaveText("Held: Story 1");
    await expect(story(page, 1)).toHaveCount(0);
    await expect(page.getByRole("heading", { level: 2, name: en.Items.results })).toBeFocused();
    expect(state.items.find((found) => found.title === "Story 1")?.status).toBe("held");

    const filter = page.getByRole("button", { name: en.Items.filter.held });
    await filter.focus();
    await page.keyboard.press("Space");
    await expect(story(page, 1)).toBeVisible();
    const approve = page.getByRole("button", { name: "Approve: Story 1" });
    await approve.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("main").getByRole("status")).toHaveText("Approved: Story 1");
    await expect(story(page, 1)).toHaveCount(0);

    await page.getByRole("button", { name: en.Items.filter.hidden }).click();
    await page.getByRole("button", { name: "Restore: Story 5" }).click();
    await expect(page.getByRole("main").getByRole("status")).toHaveText("Restored: Story 5");
    await page.getByRole("button", { name: en.Items.filter.new }).click();
    await expect(story(page, 5)).toBeVisible();
    await expect(story(page, 1)).toBeVisible();
    // every step carried the CSRF token (the mock refuses a change without it)
    expect(state.calls.filter((call) => call.startsWith("POST /api/items/"))).toHaveLength(3);
  });

  test("hiding a held article takes it out of the held list", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/items");
    await page.getByRole("button", { name: en.Items.filter.held }).click();
    await page.getByRole("button", { name: "Hide: Story 4" }).click();
    await expect(page.getByRole("main").getByRole("status")).toHaveText("Hidden: Story 4");
    await expect(page.getByText(en.Items.empty.held)).toBeVisible();
  });

  test("someone else got there first: the person is told and the list is refreshed", async ({
    page,
  }) => {
    await mockApi(page, { signedIn: true, role: "editor", conflictOnce: true });
    await page.goto("/en/items");
    await listed(page, "en");
    await page.getByRole("button", { name: "Hold: Story 1" }).click();
    await expect(page.getByRole("main").getByRole("alert")).toHaveText(en.Problems.itemState);
    await expect(story(page, 1)).toHaveCount(0); // it is hidden now
    await expect(story(page, 2)).toBeVisible();
  });

  test("a member whose role was lowered is refused and told so", async ({ page }) => {
    // The page was opened as an editor; the backend answers as for a member.
    await mockApi(page, { signedIn: true, role: "member" });
    await page.goto("/en/items");
    await listed(page, "en");
    const refused = await page.evaluate(async () => {
      const response = await fetch("/api/items/40000000-0000-4000-8000-000000000001/hide", {
        method: "POST",
        headers: { "X-CSRF-Token": "csrf-e2e" },
      });
      return response.status;
    });
    expect(refused).toBe(403);
  });
});

// --- text from third parties ------------------------------------------------------------------------------

test.describe("hostile text", () => {
  test("is shown as text and never runs", async ({ page }) => {
    const hostile = [
      item(1, {
        title: '<img src=x onerror="window.__xss = 1"> Big news',
        excerpt: "<script>window.__xss = 2</script>",
      }),
      item(2, { url: "javascript:window.__xss = 3", title: "Script link" }),
      item(3, { url: "data:text/html,<script>window.__xss=4</script>", title: "Data link" }),
      item(4, { url: "https://user:pass@example.org/", title: "Credentials link" }),
    ];
    await mockApi(page, { signedIn: true, role: "member", items: hostile });
    await page.goto("/en/items");
    await listed(page, "en");
    expect(
      await page.evaluate(() => (window as unknown as { __xss?: number }).__xss),
    ).toBeUndefined();
    await expect(page.locator("main img")).toHaveCount(0);
    await expect(page.locator("main script")).toHaveCount(0);
    await expect(page.getByText('<img src=x onerror="window.__xss = 1"> Big news')).toBeVisible();
    for (const title of ["Script link", "Data link", "Credentials link"]) {
      await expect(page.getByRole("link", { name: new RegExp(title) })).toHaveCount(0);
      await expect(page.getByText(title)).toBeVisible();
    }
    await expect(page.getByText(en.Items.unlinked)).toHaveCount(3);
    const link = page.getByRole("link", { name: /Big news/ });
    await expect(link).toHaveAttribute("rel", "noopener noreferrer nofollow");
    await expect(link).toHaveAttribute("target", "_blank");
  });

  test("a very long word does not push the page sideways", async ({ page }) => {
    const long = "A".repeat(300);
    await mockApi(page, {
      signedIn: true,
      role: "editor",
      items: [item(1, { title: long, excerpt: long, url: `https://news.example.org/${long}` })],
    });
    await page.goto("/en/items");
    await listed(page, "en");
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
});

// --- pages, navigation, sign-in ----------------------------------------------------------------------------------

test.describe("pages", () => {
  test("twenty at a time, with the range and the buttons telling where one is", async ({
    page,
  }) => {
    const many = Array.from({ length: 45 }, (_, index) => item(index + 1));
    await mockApi(page, { signedIn: true, role: "member", items: many });
    await page.goto("/en/items");
    await listed(page, "en");
    await expect(page.getByRole("article")).toHaveCount(20);
    await expect(page.getByText("1–20 of 45")).toBeVisible();
    await expect(page.getByRole("button", { name: en.Items.previous })).toBeDisabled();
    await page.getByRole("button", { name: en.Items.next }).click();
    await expect(page.getByText("21–40 of 45")).toBeVisible();
    await page.getByRole("button", { name: en.Items.next }).click();
    await expect(page.getByText("41–45 of 45")).toBeVisible();
    await expect(page.getByRole("article")).toHaveCount(5);
    await expect(page.getByRole("button", { name: en.Items.next })).toBeDisabled();
    await page.getByRole("button", { name: en.Items.previous }).click();
    await expect(page.getByText("21–40 of 45")).toBeVisible();
  });
});

test.describe("navigation", () => {
  test("leads from the start page to the articles and marks where one is", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "member" });
    await page.goto("/en");
    const nav = page.getByRole("navigation", { name: en.Nav.main });
    await expect(nav.getByRole("link", { name: en.Nav.home })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await nav.getByRole("link", { name: en.Nav.items }).click();
    await expect(page).toHaveURL(/\/en\/items$/);
    await expect(nav.getByRole("link", { name: en.Nav.items })).toHaveAttribute(
      "aria-current",
      "page",
    );
    await expect(nav.getByRole("link", { name: en.Nav.home })).not.toHaveAttribute("aria-current");
  });

  test("is not there before sign-in", async ({ page }) => {
    await mockApi(page, { signedIn: false });
    await page.goto("/en/login");
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(en.Login.title);
    await expect(page.getByRole("navigation", { name: en.Nav.main })).toHaveCount(0);
  });

  test("sends someone who is not signed in to sign in and brings them back", async ({ page }) => {
    await mockApi(page, { signedIn: false });
    await page.goto("/en/items");
    await expect(page).toHaveURL(/\/en\/login\?next=%2Fen%2Fitems$/);
  });

  test("is not shown while the onboarding questions are open", async ({ page }) => {
    await mockApi(page, { signedIn: true, onboarded: false });
    await page.goto("/en/items");
    await expect(page).toHaveURL(/\/en\/onboarding$/);
    await expect(page.getByRole("navigation", { name: en.Nav.main })).toHaveCount(0);
  });
});

test("the default items are one of each state", () => {
  expect(defaultItems().map((found) => found.status)).toEqual([
    "new",
    "new",
    "new",
    "held",
    "hidden",
  ]);
});
