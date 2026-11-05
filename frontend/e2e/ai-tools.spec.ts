import { expect, test, type Page } from "@playwright/test";
import { violations } from "./axe";
import { mockApi } from "./api-mock";
import { defaultAiTools, tool } from "./ai-tools-mock";
import { az, en, ru, tr } from "./messages";

const messages = { en, az, tr, ru } as const;
const locales = Object.keys(messages) as (keyof typeof messages)[];

/** The page itself: the route announcer of Next.js is an alert and a status of its own. */
const main = (page: Page) => page.locator("main");

async function listed(page: Page, locale: keyof typeof messages) {
  await expect(
    main(page).getByRole("heading", { level: 2, name: messages[locale].AiTools.results }),
  ).toBeVisible();
  await expect(main(page).getByText(messages[locale].AiTools.loading)).toHaveCount(0);
}

// --- every language, both themes, both sizes (the projects) ---------------------------------------

for (const locale of locales) {
  const m = messages[locale].AiTools;
  test.describe(`/${locale}/ai-tools`, () => {
    for (const colorScheme of ["light", "dark"] as const) {
      test.describe(`${colorScheme} theme`, () => {
        test.beforeEach(async ({ page }) => {
          await page.emulateMedia({ colorScheme });
        });

        test("the list has no WCAG 2.2 AA violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "admin", aiTools: defaultAiTools() });
          await page.goto(`/${locale}/ai-tools`);
          await listed(page, locale);
          await expect(page.getByRole("heading", { level: 1 })).toHaveText(m.title);
          expect(await violations(page)).toEqual([]);
        });

        test("the empty list and the read-only view have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "member" });
          await page.goto(`/${locale}/ai-tools`);
          await listed(page, locale);
          await expect(main(page).getByText(m.empty)).toBeVisible();
          expect(await violations(page)).toEqual([]);
        });

        test("the form and its problems have no violations", async ({ page }) => {
          await mockApi(page, { signedIn: true, role: "admin" });
          await page.goto(`/${locale}/ai-tools`);
          await listed(page, locale);
          await page.getByRole("button", { name: m.actions.add }).click();
          await page.getByLabel(m.form.name, { exact: true }).fill("Fresh");
          await page.getByLabel(m.form.url, { exact: true }).fill("http://fresh.example.org/");
          await page
            .getByRole("dialog")
            .getByRole("button", { name: m.form.save, exact: true })
            .click();
          await expect(page.getByText(m.fieldErrors.urlHttpsOnly)).toBeVisible();
          expect(await violations(page)).toEqual([]);
        });

        test("an open suggestion with the tool links has no violations", async ({ page }) => {
          await mockApi(page, {
            signedIn: true,
            insight: "ready",
            aiTools: defaultAiTools(),
          });
          await page.goto(`/${locale}`);
          await page
            .getByRole("button", {
              name: `${messages[locale].Cards.tryThis}: Card 1`,
              exact: true,
            })
            .click();
          await expect(
            page.getByRole("link", {
              name: new RegExp(messages[locale].Cards.insight.openIn.replace("{tool}", "Tool 1")),
            }),
          ).toBeVisible();
          expect(await violations(page)).toEqual([]);
        });
      });
    }
  });
}

// --- behaviour (one language and one project are enough) -----------------------------------------

