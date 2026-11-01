import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import { ApiProblem } from "@/lib/api";
import type { Card, CardPage, InteractionKind, Undoable } from "@/lib/cards";

type Role = "admin" | "editor" | "member";
let role: Role = "member";
const listCards = vi.fn<(cursor?: string | null) => Promise<CardPage>>();
const putInteraction = vi.fn<(id: string, kind: InteractionKind, csrf: string) => Promise<void>>();
const deleteInteraction = vi.fn<(id: string, kind: Undoable, csrf: string) => Promise<void>>();

vi.mock("@/components/auth/session-provider", () => ({
  useSession: () => ({
    state: {
      status: "authenticated",
      session: { csrf_token: "token-1", user: { role, display_name: "A" } },
    },
  }),
}));
vi.mock("@/components/cards/insight-panel", () => ({
  InsightPanel: ({ itemId }: { itemId: string }) => <div data-testid="insight">{itemId}</div>,
}));
vi.mock("@/lib/cards", async (original) => ({
  ...(await original<typeof import("@/lib/cards")>()),
  listCards: (cursor?: string | null) => listCards(cursor),
  putInteraction: (id: string, kind: InteractionKind, csrf: string) =>
    putInteraction(id, kind, csrf),
  deleteInteraction: (id: string, kind: Undoable, csrf: string) =>
    deleteInteraction(id, kind, csrf),
}));

const { CardFeed } = await import("./card-feed");

function card(n: number, changes: Partial<Card> = {}): Card {
  return {
    id: `40000000-0000-4000-8000-${String(n).padStart(12, "0")}`,
    url: `https://news.example.org/story-${n}`,
    host: "news.example.org",
    title: `Story ${n}`,
    published_at: "2026-10-30T10:00:00Z",
    summary: `Summary ${n}`,
    category: "tool",
    saved: false,
    quality: null,
    ...changes,
  };
}
const page = (cards: Card[], next: string | null = null): CardPage => ({
  items: cards,
  next_cursor: next,
});

function show() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <CardFeed />
    </NextIntlClientProvider>,
  );
}
async function settle() {
  await act(async () => {
    await Promise.resolve();
  });
}
const button = (action: string, title: string) =>
  screen.getByRole("button", { name: `${action}: ${title}` });

beforeEach(() => {
  role = "member";
  listCards.mockResolvedValue(page([card(1), card(2)]));
  putInteraction.mockResolvedValue(undefined);
  deleteInteraction.mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the list", () => {
  it("shows each card as text with its host, and the link opens safely", async () => {
    show();
    await settle();
    const first = screen.getByRole("article", { name: /Story 1/ });
    expect(within(first).getByText("Summary 1")).toBeDefined();
    expect(within(first).getByText("news.example.org")).toBeDefined();
    const link = within(first).getByRole("link");
    expect(link.getAttribute("href")).toBe("https://news.example.org/story-1");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(screen.queryByText(/Quality:/)).toBeNull();
  });

  it("shows the quality to a reviewer only", async () => {
    role = "editor";
    listCards.mockResolvedValue(page([card(1, { quality: 4 })]));
    show();
    await settle();
    expect(screen.getByText("Quality: 4 of 5")).toBeDefined();
  });

  it("keeps text from a third party as text and an unsafe address unlinked", async () => {
    listCards.mockResolvedValue(
      page([
        card(1, {
          title: "<img src=x onerror=alert(1)> Big news",
          summary: "<script>alert(2)</script><b>bold</b>",
          url: "javascript:alert(1)",
        }),
      ]),
    );
    const { container } = show();
    await settle();
    expect(container.querySelector("img, script, b, a")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)> Big news");
    expect(container.textContent).toContain("<script>alert(2)</script><b>bold</b>");
    expect(screen.getByText(en.Items.unlinked)).toBeDefined();
  });

  it("says when there is nothing, and when it could not load, with a way to try again", async () => {
    listCards.mockResolvedValueOnce(page([]));
    show();
    await settle();
    expect(screen.getByText(en.Cards.empty)).toBeDefined();
    cleanup();
    listCards.mockRejectedValueOnce(new ApiProblem(503, "service_unavailable"));
    show();
    await settle();
    expect(screen.getByRole("alert").textContent).toBe(en.Cards.loadFailed);
    listCards.mockResolvedValue(page([card(1)]));
    fireEvent.click(screen.getByRole("button", { name: en.Cards.retry }));
    await settle();
    expect(screen.getByText("Story 1")).toBeDefined();
  });

  it("shows more with the cursor, lists a card once, and starts again if the place is stale", async () => {
    listCards.mockResolvedValueOnce(page([card(1), card(2)], "next-1"));
    show();
    await settle();
    listCards.mockResolvedValueOnce(page([card(2), card(3)], null)); // 2 again: listed once
    fireEvent.click(screen.getByRole("button", { name: en.Cards.showMore }));
    await settle();
    expect(listCards).toHaveBeenLastCalledWith("next-1");
    expect(screen.getAllByRole("article")).toHaveLength(3);
    expect(screen.queryByRole("button", { name: en.Cards.showMore })).toBeNull(); // no more

    cleanup();
    listCards.mockReset();
    listCards.mockResolvedValueOnce(page([card(1)], "old"));
    show();
    await settle();
    listCards.mockRejectedValueOnce(new ApiProblem(422, "validation_failed"));
    listCards.mockResolvedValueOnce(page([card(9)], null));
    fireEvent.click(screen.getByRole("button", { name: en.Cards.showMore }));
    await settle();
    await settle();
    expect(screen.getByText(en.Cards.refreshed)).toBeDefined();
    expect(screen.getByText("Story 9")).toBeDefined();
  });
});

