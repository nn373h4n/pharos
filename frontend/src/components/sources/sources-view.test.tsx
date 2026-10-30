import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import { ApiProblem } from "@/lib/api";
import type { Source, SourcePage } from "@/lib/sources";

type Role = "admin" | "editor" | "member";
let role: Role = "admin";
const listSources = vi.fn<(query: unknown) => Promise<SourcePage>>();
const createSource = vi.fn<(body: unknown, csrf: string) => Promise<Source>>();
const updateSource = vi.fn<(id: string, body: unknown, csrf: string) => Promise<Source>>();
const deleteSource = vi.fn<(id: string, csrf: string) => Promise<void>>();

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
vi.mock("@/components/sources/runs-panel", () => ({ RunsPanel: () => null }));
vi.mock("@/components/sources/test-panel", () => ({
  TestPanel: () => <span data-testid="test-panel" />,
}));
vi.mock("@/lib/sources", async (original) => ({
  ...(await original<typeof import("@/lib/sources")>()),
  listSources: (query: unknown) => listSources(query),
  createSource: (body: unknown, csrf: string) => createSource(body, csrf),
  updateSource: (id: string, body: unknown, csrf: string) => updateSource(id, body, csrf),
  deleteSource: (id: string, csrf: string) => deleteSource(id, csrf),
}));

const { SourcesView } = await import("./sources-view");

const id = (n: number) => `6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a${String(n).padStart(2, "0")}`;

function source(n: number, changes: Partial<Source> = {}): Source {
  return {
    id: id(n),
    type: "rss",
    name: `Source ${n}`,
    config: { url: `https://news.example.org/${n}.xml` },
    enabled: true,
    interval_seconds: 3600,
    next_fetch_at: "2026-10-30T10:00:00Z",
    last_fetched_at: "2026-10-30T09:00:00Z",
    last_success_at: "2026-10-30T09:00:00Z",
    consecutive_failures: 0,
    last_error_code: null,
    created_by: null,
    created_at: "2026-10-01T10:00:00Z",
    updated_at: "2026-10-01T10:00:00Z",
    ...changes,
  };
}

const page = (items: Source[], total = items.length): SourcePage => ({ items, total });

function show() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <SourcesView />
    </NextIntlClientProvider>,
  );
}

async function loaded() {
  await waitFor(() => expect(screen.queryByText(en.Sources.loading)).toBeNull());
}

const act = (action: string, name: string) =>
  screen.getByRole("button", { name: `${action}: ${name}` }) as HTMLButtonElement;
const status = () => screen.getByRole("status");

beforeEach(() => {
  role = "admin";
  listSources.mockResolvedValue(page([source(1), source(2)]));
  createSource.mockResolvedValue(source(3));
  updateSource.mockResolvedValue(source(1));
  deleteSource.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("who may do what", () => {
  it("lets an administrator add, change, switch and delete", async () => {
    show();
    await loaded();
    expect(screen.getByRole("button", { name: en.Sources.actions.add })).toBeDefined();
    expect(act(en.Sources.actions.edit, "Source 1")).toBeDefined();
    expect(act(en.Sources.actions.disable, "Source 1")).toBeDefined();
    expect(act(en.Sources.actions.delete, "Source 1")).toBeDefined();
    expect(screen.queryByText(en.Sources.readOnly)).toBeNull();
  });

  it.each(["editor", "member"] as const)("lets %s only look", async (who) => {
    role = who;
    show();
    await loaded();
    expect(screen.getByText("Source 1")).toBeDefined();
    expect(screen.getByText(en.Sources.readOnly)).toBeDefined();
    expect(screen.queryByRole("button", { name: en.Sources.actions.add })).toBeNull();
    expect(
      screen.queryByRole("button", { name: new RegExp(en.Sources.actions.delete) }),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: new RegExp(en.Sources.actions.edit) })).toBeNull();
  });

  it("offers the trial fetch to administrators only", async () => {
    show();
    await loaded();
    expect(screen.getAllByTestId("test-panel")).toHaveLength(2);
    cleanup();
    role = "editor";
    show();
    await waitFor(() => expect(screen.getByText("Source 1")).toBeDefined());
    expect(screen.queryByTestId("test-panel")).toBeNull();
  });

  it("links each source to its articles", async () => {
    show();
    await loaded();
    const hrefs = screen.getAllByRole("link").map((link) => link.getAttribute("href"));
    expect(hrefs).toContain(`/items?source=${id(1)}`);
  });
});

