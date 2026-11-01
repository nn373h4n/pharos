import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import { ApiProblem } from "@/lib/api";
import type { Item, ItemAction, ItemPage, ItemQuery, ItemStatus } from "@/lib/items";

type Role = "admin" | "editor" | "member";
let role: Role = "member";
const listItems = vi.fn<(query: ItemQuery) => Promise<ItemPage>>();
const actOnItem = vi.fn<(id: string, action: ItemAction, csrf: string) => Promise<Item>>();

vi.mock("@/components/auth/session-provider", () => ({
  useSession: () => ({
    state: {
      status: "authenticated",
      session: { csrf_token: "token-1", user: { role, display_name: "A" } },
    },
  }),
}));
vi.mock("@/i18n/navigation", () => ({
  Link: ({ href, children, ...props }: { href: string; children: ReactNode }) => (
    <a href={href} {...props}>
      {children}
    </a>
  ),
}));
vi.mock("@/lib/items", async (original) => ({
  ...(await original<typeof import("@/lib/items")>()),
  listItems: (query: ItemQuery) => listItems(query),
  actOnItem: (id: string, action: ItemAction, csrf: string) => actOnItem(id, action, csrf),
}));

const { ItemsView } = await import("./items-view");

function item(n: number, changes: Partial<Item> = {}): Item {
  return {
    id: `id-${n}`,
    url: `https://news.example.org/${n}`,
    title: `Story ${n}`,
    excerpt: `Excerpt ${n}`,
    lang: null,
    published_at: "2026-10-20T10:00:00Z",
    fetched_at: "2026-10-20T11:00:00Z",
    status: "new",
    hold_reason: null,
    updated_at: null,
    sources: [],
    ...changes,
  };
}

function page(items: Item[], total = items.length): ItemPage {
  return { items, total };
}

function show(sourceId?: string) {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <ItemsView sourceId={sourceId} />
    </NextIntlClientProvider>,
  );
}

async function loaded() {
  await waitFor(() => expect(screen.queryByText(en.Items.loading)).toBeNull());
}

