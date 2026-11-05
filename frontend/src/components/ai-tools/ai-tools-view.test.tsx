import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import en from "../../../messages/en.json";
import type { AiTool } from "@/lib/ai-tools";
import { ApiProblem } from "@/lib/api";

type Role = "admin" | "editor" | "member";
let role: Role = "admin";
const listAiTools = vi.fn<() => Promise<AiTool[]>>();
const createAiTool = vi.fn<(body: unknown, csrf: string) => Promise<AiTool>>();
const updateAiTool = vi.fn<(id: string, body: unknown, csrf: string) => Promise<AiTool>>();
const deleteAiTool = vi.fn<(id: string, csrf: string) => Promise<void>>();

vi.mock("@/components/auth/session-provider", () => ({
  useSession: () => ({
    state: {
      status: "authenticated",
      session: { csrf_token: "token-1", user: { role, display_name: "A" } },
    },
  }),
}));
vi.mock("@/lib/ai-tools", async (original) => ({
  ...(await original<typeof import("@/lib/ai-tools")>()),
  listAiTools: () => listAiTools(),
  createAiTool: (body: unknown, csrf: string) => createAiTool(body, csrf),
  updateAiTool: (id: string, body: unknown, csrf: string) => updateAiTool(id, body, csrf),
  deleteAiTool: (id: string, csrf: string) => deleteAiTool(id, csrf),
}));

const { AiToolsView } = await import("./ai-tools-view");

const id = (n: number) => `6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a${String(n).padStart(2, "0")}`;
const tool = (n: number, changes: Partial<AiTool> = {}): AiTool => ({
  id: id(n),
  name: `Tool ${n}`,
  url: `https://tool${n}.example.org/chat`,
  ...changes,
});

function show() {
  return render(
    <NextIntlClientProvider locale="en" messages={en}>
      <AiToolsView />
    </NextIntlClientProvider>,
  );
}

async function loaded() {
  await waitFor(() => expect(screen.queryByText(en.AiTools.loading)).toBeNull());
}

const act = (action: string, name: string) =>
  screen.getByRole("button", { name: `${action}: ${name}` }) as HTMLButtonElement;
const status = () => screen.getByRole("status");

beforeEach(() => {
  role = "admin";
  listAiTools.mockResolvedValue([tool(1), tool(2)]);
  createAiTool.mockResolvedValue(tool(3));
  updateAiTool.mockResolvedValue(tool(1));
  deleteAiTool.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("who may do what", () => {
  it("lets an administrator add, change and delete", async () => {
    show();
    await loaded();
    expect(screen.getByRole("button", { name: en.AiTools.actions.add })).toBeDefined();
    expect(act(en.AiTools.actions.edit, "Tool 1")).toBeDefined();
    expect(act(en.AiTools.actions.delete, "Tool 1")).toBeDefined();
    expect(screen.queryByText(en.AiTools.readOnly)).toBeNull();
  });

  it.each(["editor", "member"] as const)("lets %s only look", async (who) => {
    role = who;
    show();
    await loaded();
    expect(screen.getByText("Tool 1")).toBeDefined();
    expect(screen.getByText(en.AiTools.readOnly)).toBeDefined();
    expect(screen.queryByRole("button", { name: en.AiTools.actions.add })).toBeNull();
    expect(screen.queryByRole("button", { name: /Edit|Delete/ })).toBeNull();
  });
});

describe("the list", () => {
  it("shows the name and the whole address of every tool, as text and not as a link", async () => {
    show();
    await loaded();
    expect(screen.getByText("Tool 1")).toBeDefined();
    expect(screen.getByText(/https:\/\/tool1\.example\.org\/chat/)).toBeDefined();
    expect(screen.queryAllByRole("link")).toHaveLength(0);
    expect(screen.getByText("2 of 10 tools")).toBeDefined();
  });

  it("says when there is no tool, to an administrator and to others", async () => {
    listAiTools.mockResolvedValue([]);
    show();
    await loaded();
    expect(screen.getByText(en.AiTools.emptyAdmin)).toBeDefined();
    cleanup();
    role = "member";
    show();
    await loaded();
    expect(screen.getByText(en.AiTools.empty)).toBeDefined();
  });

  it("does not show an address that cannot be used, and says so", async () => {
    listAiTools.mockResolvedValue([tool(1, { url: "javascript:alert(1)" })]);
    show();
    await loaded();
    expect(screen.queryByText(/javascript/)).toBeNull();
    expect(screen.getByText(en.AiTools.addressRefused)).toBeDefined();
  });

  it("tells a failed load and tries again on request", async () => {
    listAiTools.mockRejectedValueOnce(new ApiProblem(500, "internal_error"));
    show();
    expect(await screen.findByText(en.AiTools.loadFailed)).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.retry }));
    expect(await screen.findByText("Tool 1")).toBeDefined();
  });

  it("stops offering to add at ten tools and says why", async () => {
    listAiTools.mockResolvedValue(Array.from({ length: 10 }, (_, n) => tool(n + 1)));
    show();
    await loaded();
    const add = screen.getByRole("button", { name: en.AiTools.actions.add }) as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    expect(screen.getByText(/The list is full \(10 tools\)/)).toBeDefined();
    expect(add.getAttribute("aria-describedby")).toBe("ai-tools-full");
  });
});