describe("the list", () => {
  it("shows state, last success, failures and the last error in words", async () => {
    listSources.mockResolvedValue(
      page([
        source(1, { consecutive_failures: 3, last_error_code: "timed_out" }),
        source(2, { enabled: false }),
      ]),
    );
    show();
    await loaded();
    const text = document.body.textContent ?? "";
    expect(text).not.toContain("timed_out");
    expect(text).toContain(en.Sources.health.failing);
    expect(text).toContain(en.Sources.health.off);
  });

  it("filters by type and state and starts again from the first page", async () => {
    show();
    await loaded();
    fireEvent.change(screen.getByLabelText(en.Sources.filters.type), {
      target: { value: "github" },
    });
    await waitFor(() =>
      expect(listSources).toHaveBeenLastCalledWith({
        type: "github",
        enabled: undefined,
        offset: 0,
      }),
    );
    fireEvent.change(screen.getByLabelText(en.Sources.filters.state), { target: { value: "off" } });
    await waitFor(() =>
      expect(listSources).toHaveBeenLastCalledWith({ type: "github", enabled: false, offset: 0 }),
    );
  });

  it("says when nothing was added, and invites an administrator to add", async () => {
    listSources.mockResolvedValue(page([]));
    show();
    await loaded();
    expect(screen.getByText(en.Sources.emptyAdmin)).toBeDefined();
    cleanup();
    role = "editor";
    show();
    await waitFor(() => expect(screen.getByText(en.Sources.empty)).toBeDefined());
  });

  it("pages", async () => {
    listSources.mockImplementation(async (query) =>
      page([source(((query as { offset: number }).offset ?? 0) / 20 + 1)], 45),
    );
    show();
    await loaded();
    expect(
      (screen.getByRole("button", { name: en.Sources.previous }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: en.Sources.next }));
    await waitFor(() =>
      expect(listSources).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 20 })),
    );
  });
});

describe("states", () => {
  it("shows a loading state", () => {
    listSources.mockReturnValue(new Promise(() => {}));
    show();
    expect(status().textContent).toContain(en.Sources.loading);
  });

  it("shows a failure and tries again", async () => {
    listSources.mockRejectedValueOnce(new Error("boom"));
    show();
    await waitFor(() => expect(screen.getByText(en.Sources.loadFailed)).toBeDefined());
    fireEvent.click(screen.getByRole("button", { name: en.Sources.retry }));
    await waitFor(() => expect(screen.getByText("Source 1")).toBeDefined());
  });

  it("shows a refusal without a retry button", async () => {
    listSources.mockRejectedValue(new ApiProblem(403, "forbidden"));
    show();
    await waitFor(() => expect(screen.getByText(en.Problems.forbidden)).toBeDefined());
    expect(screen.queryByRole("button", { name: en.Sources.retry })).toBeNull();
  });
});