beforeEach(() => {
  role = "member";
  listItems.mockResolvedValue(page([item(1), item(2)]));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("a member", () => {
  it("reads the current articles and has nothing to review with", async () => {
    show();
    await loaded();
    expect(listItems).toHaveBeenCalledWith({ status: "new", offset: 0 });
    expect(screen.getAllByRole("article")).toHaveLength(2);
    expect(screen.queryByRole("group", { name: en.Items.filter.label })).toBeNull();
    expect(screen.queryByRole("button", { name: /Hold|Hide|Approve|Restore/ })).toBeNull();
    expect(screen.queryByText(en.Items.status.new)).toBeNull();
  });

  it("is shown where each article goes, as a link with the host beside it", async () => {
    show();
    await loaded();
    const link = screen.getByRole("link", { name: /Story 1/ });
    expect(link.getAttribute("href")).toBe("https://news.example.org/1");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(link.textContent).toContain(en.Items.opensInNewTab.trim());
    const card = screen.getAllByRole("article")[0]!;
    expect(within(card).getByText("news.example.org")).toBeDefined();
    expect(within(card).getByText("Excerpt 1")).toBeDefined();
  });

  it("sees nothing of the review even if an answer wrongly carries it", async () => {
    listItems.mockResolvedValue(
      page([
        item(1, {
          status: "held",
          hold_reason: "link_ip_host",
          sources: [{ id: "s1", name: "Industry news" }],
          updated_at: "2026-10-20T11:00:00Z",
        }),
      ]),
    );
    show();
    await loaded();
    expect(screen.getByText("Story 1")).toBeDefined();
    expect(screen.queryByText(en.Items.reasons.link_ip_host)).toBeNull();
    expect(screen.queryByText(en.Items.status.held)).toBeNull();
    expect(screen.queryByText(/Industry news/)).toBeNull();
    expect(screen.queryAllByRole("button").filter((b) => b.getAttribute("aria-label"))).toEqual([]);
  });

  it("is told so when there is nothing yet", async () => {
    listItems.mockResolvedValue(page([]));
    show();
    await loaded();
    expect(screen.getByText(en.Items.empty.new)).toBeDefined();
  });
});

describe("text from a third party", () => {
  it("is shown as text and never becomes markup", async () => {
    listItems.mockResolvedValue(
      page([
        item(1, {
          title: "<img src=x onerror=alert(1)> Big news",
          excerpt: "<script>alert(2)</script><b>bold</b>",
        }),
      ]),
    );
    const { container } = show();
    await loaded();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)> Big news");
    expect(container.textContent).toContain("<script>alert(2)</script><b>bold</b>");
  });

  it.each([
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "//evil.example/x",
    "https://user:pass@example.org/",
  ])("is not linked when the address is %j", async (url) => {
    listItems.mockResolvedValue(page([item(1, { url })]));
    const { container } = show();
    await loaded();
    expect(container.querySelector("a")).toBeNull();
    expect(screen.getByText("Story 1")).toBeDefined();
    expect(screen.getByText(en.Items.unlinked)).toBeDefined();
  });
});

describe("a reviewer", () => {
  beforeEach(() => {
    role = "editor";
  });

  it("chooses which state to look at, and sees why an article is held", async () => {
    listItems.mockImplementation(async (query) =>
      query.status === "held"
        ? page([item(7, { status: "held", hold_reason: "link_ip_host" })])
        : page([item(1, { sources: [{ id: "s1", name: "Industry news" }] })]),
    );
    show();
    await loaded();
    const group = screen.getByRole("group", { name: en.Items.filter.label });
    const buttons = within(group).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual([
      en.Items.filter.new,
      en.Items.filter.held,
      en.Items.filter.hidden,
    ]);
    expect(buttons[0]!.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("From Industry news")).toBeDefined();
    fireEvent.click(buttons[1]!);
    await waitFor(() => expect(screen.getByText("Story 7")).toBeDefined());
    expect(listItems).toHaveBeenLastCalledWith({ status: "held", offset: 0 });
    expect(buttons[1]!.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText(en.Items.reasons.link_ip_host)).toBeDefined();
    expect(screen.getByText(en.Items.status.held)).toBeDefined();
  });

  it("is offered the steps that fit the state, each with a name of its own", async () => {
    listItems.mockResolvedValue(
      page([
        item(1),
        item(2, { status: "held", hold_reason: "editor_hold" }),
        item(3, { status: "hidden" }),
      ]),
    );
    show();
    await loaded();
    const names = screen.getAllByRole("button").map((b) => b.getAttribute("aria-label"));
    expect(names.filter(Boolean)).toEqual([
      "Hold: Story 1",
      "Hide: Story 1",
      "Approve: Story 2",
      "Hide: Story 2",
      "Restore: Story 3",
    ]);
  });

  it("says in words what an unknown reason means", async () => {
    listItems.mockResolvedValue(
      page([item(1, { status: "held", hold_reason: "something_new_in_the_backend" })]),
    );
    show();
    await loaded();
    expect(screen.getByText(en.Items.reasons.other)).toBeDefined();
  });

  it("holds an article: it leaves the list, the result is announced, focus goes to the list", async () => {
    actOnItem.mockResolvedValue(item(1, { status: "held", hold_reason: "editor_hold" }));
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "Hold: Story 1" }));
    await waitFor(() => expect(screen.queryByText("Story 1")).toBeNull());
    expect(actOnItem).toHaveBeenCalledWith("id-1", "hold", "token-1");
    expect(screen.getByRole("status").textContent).toBe("Held: Story 1");
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: en.Items.results }));
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(screen.getByText(`Total: 1`)).toBeDefined();
  });

  it("refreshes the list when someone else got to the article first", async () => {
    actOnItem.mockRejectedValue(new ApiProblem(409, "item_state"));
    show();
    await loaded();
    listItems.mockResolvedValue(page([item(2)]));
    fireEvent.click(screen.getByRole("button", { name: "Hide: Story 1" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe(en.Problems.itemState));
    await waitFor(() => expect(screen.queryByText("Story 1")).toBeNull());
    expect(listItems).toHaveBeenCalledTimes(2);
    // the button that was pressed went away with the old list: focus is not lost to the page
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: en.Items.results }));
  });

  it.each([
    [new ApiProblem(403, "forbidden"), en.Problems.forbidden],
    [new ApiProblem(429, "rate_limited", 12), "Too many requests. Try again in 12 seconds."],
    [new ApiProblem(429, "rate_limited"), en.Problems.rateLimitedNoWait],
    [new ApiProblem(0, "network_error"), en.Problems.unavailable],
    [new ApiProblem(403, "csrf_failed"), en.Problems.csrf],
    [new Error("boom"), en.Problems.generic],
  ])("tells a failed step in words: %j", async (error, text) => {
    actOnItem.mockRejectedValue(error);
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "Hide: Story 1" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe(text));
    expect(screen.getByText("Story 1")).toBeDefined(); // nothing was removed
  });

  it("does not let a second step start while one is being sent", async () => {
    let finish: (value: Item) => void = () => {};
    actOnItem.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    show();
    await loaded();
    const hide = screen.getByRole("button", { name: "Hide: Story 1" });
    fireEvent.click(hide);
    await waitFor(() => expect((hide as HTMLButtonElement).disabled).toBe(true));
    expect(
      (screen.getByRole("button", { name: "Hold: Story 1" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    await act(async () => finish(item(1, { status: "hidden" })));
  });

  it("cannot change the list, the filter or the page while a step is on its way", async () => {
    let finish: (value: Item) => void = () => {};
    actOnItem.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    listItems.mockResolvedValue(page([item(1), item(2)], 45));
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: "Hide: Story 1" }));
    const off = (name: string) =>
      (screen.getByRole("button", { name }) as HTMLButtonElement).disabled;
    await waitFor(() => expect(off("Hide: Story 2")).toBe(true)); // another article's step too
    expect(off(en.Items.filter.held)).toBe(true);
    expect(off(en.Items.filter.new)).toBe(true);
    expect(off(en.Items.next)).toBe(true);
    await act(async () => finish(item(1, { status: "hidden" })));
    expect(off(en.Items.filter.held)).toBe(false);
    expect(off("Hide: Story 2")).toBe(false);
  });

  it("cannot go back a page while a step is on its way", async () => {
    let finish: (value: Item) => void = () => {};
    actOnItem.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    listItems.mockImplementation(async ({ offset }) => page([item((offset ?? 0) + 1)], 45));
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: en.Items.next }));
    await waitFor(() => expect(screen.getByText("Story 21")).toBeDefined());
    const previous = () =>
      screen.getByRole("button", { name: en.Items.previous }) as HTMLButtonElement;
    expect(previous().disabled).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Hide: Story 21" }));
    await waitFor(() => expect(previous().disabled).toBe(true));
    await act(async () => finish(item(21, { status: "hidden" })));
  });

  it("goes back a page when the last article of a later page leaves", async () => {
    listItems.mockImplementation(async ({ offset }) =>
      offset === 20
        ? page([item(21)], 21)
        : page(
            Array.from({ length: 20 }, (_, n) => item(n + 1)),
            21,
          ),
    );
    actOnItem.mockResolvedValue(item(21, { status: "hidden" }));
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: en.Items.next }));
    await waitFor(() => expect(screen.getByText("Story 21")).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: "Hide: Story 21" }));
    await waitFor(() => expect(listItems).toHaveBeenLastCalledWith({ status: "new", offset: 0 }));
    await waitFor(() => expect(screen.getByText("Story 1")).toBeDefined());
    expect(screen.queryByText(en.Items.empty.new)).toBeNull();
    expect(screen.getAllByRole("article")).toHaveLength(20);
  });
});

