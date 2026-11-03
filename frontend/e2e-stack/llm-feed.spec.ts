import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { en } from "../e2e/messages";
import {
  ADMIN_USER,
  FEED_URL,
  compose,
  finishOnboarding,
  insertSource,
  makeDue,
  redis,
  rss,
  serve,
  signInAsAdmin,
  sql,
  sqlNumber,
  userId,
  waitForRuns,
  type Story,
} from "./support";

// The whole M4 path with the real worker, the real database, the real broker and the test provider
// (PHAROS_LLM_PROVIDER=fake in the worker only; see deploy/docker-compose.stack-e2e.yml): an article
// is fetched, read by the filter and the enrichment, appears as a card for the profession it fits,
// "Try this" is made by the worker and shown, a save is stored, and the model page shows the
// spending without any key. The test provider has no quality, so nothing here says how good a model is
// (that is the evaluation, M4-09).
test.describe.configure({ mode: "serial" });
test.setTimeout(300_000);

const NAME = "LLM news";
const WHERE = `name = '${NAME}'`;
const FIRST_TITLE = "AI at work: reconciling ledgers";
const stories: Story[] = [
  { id: "m4-one", title: FIRST_TITLE },
  { id: "m4-two", title: "AI at work: drafting a job advert" },
  { id: "m4-off", title: "OFFTOPIC football results" },
];
const READ = `title LIKE 'AI at work%'`;

// The worker's key in the overlay: a canary shaped like a key, never valid anywhere.
const CANARY = "STACKCANARY-0f3a9c1d7b5e4a2c8d6f";
const WCAG = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

test.beforeAll(() => {
  // Earlier specs leave hundreds of unread articles (a 10,000-entry feed gives 200), and the sweep
  // reads twenty a minute, oldest first. An editor's decision takes an article out of the queue
  // (`reviewed_at`); do that for them so this spec waits for its own articles only.
  sql("UPDATE items SET reviewed_at = now() WHERE enriched_at IS NULL AND reviewed_at IS NULL");
  // The same for the day's spending: what earlier specs' articles cost must not decide whether this
  // spec's insights may be made (insights stop at half of the day's limit).
  redis(`DEL ${counter()} ${counter()}:warned ${counter()}:exhausted`);
  serve("llm.xml", rss(stories));
  finishOnboarding(`subject = '${ADMIN_USER}'`);
  // Earlier specs also left cards (a feed of 200 articles was read by the worker), and the Home
  // feed shows twenty at a time, best first. The administrator marks them "not useful", as a person
  // would, so this spec's own cards are on the first page. Undone in afterAll.
  sql(
    `INSERT INTO interactions (org_id, user_id, item_id, type) ` +
      `SELECT i.org_id, u.id, i.id, 'not_useful' FROM items i, users u ` +
      `WHERE u.subject = '${ADMIN_USER}' AND i.org_id = u.org_id AND i.summary IS NOT NULL ` +
      `AND i.title NOT LIKE 'AI at work%' ON CONFLICT DO NOTHING`,
  );
  insertSource(NAME, `${FEED_URL}/llm.xml`);
});

test("the worker reads the articles: the filter, the enrichment and the profession fit", async () => {
  makeDue(WHERE);
  await waitForRuns(WHERE, 1);
  // The sweep runs every minute; two articles are read, one is judged not relevant.
  await expect
    .poll(() => sqlNumber(`SELECT count(*) FROM items WHERE ${READ} AND summary IS NOT NULL`), {
      timeout: 200_000,
      intervals: [3000],
    })
    .toBe(2);
  // The off-topic article was read (it will not be asked again) but has no summary and no fit.
  expect(
    sqlNumber(
      `SELECT count(*) FROM items WHERE title LIKE 'OFFTOPIC%' AND enriched_at IS NOT NULL AND summary IS NULL`,
    ),
  ).toBe(1);
  expect(
    sqlNumber(
      `SELECT count(*) FROM item_professions WHERE item_id IN (SELECT id FROM items WHERE ${READ})`,
    ),
  ).toBeGreaterThanOrEqual(2);
  expect(
    sqlNumber(
      `SELECT count(*) FROM item_professions WHERE item_id IN (SELECT id FROM items WHERE title LIKE 'OFFTOPIC%')`,
    ),
  ).toBe(0);
});