describe("switching on and off", () => {
  it("sends the change with the CSRF token, reloads and says so", async () => {
    show();
    await loaded();
    fireEvent.click(act(en.Sources.actions.disable, "Source 1"));
    await waitFor(() => expect(status().textContent).toContain("Switched off: Source 1"));
    expect(updateSource).toHaveBeenCalledWith(id(1), { enabled: false }, "token-1");
    expect(listSources).toHaveBeenCalledTimes(2);
  });

  it("tells a refusal as an alert", async () => {
    updateSource.mockRejectedValue(new ApiProblem(503, "service_unavailable"));
    show();
    await loaded();
    fireEvent.click(act(en.Sources.actions.disable, "Source 1"));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(en.Problems.unavailable);
  });

  it("locks everything while a step is on its way", async () => {
    let finish: (value: Source) => void = () => {};
    updateSource.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    show();
    await loaded();
    fireEvent.click(act(en.Sources.actions.disable, "Source 1"));
    await waitFor(() => expect(act(en.Sources.actions.disable, "Source 2").disabled).toBe(true));
    expect(
      (screen.getByRole("button", { name: en.Sources.actions.add }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect((screen.getByLabelText(en.Sources.filters.type) as HTMLSelectElement).disabled).toBe(
      true,
    );
    finish(source(1));
    await waitFor(() => expect(act(en.Sources.actions.disable, "Source 2").disabled).toBe(false));
  });
});

describe("adding", () => {
  async function open() {
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: en.Sources.actions.add }));
    return screen.findByRole("dialog");
  }
  const fill = (label: string, value: string) =>
    fireEvent.change(screen.getByLabelText(label), { target: { value } });

  it("sends a new source and shows the first page", async () => {
    await open();
    fill(en.Sources.form.name, "Fresh");
    fill(en.Sources.form.url, "https://fresh.example.org/feed.xml");
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.save }));
    await waitFor(() => expect(createSource).toHaveBeenCalledTimes(1));
    expect(createSource.mock.calls[0]![0]).toMatchObject({
      type: "rss",
      name: "Fresh",
      config: { url: "https://fresh.example.org/feed.xml" },
    });
    expect(createSource.mock.calls[0]![1]).toBe("token-1");
    await waitFor(() => expect(status().textContent).toContain("Added: Fresh"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows a refusal that names a field at the field and keeps the dialog", async () => {
    await open();
    fill(en.Sources.form.name, "Fresh");
    fill(en.Sources.form.url, "https://fresh.example.org/feed.xml");
    createSource.mockRejectedValue(
      new ApiProblem(422, "validation_failed", null, [
        { loc: ["body", "config"], type: "url_not_allowed" },
      ]),
    );
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.save }));
    await waitFor(() =>
      expect(screen.getByLabelText(en.Sources.form.url).getAttribute("aria-invalid")).toBe("true"),
    );
    expect(screen.getByRole("dialog")).toBeDefined();
  });

  it("tells a conflict for the whole form", async () => {
    await open();
    fill(en.Sources.form.name, "Fresh");
    fill(en.Sources.form.url, "https://fresh.example.org/feed.xml");
    createSource.mockRejectedValue(new ApiProblem(409, "source_conflict"));
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.save }));
    await waitFor(() =>
      expect(
        within(screen.getByRole("dialog")).getByText(en.Problems.sourceConflict),
      ).toBeDefined(),
    );
  });

  it("closes with Cancel without sending", async () => {
    await open();
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.cancel }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(createSource).not.toHaveBeenCalled();
  });
});

describe("editing", () => {
  it("sends the name, settings and interval but never the type", async () => {
    show();
    await loaded();
    fireEvent.click(act(en.Sources.actions.edit, "Source 1"));
    await screen.findByRole("dialog");
    fireEvent.change(screen.getByLabelText(en.Sources.form.name), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: en.Sources.form.save }));
    await waitFor(() => expect(updateSource).toHaveBeenCalledTimes(1));
    const [sent, body] = updateSource.mock.calls[0]!;
    expect(sent).toBe(id(1));
    expect(body).toMatchObject({ name: "Renamed" });
    expect(body).not.toHaveProperty("type");
    await waitFor(() => expect(status().textContent).toContain("Saved: Renamed"));
  });
});