describe("what the model said", () => {
  const read = {
    summary: "<b>Cuts</b> month-end work. See https://evil.example now",
    category: "tool",
    quality: 4,
  };

  it("shows the summary as text and the category to a member, but not the score", async () => {
    listItems.mockResolvedValue(page([item(1, read)]));
    const { container } = show();
    await loaded();
    expect(container.querySelector("b")).toBeNull(); // third-party-influenced text stays text
    expect(container.textContent).toContain("<b>Cuts</b> month-end work.");
    expect(screen.getByText(en.Items.categories.tool)).toBeDefined();
    expect(screen.queryByText(/Quality:/)).toBeNull();
    // the summary is never a link, whatever it contains
    expect(container.querySelectorAll("a")).toHaveLength(1); // only the title
  });

  it("shows nothing extra for an article nobody has read yet", async () => {
    listItems.mockResolvedValue(page([item(1)]));
    show();
    await loaded();
    expect(screen.queryByText(en.Items.summary)).toBeNull();
    expect(screen.queryByText(en.Items.categories.tool)).toBeNull();
  });

  it("ignores a category it does not know", async () => {
    listItems.mockResolvedValue(page([item(1, { ...read, category: "<script>" })]));
    const { container } = show();
    await loaded();
    expect(container.textContent).not.toContain("<script>");
  });

  it("shows the score and the reason to a reviewer", async () => {
    role = "editor";
    listItems.mockResolvedValue(
      page([item(1, { ...read, status: "held", hold_reason: "low_quality", quality: 2 })]),
    );
    show();
    await loaded();
    expect(screen.getByText("Quality: 2 of 5")).toBeDefined();
    expect(screen.getByText(en.Items.reasons.low_quality)).toBeDefined();
  });
});

