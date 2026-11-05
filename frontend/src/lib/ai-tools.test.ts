import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiProblem } from "@/lib/api";
import {
  approvedTools,
  createAiTool,
  deleteAiTool,
  fieldKey,
  forgetApprovedTools,
  isAiTool,
  listAiTools,
  problemsByField,
  toolHref,
  updateAiTool,
} from "@/lib/ai-tools";

const ID = "6a1f0c2e-3b4d-4e5f-8a9b-0c1d2e3f4a01";
const TOOL = { id: ID, name: "Alpha", url: "https://alpha.example.org/" };

function answer(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  forgetApprovedTools();
});
afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

describe("toolHref", () => {
  it.each([
    ["https://chat.example.org/", "https://chat.example.org/"],
    ["https://chat.example.org", "https://chat.example.org/"],
    ["https://ai.corp.internal:8443/portal?x=1", "https://ai.corp.internal:8443/portal?x=1"],
    ["https://intranet/", "https://intranet/"],
    ["https://123.example.com/", "https://123.example.com/"],
    ["https://localhost.example.org/", "https://localhost.example.org/"],
  ])("accepts %s", (value, expected) => {
    expect(toolHref(value)).toBe(expected);
  });

  it.each([
    "http://chat.example.org/",
    "HTTPS://chat.example.org/",
    "javascript:alert(1)",
    "data:text/html,x",
    "//chat.example.org/",
    "https://user:pw@chat.example.org/",
    "https://chat.example.org/#frag",
    "https://chat.example.org/a b",
    "https://chat.example.org/‮",
    "https://127.0.0.1/",
    "https://2130706433/",
    "https://0x7f.1/",
    "https://[::1]/",
    "https://[v1.x]/",
    "https://localhost/",
    "https://LOCALHOST./",
    "https://app.localhost:8443/",
    "https://10.1.2.3:8080/",
    "https:///path",
    "",
    42,
    null,
    undefined,
  ])("refuses %s", (value) => {
    expect(toolHref(value)).toBeNull();
  });
});

describe("shape checks", () => {
  it("accepts a tool and nothing that only looks like one", () => {
    expect(isAiTool(TOOL)).toBe(true);
    for (const bad of [
      null,
      "x",
      [],
      { ...TOOL, id: "../x" },
      { ...TOOL, name: 1 },
      { id: ID, name: "n" },
    ]) {
      expect(isAiTool(bad)).toBe(false);
    }
  });

  it("refuses a malformed list and a malformed tool", async () => {
    fetchMock.mockResolvedValueOnce(answer({ items: [{ id: "nope" }] }));
    await expect(listAiTools()).rejects.toThrow("malformed_response");
    fetchMock.mockResolvedValueOnce(answer({ id: ID }, 201));
    await expect(createAiTool({ name: "n", url: "https://x.example.org/" }, "t")).rejects.toThrow(
      "malformed_response",
    );
  });

  it("never puts something that is not an identifier into a path", async () => {
    await expect(updateAiTool("../../x", { name: "n" }, "t")).rejects.toThrow(
      "malformed_identifier",
    );
    await expect(deleteAiTool("1/../2", "t")).rejects.toThrow("malformed_identifier");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("the calls", () => {
  it("reads the list", async () => {
    fetchMock.mockResolvedValueOnce(answer({ items: [TOOL] }));
    expect(await listAiTools()).toEqual([TOOL]);
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/ai-tools");
  });

  it("sends the token and the body of a change, with the right method and path", async () => {
    fetchMock.mockResolvedValueOnce(answer(TOOL, 201));
    await createAiTool({ name: "Alpha", url: TOOL.url }, "token-1");
    fetchMock.mockResolvedValueOnce(answer(TOOL));
    await updateAiTool(ID, { name: "Alpha" }, "token-2");
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await deleteAiTool(ID, "token-3");
    const calls = fetchMock.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method,
      csrf: (init?.headers as Record<string, string>)["X-CSRF-Token"],
    }));
    expect(calls).toEqual([
      { url: "/api/ai-tools", method: "POST", csrf: "token-1" },
      { url: `/api/ai-tools/${ID}`, method: "PATCH", csrf: "token-2" },
      { url: `/api/ai-tools/${ID}`, method: "DELETE", csrf: "token-3" },
    ]);
  });
});

describe("the list kept for the cards", () => {
  it("asks once for many cards, and again after a minute", async () => {
    fetchMock.mockImplementation(async () => answer({ items: [TOOL] }));
    const first = approvedTools(1_000);
    const second = approvedTools(30_000);
    expect(await first).toEqual([TOOL]);
    expect(await second).toEqual([TOOL]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await approvedTools(70_000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not keep a failed answer: the next card asks again", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("offline"));
    await expect(approvedTools(1_000)).rejects.toBeInstanceOf(ApiProblem);
    fetchMock.mockResolvedValueOnce(answer({ items: [TOOL] }));
    expect(await approvedTools(2_000)).toEqual([TOOL]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("forgets the list when an administrator changes it", async () => {
    fetchMock.mockImplementation(async () => answer({ items: [TOOL] }));
    await approvedTools(1_000);
    fetchMock.mockResolvedValueOnce(answer(TOOL, 201));
    await createAiTool({ name: "Alpha", url: TOOL.url }, "t");
    await approvedTools(2_000);
    expect(fetchMock.mock.calls.filter(([url]) => url === "/api/ai-tools").length).toBe(3);
  });
});

describe("what is wrong with a form entry", () => {
  it.each([
    ["name", "invalid_text", "nameInvalid"],
    ["name", "string_too_long", "nameTooLong"],
    ["name", "string_too_short", "nameMissing"],
    ["url", "https_only", "urlHttpsOnly"],
    ["url", "credentials_not_allowed", "urlCredentials"],
    ["url", "ip_address_not_allowed", "urlIp"],
    ["url", "local_host_not_allowed", "urlLocal"],
    ["url", "string_too_long", "urlTooLong"],
    ["url", "string_too_short", "urlMissing"],
    ["url", "invalid_url", "urlInvalid"],
    ["url", "something_new", "urlInvalid"],
  ])("tells %s / %s as %s", (field, type, key) => {
    expect(fieldKey({ loc: ["body", field], type })?.key).toBe(key);
  });

  it("ignores a problem about something that is not on the form", () => {
    expect(fieldKey({ loc: ["body", "org_id"], type: "extra_forbidden" })).toBeNull();
  });

  it("keeps the first problem of each entry", () => {
    expect(
      problemsByField([
        { loc: ["body", "url"], type: "https_only" },
        { loc: ["body", "url"], type: "invalid_url" },
        { loc: ["body", "name"], type: "invalid_text" },
      ]),
    ).toEqual({ url: "urlHttpsOnly", name: "nameInvalid" });
  });
});
