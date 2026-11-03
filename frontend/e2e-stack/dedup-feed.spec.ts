import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { en } from "../e2e/messages";
import {
  ADMIN_USER,
  FEED_URL,
  finishOnboarding,
  insertSource,
  makeDue,
  rss,
  serve,
  signInAsAdmin,
  sql,
  sqlNumber,
  waitForRuns,
  type Story,
} from "./support";

// Near-duplicates in the real stack (M4-05, ADR-0007, ADR-0021): the worker reads the articles,
// embeds them with the test provider (only an identical set of words counts as one story, set in
// deploy/docker-compose.stack-e2e.yml), joins a syndicated copy to the cluster of the original, and
// the Home feed shows one card for the story. Nothing is lost: the copy is still an article that
// editors can see. The test provider hashes words; it says nothing about meaning or any language.
test.describe.configure({ mode: "serial" });
test.setTimeout(300_000);

const NAME = "Syndication";
const WHERE = `name = '${NAME}'`;
const ORIGINAL = "Ledger reconciliation workflow tutorial for accountants quarterly";
const COPY = "QUARTERLY ACCOUNTANTS FOR TUTORIAL WORKFLOW RECONCILIATION LEDGER";
const OTHER_ONE = "Onboarding checklist template for new finance hires";
const OTHER_TWO = "Cloud cost dashboard walkthrough for controllers";
const stories: Story[] = [
  { id: "syn-original", title: ORIGINAL },
  { id: "syn-copy", title: COPY },
  { id: "syn-one", title: OTHER_ONE },
  { id: "syn-two", title: OTHER_TWO },
];
const MINE = `title IN ('${ORIGINAL}', '${COPY}', '${OTHER_ONE}', '${OTHER_TWO}')`;

test.beforeAll(() => {
  serve("syndication.xml", rss(stories));
  finishOnboarding(`subject = '${ADMIN_USER}'`);
  insertSource(NAME, `${FEED_URL}/syndication.xml`);
  makeDue(WHERE);
});

test("the worker reads four articles, embeds them and joins the copy to the original", async () => {
  await waitForRuns(WHERE, 1);
  await expect
    .poll(
      () =>
        sqlNumber(
          `SELECT count(*) FROM item_embeddings e JOIN items i ON i.id = e.item_id WHERE ${MINE.replaceAll("title", "i.title")}`,
        ),
      { timeout: 280_000, intervals: [3000] },
    )
    .toBe(4);
  // The copy and the original share a cluster; the two other stories have none.
  const clusters = sql(
    `SELECT title || '|' || coalesce(cluster_id::text, '-') FROM items WHERE ${MINE} ORDER BY title`,
  ).split("\n");
  const byTitle = new Map(clusters.map((line) => line.split("|") as [string, string]));
  const original = byTitle.get(ORIGINAL);
  expect(original).toBeDefined();
  expect(original).not.toBe("-");
  expect(byTitle.get(COPY)).toBe(original);
  expect(byTitle.get(OTHER_ONE)).toBe("-");
  expect(byTitle.get(OTHER_TWO)).toBe("-");
  // The vectors are numbers of one size, all for the model the worker was told to use.
  expect(
    sql(
      `SELECT DISTINCT model || ':' || dim FROM item_embeddings e JOIN items i ON i.id = e.item_id WHERE ${MINE.replaceAll("title", "i.title")}`,
    ),
  ).toBe("stack-embedding:64");
});

test("the Home feed shows one card for the story, and no article is lost", async ({ page }) => {
  await signInAsAdmin(page);
  await page.goto("/en");
  const main = page.getByRole("main");
  await expect(main.getByRole("heading", { level: 2, name: en.Cards.results })).toBeVisible();
  for (const title of [OTHER_ONE, OTHER_TWO]) {
    await expect(main.getByRole("article").filter({ hasText: title })).toBeVisible();
  }
  // One of the two copies is the card (the better one; equal here, so the one seen first).
  await expect(
    main.getByRole("article").filter({ hasText: /ledger reconciliation|LEDGER/i }),
  ).toHaveCount(1);
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(results.violations).toEqual([]);

  // Positive control: both articles are still there for the people who review them.
  await page.goto("/en/items");
  const list = page.getByRole("main");
  await expect(list.getByRole("heading", { level: 3, name: ORIGINAL })).toBeVisible();
  await expect(list.getByRole("heading", { level: 3, name: COPY })).toBeVisible();
});

test("the model page counts the embedding calls as numbers", async ({ page }) => {
  expect(
    sqlNumber(`SELECT count(*) FROM llm_usage WHERE stage = 'embedding'`),
  ).toBeGreaterThanOrEqual(4);
  expect(
    sqlNumber(
      `SELECT count(*) FROM llm_usage WHERE stage = 'embedding' AND (tokens_out <> 0 OR outcome <> 'ok')`,
    ),
  ).toBe(0);
  await signInAsAdmin(page);
  await page.goto("/en/llm");
  await expect(page.getByRole("main").getByText(en.Llm.stage.embedding).first()).toBeVisible();
});
