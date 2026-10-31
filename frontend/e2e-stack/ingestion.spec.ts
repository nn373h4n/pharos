import { expect, test } from "@playwright/test";
import { en } from "../e2e/messages";
import {
  FEED_URL,
  STACK,
  finishOnboarding,
  insertSource,
  lastOutcome,
  makeDue,
  rss,
  serve,
  signInAsAdmin,
  sql,
  sqlNumber,
  waitForRuns,
  ADMIN_USER,
  type Story,
} from "./support";

// The whole path of an article, with the real worker, the real database and a fake feed host that
// only the worker can reach (deploy/docker-compose.stack-e2e.yml): an administrator adds a source,
// the worker fetches it, the articles appear; fetching again adds nothing; a changed entry replaces
// its copy; and the places the worker must never reach are blocked.
//
// Not repeated here, because the integration tests already prove them with a controlled clock:
// a worker killed in the middle of a fetch leaves a lease that runs out
// (test_ingest_scheduler.py::test_a_worker_that_dies_leaves_a_lease_that_runs_out_and_the_source_is_fetched_again).
test.describe.configure({ mode: "serial" });
test.setTimeout(240_000);

const NAME = "Fake news";
const WHERE = `name = '${NAME}'`;
const stories: Story[] = [
  { id: "one", title: "Story one" },
  { id: "two", title: "Story two" },
  { id: "three", title: "Story three" },
];

const itemsOfSource = () =>
  sqlNumber(
    `SELECT count(*) FROM item_sources WHERE source_id IN (SELECT id FROM sources WHERE ${WHERE})`,
  );

test.beforeAll(() => {
  serve("news.xml", rss(stories));
  finishOnboarding(`subject = '${ADMIN_USER}'`);
});

test("an administrator adds a source, the worker fetches it and the articles appear", async ({
  page,
}) => {
  await signInAsAdmin(page);
  await page.goto("/en/sources");
  await page.getByRole("button", { name: en.Sources.actions.add }).click();
  await page.getByLabel(en.Sources.form.name).fill(NAME);
  await page.getByLabel(en.Sources.form.url).fill(`${FEED_URL}/news.xml`);
  await page.getByLabel(en.Sources.form.enabledNow).check();
  await page.getByRole("button", { name: en.Sources.form.save }).click();
  await expect(page.getByRole("heading", { level: 3, name: NAME })).toBeVisible();

  await waitForRuns(WHERE, 1);
  expect(lastOutcome(WHERE)).toBe("ok:");
  expect(itemsOfSource()).toBe(3);

  await page.goto("/en/items");
  for (const story of stories) {
    await expect(page.getByRole("heading", { level: 3, name: story.title })).toBeVisible();
  }
  await page.goto("/en/sources");
  const card = page.getByRole("article").filter({ hasText: NAME });
  await expect(card).toContainText(en.Sources.health.ok);
});

test("fetching the same feed again adds nothing", async () => {
  makeDue(WHERE);
  await waitForRuns(WHERE, 2);
  // The fake host sends Last-Modified, so the worker may be told "not modified": both add nothing.
  expect(["ok:", "not_modified:"]).toContain(lastOutcome(WHERE));
  expect(itemsOfSource()).toBe(3);
  expect(
    sqlNumber(
      `SELECT items_new FROM fetch_runs WHERE source_id IN (SELECT id FROM sources WHERE ${WHERE}) ORDER BY id DESC LIMIT 1`,
    ),
  ).toBe(0);
});

test("a changed entry replaces its copy instead of adding another", async ({ page }) => {
  serve("news.xml", rss([{ id: "one", title: "Story one, corrected" }, ...stories.slice(1)]));
  makeDue(WHERE);
  await waitForRuns(WHERE, 3);
  expect(itemsOfSource()).toBe(3);
  expect(sqlNumber(`SELECT count(*) FROM items WHERE title = 'Story one, corrected'`)).toBe(1);
  expect(sqlNumber(`SELECT count(*) FROM items WHERE title = 'Story one'`)).toBe(0);
  await signInAsAdmin(page);
  await page.goto("/en/items");
  await expect(page.getByRole("heading", { level: 3, name: "Story one, corrected" })).toBeVisible();
});