describe("deleting", () => {
  async function ask(name = "Source 1") {
    fireEvent.click(act(en.Sources.actions.delete, name));
    return screen.findByRole("dialog");
  }

  it("asks first, and Cancel deletes nothing", async () => {
    show();
    await loaded();
    const dialog = await ask();
    expect(within(dialog).getByText(/Source 1/)).toBeDefined();
    fireEvent.click(within(dialog).getByRole("button", { name: en.Sources.deleteDialog.cancel }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(deleteSource).not.toHaveBeenCalled();
  });

  it("deletes after confirmation", async () => {
    show();
    await loaded();
    const dialog = await ask();
    fireEvent.click(within(dialog).getByRole("button", { name: en.Sources.deleteDialog.confirm }));
    await waitFor(() => expect(deleteSource).toHaveBeenCalledWith(id(1), "token-1"));
    await waitFor(() => expect(status().textContent).toContain("Deleted: Source 1"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("goes back a page when the last source of a later page is deleted", async () => {
    listSources.mockImplementation(async (query) => {
      const offset = (query as { offset: number }).offset;
      return offset === 0 ? page([source(1)], 21) : page([source(2)], 21);
    });
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: en.Sources.next }));
    await waitFor(() => expect(screen.getByText("Source 2")).toBeDefined());
    const dialog = await ask("Source 2");
    fireEvent.click(within(dialog).getByRole("button", { name: en.Sources.deleteDialog.confirm }));
    await waitFor(() =>
      expect(listSources).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 0 })),
    );
  });

  it("tells a refusal and closes the question", async () => {
    deleteSource.mockRejectedValue(new ApiProblem(404, "not_found"));
    show();
    await loaded();
    const dialog = await ask();
    fireEvent.click(within(dialog).getByRole("button", { name: en.Sources.deleteDialog.confirm }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(en.Problems.notFound);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("answers that arrive late or at the wrong time", () => {
  it("shows the newest request, not the one that answers last", async () => {
    let first: (value: SourcePage) => void = () => {};
    listSources.mockReturnValueOnce(new Promise((resolve) => (first = resolve)));
    show();
    await waitFor(() => expect(listSources).toHaveBeenCalledTimes(1));
    listSources.mockResolvedValue(page([source(7)]));
    fireEvent.change(screen.getByLabelText(en.Sources.filters.type), {
      target: { value: "github" },
    });
    await waitFor(() => expect(screen.getByText("Source 7")).toBeDefined());
    first(page([source(1)]));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.queryByText("Source 1")).toBeNull();
    expect(screen.getByText("Source 7")).toBeDefined();
  });

  it("starts from the first page when a filter changes", async () => {
    listSources.mockResolvedValue(page([source(1)], 45));
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: en.Sources.next }));
    await waitFor(() =>
      expect(listSources).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 20 })),
    );
    fireEvent.change(screen.getByLabelText(en.Sources.filters.state), { target: { value: "on" } });
    await waitFor(() =>
      expect(listSources).toHaveBeenLastCalledWith({ type: undefined, enabled: true, offset: 0 }),
    );
  });

  it("does not close the question while the deletion is on its way", async () => {
    let finish: () => void = () => {};
    deleteSource.mockReturnValue(new Promise<void>((resolve) => (finish = resolve)));
    show();
    await loaded();
    fireEvent.click(act(en.Sources.actions.delete, "Source 1"));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: en.Sources.deleteDialog.confirm }));
    await waitFor(() => expect(deleteSource).toHaveBeenCalled());
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(screen.queryByRole("dialog")).not.toBeNull();
    finish();
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("while the next list is on its way", () => {
  it("does not keep showing the previous one", async () => {
    show();
    await loaded();
    expect(screen.getByText("Source 1")).toBeDefined();
    listSources.mockReturnValue(new Promise(() => {}));
    fireEvent.change(screen.getByLabelText(en.Sources.filters.type), {
      target: { value: "github" },
    });
    await waitFor(() => expect(status().textContent).toContain(en.Sources.loading));
    expect(screen.queryByText("Source 1")).toBeNull();
  });

  it("moves focus to the list heading after a step, so it is not lost", async () => {
    show();
    await loaded();
    fireEvent.click(act(en.Sources.actions.disable, "Source 1"));
    await waitFor(() => expect(status().textContent).toContain("Switched off: Source 1"));
    expect(document.activeElement).toBe(
      screen.getByRole("heading", { level: 2, name: en.Sources.results }),
    );
  });
});
