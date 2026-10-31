import AxeBuilder from "@axe-core/playwright";
import type { Page } from "@playwright/test";

export const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

/**
 * The WCAG violations of the page as it is now. Colour transitions are switched off first: a button
 * that was just clicked is still changing colour for a moment, and axe measures the contrast of
 * a colour halfway between two (it did, in CI), which is not what a person sees once it settles.
 */
export async function violations(page: Page) {
  await page.addStyleTag({
    content: "*, *::before, *::after { transition: none !important; animation: none !important; }",
  });
  return (await new AxeBuilder({ page }).withTags(TAGS).analyze()).violations;
}