test("hostile text from a feed stays text and a link to a machine is held", async ({ page }) => {
  serve(
    "hostile.xml",
    rss([
      { id: "x1", title: "<img src=x onerror=alert(1)> Hostile title", url: "javascript:alert(1)" },
      { id: "x2", title: "Points at a machine", url: "http://203.0.113.9/story" },
      { id: "x3", title: "Ordinary story" },
    ]),
  );
  insertSource("Hostile feed", `${FEED_URL}/hostile.xml`);
  await waitForRuns("name = 'Hostile feed'", 1);
  expect(lastOutcome("name = 'Hostile feed'")).toBe("ok:");
  // the address that is not a web address never became an article
  expect(sqlNumber(`SELECT count(*) FROM items WHERE canonical_url LIKE 'javascript:%'`)).toBe(0);

  const dialogs: string[] = [];
  page.on("dialog", (dialog) => {
    dialogs.push(dialog.message());
    void dialog.dismiss();
  });
  await signInAsAdmin(page);
  await page.goto("/en/items");
  await expect(page.getByRole("heading", { level: 3, name: "Ordinary story" })).toBeVisible();
  await expect(page.locator("main img")).toHaveCount(0);
  await page.getByRole("button", { name: en.Items.filter.held }).click();
  await expect(page.getByRole("heading", { level: 3, name: /Points at a machine/ })).toBeVisible();
  await expect(page.getByText(en.Items.reasons.link_ip_host)).toBeVisible();
  expect(dialogs).toEqual([]);
});

test("a feed of ten thousand entries is cut to what is allowed, quickly", async () => {
  const many: Story[] = Array.from({ length: 10_000 }, (_, n) => ({
    id: `m${n}`,
    title: `Many ${n}`,
    summary: "",
    url: `https://news.example.org/m/${n}`,
  }));
  serve("many.xml", rss(many));
  insertSource("Many entries", `${FEED_URL}/many.xml`);
  await waitForRuns("name = 'Many entries'", 1);
  expect(lastOutcome("name = 'Many entries'")).toBe("ok:");
  const seen = sqlNumber(
    `SELECT items_seen FROM fetch_runs WHERE source_id IN (SELECT id FROM sources WHERE name = 'Many entries') ORDER BY id DESC LIMIT 1`,
  );
  expect(seen).toBeGreaterThan(0);
  expect(seen).toBeLessThanOrEqual(200);
  expect(
    sqlNumber(
      `SELECT count(*) FROM item_sources WHERE source_id IN (SELECT id FROM sources WHERE name = 'Many entries')`,
    ),
  ).toBe(200);
  expect(
    sqlNumber(
      `SELECT duration_ms FROM fetch_runs WHERE source_id IN (SELECT id FROM sources WHERE name = 'Many entries') ORDER BY id DESC LIMIT 1`,
    ),
  ).toBeLessThan(20_000);
});

test("the worker never reaches the places inside the stack, and says so", async () => {
  const targets: [string, string][] = [
    ["Blocked redis", "http://redis:6379/"],
    ["Blocked postgres", "http://postgres:5432/"],
    ["Blocked api", "http://api:8000/api/healthz"],
    ["Blocked web", "http://web:3000/"],
    ["Blocked metadata", "http://169.254.169.254/latest/meta-data/"],
    ["Blocked loopback", "http://127.0.0.1/"],
    // the address people open this installation at
    ["Blocked public url", `${STACK}/api/healthz`],
    // the fake host itself, on a port the worker was not given
    ["Blocked other port", "http://feed.example.test:9090/news.xml"],
    // On the one port the worker may use: only the address policy stops these, not the port rule.
    ["Blocked redis address", "http://redis:8080/"],
    ["Blocked postgres address", "http://postgres:8080/"],
    ["Blocked api address", "http://api:8080/"],
    ["Blocked web address", "http://web:8080/"],
  ];
  for (const [name, url] of targets) insertSource(name, url);
  for (const [name] of targets) {
    await waitForRuns(`name = '${name}'`, 1);
    expect(lastOutcome(`name = '${name}'`), name).toMatch(/^blocked:/);
  }
  // nothing was fetched from any of them
  expect(
    sqlNumber(
      `SELECT count(*) FROM raw_items WHERE source_id IN (SELECT id FROM sources WHERE name LIKE 'Blocked %')`,
    ),
  ).toBe(0);
  expect(sql(`SELECT count(*) FROM items WHERE title LIKE 'Blocked %'`)).toBe("0");
});
