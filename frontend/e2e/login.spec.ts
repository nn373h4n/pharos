import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { CSRF_TOKEN, USER_NAME, mockApi } from "./api-mock";
import { az, en, ru, tr } from "./messages";

const messages = { en, az, tr, ru } as const;
const locales = Object.keys(messages) as (keyof typeof messages)[];
const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

async function expectNoViolations(page: Page) {
  const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  expect(results.violations).toEqual([]);
}

// Tabs forward until the element with this id has focus: proves it can be reached by keyboard.
async function tabTo(page: Page, id: string) {
  for (let step = 0; step < 15; step += 1) {
    await page.keyboard.press("Tab");
    if (await page.evaluate((wanted) => document.activeElement?.id === wanted, id)) return;
  }
  throw new Error(`#${id} cannot be reached with the Tab key`);
}

for (const locale of locales) {
  const text = messages[locale];

  test.describe(`/${locale}/login`, () => {
    for (const colorScheme of ["light", "dark"] as const) {
      test(`has no WCAG 2.2 AA violations (${colorScheme} theme)`, async ({ page }) => {
        await mockApi(page);
        await page.emulateMedia({ colorScheme });
        await page.goto(`/${locale}/login`);
        await expect(page.getByRole("heading", { level: 1 })).toHaveText(text.Login.title);
        await expect(page.getByLabel(text.Login.username)).toBeVisible();
        await expectNoViolations(page);
      });

      test(`has no violations while showing an error (${colorScheme} theme)`, async ({ page }) => {
        await mockApi(page, { loginResult: "wrong" });
        await page.emulateMedia({ colorScheme });
        await page.goto(`/${locale}/login`);
        await page.getByLabel(text.Login.username).fill("ana");
        await page.getByLabel(text.Login.password, { exact: true }).fill("wrong");
        await page.getByRole("button", { name: text.Login.submit }).click();
        await expect(page.locator("main").getByRole("alert")).toHaveText(
          text.LoginErrors.invalid_credentials,
        );
        await expectNoViolations(page);
      });
    }

    test("signs in with the keyboard alone and lands on the page that was asked for", async ({
      page,
    }) => {
      const api = await mockApi(page);
      await page.goto(`/${locale}/login?next=${encodeURIComponent(`/${locale}`)}`);
      await tabTo(page, "username");
      await page.keyboard.type("ana");
      await page.keyboard.press("Tab");
      await expect(page.getByLabel(text.Login.password, { exact: true })).toBeFocused();
      await page.keyboard.type("correct horse battery");
      await page.keyboard.press("Enter");
      await expect(page).toHaveURL(new RegExp(`/${locale}$`));
      await expect(page.getByText(text.Home.welcome.replace("{name}", USER_NAME))).toBeVisible();
      expect(api.calls).toContain("POST /api/auth/local/login");
    });

    test("offers single sign-on first, naming the provider, and keeps the target page", async ({
      page,
    }) => {
      await mockApi(page);
      await page.goto(`/${locale}/login?next=${encodeURIComponent(`/${locale}`)}`);
      const link = page.getByRole("link", {
        name: text.Login.ssoButton.replace("{provider}", "Contoso"),
      });
      await expect(link).toHaveAttribute(
        "href",
        `/api/auth/oidc/login?next=${encodeURIComponent(`/${locale}`)}`,
      );
    });

    test("never carries a target outside this site into the single sign-on link", async ({
      page,
    }) => {
      await mockApi(page);
      await page.goto(`/${locale}/login?next=${encodeURIComponent("//evil.test/x")}`);
      const link = page.getByRole("link", {
        name: text.Login.ssoButton.replace("{provider}", "Contoso"),
      });
      await expect(link).toHaveAttribute("href", "/api/auth/oidc/login?next=%2F");
    });

    test("says what went wrong after a failed single sign-on", async ({ page }) => {
      await mockApi(page);
      await page.goto(`/${locale}/login?error=account_disabled`);
      await expect(page.locator("main").getByRole("alert")).toHaveText(
        text.LoginErrors.account_disabled,
      );
      await expectNoViolations(page);
    });

    test("tells how long to wait after too many attempts", async ({ page }) => {
      await mockApi(page, { loginResult: "limited" });
      await page.goto(`/${locale}/login`);
      await page.getByLabel(text.Login.username).fill("ana");
      await page.getByLabel(text.Login.password, { exact: true }).fill("x");
      await page.getByRole("button", { name: text.Login.submit }).click();
      await expect(page.locator("main").getByRole("alert")).toHaveText(
        text.LoginErrors.rate_limited.replace("{seconds}", "30"),
      );
    });

    test("sends an anonymous visitor from the app to sign in, then back", async ({ page }) => {
      await mockApi(page);
      await page.goto(`/${locale}`);
      await expect(page).toHaveURL(new RegExp(`/${locale}/login\\?next=%2F${locale}$`));
      await page.getByLabel(text.Login.username).fill("ana");
      await page.getByLabel(text.Login.password, { exact: true }).fill("correct horse battery");
      await page.getByRole("button", { name: text.Login.submit }).click();
      await expect(page).toHaveURL(new RegExp(`/${locale}$`));
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(text.Home.title);
    });

    test("shows who is signed in and signs out with the CSRF token", async ({ page }) => {
      const api = await mockApi(page, { signedIn: true, role: "editor" });
      await page.goto(`/${locale}`);
      const account = page.getByRole("group", { name: text.Shell.account });
      await expect(account).toContainText(USER_NAME);
      await expect(account).toContainText(text.Shell.roleEditor);
      await account.getByRole("button", { name: text.Shell.signOut }).click();
      await expect(page).toHaveURL(new RegExp(`/${locale}/login`));
      expect(api.logoutCsrf).toBe(CSRF_TOKEN);
      expect(api.signedIn).toBe(false);
    });
  });
}

test.describe("signing out of a single sign-on session", () => {
  test("ends up on the provider's sign-out page and stays there", async ({ page }) => {
    const providerPage = "https://idp.example/logout?client=pharos";
    await page.route("https://idp.example/**", (route) =>
      route.fulfill({ contentType: "text/html", body: "<p>provider signed you out</p>" }),
    );
    await mockApi(page, { signedIn: true, providerLogoutUrl: providerPage });
    await page.goto("/en");
    await page.getByRole("button", { name: en.Shell.signOut }).click();
    await expect(page).toHaveURL(providerPage);
    // The app must not move the browser on to its own sign-in page behind the provider's back.
    await page.waitForTimeout(1500);
    await expect(page).toHaveURL(providerPage);
  });
});

test.describe("what the sign-in page offers", () => {
  test("shows no password form when password sign-in is off", async ({ page }) => {
    await mockApi(page, { localLogin: "off" });
    await page.goto("/en/login");
    await expect(page.getByRole("link", { name: /Contoso/ })).toBeVisible();
    await expect(page.getByLabel(en.Login.username)).toHaveCount(0);
  });

  test("shows only the password form when no provider is configured", async ({ page }) => {
    await mockApi(page, { provider: null, localLogin: "break-glass" });
    await page.goto("/en/login");
    await expect(page.getByLabel(en.Login.username)).toBeVisible();
    await expect(page.getByText(en.Login.breakGlassHint)).toBeVisible();
    await expect(page.getByRole("link", { name: /Contoso/ })).toHaveCount(0);
  });

  test("the keyboard reaches the single sign-on link", async ({ page }) => {
    await mockApi(page);
    await page.goto("/en/login");
    const link = page.getByRole("link", { name: /Contoso/ });
    await link.focus();
    await expect(link).toBeFocused();
  });
});