describe("states", () => {
  it("shows that it is loading", async () => {
    listItems.mockReturnValue(new Promise(() => {}));
    show();
    expect(screen.getByRole("status").textContent).toBe(en.Items.loading);
  });

  it("keeps the place where results are announced in the page all the time", async () => {
    role = "editor";
    actOnItem.mockResolvedValue(item(1, { status: "held" }));
    show();
    const region = screen.getByRole("status");
    await loaded();
    expect(screen.getByRole("status")).toBe(region); // the same element, empty, before any step
    expect(region.textContent).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Hold: Story 1" }));
    await waitFor(() => expect(region.textContent).toBe("Held: Story 1"));
    expect(screen.getByRole("status")).toBe(region); // text changed in place: it will be read out
  });

  it("offers another try when the list could not be loaded", async () => {
    listItems.mockRejectedValueOnce(new Error("boom"));
    show();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(en.Items.loadFailed);
    fireEvent.click(screen.getByRole("button", { name: en.Items.retry }));
    await waitFor(() => expect(screen.getByText("Story 1")).toBeDefined());
  });

  it("says plainly when the backend refuses, and offers no retry", async () => {
    listItems.mockRejectedValue(new ApiProblem(403, "forbidden"));
    show();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(en.Problems.forbidden);
    expect(screen.queryByRole("button", { name: en.Items.retry })).toBeNull();
  });

  it("says when the service is down", async () => {
    listItems.mockRejectedValue(new ApiProblem(503, "service_unavailable"));
    show();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(en.Problems.unavailable);
  });

  it.each<[ItemStatus, string]>([
    ["held", en.Items.empty.held],
    ["hidden", en.Items.empty.hidden],
  ])("says what an empty %s list means", async (status, text) => {
    role = "admin";
    listItems.mockResolvedValue(page([]));
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: en.Items.filter[status] }));
    await waitFor(() => expect(screen.getByText(text)).toBeDefined());
  });
});

