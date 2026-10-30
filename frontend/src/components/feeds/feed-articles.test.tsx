import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import type { Feed, FeedItem, FeedItemPage } from "@/lib/feeds";

const listFeedItems = vi.fn<(id: string, offset?: number) => Promise<FeedItemPage>>();
vi.mock("@/lib/feeds", async (original) => ({
  ...(await original<typeof import("@/lib/feeds")>()),
  listFeedItems: (id: string, offset?: number) => listFeedItems(id, offset),
}));

const { FeedArticles } = await import("./feed-articles");

const feed = { id: "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b", name: "F" } as Feed;
const item = (n: number, changes: Partial<FeedItem> = {}): FeedItem => ({
  id: `6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a${String(n).padStart(2, "0")}`,
  url: `https://news.example.org/${n}`,
  title: `Story ${n}`,
  excerpt: `Excerpt ${n}`,
  lang: null,
  published_at: "2026-10-20T10:00:00Z",
  fetched_at: "2026-10-20T11:00:00Z",
  status: "new",
  hold_reason: null,
  ...changes,
});

function show() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <FeedArticles feed={feed} />
    </NextIntlClientProvider>,
  );
}

function open() {
  const details = document.querySelector("details")!;
  details.open = true;
  act(() => {
    fireEvent(details, new Event("toggle"));
  });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("the articles of a feed", () => {
  it("are not asked for until the section is opened", () => {
    show();
    expect(listFeedItems).not.toHaveBeenCalled();
  });

  it("show as text, with a safe link that opens in a new tab", async () => {
    listFeedItems.mockResolvedValue({
      items: [item(1, { title: "<b>Odd</b> story" }), item(2, { url: "javascript:alert(1)" })],
      total: 2,
    });
    show();
    open();
    await waitFor(() => expect(screen.getByText("Excerpt 1")).toBeDefined());
    expect(listFeedItems).toHaveBeenCalledWith(feed.id, 0);
    const links = document.querySelectorAll("a");
    expect(links).toHaveLength(1);
    expect(links[0]!.getAttribute("href")).toBe("https://news.example.org/1");
    expect(links[0]!.getAttribute("target")).toBe("_blank");
    expect(links[0]!.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(links[0]!.textContent).toContain("<b>Odd</b> story");
    expect(document.querySelector("b")).toBeNull();
    expect(screen.getByText(en.Items.unlinked)).toBeDefined();
  });

  it("say why a rule flagged one", async () => {
    listFeedItems.mockResolvedValue({
      items: [item(1, { status: "held", hold_reason: "link_ip_host" })],
      total: 1,
    });
    show();
    open();
    await waitFor(() => expect(screen.getByText(en.Feeds.articles.flagged)).toBeDefined());
    expect(screen.getByText(en.Items.reasons.link_ip_host)).toBeDefined();
  });

  it("say so when there are none or they could not be loaded", async () => {
    listFeedItems.mockResolvedValueOnce({ items: [], total: 0 });
    const { unmount } = show();
    open();
    await waitFor(() => expect(screen.getByText(en.Feeds.articles.empty)).toBeDefined());
    unmount();
    listFeedItems.mockRejectedValueOnce(new Error("boom"));
    show();
    open();
    await waitFor(() => expect(screen.getByText(en.Feeds.articles.failed)).toBeDefined());
  });

  it("page", async () => {
    listFeedItems.mockImplementation(async (_id, offset) => ({
      items: [item((offset ?? 0) / 20 + 1)],
      total: 45,
    }));
    show();
    open();
    await waitFor(() => expect(screen.getByText("Story 1")).toBeDefined());
    expect(
      (screen.getByRole("button", { name: en.Items.previous }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: en.Items.next }));
    await waitFor(() => expect(screen.getByText("Story 2")).toBeDefined());
    expect(listFeedItems).toHaveBeenLastCalledWith(feed.id, 20);
  });
});
