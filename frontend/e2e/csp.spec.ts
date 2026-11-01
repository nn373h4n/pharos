import { expect, test, type Page } from "@playwright/test";
import { mockApi } from "./api-mock";

// The Content-Security-Policy (F-03, ADR-0019): every page carries one with a nonce made for the
// request, the scripts of the page carry that nonce, and the browser reports no violation while
// the page loads and works.

/** Collect what the browser reports as a policy violation, from the page itself. */
async function watchViolations(page: Page): Promise<string[]> {
  const found: string[] = [];
  await page.exposeFunction("reportViolation", (text: string) => found.push(text));
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (event) => {
      (window as unknown as { reportViolation: (t: string) => void }).reportViolation(
        `${event.violatedDirective} ${event.blockedURI}`,
      );
    });
  });
  page.on("console", (message) => {
    if (message.text().includes("Content Security Policy")) found.push(message.text());
  });
  return found;
}

for (const path of ["/en/login", "/en", "/az/items", "/tr/onboarding", "/ru/sources"]) {
  test(`${path} carries a policy with its own nonce and breaks none of it`, async ({ page }) => {
    await mockApi(page, { signedIn: true, role: "admin" });
    const violations = await watchViolations(page);
    const response = await page.goto(path);
    const policy = response?.headers()["content-security-policy"] ?? "";
    const nonce = /'nonce-([^']+)'/.exec(policy)?.[1];
    expect(nonce, policy).toBeTruthy();
    expect(policy).toContain("object-src 'none'");
    expect(policy).toContain("frame-ancestors 'none'");
    expect(policy).not.toMatch(/script-src[^;]*'unsafe-inline'/);
    // Every script the server wrote has that nonce (a script without it would not run).
    const scripts = await page.evaluate(
      () => [...document.querySelectorAll("script")].map((s) => s.nonce), // the attribute itself is hidden
    );
    expect(scripts.length).toBeGreaterThan(0);
    for (const found of scripts) expect(found).toBe(nonce);
    await page.waitForLoadState("networkidle");
    expect(violations).toEqual([]);
  });
}

// A path with a dot in it is not a page of the app. The proxy answers it itself, as plain text and
// with the policy, instead of letting Next send a prebuilt 404 page whose scripts have no nonce.
for (const path of ["/en/nope.txt", "/en/items/a.b", "/ru/x.y.z", "/a.js", "/en/%2e%2e/x.y"]) {
  test(`${path} is a plain 404 under the policy`, async ({ request }) => {
    const response = await request.get(path);
    expect(response.status()).toBe(404);
    expect(response.headers()["content-security-policy"]).toContain("object-src 'none'");
    expect(response.headers()["content-type"]).toContain("text/plain");
    expect(await response.text()).not.toContain("<script");
  });
}

test("each request gets a nonce of its own", async ({ request }) => {
  const nonce = async () =>
    /'nonce-([^']+)'/.exec(
      (await request.get("/en/login")).headers()["content-security-policy"] ?? "",
    )?.[1];
  const first = await nonce();
  const second = await nonce();
  expect(first).toBeTruthy();
  expect(first).not.toBe(second);
});

test("the page still works under the policy: it hydrates and reacts", async ({ page }) => {
  await mockApi(page, { signedIn: false, localLogin: "on" });
  const violations = await watchViolations(page);
  await page.goto("/en/login");
  // Hydration happened if a client component reacts: the language switcher is a link, the form is
  // client-side. Typing into the form and seeing its state is enough.
  const user = page.getByLabel(/username/i);
  await user.fill("someone");
  await expect(user).toHaveValue("someone");
  expect(violations).toEqual([]);
});