describe("pages", () => {
  it("moves through the list twenty at a time and says where it is", async () => {
    listItems.mockImplementation(async ({ offset }) => page([item((offset ?? 0) + 1)], 45));
    show();
    await loaded();
    expect(screen.getByText("1–1 of 45")).toBeDefined();
    const previous = screen.getByRole("button", { name: en.Items.previous }) as HTMLButtonElement;
    const next = screen.getByRole("button", { name: en.Items.next }) as HTMLButtonElement;
    expect(previous.disabled).toBe(true);
    fireEvent.click(next);
    await waitFor(() => expect(listItems).toHaveBeenLastCalledWith({ status: "new", offset: 20 }));
    await waitFor(() => expect(screen.getByText("21–21 of 45")).toBeDefined());
    const again = screen.getByRole("button", { name: en.Items.previous }) as HTMLButtonElement;
    expect(again.disabled).toBe(false); // the page was rebuilt: ask for the button again
    fireEvent.click(again);
    await waitFor(() => expect(listItems).toHaveBeenLastCalledWith({ status: "new", offset: 0 }));
  });

  it("has no next page at the end", async () => {
    listItems.mockResolvedValue(page([item(1), item(2)], 2));
    show();
    await loaded();
    expect(
      (screen.getByRole("button", { name: en.Items.next }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("starts from the first page again when the state changes", async () => {
    role = "admin";
    listItems.mockImplementation(async ({ offset }) => page([item((offset ?? 0) + 1)], 45));
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: en.Items.next }));
    await waitFor(() => expect(listItems).toHaveBeenLastCalledWith({ status: "new", offset: 20 }));
    fireEvent.click(screen.getByRole("button", { name: en.Items.filter.held }));
    await waitFor(() => expect(listItems).toHaveBeenLastCalledWith({ status: "held", offset: 0 }));
  });
});

describe("while the next list loads", () => {
  it("shows that it is loading and not the list that was there before", async () => {
    role = "admin";
    let release: (value: ItemPage) => void = () => {};
    listItems.mockImplementation(({ status }) =>
      status === "held"
        ? new Promise((resolve) => (release = resolve))
        : Promise.resolve(page([item(1)])),
    );
    show();
    await loaded();
    expect(screen.getByText("Story 1")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: en.Items.filter.held }));
    expect(screen.getByText(en.Items.loading)).toBeDefined();
    expect(screen.queryByText("Story 1")).toBeNull();
    await act(async () => release(page([item(7, { status: "held" })])));
    expect(screen.getByText("Story 7")).toBeDefined();
  });
});

describe("answers that arrive late", () => {
  it("never replace what was asked for last", async () => {
    role = "admin";
    const resolvers: Record<string, (value: ItemPage) => void> = {};
    listItems.mockImplementation(
      ({ status }) => new Promise((resolve) => (resolvers[status ?? "new"] = resolve)),
    );
    show();
    await waitFor(() => expect(resolvers.new).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: en.Items.filter.held }));
    await waitFor(() => expect(resolvers.held).toBeDefined());
    await act(async () => resolvers.held!(page([item(7, { status: "held" })])));
    await act(async () => resolvers.new!(page([item(1)])));
    expect(screen.getByText("Story 7")).toBeDefined();
    expect(screen.queryByText("Story 1")).toBeNull();
  });
});

describe("the articles of one source", () => {
  const id = "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a5b";

  it("asks for that source only and offers the way back", async () => {
    show(id);
    await loaded();
    expect(listItems).toHaveBeenCalledWith({ status: "new", offset: 0, sourceId: id });
    expect(screen.getByText(en.Items.source.only)).toBeDefined();
    expect(screen.getByRole("link", { name: en.Items.source.all }).getAttribute("href")).toBe(
      "/items",
    );
  });

  it("shows no chip for all sources", async () => {
    show();
    await loaded();
    expect(screen.queryByText(en.Items.source.only)).toBeNull();
  });
});