test("what the model calls cost is booked as numbers, never as text", async () => {
  expect(sqlNumber(`SELECT count(*) FROM llm_usage WHERE outcome = 'ok'`)).toBeGreaterThanOrEqual(
    3,
  );
  // Every stored column is an identifier, a number, a name or a time: nothing can hold an article.
  const columns = sql(
    `SELECT string_agg(column_name, ',' ORDER BY column_name) FROM information_schema.columns WHERE table_name = 'llm_usage'`,
  ).split(",");
  expect(columns).toEqual(
    expect.arrayContaining([
      "stage",
      "provider",
      "model",
      "tokens_in",
      "cost_micro_usd",
      "outcome",
    ]),
  );
  for (const forbidden of ["title", "text", "excerpt", "prompt", "body", "content"]) {
    expect(columns.filter((name) => name.includes(forbidden))).toEqual([]);
  }
});

test("the Home feed shows the two cards, Try this is made by the worker, and a save is stored", async ({
  page,
}) => {
  await signInAsAdmin(page);
  await page.goto("/en");
  const main = page.getByRole("main");
  await expect(main.getByRole("heading", { level: 2, name: en.Cards.results })).toBeVisible();
  for (const story of stories.slice(0, 2)) {
    await expect(main.getByRole("article").filter({ hasText: story.title })).toBeVisible();
  }
  await expect(main.getByText("OFFTOPIC football results")).toHaveCount(0);
  await expect(main.getByText("A demo summary of the article.").first()).toBeVisible();

  const first = FIRST_TITLE;
  const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  expect(results.violations).toEqual([]);

  await page.getByRole("button", { name: `${en.Cards.tryThis}: ${first}`, exact: true }).click();
  // The API queues the task, the worker makes the text, the panel polls until it is ready.
  await expect(page.getByLabel(en.Cards.insight.promptLabel).first()).toContainText(
    "Summarise this in three bullets",
    { timeout: 150_000 },
  );
  await expect(page.getByText(en.Cards.insight.productNote)).toBeVisible();
  expect(sqlNumber(`SELECT count(*) FROM item_insights`)).toBeGreaterThanOrEqual(1);

  await page.getByRole("button", { name: `${en.Cards.save}: ${first}`, exact: true }).click();
  await expect(
    page.getByRole("button", { name: `${en.Cards.saved}: ${first}`, exact: true }),
  ).toBeVisible();
  const me = userId(`subject = '${ADMIN_USER}'`);
  expect(
    sqlNumber(`SELECT count(*) FROM interactions WHERE user_id = '${me}' AND type = 'save'`),
  ).toBe(1);
});

test("the model page shows the spending and never a key", async ({ page }) => {
  await signInAsAdmin(page);
  await page.goto("/en/llm");
  const main = page.getByRole("main");
  await expect(main.getByRole("heading", { level: 1, name: en.Llm.title })).toBeVisible();
  await expect(main.getByText(/ used \(\d+%\)\./)).toBeVisible();
  const results = await new AxeBuilder({ page }).withTags(WCAG).analyze();
  expect(results.violations).toEqual([]);
  await expect(main).not.toContainText(CANARY);
});

// The daily limit (plan criterion 3): at 80% an administrator is warned, at 100% no new generation
// starts and what is already made keeps showing. The counter is moved as an operator would, in Redis;
// the worker's own reserve script decides what happens next.
const CAP = 2_000_000; // the Compose default of PHAROS_LLM_DAILY_BUDGET_MICRO_USD
const today = () => new Date().toISOString().slice(0, 10).replace(/-/g, "");
const counter = () =>
  `pharos:worker:llm:spent:${sql(`SELECT id FROM organizations WHERE slug = '${process.env.STACK_ORG_SLUG ?? "stack"}'`)}:${today()}`;