describe("adding", () => {
  async function open() {
    show();
    await loaded();
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.actions.add }));
    return screen.findByRole("dialog");
  }
  const fill = (label: string, value: string) =>
    fireEvent.change(screen.getByLabelText(label), { target: { value } });

  it("sends a new tool with the token and shows the list again", async () => {
    await open();
    fill(en.AiTools.form.name, "  Fresh  ");
    fill(en.AiTools.form.url, " https://fresh.example.org/ ");
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.form.save }));
    await waitFor(() => expect(createAiTool).toHaveBeenCalledTimes(1));
    expect(createAiTool.mock.calls[0]![0]).toEqual({
      name: "Fresh",
      url: "https://fresh.example.org/",
    });
    expect(createAiTool.mock.calls[0]![1]).toBe("token-1");
    await waitFor(() => expect(status().textContent).toContain("Added: Fresh"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(listAiTools).toHaveBeenCalledTimes(2));
  });

  it.each([
    ["https_only", "urlHttpsOnly"],
    ["credentials_not_allowed", "urlCredentials"],
    ["ip_address_not_allowed", "urlIp"],
    ["invalid_url", "urlInvalid"],
    ["string_too_long", "urlTooLong"],
  ] as const)("tells at the address field what is wrong (%s)", async (type, key) => {
    await open();
    fill(en.AiTools.form.name, "Fresh");
    fill(en.AiTools.form.url, "http://fresh.example.org/");
    createAiTool.mockRejectedValue(
      new ApiProblem(422, "validation_failed", null, [{ loc: ["body", "url"], type }]),
    );
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.form.save }));
    const field = screen.getByLabelText(en.AiTools.form.url);
    await waitFor(() => expect(field.getAttribute("aria-invalid")).toBe("true"));
    expect(screen.getByText(en.AiTools.fieldErrors[key])).toBeDefined();
    expect(screen.getByRole("dialog")).toBeDefined(); // nothing is lost: the dialog stays
    expect(field.getAttribute("aria-describedby")).toContain("-error");
  });

  it("tells at the name field that the name is not plain text", async () => {
    await open();
    fill(en.AiTools.form.name, "<b>x</b>");
    fill(en.AiTools.form.url, "https://fresh.example.org/");
    createAiTool.mockRejectedValue(
      new ApiProblem(422, "validation_failed", null, [
        { loc: ["body", "name"], type: "invalid_text" },
      ]),
    );
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.form.save }));
    await waitFor(() =>
      expect(screen.getByLabelText(en.AiTools.form.name).getAttribute("aria-invalid")).toBe("true"),
    );
    expect(screen.getByText(en.AiTools.fieldErrors.nameInvalid)).toBeDefined();
  });

  it.each([
    ["ai_tool_conflict", 409, en.Problems.aiToolConflict],
    ["ai_tool_limit", 409, en.Problems.aiToolLimit],
    ["forbidden", 403, en.Problems.forbidden],
  ])("tells a refusal of the whole request (%s) in the dialog", async (code, status, text) => {
    await open();
    fill(en.AiTools.form.name, "Fresh");
    fill(en.AiTools.form.url, "https://fresh.example.org/");
    createAiTool.mockRejectedValue(new ApiProblem(status, code));
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.form.save }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(text);
    expect(screen.getByRole("dialog")).toBeDefined();
  });

  it("locks the form while the request is on its way and cannot be sent twice", async () => {
    await open();
    fill(en.AiTools.form.name, "Fresh");
    fill(en.AiTools.form.url, "https://fresh.example.org/");
    let finish: (value: AiTool) => void = () => {};
    createAiTool.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.form.save }));
    await waitFor(() =>
      expect((screen.getByLabelText(en.AiTools.form.name) as HTMLInputElement).disabled).toBe(true),
    );
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.form.saving }));
    expect(createAiTool).toHaveBeenCalledTimes(1);
    finish(tool(3));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });
});

describe("changing and deleting", () => {
  it("sends the changed fields of a tool and says so", async () => {
    show();
    await loaded();
    fireEvent.click(act(en.AiTools.actions.edit, "Tool 1"));
    await screen.findByRole("dialog");
    const name = screen.getByLabelText(en.AiTools.form.name) as HTMLInputElement;
    expect(name.value).toBe("Tool 1"); // the form starts from the stored tool
    fireEvent.change(name, { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.form.save }));
    await waitFor(() => expect(updateAiTool).toHaveBeenCalledTimes(1));
    expect(updateAiTool.mock.calls[0]![0]).toBe(id(1));
    expect(updateAiTool.mock.calls[0]![2]).toBe("token-1");
    await waitFor(() => expect(status().textContent).toContain("Saved: Renamed"));
  });

  it("asks before deleting and deletes on confirmation", async () => {
    show();
    await loaded();
    fireEvent.click(act(en.AiTools.actions.delete, "Tool 2"));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.textContent).toContain("Tool 2");
    expect(deleteAiTool).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.deleteDialog.confirm }));
    await waitFor(() => expect(deleteAiTool).toHaveBeenCalledWith(id(2), "token-1"));
    await waitFor(() => expect(status().textContent).toContain("Deleted: Tool 2"));
  });

  it("deletes nothing when the question is answered with Cancel", async () => {
    show();
    await loaded();
    fireEvent.click(act(en.AiTools.actions.delete, "Tool 2"));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(deleteAiTool).not.toHaveBeenCalled();
  });

  it("reloads the list when the tool was already gone (404) and says so", async () => {
    deleteAiTool.mockRejectedValue(new ApiProblem(404, "not_found"));
    show();
    await loaded();
    fireEvent.click(act(en.AiTools.actions.delete, "Tool 2"));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: en.AiTools.deleteDialog.confirm }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(en.Problems.notFound);
    await waitFor(() => expect(listAiTools).toHaveBeenCalledTimes(2));
  });
});