test.describe("the AI tools page", () => {
  test("an administrator adds a tool with the keyboard, and it is on the list", async ({
    page,
  }) => {
    const state = await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/ai-tools");
    await listed(page, "en");
    await page.getByRole("button", { name: en.AiTools.actions.add }).focus();
    await page.keyboard.press("Enter");
    const name = page.getByLabel(en.AiTools.form.name, { exact: true });
    await expect(name).toBeFocused();
    await name.fill("Fresh Assistant");
    await page.keyboard.press("Tab");
    await page.keyboard.type("https://fresh.example.org/chat");
    await page.keyboard.press("Enter");
    await expect(main(page).getByRole("status")).toContainText("Added: Fresh Assistant");
    await expect(
      main(page).getByRole("heading", { level: 3, name: "Fresh Assistant" }),
    ).toBeVisible();
    expect(state.calls).toContain("POST /api/ai-tools");
    expect(state.aiTools.map((t) => t.name)).toEqual(["Fresh Assistant"]);
  });

  test("a problem with the address is told at the address and nothing is lost", async ({
    page,
  }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en/ai-tools");
    await page.getByRole("button", { name: en.AiTools.actions.add }).click();
    await page.getByLabel(en.AiTools.form.name, { exact: true }).fill("Fresh");
    await page.getByLabel(en.AiTools.form.url, { exact: true }).fill("https://127.0.0.1/");
    await page
      .getByRole("dialog")
      .getByRole("button", { name: en.AiTools.form.save, exact: true })
      .click();
    await expect(page.getByText(en.AiTools.fieldErrors.urlIp)).toBeVisible();
    await expect(page.getByLabel(en.AiTools.form.url, { exact: true })).toHaveValue(
      "https://127.0.0.1/",
    );
    await expect(page.getByLabel(en.AiTools.form.url, { exact: true })).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });

  test("a name that is taken is told for the whole form", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin", aiTools: [tool(1)] });
    await page.goto("/en/ai-tools");
    await page.getByRole("button", { name: en.AiTools.actions.add }).click();
    await page.getByLabel(en.AiTools.form.name, { exact: true }).fill("tool 1");
    await page.getByLabel(en.AiTools.form.url, { exact: true }).fill("https://other.example.org/");
    await page
      .getByRole("dialog")
      .getByRole("button", { name: en.AiTools.form.save, exact: true })
      .click();
    await expect(page.getByRole("dialog").getByRole("alert")).toHaveText(
      en.Problems.aiToolConflict,
    );
  });

  test("a tool is deleted after the question, and Cancel keeps it", async ({ page }) => {
    const state = await mockApi(page, { signedIn: true, role: "admin", aiTools: defaultAiTools() });
    await page.goto("/en/ai-tools");
    await listed(page, "en");
    await page.getByRole("button", { name: `${en.AiTools.actions.delete}: Tool 2` }).click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: en.AiTools.deleteDialog.cancel, exact: true })
      .click();
    await expect(main(page).getByRole("heading", { level: 3, name: "Tool 2" })).toBeVisible();
    expect(state.calls).not.toContain(`DELETE /api/ai-tools/${tool(2).id}`);
    await page.getByRole("button", { name: `${en.AiTools.actions.delete}: Tool 2` }).click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: en.AiTools.deleteDialog.confirm, exact: true })
      .click();
    await expect(main(page).getByRole("status")).toContainText("Deleted: Tool 2");
    await expect(main(page).getByRole("heading", { level: 3, name: "Tool 2" })).toHaveCount(0);
  });

  test("the link is in the navigation for an administrator only", async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    await page.goto("/en");
    await expect(page.getByRole("link", { name: en.Nav.aiTools })).toBeVisible();
    await mockApi(page, { signedIn: true, role: "editor" });
    await page.goto("/en");
    await expect(page.getByRole("link", { name: en.Nav.sources })).toBeVisible();
    await expect(page.getByRole("link", { name: en.Nav.aiTools })).toHaveCount(0);
  });

  test("a card offers the approved tools next to Copy, and only then", async ({ page }) => {
    await mockApi(page, { signedIn: true, insight: "ready", aiTools: [tool(1)] });
    await page.goto("/en");
    await page.getByRole("button", { name: `${en.Cards.tryThis}: Card 1`, exact: true }).click();
    const link = page.getByRole("link", { name: /Open in Tool 1/ });
    await expect(link).toBeVisible();
    await expect(link).toHaveAttribute("href", "https://tool1.example.org/chat");
    await expect(link).toHaveAttribute("target", "_blank");
    await expect(link).toHaveAttribute("rel", /noopener/);
    await expect(link).toHaveAttribute("rel", /noreferrer/);
    await expect(
      page.getByRole("button", { name: `${en.Cards.insight.copy}: Card 1` }),
    ).toBeVisible();
  });

  test("a card with no approved tool offers Copy alone", async ({ page }) => {
    await mockApi(page, { signedIn: true, insight: "ready" });
    await page.goto("/en");
    await page.getByRole("button", { name: `${en.Cards.tryThis}: Card 1`, exact: true }).click();
    await expect(page.getByLabel(en.Cards.insight.promptLabel).first()).toBeVisible();
    await expect(page.getByText(en.Cards.insight.openInTitle)).toHaveCount(0);
  });
});
