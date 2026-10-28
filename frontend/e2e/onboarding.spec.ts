import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { CSRF_TOKEN, PROFESSIONS, mockApi } from "./api-mock";
import { az, en, ru, tr } from "./messages";

const messages = { en, az, tr, ru } as const;
const locales = Object.keys(messages) as (keyof typeof messages)[];
const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

// Presses Tab until the focused element matches the selector: proves the keyboard reaches it.
async function tabTo(page: Page, selector: string) {
  for (let presses = 0; presses < 40; presses += 1) {
    await page.keyboard.press("Tab");
    if (await page.evaluate((wanted) => document.activeElement?.matches(wanted) ?? false, selector))
      return;
  }
  throw new Error(`${selector} cannot be reached with the Tab key`);
}

const SUBMIT = "button[type=submit]";

for (const locale of locales) {
  const text = messages[locale];

  test.describe(`/${locale}/onboarding`, () => {
    test("is where someone who has not answered the questions is sent", async ({ page }) => {
      await mockApi(page, { signedIn: true, onboarded: false });
      await page.goto(`/${locale}`);
      await expect(page).toHaveURL(new RegExp(`/${locale}/onboarding$`));
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(text.Onboarding.title);
    });

    test("sends someone who is done straight to the start page", async ({ page }) => {
      await mockApi(page, { signedIn: true });
      await page.goto(`/${locale}/onboarding`);
      await expect(page).toHaveURL(new RegExp(`/${locale}$`));
    });

    for (const colorScheme of ["light", "dark"] as const) {
      for (const [index, step] of ["profession", "interests", "language", "aiLevel"].entries()) {
        test(`step ${index + 1} has no WCAG 2.2 AA violations (${colorScheme} theme)`, async ({
          page,
        }) => {
          // A step cannot be reached before the ones in front of it are answered, so the profile
          // already holds those answers.
          const answered = {
            ...(index >= 1 ? { profession_id: PROFESSIONS[0]!.id } : {}),
            ...(index >= 2 ? { interests: ["research"] } : {}),
            ...(index >= 3 ? { locale } : {}),
          };
          await mockApi(page, { signedIn: true, onboarded: false, profile: answered });
          await page.emulateMedia({ colorScheme });
          await page.goto(`/${locale}/onboarding?step=${index + 1}`);
          await expect(page.getByRole("heading", { level: 2 })).toHaveText(
            text.Onboarding[step as "profession" | "interests" | "language" | "aiLevel"].heading,
          );
          const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
          expect(results.violations).toEqual([]);
        });
      }
    }

    test("is completed with the keyboard alone, well inside two minutes", async ({ page }) => {
      const api = await mockApi(page, { signedIn: true, onboarded: false });
      await page.goto(`/${locale}/onboarding`);
      await expect(page.getByRole("heading", { level: 2 })).toHaveText(
        text.Onboarding.profession.heading,
      );
      const started = Date.now();

      await tabTo(page, "input[name=profession]");
      await page.keyboard.press("Space");
      await tabTo(page, SUBMIT);
      await page.keyboard.press("Enter");

      await expect(page.getByRole("heading", { level: 2 })).toHaveText(
        text.Onboarding.interests.heading,
      );
      await expect(page.getByRole("heading", { level: 2 })).toBeFocused();
      await tabTo(page, "input[name=interests]");
      await page.keyboard.press("Space");
      await tabTo(page, SUBMIT);
      await page.keyboard.press("Enter");

      await expect(page.getByRole("heading", { level: 2 })).toHaveText(
        text.Onboarding.language.heading,
      );
      await tabTo(page, "input[name=language]"); // the first language is English
      if (locale !== "en") {
        // Choose the language that is already shown, so the page does not reload.
        const own = ["en", "az", "tr", "ru"].indexOf(locale);
        for (let step = 0; step < own; step += 1) await page.keyboard.press("ArrowDown");
      }
      await page.keyboard.press("Space");
      await tabTo(page, SUBMIT);
      await page.keyboard.press("Enter");

      await expect(page.getByRole("heading", { level: 2 })).toHaveText(
        text.Onboarding.aiLevel.heading,
      );
      await tabTo(page, "input[name=ai-level]");
      await page.keyboard.press("Space");
      await tabTo(page, SUBMIT);
      await page.keyboard.press("Enter");

      await expect(page).toHaveURL(new RegExp(`/${locale}$`));
      await expect(page.getByRole("heading", { level: 1 })).toHaveText(text.Home.title);
      expect(Date.now() - started).toBeLessThan(120_000);

      expect(api.saves).toEqual([
        { profession_id: PROFESSIONS[0]!.id }, // an unchanged department is not sent
        { interests: ["writing"] },
        { locale },
        { ai_level: "beginner" },
      ]);
      expect(api.profileCsrf).toEqual(Array(5).fill(CSRF_TOKEN)); // four saves and the completion
      expect(api.onboarded).toBe(true);
    });

    test("tells the person to choose before going on", async ({ page }) => {
      const api = await mockApi(page, { signedIn: true, onboarded: false });
      await page.goto(`/${locale}/onboarding`);
      await page.getByRole("button", { name: text.Onboarding.next }).click();
      await expect(page.locator("main").getByRole("alert")).toHaveText(
        text.Onboarding.problems.choose_profession,
      );
      expect(api.saves).toEqual([]);
    });
  });
}

test.describe("onboarding in more detail", () => {
  test("continues where the person stopped", async ({ page }) => {
    await mockApi(page, {
      signedIn: true,
      onboarded: false,
      profile: { profession_id: PROFESSIONS[0]!.id, interests: ["research"] },
    });
    await page.goto("/en/onboarding");
    await expect(page.getByRole("heading", { level: 2 })).toHaveText(
      en.Onboarding.language.heading,
    );
    await page.getByRole("button", { name: en.Onboarding.back }).click();
    await expect(
      page.getByRole("checkbox", { name: en.Onboarding.interestNames.research }),
    ).toBeChecked();
  });

  test("switches the whole page to the language that was chosen and goes on", async ({ page }) => {
    await mockApi(page, {
      signedIn: true,
      onboarded: false,
      profile: { profession_id: PROFESSIONS[0]!.id, interests: ["research"] },
    });
    await page.goto("/en/onboarding");
    await page.getByRole("radio", { name: "Azərbaycanca" }).check();
    await page.getByRole("button", { name: en.Onboarding.next }).click();
    await expect(page).toHaveURL(/\/az\/onboarding\?step=4$/);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(az.Onboarding.title);
    await expect(page.getByRole("heading", { level: 2 })).toHaveText(az.Onboarding.aiLevel.heading);
  });

  test("offers the departments as an optional choice and sends it along", async ({ page }) => {
    const api = await mockApi(page, { signedIn: true, onboarded: false });
    await page.goto("/en/onboarding");
    await page.getByRole("radio", { name: "HR specialist" }).check();
    await page.getByLabel(en.Onboarding.profession.department).selectOption({ label: "Finance" });
    await page.getByRole("button", { name: en.Onboarding.next }).click();
    await expect(page.getByRole("heading", { level: 2 })).toHaveText(
      en.Onboarding.interests.heading,
    );
    expect(api.saves[0]).toEqual({
      profession_id: PROFESSIONS[1]!.id,
      department_id: "20000000-0000-4000-8000-000000000001",
    });
  });
});