test("at 80% of the daily limit an administrator is warned", async ({ page }) => {
  redis(`SET ${counter()} ${Math.floor(CAP * 0.85)} EX 86400`);
  // Reading a new article is a call that crosses the line. (An insight could not: insights may use
  // only half of the day, so that people asking for them cannot stop the reading.)
  const mid: Story = { id: "m4-mid", title: "AI at work: a mid story" };
  serve("llm.xml", rss([...stories, mid]));
  makeDue(WHERE);
  await expect
    .poll(() => redis(`EXISTS ${counter()}:warned`), { timeout: 150_000, intervals: [3000] })
    .toBe("1");
  await expect
    .poll(
      () =>
        sqlNumber(
          `SELECT count(*) FROM items WHERE title = '${mid.title}' AND summary IS NOT NULL`,
        ),
      {
        timeout: 150_000,
        intervals: [3000],
      },
    )
    .toBe(1);
  await signInAsAdmin(page);
  await page.goto("/en/llm");
  await expect(page.getByRole("main").getByRole("alert")).toHaveText(en.Llm.budget.warned);
});

test("at 100% no new article is read, and what exists keeps showing", async ({ page }) => {
  redis(`SET ${counter()} ${CAP} EX 86400`);
  const late: Story = { id: "m4-late", title: "AI at work: a late story" };
  serve("llm.xml", rss([...stories, { id: "m4-mid", title: "AI at work: a mid story" }, late]));
  makeDue(WHERE);
  await expect
    .poll(() => sqlNumber(`SELECT count(*) FROM items WHERE title = '${late.title}'`), {
      timeout: 150_000,
      intervals: [3000],
    })
    .toBe(1);
  // The sweep tries it, the reserve refuses, the refusal is the first of the day.
  await expect
    .poll(() => redis(`EXISTS ${counter()}:exhausted`), { timeout: 150_000, intervals: [3000] })
    .toBe("1");
  expect(
    sqlNumber(
      `SELECT count(*) FROM items WHERE title = '${late.title}' AND enriched_at IS NULL AND status = 'new'`,
    ),
  ).toBe(1);

  await signInAsAdmin(page);
  await page.goto("/en/llm");
  await expect(page.getByRole("main").getByRole("alert")).toHaveText(en.Llm.budget.exhausted);

  // The cards made before the limit, and their insight, still show.
  await page.goto("/en");
  const main = page.getByRole("main");
  await expect(main.getByRole("article").filter({ hasText: FIRST_TITLE })).toBeVisible();
  await expect(main.getByRole("article").filter({ hasText: late.title })).toHaveCount(0);
  await page
    .getByRole("button", { name: `${en.Cards.tryThis}: ${FIRST_TITLE}`, exact: true })
    .click();
  await expect(page.getByLabel(en.Cards.insight.promptLabel).first()).toBeVisible({
    timeout: 30_000,
  });
});

test("the key stays in the worker: not in the API, a response, the database or any log", async ({
  page,
}) => {
  // Positive controls: the worker really holds the canary and the API does not (so the searches
  // below look for something that exists, and the API could not leak what it never had).
  expect(compose("exec -T worker printenv PHAROS_LLM_API_KEY").trim()).toBe(CANARY);
  expect(compose(`exec -T api sh -c "printenv PHAROS_LLM_API_KEY || true"`).trim()).toBe("");

  await signInAsAdmin(page);
  for (const path of ["/api/llm/status", "/api/cards", "/api/me/profile"]) {
    const response = await page.request.get(path);
    expect(response.ok(), path).toBe(true);
    expect(await response.text(), path).not.toContain(CANARY);
  }
  for (const table of ["llm_usage", "audit_log", "items", "item_insights", "interactions"]) {
    expect(
      sqlNumber(`SELECT count(*) FROM ${table} t WHERE t::text LIKE '%STACKCANARY%'`),
      table,
    ).toBe(0);
  }
  const logs = compose("logs --no-color");
  expect(logs.length).toBeGreaterThan(1000); // the search covers real logs
  expect(logs).not.toContain(CANARY);
});

test.afterAll(() => {
  // Leave the counter, and the administrator's feed, as the next spec would expect to find them.
  redis(`DEL ${counter()} ${counter()}:warned ${counter()}:exhausted`);
  sql(
    `DELETE FROM interactions WHERE type = 'not_useful' AND user_id IN ` +
      `(SELECT id FROM users WHERE subject = '${ADMIN_USER}') ` +
      `AND item_id IN (SELECT id FROM items WHERE title NOT LIKE 'AI at work%')`,
  );
});
