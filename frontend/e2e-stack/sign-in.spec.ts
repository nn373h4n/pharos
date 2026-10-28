import { execSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";
import { en } from "../e2e/messages";

// These tests run against the real stack started by scripts/stack-e2e.sh: Caddy over HTTPS, the web
// app, the API, PostgreSQL, Redis and a development Keycloak with two fictional people.
const STACK = process.env.STACK_URL ?? "";
const ADMIN_USER = process.env.STACK_ADMIN_USER ?? "";
const ADMIN_PASSWORD = process.env.STACK_ADMIN_PASSWORD ?? "";
const PSQL = process.env.STACK_PSQL ?? "";

const SSO_BUTTON = en.Login.ssoButton.replace("{provider}", "Keycloak (development)");
const ALICE = { user: "alice", password: "dev-only-password", name: "Alice Example" };
const BOB = { user: "bob", password: "dev-only-password", email: "bob@example.test" };

/** Runs SQL as an operator would (psql inside the stack's database container). */
function sql(statement: string): string {
  return execSync(PSQL, { input: statement, encoding: "utf8" }).trim();
}

async function signInAtKeycloak(page: Page, person: { user: string; password: string }) {
  await page.locator("#username").fill(person.user);
  await page.locator("#password").fill(person.password);
  await page.locator("#kc-login").click();
}

async function startSso(page: Page, next = "/en") {
  await page.goto(`/en/login?next=${encodeURIComponent(next)}`);
  await page.getByRole("link", { name: SSO_BUTTON }).click();
}

const account = (page: Page) => page.getByRole("group", { name: en.Shell.account });

test.describe("single sign-on through Keycloak", () => {
  test("an anonymous visitor is sent to the sign-in page", async ({ page }) => {
    await page.goto("/en");
    await expect(page).toHaveURL(/\/en\/login/);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText(en.Login.title);
    await expect(page.getByRole("link", { name: SSO_BUTTON })).toBeVisible();
  });

  test("signing in at the provider lands in the app with the right person", async ({ page }) => {
    await startSso(page);
    await expect(page).toHaveURL(/auth\.localhost/);
    await signInAtKeycloak(page, ALICE);
    await expect(page).toHaveURL(new RegExp(`^${STACK}/en`));
    await expect(account(page)).toContainText(ALICE.name);
    await expect(account(page)).toContainText(en.Shell.roleMember);
  });

  test("the session cookie cannot be read by scripts and is bound to the site", async ({
    page,
    context,
  }) => {
    await startSso(page);
    await signInAtKeycloak(page, ALICE);
    await expect(account(page)).toBeVisible();
    const cookies = await context.cookies(STACK);
    const session = cookies.find((cookie) => cookie.name === "__Host-pharos_session");
    expect(session, "the session cookie").toBeDefined();
    expect(session).toMatchObject({ httpOnly: true, secure: true, sameSite: "Lax", path: "/" });
    expect(await page.evaluate(() => document.cookie)).not.toContain("pharos_session");
    // The one-time cookie of the sign-in flow is gone again.
    expect(cookies.find((cookie) => cookie.name === "__Host-pharos_oidc")).toBeUndefined();
  });

  test("signing out ends the provider's session too", async ({ page }) => {
    await startSso(page);
    await signInAtKeycloak(page, ALICE);
    await account(page).getByRole("button", { name: en.Shell.signOut }).click();
    // Pharos does not keep ID tokens, so it cannot send an `id_token_hint`; Keycloak then asks
    // the person to confirm before it ends its own session and sends them back.
    await expect(page).toHaveURL(/auth\.localhost.*\/logout/);
    await page.locator("#kc-logout").click();
    await expect(page).toHaveURL(/\/login/);
    await expect(page.getByRole("link", { name: SSO_BUTTON })).toBeVisible();
    // Had the provider's session survived, this would sign straight back in.
    await page.getByRole("link", { name: SSO_BUTTON }).click();
    await expect(page.locator("#kc-login")).toBeVisible();
    // And the app itself no longer knows the person.
    await page.goto("/en");
    await expect(page).toHaveURL(/auth\.localhost|\/en\/login/);
  });

  test("a second person signs in as themselves, not as the first", async ({ page }) => {
    await startSso(page);
    await signInAtKeycloak(page, BOB);
    await expect(account(page)).toContainText("Bob");
    await expect(account(page)).not.toContainText(ALICE.name);
  });

  test("an account that is disabled loses its session and cannot come back", async ({ page }) => {
    await startSso(page);
    await signInAtKeycloak(page, BOB);
    await expect(account(page)).toBeVisible();
    try {
      sql(`UPDATE users SET status = 'disabled' WHERE email = '${BOB.email}'`);
      await page.reload();
      await expect(page).toHaveURL(/\/en\/login/); // the next request already refused the session
      await page.getByRole("link", { name: SSO_BUTTON }).click();
      // Keycloak still knows Bob, so this signs in there and comes back refused.
      await expect(page).toHaveURL(/\/en\/login\?error=account_disabled/);
      await expect(page.locator("main").getByRole("alert")).toHaveText(
        en.LoginErrors.account_disabled,
      );
    } finally {
      sql(`UPDATE users SET status = 'active' WHERE email = '${BOB.email}'`);
    }
  });
});

test.describe("the break-glass administrator", () => {
  test("can sign in with a password and sign out", async ({ page }) => {
    await page.goto("/en/login");
    await page.getByLabel(en.Login.username).fill(ADMIN_USER);
    await page.getByLabel(en.Login.password, { exact: true }).fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: en.Login.submit }).click();
    await expect(account(page)).toContainText(en.Shell.roleAdmin);
    await account(page).getByRole("button", { name: en.Shell.signOut }).click();
    await expect(page).toHaveURL(/\/login/);
    await page.goto("/en");
    await expect(page).toHaveURL(/\/en\/login/);
  });

  test("a wrong password is refused with a message and no session", async ({ page, context }) => {
    await page.goto("/en/login");
    await page.getByLabel(en.Login.username).fill(ADMIN_USER);
    await page.getByLabel(en.Login.password, { exact: true }).fill("not the password");
    await page.getByRole("button", { name: en.Login.submit }).click();
    await expect(page.locator("main").getByRole("alert")).toHaveText(
      en.LoginErrors.invalid_credentials,
    );
    const cookies = await context.cookies(STACK);
    expect(cookies.find((cookie) => cookie.name === "__Host-pharos_session")).toBeUndefined();
  });

  test("an ordinary person cannot use the password form (break-glass is for administrators)", async ({
    page,
  }) => {
    // Alice exists only at the provider and has no password here; the answer is the same
    // as for a wrong password, so the form does not reveal which accounts exist.
    await page.goto("/en/login");
    await page.getByLabel(en.Login.username).fill(ALICE.user);
    await page.getByLabel(en.Login.password, { exact: true }).fill(ALICE.password);
    await page.getByRole("button", { name: en.Login.submit }).click();
    await expect(page.locator("main").getByRole("alert")).toHaveText(
      en.LoginErrors.invalid_credentials,
    );
  });
});

test.describe("what the operator can see afterwards", () => {
  test("sign-ins and refusals are in the audit log, without secrets", async () => {
    const rows = sql(
      "SELECT action || '|' || outcome || '|' || coalesce(details::text, '') FROM audit_logs ORDER BY id",
    );
    expect(rows).toContain("auth.login|success|");
    expect(rows).toContain("auth.login|failure|");
    expect(rows).toContain("auth.logout|success|");
    expect(rows).toContain("user.created|success|");
    for (const secret of [
      ADMIN_PASSWORD,
      ALICE.password,
      "dev-only-client-secret",
      "not the password",
    ]) {
      expect(rows).not.toContain(secret);
    }
  });

  test("people created by single sign-on are plain members", async () => {
    const roles = sql("SELECT role FROM users WHERE issuer LIKE 'https://%' ORDER BY subject");
    expect(roles.split("\n").every((role) => role === "member")).toBe(true);
    expect(roles.length).toBeGreaterThan(0);
  });
});