describe("what the buttons do", () => {
  it("saves and unsaves, with the CSRF token, and says so", async () => {
    show();
    await settle();
    const id = card(1).id;
    fireEvent.click(button(en.Cards.save, "Story 1"));
    await settle();
    expect(putInteraction).toHaveBeenCalledWith(id, "save", "token-1");
    expect(screen.getByText("Saved: Story 1")).toBeDefined();
    fireEvent.click(button(en.Cards.saved, "Story 1"));
    await settle();
    expect(deleteInteraction).toHaveBeenCalledWith(id, "save", "token-1");
    expect(button(en.Cards.save, "Story 1").getAttribute("aria-pressed")).toBe("false");
  });

  it("marks useful and takes it back", async () => {
    show();
    await settle();
    fireEvent.click(button(en.Cards.useful, "Story 1"));
    await settle();
    expect(putInteraction).toHaveBeenCalledWith(card(1).id, "useful", "token-1");
    expect(button(en.Cards.useful, "Story 1").getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(button(en.Cards.useful, "Story 1"));
    await settle();
    expect(deleteInteraction).toHaveBeenCalledWith(card(1).id, "useful", "token-1");
  });

  it("does not show a card as useful when the backend refused", async () => {
    putInteraction.mockRejectedValue(new ApiProblem(503, "service_unavailable"));
    show();
    await settle();
    fireEvent.click(button(en.Cards.useful, "Story 1"));
    await settle();
    expect(button(en.Cards.useful, "Story 1").getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByText(en.Problems.unavailable)).toBeDefined();
  });

  it("hides a card that is not useful, announces it, moves focus to the list, and undoes it in place", async () => {
    show();
    await settle();
    fireEvent.click(button(en.Cards.notUseful, "Story 1"));
    await settle();
    expect(putInteraction).toHaveBeenCalledWith(card(1).id, "not_useful", "token-1");
    expect(screen.queryByText("Story 1")).toBeNull();
    expect(screen.getByText("Hidden: Story 1")).toBeDefined();
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: en.Cards.results }));
    fireEvent.click(screen.getByRole("button", { name: en.Cards.undo }));
    await settle();
    expect(deleteInteraction).toHaveBeenCalledWith(card(1).id, "not_useful", "token-1");
    const titles = screen.getAllByRole("article").map((a) => a.getAttribute("aria-labelledby"));
    expect(titles).toEqual([`card-${card(1).id}`, `card-${card(2).id}`]); // back first
    expect(screen.queryByRole("button", { name: en.Cards.undo })).toBeNull();
  });

  it("hides two cards in a row even while the first request is still on its way", async () => {
    let finishFirst: () => void = () => {};
    putInteraction.mockImplementationOnce(
      () => new Promise<void>((resolve) => (finishFirst = resolve)),
    );
    listCards.mockResolvedValue(page([card(1), card(2), card(3)]));
    show();
    await settle();
    fireEvent.click(button(en.Cards.notUseful, "Story 1")); // waits
    fireEvent.click(button(en.Cards.notUseful, "Story 2")); // answered at once
    await settle();
    expect(screen.queryByText("Story 2")).toBeNull();
    finishFirst();
    await settle();
    // Both stay hidden: the second did not bring the first back from an old copy of the list.
    expect(screen.queryByText("Story 1")).toBeNull();
    expect(screen.queryByText("Story 2")).toBeNull();
    expect(screen.getByText("Story 3")).toBeDefined();
    // And undo puts the last hidden card back where it was in the order.
    fireEvent.click(screen.getByRole("button", { name: en.Cards.undo }));
    await settle();
    const ids = screen.getAllByRole("article").map((a) => a.getAttribute("aria-labelledby"));
    expect(ids).toEqual([`card-${card(1).id}`, `card-${card(3).id}`]);
  });

  it("puts a card back in its own place after a page was added", async () => {
    listCards.mockResolvedValueOnce(page([card(1), card(2)], "next-1"));
    show();
    await settle();
    fireEvent.click(button(en.Cards.notUseful, "Story 1"));
    await settle();
    listCards.mockResolvedValueOnce(page([card(3)], null));
    fireEvent.click(screen.getByRole("button", { name: en.Cards.showMore }));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: en.Cards.undo }));
    await settle();
    const ids = screen.getAllByRole("article").map((a) => a.getAttribute("aria-labelledby"));
    expect(ids).toEqual([card(1), card(2), card(3)].map((c) => `card-${c.id}`));
  });

  it("puts a card from the middle back in the middle", async () => {
    listCards.mockResolvedValue(page([card(1), card(2), card(3)]));
    show();
    await settle();
    fireEvent.click(button(en.Cards.notUseful, "Story 2"));
    await settle();
    fireEvent.click(screen.getByRole("button", { name: en.Cards.undo }));
    await settle();
    const ids = screen.getAllByRole("article").map((a) => a.getAttribute("aria-labelledby"));
    expect(ids).toEqual([card(1), card(2), card(3)].map((c) => `card-${c.id}`));
  });

  it("locks only the card whose step is on its way", async () => {
    let finish: () => void = () => {};
    putInteraction.mockImplementationOnce(() => new Promise<void>((resolve) => (finish = resolve)));
    show();
    await settle();
    fireEvent.click(button(en.Cards.save, "Story 1"));
    await settle();
    expect((button(en.Cards.save, "Story 1") as HTMLButtonElement).disabled).toBe(true);
    expect((button(en.Cards.save, "Story 2") as HTMLButtonElement).disabled).toBe(false);
    finish();
    await settle();
    expect((button(en.Cards.saved, "Story 1") as HTMLButtonElement).disabled).toBe(false);
  });

  it("locks the useful button while its request is on its way", async () => {
    let finish: () => void = () => {};
    putInteraction.mockImplementationOnce(() => new Promise<void>((resolve) => (finish = resolve)));
    show();
    await settle();
    const useful = button(en.Cards.useful, "Story 1") as HTMLButtonElement;
    fireEvent.click(useful);
    await settle();
    expect(useful.disabled).toBe(true);
    finish();
    await settle();
    expect(useful.disabled).toBe(false);
    expect(useful.getAttribute("aria-pressed")).toBe("true");
  });

  it("opens the suggestion only when asked, records a look, and closes it again", async () => {
    show();
    await settle();
    expect(screen.queryByTestId("insight")).toBeNull();
    const open = button(en.Cards.tryThis, "Story 1");
    expect(open.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(open);
    await settle();
    expect(screen.getByTestId("insight").textContent).toBe(card(1).id);
    expect(open.getAttribute("aria-expanded")).toBe("true");
    expect(putInteraction).toHaveBeenCalledWith(card(1).id, "view", "token-1");
    fireEvent.click(open);
    expect(screen.queryByTestId("insight")).toBeNull();
  });

  it("tells a refusal in words and leaves the card where it was", async () => {
    putInteraction.mockRejectedValue(new ApiProblem(429, "rate_limited", 30));
    show();
    await settle();
    fireEvent.click(button(en.Cards.notUseful, "Story 1"));
    await settle();
    expect(screen.getByText("Too many requests. Try again in 30 seconds.")).toBeDefined();
    expect(screen.getByText("Story 1")).toBeDefined();
  });
});
