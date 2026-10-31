import { expect, test, type Browser, type Page } from "@playwright/test";
import { en } from "../e2e/messages";
import {
  FEED_URL,
  finishOnboarding,
  makeDue,
  rss,
  runs,
  serve,
  signInAsAdmin,
  sql,
  sqlNumber,
  userId,
  waitForRuns,
  ADMIN_USER,
} from "./support";

// Personal feeds against the real stack: a person's own feed is fetched by the real worker and seen
// by that person only; addresses that point inwards are refused and audited; and when the person is
// disabled the worker stops fetching their feed.
test.describe.configure({ mode: "serial" });
test.setTimeout(300_000);

const SSO_BUTTON = en.Login.ssoButton.replace("{provider}", "Keycloak (development)");
const ALICE = { user: "alice", password: "dev-only-password", name: "Alice Example" };
const BOB = { user: "bob", password: "dev-only-password", name: "Bob Example" };
const FEED = "Alice reading";

async function signInWithSso(page: Page, person: { user: string; password: string; name: string }) {
  await page.goto("/en/login?next=%2Fen");
  await page.getByRole("link", { name: SSO_BUTTON }).click();
  await page.locator("#username").fill(person.user);
  await page.locator("#password").fill(person.password);
  await page.locator("#kc-login").click();
  await expect(page.getByRole("group", { name: en.Shell.account })).toContainText(person.name);
}

async function personPage(browser: Browser, person: typeof ALICE) {
  const context = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await context.newPage();
  await signInWithSso(page, person);
  finishOnboarding(`display_name = '${person.name}'`);
  return page;
}

const aliceFeeds = () =>
  `owner_user_id = (SELECT id FROM users WHERE display_name = '${ALICE.name}')`;

test.beforeAll(() => {
  serve("alice.xml", rss([{ id: "a1", title: "Alice private story" }]));
  finishOnboarding(`subject = '${ADMIN_USER}'`);
});

test("a person adds a feed of their own and the worker fetches it", async ({ browser }) => {
  const page = await personPage(browser, ALICE);
  await page.goto("/en/my-feeds");
  await page.getByRole("button", { name: en.Feeds.actions.add }).click();
  await page.getByLabel(en.Sources.form.name).fill(FEED);
  await page.getByLabel(en.Sources.form.url).fill(`${FEED_URL}/alice.xml`);
  await page.getByRole("button", { name: en.Sources.form.save }).click();
  await expect(page.getByRole("heading", { level: 3, name: FEED })).toBeVisible();

  await waitForRuns(aliceFeeds(), 1);
  await page.reload();
  const card = page.getByRole("article").filter({ hasText: FEED });
  await expect(card).toContainText(en.Sources.health.ok);
  await card.getByText(en.Feeds.articles.title, { exact: true }).click();
  await expect(card.getByText("Alice private story")).toBeVisible();
});

test("nobody else sees the feed or its articles: not another person, not an administrator", async ({
  browser,
  page,
}) => {
  const bob = await personPage(browser, BOB);
  await bob.goto("/en/my-feeds");
  await expect(bob.getByText(en.Feeds.empty)).toBeVisible();
  await bob.goto("/en/items");
  await expect(bob.getByText("Alice private story")).toHaveCount(0);
  const id = sql(`SELECT id FROM sources WHERE ${aliceFeeds()} LIMIT 1`);
  expect((await bob.request.get(`/api/me/feeds/${id}`)).status()).toBe(404);
  expect((await bob.request.get(`/api/me/feeds/${id}/items`)).status()).toBe(404);

  await signInAsAdmin(page);
  await page.goto("/en/items");
  await expect(page.getByText("Alice private story")).toHaveCount(0);
  await page.getByRole("button", { name: en.Items.filter.held }).click();
  await expect(page.getByText("Alice private story")).toHaveCount(0);
  await page.goto("/en/sources");
  await expect(page.getByText(FEED, { exact: true })).toHaveCount(0);
  expect((await page.request.get(`/api/me/feeds/${id}`)).status()).toBe(404);
  const own = await page.request.get("/api/me/feeds");
  expect(own.status()).toBe(200);
  // the administrator may have feeds of their own (the accessibility checks seed one); never Alice's
  const listed = (await own.json()) as { items: { id: string }[] };
  expect(listed.items.map((feed) => feed.id)).not.toContain(id);
});

test("addresses that point inwards are refused and the attempt is audited", async ({ browser }) => {
  const page = await personPage(browser, ALICE);
  await page.goto("/en/my-feeds");
  const before = sqlNumber(
    `SELECT count(*) FROM audit_logs WHERE action = 'source.created' AND outcome = 'failure' AND details->>'reason' = 'url_not_allowed'`,
  );
  for (const address of ["http://127.1/", "http://[::ffff:7f00:1]/"]) {
    await page.getByRole("button", { name: en.Feeds.actions.add }).click();
    await page.getByLabel(en.Sources.form.name).fill("Inward");
    await page.getByLabel(en.Sources.form.url).fill(address);
    await page.getByRole("button", { name: en.Sources.form.save }).click();
    await expect(page.getByLabel(en.Sources.form.url)).toHaveAttribute("aria-invalid", "true");
    await page.getByRole("button", { name: en.Sources.form.cancel }).click();
  }
  expect(
    sqlNumber(
      `SELECT count(*) FROM audit_logs WHERE action = 'source.created' AND outcome = 'failure' AND details->>'reason' = 'url_not_allowed'`,
    ),
  ).toBe(before + 2);
  // the trace says what happened and to whom, never where
  expect(
    sqlNumber(
      `SELECT count(*) FROM audit_logs WHERE details::text LIKE '%127%' OR details::text LIKE '%7f00%'`,
    ),
  ).toBe(0);
  expect(sqlNumber(`SELECT count(*) FROM sources WHERE name = 'Inward'`)).toBe(0);
});

test("when the person is disabled the worker stops fetching their feed, and resumes after", async () => {
  const before = runs(aliceFeeds());
  expect(before).toBeGreaterThanOrEqual(1);
  sql(
    `UPDATE users SET status = 'disabled' WHERE id = '${userId(`display_name = '${ALICE.name}'`)}'`,
  );
  makeDue(aliceFeeds());
  // two dispatch rounds pass (it runs every minute) and the feed is still not fetched
  await new Promise((resolve) => setTimeout(resolve, 130_000));
  expect(runs(aliceFeeds())).toBe(before);

  sql(
    `UPDATE users SET status = 'active' WHERE id = '${userId(`display_name = '${ALICE.name}'`)}'`,
  );
  makeDue(aliceFeeds());
  await waitForRuns(aliceFeeds(), before + 1);
});
