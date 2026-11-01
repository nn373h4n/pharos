import type { Request, Route } from "@playwright/test";

// The cards part of the mocked API (docs/API.md, "Cards" and "Insights"): a ranked list in pages of
// twenty with a cursor, the suggestion of a card (ready, made after a wait, or unavailable) and
// the interactions, which are remembered here so a test can look at them.

export type MockCard = {
  id: string;
  url: string;
  host: string;
  title: string;
  published_at: string | null;
  summary: string;
  category: string;
  saved: boolean;
  quality: number | null;
};

export type CardsOptions = {
  cards?: MockCard[];
  /** The list fails: "down" answers 503. */
  cardsFail?: "down";
  /** How the suggestion goes: ready at once, ready after two waits, or not available. */
  insight?: "ready" | "slow" | "off" | "rejected" | "hostile" | "never";
};

export type CardsState = {
  cards: MockCard[];
  /** "kind item" for every interaction the page sent, and the kinds taken back. */
  interactions: string[];
  insightReads: number;
};

export const CARD_PAGE = 20;
const CURSOR = "e2e-cursor";

export function card(n: number, changes: Partial<MockCard> = {}): MockCard {
  return {
    id: `50000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    url: `https://news.example.org/card-${n}`,
    host: "news.example.org",
    title: `Card ${n}`,
    published_at: `2026-10-${String(Math.max(1, 28 - n)).padStart(2, "0")}T10:00:00Z`,
    summary: `How card ${n} helps your work.`,
    category: ["tool", "technique", "tutorial", "news"][n % 4]!,
    saved: false,
    quality: 4,
    ...changes,
  };
}

export function defaultCards(): MockCard[] {
  return Array.from({ length: 25 }, (_, i) => card(i + 1));
}

const READY = {
  profession: "accountant",
  language: "en",
  summary: "It shortens the month-end close.",
  how_to_apply: ["Open the ledger extract.", "Ask the assistant for unusual entries."],
  try_prompt: "List unusual entries in [LEDGER EXTRACT] and explain why each looks unusual.",
  safety_note: "Do not paste real client data; replace it with placeholders.",
};

const HOSTILE = {
  ...READY,
  summary: "<img src=x onerror=alert(1)> " + "very-long-unbroken-text-".repeat(12),
  how_to_apply: ["<script>alert(2)</script>", "[click](http://evil.example)"],
  try_prompt: "<b>bold</b> " + "x".repeat(300) + " [TEXT]",
  safety_note: "<a href='http://evil.example'>go</a>",
};

type Problem = (
  status: number,
  code: string,
  headers?: Record<string, string>,
) => Parameters<Route["fulfill"]>[0];

export async function handleCards(
  route: Route,
  request: Request,
  path: string,
  state: CardsState,
  options: CardsOptions,
  problem: Problem,
): Promise<boolean> {
  const method = request.method();
  if (path === "/api/cards" && method === "GET") {
    if (options.cardsFail === "down") {
      await route.fulfill(problem(503, "service_unavailable"));
      return true;
    }
    const url = new URL(request.url());
    const cursor = url.searchParams.get("cursor");
    if (cursor !== null && cursor !== CURSOR) {
      await route.fulfill(problem(422, "validation_failed"));
      return true;
    }
    const start = cursor ? CARD_PAGE : 0;
    const items = state.cards.slice(start, start + CARD_PAGE);
    await route.fulfill({
      json: {
        items,
        next_cursor: !cursor && state.cards.length > CARD_PAGE ? CURSOR : null,
      },
    });
    return true;
  }
  const insight = /^\/api\/items\/([^/]+)\/insight$/.exec(path);
  if (insight && method === "GET") {
    state.insightReads += 1;
    const mode = options.insight ?? "ready";
    const body =
      mode === "ready"
        ? { status: "ready", reason: null, insight: READY }
        : mode === "hostile"
          ? { status: "ready", reason: null, insight: HOSTILE }
          : mode === "slow"
            ? state.insightReads < 3
              ? { status: "pending", reason: null, insight: null }
              : { status: "ready", reason: null, insight: READY }
            : mode === "never"
              ? { status: "pending", reason: null, insight: null }
              : { status: "unavailable", reason: mode, insight: null };
    await route.fulfill({ json: body });
    return true;
  }
  const interaction = /^\/api\/items\/([^/]+)\/interactions\/([a-z_]+)$/.exec(path);
  if (interaction && (method === "PUT" || method === "DELETE")) {
    if (request.headers()["x-csrf-token"] === undefined) {
      await route.fulfill(problem(403, "csrf_failed"));
      return true;
    }
    const id = interaction[1]!;
    const kind = interaction[2]!;
    state.interactions.push(`${method} ${kind} ${id}`);
    const found = state.cards.find((c) => c.id === id);
    if (found && kind === "save") found.saved = method === "PUT";
    await route.fulfill({ status: 204 });
    return true;
  }
  return false;
}
